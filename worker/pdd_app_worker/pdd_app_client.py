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
        """识别风控/登录墙信号。命中返回信号名，否则 None。

        实名认证 / 身份验证 = PDD 对账号本身的风控（不是设备级），命中说明
        这个账号已经污染了，应该 quarantine 换号；继续在同一设备上重登一个
        新号通常能恢复。
        """
        risk_signatures = [
            ("slide_verify",      '//*[contains(@text, "拖动滑块")]'),
            ("slide_verify",      '//*[contains(@text, "向右滑动")]'),
            ("captcha",           '//*[contains(@text, "验证码")]'),
            ("login_wall",        '//*[contains(@text, "登录拼多多")]'),
            ("login_wall",        '//*[contains(@text, "请先登录")]'),
            ("rate_limited",      '//*[contains(@text, "操作过于频繁")]'),
            ("rate_limited",      '//*[contains(@text, "稍后再试")]'),
            # 实名认证墙：触发条件多为账号被风控、异地登录、短时间内大量搜索
            ("real_name_wall",    '//*[contains(@text, "实名认证")]'),
            ("real_name_wall",    '//*[contains(@text, "身份验证")]'),
            ("real_name_wall",    '//*[contains(@text, "请完成实名")]'),
            ("real_name_wall",    '//*[contains(@text, "上传身份证")]'),
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
        """等结果列表 RecyclerView 出现。

        PDD 反爬手段之一是 **lazy-render 价格/销量**：标题渲染快，价格/销量延
        迟渲染或只渲染中心卡片。所以这里：
        1. 多等一会（3s）让首屏稳定
        2. 做一次"暖屏滚动"（下滑 300px + 上滑回来）强制 PDD 渲染所有可见卡片
        """
        def _do_sync():
            time.sleep(3.0)
            w, h = self._d.window_size()
            mid_x = w // 2
            # 暖屏：下滑 300px 触发懒加载
            self._d.swipe(mid_x, int(h * 0.6), mid_x, int(h * 0.6) - 300, 0.4)
            time.sleep(0.8)
            # 滑回原位
            self._d.swipe(mid_x, int(h * 0.6) - 300, mid_x, int(h * 0.6), 0.4)
            time.sleep(1.0)

        await asyncio.to_thread(_do_sync)

    async def _collect_items(
        self, target_count: int, scroll_screens: int
    ) -> list[dict[str, Any]]:
        """抓商品卡片。先 dump 当前屏的所有 item，再滚动 N 次合并去重。

        每屏内部如果发现 ≥ 50% 卡片缺价（lazy-render 未完成），延迟 1s 再 dump
        一次，按 title 合并、更晚 dump 的非零价格覆盖较早的零价格。
        """
        seen_titles: dict[str, dict[str, Any]] = {}  # title → 最新数据

        for screen_idx in range(scroll_screens):
            cards = await self._dump_with_lazy_recovery()
            for card in cards:
                title = card.get("title", "").strip()
                if not title:
                    continue
                if title in seen_titles:
                    # 已存在 → 非零字段补全（lazy-render 二次抓到的值优先）
                    existing = seen_titles[title]
                    for k in ("price", "sales"):
                        if not existing.get(k) and card.get(k):
                            existing[k] = card[k]
                    continue
                seen_titles[title] = card
                if len(seen_titles) >= target_count:
                    return list(seen_titles.values())
            if screen_idx < scroll_screens - 1:
                await self._human_scroll_down()
                await _sleep_jitter(1.0)
        return list(seen_titles.values())

    async def _dump_with_lazy_recovery(self) -> list[dict[str, Any]]:
        """dump 一次；如果 ≥ 50% 卡片缺价，做微滚动 + 再 dump，按 title 合并。

        关键差异（vs 上一版本）：第二次 dump 前**强制触发 RecyclerView
        重新 bind ViewHolder** —— 做一个小幅度上下滚动让所有 view 重新进入
        viewport center，PDD 才会渲染价格/销量。
        """
        first = await self._dump_visible_cards()
        if not first:
            return first
        missing = sum(1 for c in first if not c.get("price"))
        if missing < len(first) * 0.5:
            return first

        logger.info(
            f"[{self.serial}] {missing}/{len(first)} cards missing price — "
            f"micro-scroll + redump"
        )

        # 微滚动：上滑 100 → 下滑 100，让 RecyclerView 重 bind 所有 ViewHolder
        def _micro_scroll():
            w, h = self._d.window_size()
            mx = w // 2
            self._d.swipe(mx, int(h * 0.6), mx, int(h * 0.6) - 150, 0.3)
            time.sleep(0.6)
            self._d.swipe(mx, int(h * 0.6) - 150, mx, int(h * 0.6), 0.3)
            time.sleep(1.0)

        await asyncio.to_thread(_micro_scroll)
        second = await self._dump_visible_cards()
        if not second:
            return first

        # 还缺 → 再来一次（最多 3 次 dump）
        still_missing = sum(1 for c in second if not c.get("price"))
        if still_missing >= len(second) * 0.5:
            logger.info(
                f"[{self.serial}] still {still_missing}/{len(second)} missing — "
                f"one more micro-scroll + redump"
            )
            await asyncio.to_thread(_micro_scroll)
            third = await self._dump_visible_cards()
        else:
            third = []

        by_title: dict[str, dict[str, Any]] = {c["title"]: dict(c) for c in first}
        for dump_pass in (second, third):
            for c in dump_pass:
                t = c.get("title")
                if not t:
                    continue
                if t not in by_title:
                    by_title[t] = dict(c)
                    continue
                # 非零的覆盖零的（多次 dump 取最完整的字段）
                for k in ("price", "sales"):
                    if not by_title[t].get(k) and c.get(k):
                        by_title[t][k] = c[k]
        return list(by_title.values())

    async def _dump_visible_cards(self) -> list[dict[str, Any]]:
        """解析当前屏可见的商品卡片列表（Day 3 真机校准版）。

        实测发现的 PDD 反爬手法（基于 2026-04 版 PDD APP）：
        1. 大部分 resource-id 被混淆成 `id/pdd`，多个不同元素共用 —— 不可作
           唯一定位
        2. **例外**：商品标题用 `id/tv_title`（未混淆，可作金锚点）
        3. 标题 `text` 被截断（截到 30 字 / 50 字符），`content-desc` 才有完
           整标题 —— 必须取 desc
        4. 价格被拆成多个紧邻 TextView：`¥` + 数字逐位 —— 单个 TextView 内
           看不到完整价格，需按 y 坐标聚合
        5. 双列布局：x < 540 是左列，x >= 540 是右列，一屏 4 个商品

        解析流程：
        - 锚点：每个 `id/tv_title` TextView 视为一个商品卡片
        - 卡片范围：以标题为左上角，向下扩 250px、横向扩到同列宽度
        - 价格：卡片范围内所有 `¥/￥/纯数字` TextView 按 x 排序拼接
        - 销量：卡片范围内含"已拼/已售"的 TextView
        - 广告：卡片范围内出现 `text=='广告'` 的小 TextView
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

        # 收集所有元素并标准化
        all_nodes: list[dict[str, Any]] = []
        for n in root.iter("node"):
            bounds = _parse_bounds(n.get("bounds", ""))
            if bounds is None:
                continue
            x1, y1, x2, y2 = bounds
            all_nodes.append({
                "class": n.get("class", ""),
                "rid": n.get("resource-id", ""),
                "text": (n.get("text") or "").strip(),
                "desc": (n.get("content-desc") or "").strip(),
                "bounds": bounds,
                "cx": (x1 + x2) // 2,
                "cy": (y1 + y2) // 2,
            })

        # 1. 找所有商品标题（PDD 唯一可靠的金锚点）
        title_anchors = [
            e for e in all_nodes
            if e["rid"] == "com.xunmeng.pinduoduo:id/tv_title"
        ]
        if not title_anchors:
            logger.warning(
                f"[{self.serial}] no tv_title elements found "
                f"(may not be on search result page; total_nodes={len(all_nodes)})"
            )
            return []

        # 2. 每个标题 → 一个商品卡片
        CARD_UP_EXTEND = 70     # 标题上方延伸 70px 捕获"广告"标识
        CARD_DOWN_EXTEND = 250  # 标题向下延伸 250px 算同卡片
        CARD_X_PAD = 50         # x 方向容差，应对子元素稍微出格

        # 价格小 TextView 候选：要么含 ¥/￥，要么是 1-5 位纯数字（可能带小数点）
        _price_token_re = re.compile(r"^(\d+(?:\.\d+)?|¥|￥)$")

        items: list[dict[str, Any]] = []
        for t in title_anchors:
            tx1, ty1, tx2, ty2 = t["bounds"]
            card_y_min = ty1 - CARD_UP_EXTEND
            card_y_max = ty2 + CARD_DOWN_EXTEND
            card_x_min = tx1 - CARD_X_PAD
            card_x_max = tx2 + CARD_X_PAD

            def _in_card(e: dict[str, Any]) -> bool:
                return (
                    card_y_min <= e["cy"] <= card_y_max
                    and card_x_min <= e["cx"] <= card_x_max
                )

            # 完整标题：优先 content-desc，回退 text
            title = t["desc"] or t["text"]
            if not title:
                continue

            # 拼接价格：把卡片范围内所有"价格 token"按 x 排序后拼字符串
            price_tokens = [
                e for e in all_nodes
                if _in_card(e)
                and "TextView" in e["class"]
                and _price_token_re.match(e["text"])
            ]
            price = None
            if price_tokens:
                # 同行 token（y 接近 ¥ 那行的中位 y）才聚合，避免拼到隔壁行的数字
                # 找含 ¥ 的 token 的 y 作为基准；没有就取最低 y 那组
                yen_y = next(
                    (e["cy"] for e in price_tokens if e["text"] in ("¥", "￥")),
                    min(e["cy"] for e in price_tokens),
                )
                same_row = [
                    e for e in price_tokens
                    if abs(e["cy"] - yen_y) < 30  # 同行容差 30px
                ]
                same_row.sort(key=lambda e: e["bounds"][0])
                combined = "".join(e["text"] for e in same_row)
                price = parse_price(combined)

            # 销量：含"已拼/已售"的 TextView
            sales = 0
            for e in all_nodes:
                if _in_card(e) and ("已拼" in e["text"] or "已售" in e["text"]):
                    parsed = parse_sales(e["text"])
                    if parsed:
                        sales = parsed
                        break

            # 是否是广告位（PDD 在标题右上角会塞个"广告"小 TextView）
            is_ad = any(
                _in_card(e) and e["text"] == "广告"
                for e in all_nodes
            )

            items.append({
                "title": title,
                "price": price or 0.0,
                "sales": sales,
                "is_ad": is_ad,
                "bounds": list(t["bounds"]),  # JSON-friendly
            })

        # 去重：title 相同的合并
        seen_titles: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for it in items:
            if it["title"] in seen_titles:
                continue
            seen_titles.add(it["title"])
            deduped.append(it)

        # 统计日志
        ad_count = sum(1 for it in deduped if it["is_ad"])
        no_price = sum(1 for it in deduped if not it["price"])
        logger.info(
            f"[{self.serial}] dumped {len(deduped)} cards "
            f"(anchors={len(title_anchors)}, ads={ad_count}, missing_price={no_price})"
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
