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
        await self._unlock_if_needed()

    async def _unlock_if_needed(self) -> None:
        """处理空锁屏：亮屏 + 多策略上滑。

        策略链（首个成功的就 return）：
        1. uiautomator2 内置 d.unlock()
        2. 通过 adb input shell 强滑（屏幕底 →顶，0.6s 慢滑）×3
        3. KEYCODE_MENU（部分锁屏对菜单键敏感）
        全失败才报错。只能解开"无密码"锁屏；密码/手势/指纹都不行。
        """
        def _do_unlock() -> str:
            d = self._d
            if not d.info.get("screenOn"):
                d.screen_on()
                time.sleep(1.0)

            def _looks_locked() -> tuple[bool, int, int, int]:
                """返回 (是否锁屏, 总节点数, systemui节点数, PDD节点数)。"""
                xml = d.dump_hierarchy()
                total = xml.count("<node ")
                sysui = xml.count("com.android.systemui")
                pdd = xml.count("com.xunmeng.pinduoduo")
                launcher = xml.lower().count("launcher")
                # 锁屏特征：节点很少 + 几乎全是 systemui + 完全没有 PDD/launcher
                locked = (
                    total < 25
                    and sysui >= 1
                    and pdd == 0
                    and launcher == 0
                )
                return locked, total, sysui, pdd

            locked, total, sysui, pdd_n = _looks_locked()
            logger.info(
                f"[{self.serial}] initial dump: total={total} sysui={sysui} pdd={pdd_n} "
                f"→ {'LOCKED' if locked else 'UNLOCKED'}"
            )
            if not locked:
                return "not_locked"

            # ── Strategy 1: built-in d.unlock()
            try:
                d.unlock()
                time.sleep(1.5)
                locked, *_ = _looks_locked()
                if not locked:
                    return "unlocked_builtin"
            except Exception as e:
                logger.debug(f"d.unlock() raised: {e}")

            # ── Strategy 2: aggressive shell-swipe from very bottom to very top
            w, h = d.window_size()
            for attempt in range(3):
                # `input swipe x1 y1 x2 y2 duration_ms` —— 直接走 Android input
                # subsystem，比 d.swipe() 更接近真实输入
                d.shell(f"input swipe {w // 2} {h - 5} {w // 2} 5 600")
                time.sleep(1.3)
                locked, total, sysui, pdd_n = _looks_locked()
                logger.info(
                    f"[{self.serial}] shell-swipe attempt {attempt + 1}: "
                    f"total={total} sysui={sysui} pdd={pdd_n} "
                    f"→ {'still locked' if locked else 'UNLOCKED'}"
                )
                if not locked:
                    return f"unlocked_shell_swipe_attempt_{attempt + 1}"

            # ── Strategy 3: MENU key
            d.shell("input keyevent 82")
            time.sleep(1.0)
            locked, *_ = _looks_locked()
            if not locked:
                return "unlocked_menu_key"

            # ── Strategy 4: long press home (Honor-specific)
            d.shell("input keyevent --longpress KEYCODE_HOME")
            time.sleep(1.0)
            locked, *_ = _looks_locked()
            if not locked:
                return "unlocked_long_home"

            return "still_locked"

        status = await asyncio.to_thread(_do_unlock)
        if status == "not_locked":
            logger.debug(f"[{self.serial}] screen not locked, skip unlock")
        elif status.startswith("unlocked"):
            logger.info(f"[{self.serial}] unlock OK via: {status}")
        else:
            raise RuntimeError(
                "lock_screen_unlock_failed: 4 种策略都没解开锁屏。"
                "如果手机上配置了密码/手势/指纹，请到【设置→安全→锁屏密码】关掉。"
                "如果没密码却还是失败，可能是 Honor Magic UI 锁屏对自动滑动有特殊过滤，"
                "需要去【设置→系统和更新→开发人员选项→关闭防止误触】或换 swipe 实现。"
            )

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

        2026-04 实测 PDD 首页搜索栏特征：
        - class=android.widget.TextView（不是 EditText！EditText 在二级搜索页）
        - resource-id="com.xunmeng.pinduoduo:id/pdd"（注意此 rid 被 PDD 全局
          复用，单独不可作为唯一定位）
        - content-desc="搜索"（**唯一可靠定位**）
        - text 是占位符（上一次搜索关键词，比如"蒸蛋盖"）
        - 旁边 [941,176] 有"拍照搜索" desc='拍照搜索'，不要误点
        """
        await _sleep_jitter(0.8)
        candidates = [
            # 主选：精确匹配 content-desc="搜索"（排除"拍照搜索"等组合词）
            '//android.widget.TextView[@content-desc="搜索"]',
            # 次选：所有 desc="搜索" 元素（万一 PDD 把 TextView 换成别的 class）
            '//*[@content-desc="搜索"]',
            # 兜底（留着以防 PDD 又换回 EditText 形态）
            '//android.widget.EditText[contains(@text, "搜索")]',
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
                    logger.info(f"[{self.serial}] tapped search entry via: {xpath}")
                    break
            except Exception as exc:
                logger.debug(f"[{self.serial}] tap_search_entry candidate failed: {xpath} -> {exc}")
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

        实现思路（Day 3 校准后的版本）：
        - PDD 结果页商品卡片用 RecyclerView 渲染，每个卡片是一个 ViewGroup
        - 一个完整卡片节点子树里通常会同时出现：
            * 标题（一段 ≥ 6 个汉字的 TextView）
            * 价格（带 ¥ 或 ￥ 的 TextView）
            * 销量（含"已拼"或"已售"的 TextView）
        - 我们不强求标题/销量必须能取到，但**价格必须有**（没价格的不算商品卡）

        解析策略：先按 RecyclerView 容器框定范围，然后按 bounds 把元素聚合
        成卡片 —— 同一卡片内的元素 y 坐标接近（差距 < CARD_HEIGHT_THRESHOLD）。
        """
        import xml.etree.ElementTree as ET

        def _do_dump():
            return self._d.dump_hierarchy()

        xml_str = await asyncio.to_thread(_do_dump)
        try:
            root = ET.fromstring(xml_str)
        except ET.ParseError as e:
            logger.warning(f"[{self.serial}] dump_hierarchy unparseable: {e}")
            return []

        # 1. 收集所有"价格 TextView"作为卡片锚点
        price_elements: list[dict[str, Any]] = []
        text_elements: list[dict[str, Any]] = []
        sales_elements: list[dict[str, Any]] = []
        for n in root.iter("node"):
            cls = n.get("class", "")
            if "TextView" not in cls:
                continue
            text = (n.get("text") or "").strip()
            if not text:
                continue
            bounds = _parse_bounds(n.get("bounds", ""))
            if bounds is None:
                continue
            x1, y1, x2, y2 = bounds
            element = {
                "text": text,
                "bounds": bounds,
                "center": ((x1 + x2) // 2, (y1 + y2) // 2),
                "node": n,
            }
            if "¥" in text or "￥" in text:
                price_elements.append(element)
            elif "已拼" in text or "已售" in text:
                sales_elements.append(element)
            else:
                # 过滤掉明显的系统栏 / 装饰文本
                if "android.systemui" in n.get("resource-id", ""):
                    continue
                if len(text) < 4 or text.isdigit():
                    continue
                text_elements.append(element)

        if not price_elements:
            logger.warning(
                f"[{self.serial}] no price elements found in dump "
                f"(may be on splash/loading screen)"
            )
            return []

        # 2. 把每个价格元素当作一个商品卡片的锚点，向上/左/下找标题、销量
        CARD_HEIGHT = 600  # 经验值：单屏最多 ~4 个卡片 / 屏高 2400 ≈ 600
        items: list[dict[str, Any]] = []
        for price_el in price_elements:
            px, py = price_el["center"]
            # 找标题：同卡片内（|y - py| < CARD_HEIGHT/2）+ x 接近 + 文本最长
            title_candidates = [
                e for e in text_elements
                if abs(e["center"][1] - py) < CARD_HEIGHT // 2
                and abs(e["center"][0] - px) < 600  # 同列卡片
                and e["center"][1] < py  # 标题在价格上方
            ]
            title_candidates.sort(key=lambda e: -len(e["text"]))
            title = title_candidates[0]["text"] if title_candidates else None

            # 找销量：同卡片内 + 价格附近（通常在价格右边）
            sales_candidates = [
                e for e in sales_elements
                if abs(e["center"][1] - py) < CARD_HEIGHT // 2
                and abs(e["center"][0] - px) < 600
            ]
            sales = (
                parse_sales(sales_candidates[0]["text"])
                if sales_candidates else 0
            )

            price = parse_price(price_el["text"])
            if price is None or price <= 0:
                continue
            if not title:
                continue  # 没标题没法去重 / 后续 scoring，跳过
            items.append({
                "title": title,
                "price": price,
                "sales": sales or 0,
                "bounds": price_el["bounds"],
            })

        # 去重：title 相同的合并（同一卡片不同区域多次匹配）
        seen_titles: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for it in items:
            if it["title"] in seen_titles:
                continue
            seen_titles.add(it["title"])
            deduped.append(it)

        logger.info(
            f"[{self.serial}] dumped {len(deduped)} cards from current screen "
            f"(price_anchors={len(price_elements)}, "
            f"title_pool={len(text_elements)}, sales_pool={len(sales_elements)})"
        )
        return deduped

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
_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _parse_bounds(s: str) -> tuple[int, int, int, int] | None:
    """'[0,398][540,1100]' → (0, 398, 540, 1100)。"""
    m = _BOUNDS_RE.match(s or "")
    if not m:
        return None
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


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
