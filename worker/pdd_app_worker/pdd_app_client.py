"""PDD APP 操作封装（uiautomator2 + 物理手机）。

设计目标：
- 把"打开 PDD APP → 搜索 → 解析结果"这套流程封装成 ``PddAppClient.search()``
  一个调用，main.py 完全不需要知道 UI 选择器细节
- 失败/风控信号在内部捕获并转成 risk_signals 字符串列表（worker 推回 backend）
- 人类化操作（随机延迟 + 非线性滑动）防止行为指纹太机械

阶段：
- Day 2（当前）：完成搜索流，结果列表先采前 20 个卡片；选择器都是初稿，
  Day 3 用真机校准后会迭代
- Day 3+：详情页、历史价、加自检任务（self_check）

关键风险点（Day 3 联调时重点验证）：
1. 弹窗：开屏广告、金币漂浮窗、登录引导、新人优惠券 —— 需要 dismiss 兜底
2. 滑块验证：搜索后偶尔出现滑动拼图 —— 检测到立刻 abort，返回 risk_signal
3. 列表懒加载：滚动后才渲染下半屏 —— 用 swipe + wait_idle 触发
4. 多账号/多设备状态漂移：5514 跟 4310 在同一台手机上切换可能触发风控 —— 1机1号绑定
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ─── 常量 ──────────────────────────────────────────────────
PDD_PACKAGE = "com.xunmeng.pinduoduo"
DEFAULT_MAX_ITEMS = 20
APP_START_TIMEOUT = 30  # 启动 PDD APP 等待秒数（冷启动可能要 10-20s）
SEARCH_RESULT_TIMEOUT = 15  # 提交搜索后等结果列表出现的最长时间

# 同一台手机两次任务之间最少间隔（人类不会 1 秒内连发搜索），由 worker
# 在调用层维护即可，client 内部只对单次任务内的步骤加 jitter。
_TASK_GAP_FLOOR_SECONDS = 5.0


@dataclass
class PddSearchResult:
    """worker → backend 推回前的结构化结果。"""

    items: list[dict[str, Any]] = field(default_factory=list)
    risk_signals: list[str] = field(default_factory=list)
    raw_screenshot_path: str | None = None
    error: str | None = None  # 仅在 failed 时填


# ─── 人类化操作辅助 ────────────────────────────────────────

async def _sleep_jitter(base: float, jitter: float = 0.4) -> None:
    """带抖动的 sleep —— base ± jitter*base 范围内随机。"""
    delta = random.uniform(-jitter * base, jitter * base)
    await asyncio.sleep(max(0.05, base + delta))


def _humanize_swipe_path(d, start_xy: tuple[int, int], end_xy: tuple[int, int]) -> None:
    """非线性滑动：把直线插成 6-10 个点，每点微抖动。

    人类滑动不是 1 帧到位，机器学习反爬会盯线性路径。这里用样本点 + 时间
    扰动模拟手指轨迹。
    """
    x1, y1 = start_xy
    x2, y2 = end_xy
    steps = random.randint(6, 10)
    points = []
    for i in range(steps + 1):
        t = i / steps
        # 起步快、末尾减速（ease-out），更像滑动
        eased = 1 - (1 - t) ** 2
        x = x1 + (x2 - x1) * eased + random.randint(-3, 3)
        y = y1 + (y2 - y1) * eased + random.randint(-3, 3)
        points.append((int(x), int(y)))
    # uiautomator2 的 swipe_points 接受 [(x, y, t_ms), ...]，t 是相对起点
    total_ms = random.randint(450, 850)
    per_step_ms = total_ms // (steps + 1)
    points_with_t = [(x, y, per_step_ms * (i + 1)) for i, (x, y) in enumerate(points)]
    d.swipe_points(points_with_t, 0.05)


# ─── 主客户端 ──────────────────────────────────────────────

class PddAppClient:
    """连一台物理手机，对其 PDD APP 做搜索/详情等操作。

    用法（async context manager）::

        async with PddAppClient(serial="PKT0220416005274") as cli:
            result = await cli.search("机械键盘")

    退出时不停止 PDD APP（避免冷启动开销），但会把 APP 切回首页并 swipe
    随机滚动几下，避免下次进来停在搜索结果页。
    """

    def __init__(self, serial: str) -> None:
        self.serial = serial
        self._d: Any = None  # uiautomator2.Device, 延迟 init

    async def __aenter__(self) -> "PddAppClient":
        await self._connect()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self._post_task_cleanup()

    async def _connect(self) -> None:
        """连接 uiautomator2。会自动在手机上推/启动 atx-agent。"""
        def _do_connect():
            import uiautomator2 as u2
            d = u2.connect(self.serial)
            info = d.info
            logger.info(
                f"[{self.serial}] connected: "
                f"sdk={info.get('sdkInt')} brand={info.get('productName')} "
                f"display={info.get('displaySizeDpX')}x{info.get('displaySizeDpY')}"
            )
            return d

        self._d = await asyncio.to_thread(_do_connect)

    async def _post_task_cleanup(self) -> None:
        """任务结束清场：随机滚动一下首页或返回桌面，避免下次进来卡在结果页。"""
        if not self._d:
            return
        try:
            await asyncio.to_thread(self._d.press, "back")
            await _sleep_jitter(0.6)
            await asyncio.to_thread(self._d.press, "back")
        except Exception as e:
            logger.debug(f"[{self.serial}] cleanup ignored: {e}")

    # ── 公开 API ────────────────────────────────────────────

    async def search(
        self,
        keyword: str,
        max_items: int = DEFAULT_MAX_ITEMS,
        mode: str = "fast",
    ) -> PddSearchResult:
        """主入口：搜索关键词并返回前 N 个商品卡片。

        mode:
        - "fast"：单屏，约 20 个商品，~30s
        - "deep"：滚动 3 屏，约 60 个商品，~90s，更适合做长尾分析
        """
        target_count = max_items if mode == "fast" else max_items * 3
        result = PddSearchResult()
        t0 = time.monotonic()

        try:
            await self._ensure_app_foreground()
            await self._dismiss_popups()
            await self._tap_search_entry()
            await self._type_keyword(keyword)
            await self._submit_search()
            # 提交后立刻检风控
            risk = await self._detect_risk_walls()
            if risk:
                result.risk_signals.append(risk)
                result.error = f"risk_wall:{risk}"
                logger.warning(f"[{self.serial}] search aborted: risk={risk}")
                return result

            await self._wait_search_results()
            items = await self._collect_items(target_count, scroll_screens=1 if mode == "fast" else 3)
            result.items = items

            if not items:
                result.risk_signals.append("empty_result")
                result.error = "empty_result"
        except Exception as exc:
            logger.exception(f"[{self.serial}] search failed: {exc}")
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            elapsed = time.monotonic() - t0
            logger.info(
                f"[{self.serial}] search('{keyword}', mode={mode}) → "
                f"items={len(result.items)} risks={result.risk_signals} "
                f"elapsed={elapsed:.1f}s"
            )
        return result

    # ── 内部步骤 ────────────────────────────────────────────

    async def _ensure_app_foreground(self) -> None:
        """确保 PDD 在前台。已开就 use_default；没开就启动。"""
        def _do():
            current = self._d.app_current()
            if current.get("package") == PDD_PACKAGE:
                return "already"
            self._d.app_start(PDD_PACKAGE, use_monkey=False, wait=True)
            return "started"

        status = await asyncio.to_thread(_do)
        logger.info(f"[{self.serial}] app state: {status}")
        if status == "started":
            # 冷启动给开屏广告 / splash 留时间
            await _sleep_jitter(3.5, jitter=0.3)

    async def _dismiss_popups(self) -> None:
        """关掉常见的开屏弹窗（金币、新人券、推送权限、订阅引导等）。

        策略：用 XPath 找几个常见关闭按钮，找到就点；找不到就跳过。
        所有匹配都做 wait(timeout=1)，不阻塞主流程。

        Day 3 联调时把实际遇到的弹窗 dump XML 后再加 XPath。
        """
        candidates = [
            # 资源 ID 类（最稳定）
            '//*[@resource-id="com.xunmeng.pinduoduo:id/btn_close"]',
            '//*[@resource-id="com.xunmeng.pinduoduo:id/btn_cancel"]',
            '//*[@resource-id="com.xunmeng.pinduoduo:id/iv_close"]',
            # 文本类（次稳定，UI 改版可能丢）
            '//android.widget.TextView[@text="跳过"]',
            '//android.widget.TextView[@text="关闭"]',
            '//android.widget.TextView[@text="暂不"]',
            '//android.widget.TextView[@text="以后再说"]',
            '//android.widget.Button[@text="取消"]',
        ]
        for xpath in candidates:
            try:
                el = await asyncio.to_thread(
                    lambda x=xpath: self._d.xpath(x).wait(timeout=0.8)
                )
                if el:
                    await asyncio.to_thread(lambda x=xpath: self._d.xpath(x).click())
                    logger.info(f"[{self.serial}] dismissed popup: {xpath}")
                    await _sleep_jitter(0.6)
            except Exception:
                continue

    async def _tap_search_entry(self) -> None:
        """点首页顶部搜索栏。

        TODO Day 3：用真机 weditor 抓 resource-id 替换 text fallback。
        """
        await _sleep_jitter(0.8)
        candidates = [
            '//*[@resource-id="com.xunmeng.pinduoduo:id/pdd"]/android.widget.EditText',
            '//android.widget.EditText[contains(@text, "搜索")]',
            '//android.view.View[contains(@content-desc, "搜索框")]',
        ]
        clicked = False
        for xpath in candidates:
            try:
                el = await asyncio.to_thread(
                    lambda x=xpath: self._d.xpath(x).wait(timeout=2.5)
                )
                if el:
                    await asyncio.to_thread(lambda x=xpath: self._d.xpath(x).click())
                    clicked = True
                    logger.debug(f"[{self.serial}] tapped search entry: {xpath}")
                    break
            except Exception:
                continue
        if not clicked:
            raise RuntimeError("找不到首页搜索入口 —— 检查 PDD 是否在首页且 UI 没大改")
        await _sleep_jitter(0.8)

    async def _type_keyword(self, keyword: str) -> None:
        """在搜索输入框里敲关键词（不用 paste，用真键入更像人）。"""
        # 进搜索页后输入框应该自动 focused，先 set_fastinput_ime 切到 ATX 输入法
        # 再 send_keys 才能稳定中文输入
        def _do():
            self._d.set_fastinput_ime(True)
            self._d.clear_text()  # 清掉默认 hint 残留
            self._d.send_keys(keyword, clear=True)

        await asyncio.to_thread(_do)
        await _sleep_jitter(0.6)

    async def _submit_search(self) -> None:
        """提交搜索。优先点页面上的"搜索"按钮，回退到键盘 Enter。"""
        candidates = [
            '//android.widget.TextView[@text="搜索"]',
            '//android.widget.Button[@text="搜索"]',
        ]
        for xpath in candidates:
            try:
                el = await asyncio.to_thread(
                    lambda x=xpath: self._d.xpath(x).wait(timeout=1.5)
                )
                if el:
                    await asyncio.to_thread(lambda x=xpath: self._d.xpath(x).click())
                    return
            except Exception:
                continue
        # fallback：键盘 Enter
        await asyncio.to_thread(self._d.press, "enter")

    async def _detect_risk_walls(self) -> str | None:
        """识别风控/登录墙信号。命中返回信号名，否则 None。"""
        risk_signatures = [
            ("slide_verify",  '//*[contains(@text, "拖动滑块")]'),
            ("slide_verify",  '//*[contains(@text, "向右滑动")]'),
            ("captcha",       '//*[contains(@text, "验证码")]'),
            ("login_wall",    '//*[contains(@text, "登录拼多多")]'),
            ("login_wall",    '//*[contains(@text, "请先登录")]'),
            ("rate_limited",  '//*[contains(@text, "操作过于频繁")]'),
            ("rate_limited",  '//*[contains(@text, "稍后再试")]'),
        ]
        for sig, xpath in risk_signatures:
            try:
                el = await asyncio.to_thread(
                    lambda x=xpath: self._d.xpath(x).wait(timeout=0.5)
                )
                if el:
                    return sig
            except Exception:
                continue
        return None

    async def _wait_search_results(self) -> None:
        """等结果列表 RecyclerView 出现。"""
        def _do():
            # PDD 搜索结果用的是 RecyclerView，先简单等一下
            time.sleep(1.5)
            return True

        await asyncio.to_thread(_do)

    async def _collect_items(
        self, target_count: int, scroll_screens: int
    ) -> list[dict[str, Any]]:
        """抓商品卡片。先 dump 当前屏的所有 item，再滚动 N 次合并去重。

        商品卡片在 PDD APP 里的特征（Day 2 初稿，Day 3 校准）：
        - 容器：RecyclerView 直接子 ViewGroup
        - 标题：商品标题 TextView，通常 2 行截断
        - 价格：以"¥"开头的 TextView，或带 ¥ 符号的小字
        - 销量/拼单数：含"已拼"或"件已拼"或"+人已拼"的 TextView
        """
        seen_titles: set[str] = set()
        items: list[dict[str, Any]] = []

        for screen_idx in range(scroll_screens):
            cards = await self._dump_visible_cards()
            for card in cards:
                title = card.get("title", "").strip()
                if not title or title in seen_titles:
                    continue
                seen_titles.add(title)
                items.append(card)
                if len(items) >= target_count:
                    return items
            # 还要继续滚就 swipe
            if screen_idx < scroll_screens - 1:
                await self._human_scroll_down()
                await _sleep_jitter(1.0)
        return items

    async def _dump_visible_cards(self) -> list[dict[str, Any]]:
        """解析当前屏可见的商品卡片列表。

        Day 2 占位实现：返回空列表 + 一条 TODO 日志。Day 3 用真机做 UI dump
        后填充实际的 XPath / 解析逻辑。
        """
        # TODO(Day 3): 把以下伪代码实现：
        #   1. self._d.dump_hierarchy() 拿当前 UI XML
        #   2. xml.etree 解析，找所有商品卡片 ViewGroup
        #   3. 每个卡片里取 title + price + sales + image_url（element bounds → 截图裁剪）
        #   4. 返回 [{"title": ..., "price": float, "sales": int, ...}, ...]
        logger.warning(
            f"[{self.serial}] _dump_visible_cards: NOT IMPLEMENTED — Day 3 will fill"
        )
        return []

    async def _human_scroll_down(self) -> None:
        """人类化向下滑动一屏，触发 RecyclerView 懒加载。"""
        size = await asyncio.to_thread(lambda: self._d.window_size())
        w, h = size
        start = (w // 2 + random.randint(-30, 30), int(h * 0.75))
        end = (w // 2 + random.randint(-30, 30), int(h * 0.30))
        await asyncio.to_thread(_humanize_swipe_path, self._d, start, end)


# ─── 价格 / 销量解析小工具 ────────────────────────────────

_PRICE_RE = re.compile(r"[¥￥]?\s*([0-9]+(?:\.[0-9]+)?)")
_SALES_RE = re.compile(r"([0-9.]+)([万千]?)")


def parse_price(text: str) -> float | None:
    if not text:
        return None
    m = _PRICE_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def parse_sales(text: str) -> int | None:
    """'1.2万人已拼' → 12000，'350人已拼' → 350。"""
    if not text:
        return None
    m = _SALES_RE.search(text)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2)
    if unit == "万":
        num *= 10_000
    elif unit == "千":
        num *= 1_000
    return int(num)
