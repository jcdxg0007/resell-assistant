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
import base64
import logging
import os
import random
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ─── 常量 ──────────────────────────────────────────────────
PDD_PACKAGE = "com.xunmeng.pinduoduo"
DEFAULT_MAX_ITEMS = 20
APP_START_TIMEOUT = 30  # 启动 PDD APP 等待秒数（冷启动可能要 10-20s）
SEARCH_RESULT_TIMEOUT = 15  # 提交搜索后等结果列表出现的最长时间

# ─── 主图缩略图截屏裁剪 ─────────────────────────────────────
# PDD APP 控件树拿不到图片 URL（图是渲染出来的位图），只能截屏裁剪卡片图区。
# 缩略图编码成 base64 data URL 塞进 item["image"]，方便选品页直接 <img> 引用。
# 三个参数都可用环境变量覆盖；PDD_CAPTURE_IMAGES=0 可整体关掉。
_CAPTURE_IMAGES = os.environ.get("PDD_CAPTURE_IMAGES", "1") != "0"
# 最长边 320：列表里缩成 32px 看不出差别，但点开预览时还算清楚（PDD 只能截屏
# 裁图，原图就这分辨率）。再大 base64 体积涨太快（≈面积平方）。
_THUMB_MAX_PX = int(os.environ.get("PDD_THUMB_MAX_PX", "320") or "320")  # 缩略图最长边
_THUMB_JPEG_Q = int(os.environ.get("PDD_THUMB_JPEG_Q", "70") or "70")   # JPEG 质量 1-100

# 同一台手机两次任务之间最少间隔（人类不会 1 秒内连发搜索），由 worker
# 在调用层维护即可，client 内部只对单次任务内的步骤加 jitter。
_TASK_GAP_FLOOR_SECONDS = 5.0


# ─── Session profile 抽样（Day 4 humanization rebalance）──────────────────
#
# 4310 死因复盘里"操作偏慢 + 流程过于固定"占权重 25%。Day 3.5 之前我们
# 100% 的搜索任务都走"开 APP → 长 warmup → 搜"的固定模板，单峰画像被
# cohort 分析一刀切。
#
# 真人 PDD 用户的 session 路径分布（业内电商风控公开论文 + CSDN 实战文章
# 的经验数据综合估）：
#
#   direct       开 APP → 立刻搜（"知道要买什么"）             ~45%
#   short_peek   开 APP → 滑首页 1-2 下 → 搜                   ~30%
#   standard     开 APP → 滑首页 + 短停某商品 → 搜             ~20%
#   deep         开 APP → 进详情看一阵 → 退出 → 搜             ~5%
#
# 每个搜索任务**进 search() 时掷一次骰子**抽 profile；所有 profile 决策
# 都会日志记录，便于后续按 profile 直方图审计是否真的接近真人 cohort。
#
# 多日运行后如果发现某 profile 比例偏离真人太远（例如 PDD 风控对 direct
# 加重权重 → 我们看到 direct profile 任务的 risk_signals 命中率显著高于
# 别的），就调整下面这个常量再 push。
_SESSION_PROFILE_DISTRIBUTION: list[tuple[str, float]] = [
    ("direct",     0.45),
    ("short_peek", 0.30),
    ("standard",   0.20),
    ("deep",       0.05),
]

# 各 profile 在搜完后还做一次首页短逛（_post_search_browse）的概率。
# 已经做了长 pre-search warmup 的（standard/deep）就基本不再 post-browse
# 了，避免"前后都逛 = 比真人还像真人"。
_POST_BROWSE_PROB: dict[str, float] = {
    "direct":     0.45,  # 直搜的人最常在搜完后逛一下（你提的那个观察）
    "short_peek": 0.30,
    "standard":   0.20,
    "deep":       0.10,
}


def _pick_session_profile() -> str:
    """按 _SESSION_PROFILE_DISTRIBUTION 加权抽一个 profile 名。"""
    r = random.random()
    cum = 0.0
    for name, weight in _SESSION_PROFILE_DISTRIBUTION:
        cum += weight
        if r < cum:
            return name
    return _SESSION_PROFILE_DISTRIBUTION[-1][0]


@dataclass
class PddSearchResult:
    """worker → backend 推回前的结构化结果。"""

    items: list[dict[str, Any]] = field(default_factory=list)
    risk_signals: list[str] = field(default_factory=list)
    raw_screenshot_path: str | None = None
    error: str | None = None  # 仅在 failed 时填


# ─── 人类化操作辅助 ────────────────────────────────────────

# 全局浏览节奏因子。1.0 = 原始节奏；0.7 = 整体快 30%。
# 只作用于"浏览 / 停留 / 翻页观察"类等待（_sleep_jitter 默认、warmup 滚动
# 停留、lazy-render 微滚停留、善后 back 等），**不影响**反爬关键节奏：
#   - IME 每字输入节奏（太快是机器指纹）—— 走 asyncio.sleep，不经本因子
#   - 冷启动等 APP/splash（_ensure_app_foreground 显式传 pace=False）
#   - burst 间静默 5-30 min / daily quota（在 main.py，不引用本因子）
# clamp 到 [0.3, 1.0]：下限防手滑设 0 导致"零等待裸奔"；不允许 > 1（变慢
# 没意义，要变慢直接调原始区间）。
_HUMANIZE_PACE = max(0.3, min(1.0, float(os.environ.get("HUMANIZE_PACE", "1.0"))))


def _pace_uniform(lo: float, hi: float) -> float:
    """浏览类停留时长：random.uniform(lo, hi) 再乘全局节奏因子。"""
    return random.uniform(lo, hi) * _HUMANIZE_PACE


# 商品详情页 goods_id / 主图 的提取（来自 `dumpsys activity top` 里 WebView 的
# props/url，纯被动读取，零额外动作）。真机 2026-06-07 验证：字段在两种形态里都出现——
#   ① url 字段（query 参数、URL 编码）：goods.html?thumb_url=<enc>&...&goods_id=<数字>...
#   ② props 字段（转义 JSON）：{\"thumb_url\":\"...\",\"goods_id\":\"<数字>\"}
# 用宽松字符类同时吃下 = / : / " / \ 这几种分隔，兼容两种形态。
_GOODS_ID_RE = re.compile(r'goods_id[\\":=\s]{1,5}(\d{6,})')
# 主图优先从 url 的 query 参数取（URL 编码、干净），再 unquote 还原
_THUMB_URLQ_RE = re.compile(r'thumb_url=([^&"\\]+)')
_THUMB_JSON_RE = re.compile(r'thumb_url[\\":=\s]{1,5}(https[^"\\&]+)')


def extract_goods_meta(dumpsys_top: str) -> dict[str, str | None]:
    """从 `dumpsys activity top` 文本里抠 goods_id / 主图 url / 拼好的唤起链接。

    :return: {"goods_id": str|None, "thumb_url": str|None, "detail_url": str|None}
    唤起链接形如 https://mobile.yangkeduo.com/goods.html?goods_id=<id> —— **只供人手动点**，
    绝不让 worker 自动唤起（automation 指纹）。
    """
    text = dumpsys_top or ""
    gid = None
    m = _GOODS_ID_RE.search(text)
    if m:
        gid = m.group(1)
    thumb = None
    tm = _THUMB_URLQ_RE.search(text)
    if tm:
        thumb = urllib.parse.unquote(tm.group(1))
    else:
        tj = _THUMB_JSON_RE.search(text)
        if tj:
            thumb = tj.group(1).replace("\\/", "/").replace("\\", "")
    detail_url = (
        f"https://mobile.yangkeduo.com/goods.html?goods_id={gid}" if gid else None
    )
    return {"goods_id": gid, "thumb_url": thumb, "detail_url": detail_url}


def set_humanize_pace(value: float) -> None:
    """热更新全局浏览节奏因子（被 main.apply_remote_config 调用）。

    clamp 到 [0.3, 1.0]。_sleep_jitter / _pace_uniform 都按模块全局动态查找
    本值，所以更新后下一次等待立即生效，无需重启 worker。
    """
    global _HUMANIZE_PACE
    _HUMANIZE_PACE = max(0.3, min(1.0, float(value)))


# ─── 「查物流」拟人行为（roadmap §11.4）──────────────────────────────
# burst 结束时按概率去「我的订单 → 查看物流」逛一下，提升行为多样性。
# 每日首次触发会顺带确认该号有无真实订单：有则当日继续随机查，没有则当日
# 冷却不再尝试（订单页空反而是异常信号）。开关 + 概率由 backend runtime-config
# 热更新（main.apply_remote_config → set_logistics_browse）。默认关闭。
_LOGISTICS_BROWSE_ENABLED = False
_LOGISTICS_BROWSE_PROB = 0.25   # A：每个 burst 结尾触发概率
_LOGISTICS_QUIET_PROB = 0.35    # B：inter-burst 静默期中段触发概率
# 每日探测/冷却状态（一个 worker 进程 = 一个采集号，模块级单例即可）。
# state: "unknown"（今日还没探测）/ "has_orders"（确认有单，继续随机查）/
#        "cooldown"（今日订单页空，当日不再尝试）。跨天自动重置。
_logistics_day_key: str | None = None
_logistics_state = "unknown"


def set_logistics_browse(
    enabled: bool,
    prob: float | None = None,
    quiet_prob: float | None = None,
) -> None:
    """热更新「查物流」总开关 + 两条触发概率（被 main.apply_remote_config 调用）。

    :param prob: A —— 每个 burst 结尾触发概率
    :param quiet_prob: B —— inter-burst 静默期中段触发概率
    """
    global _LOGISTICS_BROWSE_ENABLED, _LOGISTICS_BROWSE_PROB, _LOGISTICS_QUIET_PROB
    _LOGISTICS_BROWSE_ENABLED = bool(enabled)
    if prob is not None:
        try:
            _LOGISTICS_BROWSE_PROB = max(0.0, min(1.0, float(prob)))
        except (TypeError, ValueError):
            pass
    if quiet_prob is not None:
        try:
            _LOGISTICS_QUIET_PROB = max(0.0, min(1.0, float(quiet_prob)))
        except (TypeError, ValueError):
            pass


def _cn_today_key() -> str:
    """东八区当天 yyyy-mm-dd，作为每日探测/冷却的 day key。"""
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))


def should_browse_logistics(serial: str = "", prob: float | None = None) -> bool:
    """廉价决策：现在该不该查物流？（不碰设备，供 A/B 两个触发点先判断再决定开不开 PDD）

    门控顺序：总开关 → 跨天重置探测状态 → 当日冷却 → 概率掷骰。返回 True 表示
    应该执行一次 browse_logistics_now()。命中概率这一步会"消耗"本次机会，所以
    调用方拿到 True 后应当真正去执行（别再调一次本函数）。

    :param prob: 本触发点的概率；None 时用 A（burst 结尾）默认概率 _LOGISTICS_BROWSE_PROB。
                 静默期 B 应显式传 _LOGISTICS_QUIET_PROB。
    """
    global _logistics_day_key, _logistics_state
    if not _LOGISTICS_BROWSE_ENABLED:
        return False
    today = _cn_today_key()
    if today != _logistics_day_key:
        _logistics_day_key = today
        _logistics_state = "unknown"
        logger.info(f"[{serial}] logistics: 新的一天，重置查物流探测状态")
    if _logistics_state == "cooldown":
        return False
    p = _LOGISTICS_BROWSE_PROB if prob is None else prob
    return random.random() < p


async def _sleep_jitter(base: float, jitter: float = 0.4, pace: bool = True) -> None:
    """带抖动的 sleep —— base ± jitter*base 范围内随机。

    :param pace: True（默认）按 _HUMANIZE_PACE 缩放，用于浏览类等待；
                 False 用于"等 APP 起来"这类不能压缩的真实等待。
    """
    effective = base * _HUMANIZE_PACE if pace else base
    delta = random.uniform(-jitter * effective, jitter * effective)
    await asyncio.sleep(max(0.05, effective + delta))


def _jittered_point_in_bounds(
    bounds: dict[str, int], jitter_px: int = 12
) -> tuple[int, int]:
    """在 bounds 矩形内取一个"偏离中心"的随机点，给 _human_click 用。

    边界保护：抖动量不超过控件 1/3 边长，避免极端瘦控件点出框外。
    """
    left = int(bounds.get("left", 0))
    right = int(bounds.get("right", 0))
    top = int(bounds.get("top", 0))
    bottom = int(bounds.get("bottom", 0))
    cx = (left + right) // 2
    cy = (top + bottom) // 2
    max_dx = max(1, min(jitter_px, (right - left) // 3))
    max_dy = max(1, min(jitter_px, (bottom - top) // 3))
    return (
        cx + random.randint(-max_dx, max_dx),
        cy + random.randint(-max_dy, max_dy),
    )


def _humanize_swipe_path(
    d,
    start_xy: tuple[int, int],
    end_xy: tuple[int, int],
    duration_s: float | None = None,
) -> None:
    """非线性滑动：把直线插成 6-10 个点，每点微抖动 + ease-out 缓动。

    人类滑动不是 1 帧到位，更不是直线。机器学习反爬会盯：
      1. 直线 vs 曲线 —— 真人手指有微抖动，路径不是完全直线
      2. 匀速 vs 缓动 —— 真人起步快、末尾减速（手指滑到位会自然减速）
      3. 单点 vs 多点 —— 真人滑屏 = 一连串采样点，不是 1 起 1 终

    本函数用样本点 + 时间扰动模拟手指轨迹。所有 worker 的滑屏都应该走
    这个 helper，**不要直接调 d.swipe()**（直线 + 默认匀速 = 一眼机器人）。

    :param duration_s: 整段滑动时长（秒）。None 时随机 0.45-0.85s。

    注意：本函数无法控制 MotionEvent 的 pressure / size 字段——uiautomator2
    在未 root 设备上发的合成事件 pressure=1.0 / size=1.0 是常量，跟真人
    手指报告的 0.3-1.0 / 0.1-0.5 浮动差别一眼可见。这是 unrooted 设备的
    硬限制，软件层无解（要破得刷机/root 改输入子系统）。
    """
    x1, y1 = start_xy
    x2, y2 = end_xy
    steps = random.randint(6, 10)
    points = []
    for i in range(steps + 1):
        t = i / steps
        # ease-out: 起步快、末尾减速（手指物理学）
        eased = 1 - (1 - t) ** 2
        x = x1 + (x2 - x1) * eased + random.randint(-3, 3)
        y = y1 + (y2 - y1) * eased + random.randint(-3, 3)
        points.append((int(x), int(y)))
    if duration_s is None:
        total_ms = random.randint(450, 850)
    else:
        total_ms = max(60, int(duration_s * 1000))
    per_step_ms = max(20, total_ms // (steps + 1))
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
        # cleanup 行为模式（caller 在 search 跑完后用 set_cleanup_mode 更新）：
        # - "exit"：默认。按 home 把 PDD 真退到后台（burst 结束时用，让 PDD 静
        #   置 5-30 min，画像更像"间歇用户"）
        # - "soft"：仅做 0-1 次 back（最多回到结果页上一层），不退 PDD。让 burst
        #   内下一个任务在同一个 PDD session 里接着搜，行为更像真人"连搜几个词"
        self._cleanup_mode: str = "exit"

    def set_cleanup_mode(self, mode: str) -> None:
        """供 caller 在 __aexit__ 之前更新 cleanup 行为（exit / soft）。"""
        if mode not in ("exit", "soft"):
            logger.warning(f"[{self.serial}] unknown cleanup_mode={mode!r}, ignored")
            return
        self._cleanup_mode = mode

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

    async def _human_click(self, xpath: str, timeout: float = 2.5, jitter_px: int = 12) -> bool:
        """带坐标抖动的点击：先 wait 元素 → 取 bounds → 随机偏中心点 d.click(x,y)。

        返回 True 表示点到了，False 表示找不到/超时（caller 决定要不要往下走）。

        为啥不直接用 ``self._d.xpath(x).click()``？因为 uiautomator2 默认点
        bbox 几何中心，机器特征明显——真人手指几乎不可能每次都精确点中心。
        这里抖动 ±12px（受控件 1/3 边长限制），分布偏低斯/真人级。
        """
        def _do_click() -> bool:
            el = self._d.xpath(xpath).wait(timeout=timeout)
            if not el:
                return False
            info = el.info or {}
            bounds = info.get("bounds") or {}
            if not bounds:
                # 拿不到 bounds 就回退默认点击（极少见）
                self._d.xpath(xpath).click()
                return True
            x, y = _jittered_point_in_bounds(bounds, jitter_px=jitter_px)
            self._d.click(x, y)
            return True

        return await asyncio.to_thread(_do_click)

    async def _post_task_cleanup(self) -> None:
        """任务结束清场。两档行为：

        ── exit 模式（burst 结束，PDD 退后台）────────────────────────
        模拟真人"看完东西退几层 → 按 home 回桌面"。
        - 0-3 次 back（拟人地"退一层"）
        - 末尾强制 d.press("home") 把 PDD 真切到后台
        - 10% 概率跳过 back，直接 home

        2026-05-27 morning test 踩坑：BACK 键在 PDD 首页 tab 上只触发"再按
        一次返回退回桌面" toast，**不会真退到桌面**。配合 Honor X20 上 adb
        的 KEYCODE_HOME / launcher-intent 都吃瘪，所以末尾的 d.press("home")
        是必需的（走 atx-agent → InputManager，独立于 adb 子进程，是唯一在
        Honor EMUI 上能真退后台的路径）。

        ── soft 模式（burst 内中间任务，留在 PDD）────────────────────
        不退 PDD，仅做 0-1 次 back（最多从详情/结果页退到搜索建议页）。
        让下一个任务在同一个 PDD session 里接着搜，省去"退后台 + 重开 +
        冷启动 + warmup"那一整套 8-15s 浪费 + 反真人的频繁后台切换信号。

        失败/异常都 swallow——cleanup 是 best-effort，不能因为 cleanup 异常
        把整个任务结果给搞没了。
        """
        if not self._d:
            return
        if self._cleanup_mode == "soft":
            # Burst 内中间任务：PDD 留前台，但要把页面位置退到"首页"——
            #   搜索结果页（无底部 tab）→ back → 搜索输入页（无底部 tab）→ back → 首页（有底部 tab）
            # 这样下一个任务的 _ensure_home_tab 才能找到 "首页" 元素。
            # 75% 走 back×2（最干净，落到首页）；25% back×1（停在搜索输入页，
            # 拟人地"还想再搜一个，没立刻退到首页"——但这会让下个任务的
            # _ensure_home_tab 多 ~1-4s xpath 重试，是有意的拟人随机性）。
            try:
                backs = 1 if random.random() < 0.25 else 2
                for _ in range(backs):
                    await asyncio.to_thread(self._d.press, "back")
                    await _sleep_jitter(random.uniform(0.4, 0.8), jitter=0.3)
                logger.info(
                    f"[{self.serial}] cleanup mode=soft "
                    f"(stay in PDD, back x{backs})"
                )
            except Exception as e:
                logger.debug(f"[{self.serial}] soft cleanup ignored: {e}")
            return

        # exit 模式（默认）
        try:
            if random.random() < 0.10:
                await asyncio.to_thread(self._d.press, "home")
                return
            backs = random.randint(1, 3)
            for _ in range(backs):
                await asyncio.to_thread(self._d.press, "back")
                await _sleep_jitter(random.uniform(0.4, 0.9), jitter=0.3)
            # 关键：无论 back 走到 PDD 哪一层（首页/分类/结果页），最后用
            # d.press("home") 把 APP 切到后台。这条路径独立于 adb subprocess，
            # 所以 Honor 上 adb 路径失败时这条仍是兜底。
            await asyncio.to_thread(self._d.press, "home")
        except Exception as e:
            logger.debug(f"[{self.serial}] cleanup ignored: {e}")

    # ── 公开 API ────────────────────────────────────────────

    async def search(
        self,
        keyword: str,
        max_items: int = DEFAULT_MAX_ITEMS,
        mode: str = "fast",
        scroll_screens: int | None = None,
        is_first_in_burst: bool = True,
    ) -> PddSearchResult:
        """主入口：搜索关键词并返回前 N 个商品卡片。

        mode:
        - "fast"：单屏，约 20 个商品，~30s
        - "deep"：滚动 3 屏，约 60 个商品，~90s，更适合做长尾分析

        :param scroll_screens: 显式指定滚动屏数（覆盖 mode 默认值）。None 走
            mode 派生：fast=1 屏 / deep=3 屏。屏数越多 = 越可能触发百亿补贴卡
            = OCR 能验证到，但**暴露面也越大**（PDD 风控按"单 session 滚动深度"
            打分），建议 ≤ 5 屏。

        ── Day 4 humanization rebalance（2026-05-28 凌晨决议）─────────────
        本方法**每次进来掷骰子**抽 session profile，决定本次搜索的开局与收尾
        节奏。profile 直方图在 30 个 session 累计后应接近真人 cohort（不再
        是 100% standard 单峰）：

          direct       45% : 开 APP → 立刻搜（最常见，"知道要买什么"）
          short_peek   30% : 滑首页 1-2 下 → 搜（"被首页东西吸引但没点进去"）
          standard     20% : 滑首页 + 25% 概率短停详情 → 搜
          deep          5% : 滑首页 + 100% 进详情停留 → 搜（"逛着逛着想到要搜啥"）

        搜完后再按 profile 决定是否做 _post_search_browse（"搜到东西回首页
        逛一下再退"）。详见 docs/PDD-自建采集-roadmap.md "Day 4 humanization
        rebalance" 章节（待补）。
        """
        target_count = max_items if mode == "fast" else max_items * 3
        if scroll_screens is None:
            scroll_screens_eff = 1 if mode == "fast" else 3
        else:
            scroll_screens_eff = max(1, min(int(scroll_screens), 5))
        result = PddSearchResult()
        t0 = time.monotonic()

        # debug dump 序号 + 关键词重置（每个 search() 一组屏序号）
        self._debug_dump_seq = 0
        self._debug_dump_keyword = keyword

        # 抽 session profile（Fix A）
        # 但如果是 burst 内的"接续任务"（上一个搜索刚结束，PDD 还在前台），
        # 强制走 direct 路径——真人连搜不会"回首页浏览推荐再搜下一个词"，
        # 都是在搜索结果页直接点搜索栏改关键词。
        if not is_first_in_burst:
            profile = "direct"
        else:
            profile = _pick_session_profile()

        try:
            await self._ensure_app_foreground()
            await self._ensure_home_tab()
            await self._dismiss_popups()

            # 按 profile 决定 pre-search 是否 warmup（Fix A + Fix B）
            if profile != "direct":
                await self._idle_browse_warmup(mode=profile)
                # warmup 内的 detail-page 点击 + back 不一定回到首页，补一次
                await self._ensure_home_tab()
            # direct profile 跳过 warmup，进 APP 后直奔搜索栏

            await self._tap_search_entry()
            await self._type_keyword(keyword)
            await self._submit_search()
            risk = await self._detect_risk_walls()
            if risk:
                result.risk_signals.append(risk)
                result.error = f"risk_wall:{risk}"
                logger.warning(f"[{self.serial}] search aborted: risk={risk}")
                return result

            await self._wait_search_results()
            items = await self._collect_items(
                target_count, scroll_screens=scroll_screens_eff
            )
            result.items = items

            if not items:
                result.risk_signals.append("empty_result")
                result.error = "empty_result"
            else:
                # 搜到东西后，按 profile 决定是否"搜完逛一下"再退（Fix E）
                if random.random() < _POST_BROWSE_PROB.get(profile, 0.20):
                    try:
                        await self._post_search_browse()
                    except Exception as exc:
                        logger.debug(
                            f"[{self.serial}] post-search browse swallow: {exc}"
                        )
        except Exception as exc:
            logger.exception(f"[{self.serial}] search failed: {exc}")
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            elapsed = time.monotonic() - t0
            logger.info(
                f"[{self.serial}] search('{keyword}', mode={mode}, "
                f"scroll_screens={scroll_screens_eff}, "
                f"profile={profile}, intra_burst={'no' if is_first_in_burst else 'yes'}) "
                f"→ items={len(result.items)} risks={result.risk_signals} "
                f"elapsed={elapsed:.1f}s"
            )
        return result

    async def _post_search_browse(self) -> None:
        """搜完后逛一下首页再退（真人最常见的收尾模式之一）。

        实测真人路径：
        1. 拿到搜索结果 → 略看 1-2 个商品
        2. 退回首页（不是直接出 APP）
        3. 在首页滑两下顺便看看推荐
        4. 然后才退到桌面

        我们这里把"略看 1-2 个商品"省了（OCR + collect_items 已经包了一遍
        滚动，再多就过度暴露），直接做 step 2-3：
        - back 一次或两次（结果页 → 搜索建议页 → 首页）
        - 短 warmup（mode=short）滑首页
        - 主调用方的 _post_task_cleanup 会接着按 home 退到桌面

        本函数 2-5s，**不在风控扫描路径上**（已经 search 完了），属于"温和
        善后"性质。
        """
        def _back_some(times: int) -> None:
            for _ in range(times):
                self._d.press("back")
                time.sleep(_pace_uniform(0.5, 1.0))

        backs = random.randint(1, 2)
        await asyncio.to_thread(_back_some, backs)

        # 看看是不是真的回到首页了；不强求，回不到也不影响 cleanup 阶段的 home press
        try:
            await self._ensure_home_tab()
        except Exception:
            return

        # 用 short profile 滑两下首页，2-4s
        await self._idle_browse_warmup(mode="short")

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
            # 冷启动给开屏广告 / splash 留时间（等 APP 起来，不算"浏览"，
            # 不受 HUMANIZE_PACE 压缩）
            await _sleep_jitter(3.5, jitter=0.3, pace=False)

    async def _ensure_home_tab(self) -> None:
        """点击底部 home tab，强制把 PDD 拉回首页。

        2026-05-27 morning test 事故复盘：worker 启动 PDD 后，APP 可能恢复到
        上次的搜索结果页 / 详情页 / 活动横幅页。warmup 在那种页面上跑会
        scrolls=2 detail_visited=True 看似正常，但接下来 _tap_search_entry
        找不到 content-desc 搜索栏（搜索结果页顶部是 EditText，不是 TextView）。

        本方法在 _ensure_app_foreground 后立刻调用，无论当前在哪个二级页面，
        点底部"首页"tab 都能拉回主页。已经在首页时点一下近似 no-op。

        失败不抛——让后续 _dismiss_popups + _tap_search_entry 再尝试，至少
        worker 不会因为这一步卡死整个任务。
        """
        def _do_sync() -> str:
            d = self._d
            home_xpaths = [
                "//android.widget.TextView[@text=\"首页\" and @selected=\"true\"]",
                "//android.widget.TextView[@text=\"首页\"]",
                "//*[@text=\"首页\"][@clickable=\"true\"]",
                "//*[@content-desc=\"首页\"]",
            ]
            for xp in home_xpaths:
                try:
                    el = d.xpath(xp).get(timeout=1.0)
                except Exception:
                    continue
                if not el:
                    continue
                try:
                    info = el.info or {}
                except Exception:
                    info = {}
                if info.get("selected"):
                    return "already_home:" + xp
                try:
                    el.click()
                    return "clicked:" + xp
                except Exception as exc:
                    logger.debug(
                        f"[{self.serial}] home-tab click failed via {xp}: {exc}"
                    )
                    continue
            return "NO_HOME_TAB"

        outcome = await asyncio.to_thread(_do_sync)
        logger.info(f"[{self.serial}] ensure_home_tab: {outcome}")
        if outcome == "NO_HOME_TAB":
            logger.warning(
                f"[{self.serial}] 底部 home tab 没找到 —— "
                "可能 PDD 当前在全屏弹窗 / 二级页面 / 活动页"
            )
            return
        await _sleep_jitter(1.2, jitter=0.4)

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
                if await self._human_click(xpath, timeout=0.8, jitter_px=8):
                    logger.info(f"[{self.serial}] dismissed popup: {xpath}")
                    await _sleep_jitter(0.6)
            except Exception:
                continue

    async def _click_any(self, xpaths: list[str], timeout: float = 2.0) -> bool:
        """按顺序尝试一组 xpath，点中任意一个即返回 True（多 selector 兜底抗改版）。"""
        for xp in xpaths:
            try:
                if await self._human_click(xp, timeout=timeout):
                    logger.debug(f"[{self.serial}] _click_any hit: {xp}")
                    return True
            except Exception:
                continue
        return False

    async def _ocr_find(
        self,
        targets: list[str],
        region_ratio: tuple[float, float, float, float] | None = None,
        min_conf: float = 0.4,
    ) -> tuple[str, int, int, float, str] | None:
        """截屏 → OCR 找 targets 文字 → 返回最高分命中 (target, cx, cy, conf, raw)。

        PDD 把「个人中心/待收货/查看物流」等用 Canvas 自绘，xpath 抓不到，
        靠 OCR 认字 + bbox 中心坐标兜底。region_ratio=(rx1,ry1,rx2,ry2) 按屏幕
        比例裁剪，缩小范围更快更准（如底部导航传 (0.5,0.85,1,1)）。
        """
        def _run():
            try:
                img = self._d.screenshot(format="opencv")
            except Exception as exc:
                logger.debug(f"[{self.serial}] ocr screenshot failed: {exc!r}")
                return None
            if img is None:
                return None
            region = None
            if region_ratio:
                try:
                    h, w = img.shape[:2]
                    a, b, c, e = region_ratio
                    region = (int(w * a), int(h * b), int(w * c), int(h * e))
                except Exception:
                    region = None
            from pdd_app_worker import ocr as ocr_module
            hits = ocr_module.locate_texts(img, targets, region=region, min_confidence=min_conf)
            return hits[0] if hits else None

        return await asyncio.to_thread(_run)

    async def _ocr_tap(
        self,
        targets: list[str],
        region_ratio: tuple[float, float, float, float] | None = None,
        min_conf: float = 0.4,
        jitter_px: int = 10,
    ) -> bool:
        """OCR 找到 targets 文字就点它中心（带坐标抖动）。命中返回 True。"""
        hit = await self._ocr_find(targets, region_ratio=region_ratio, min_conf=min_conf)
        if not hit:
            return False
        _t, cx, cy, conf, raw = hit

        def _click():
            jx = cx + random.randint(-jitter_px, jitter_px)
            jy = cy + random.randint(-jitter_px, jitter_px)
            self._d.click(jx, jy)

        await asyncio.to_thread(_click)
        logger.info(f"[{self.serial}] OCR 命中 '{raw}'(conf={conf:.2f}) → 点 ({cx},{cy})")
        return True

    async def _tap_text(
        self,
        targets: list[str],
        region_ratio: tuple[float, float, float, float] | None = None,
        xpath_first: bool = True,
        xpath_timeout: float = 1.0,
        min_conf: float = 0.4,
    ) -> bool:
        """点文字：先快速试 xpath（便宜），抓不到再 OCR 认字按坐标点（PDD 自绘兜底）。"""
        if xpath_first:
            xpaths: list[str] = []
            for t in targets:
                xpaths.append(f'//*[@text="{t}"]')
                xpaths.append(f'//*[@content-desc="{t}"]')
            if await self._click_any(xpaths, timeout=xpath_timeout):
                return True
        return await self._ocr_tap(targets, region_ratio=region_ratio, min_conf=min_conf)

    async def maybe_browse_logistics(self, trigger: str = "A") -> str | None:
        """按概率查订单/物流（roadmap §11.4），best-effort 不抛。

        = 廉价决策门控 should_browse_logistics() + 真正执行 browse_logistics_now()。
        给"已有前台 PDD"的调用方用（如 burst 结尾 A）。静默期(B)的调用方应该先
        调模块级 should_browse_logistics() 决定要不要开 PDD，再调 browse_logistics_now()，
        避免概率没命中却白白前台化 PDD。

        :return: 概率没命中 → None（没执行）；执行了 → 结果状态字符串
                 viewed / empty / nav_failed（供上报落库）。
        """
        if not should_browse_logistics(self.serial):
            return None
        return await self.browse_logistics_now(trigger=trigger)

    async def browse_logistics_now(self, trigger: str = "A") -> str:
        """真正执行一次查物流并更新每日探测状态（不做开关/概率门控）。

        每日首次触发顺带确认该号有无真实订单：有→当日继续随机查；订单页空→
        当日冷却不再尝试（空订单页反而是异常信号）。导航失败保持 unknown 下次再试。

        :param trigger: 触发点 A（burst 结尾）/ B（静默期），仅用于日志。
        :return: 结果状态 viewed（有单已查物流）/ empty（订单页空）/
                 nav_failed（没到订单页 or 异常）。供上报落库。
        """
        global _logistics_state
        logger.info(f"[{self.serial}] logistics: 触发查物流 (trigger={trigger} state={_logistics_state})")
        try:
            result = await self._browse_logistics_once()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{self.serial}] logistics: 查物流异常(swallow): {exc}")
            return "nav_failed"
        if result is True:
            _logistics_state = "has_orders"
            logger.info(f"[{self.serial}] logistics: 已查物流，确认有真实订单")
            return "viewed"
        if result is False:
            _logistics_state = "cooldown"
            logger.info(f"[{self.serial}] logistics: 订单页为空 → 今日冷却，不再尝试")
            return "empty"
        # None：没到订单页（导航失败），保持 unknown，下次再试
        return "nav_failed"

    async def _on_profile_page(self) -> bool:
        """是否在「个人中心」页：先 xpath，再 OCR 认「我的订单/查看全部/商品收藏」。"""
        def _check_xpath() -> bool:
            d = self._d
            for xp in ('//*[@text="我的订单"]', '//*[@text="查看全部"]', '//*[@text="商品收藏"]'):
                try:
                    if d.xpath(xp).exists:
                        return True
                except Exception:
                    continue
            return False
        if await asyncio.to_thread(_check_xpath):
            return True
        # PDD 自绘 → OCR 兜底
        hit = await self._ocr_find(["我的订单", "查看全部", "商品收藏", "收货地址", "多多钱包"])
        return hit is not None

    async def _go_profile_tab(self) -> bool:
        """进底部「个人中心」tab（最右）。PDD 底部导航是自定义渲染、文字节点常拿
        不到，依次尝试：xpath → OCR 认字点坐标 → 最右下角坐标硬兜底。进去后用
        _on_profile_page 自校验，确认真到了个人中心页。"""
        # 1) xpath 快速试
        if await self._click_any([
            '//*[@text="个人中心"]',
            '//*[@content-desc="个人中心"]',
            '//*[contains(@content-desc,"个人中心")]',
        ], timeout=1.2):
            await _sleep_jitter(1.2, jitter=0.3)
            if await self._on_profile_page():
                return True
        # 2) OCR 在底部导航区认「个人中心」(只扫底部 15%、右半屏，又快又准)
        if await self._ocr_tap(["个人中心", "我的"], region_ratio=(0.5, 0.85, 1.0, 1.0)):
            await _sleep_jitter(1.3, jitter=0.3)
            if await self._on_profile_page():
                return True
        # 3) 坐标硬兜底：底部 5 个 tab，「个人中心」是第 5 个 → 中心 x≈0.90w，y≈0.965h
        try:
            def _tap_corner():
                w, h = self._d.window_size()
                self._d.click(int(w * 0.90), int(h * 0.965))
            await asyncio.to_thread(_tap_corner)
            await _sleep_jitter(1.3, jitter=0.3)
        except Exception:
            pass
        return await self._on_profile_page()

    async def _browse_logistics_once(self) -> bool | None:
        """进 个人中心 → 我的订单 →（有单则）查看物流 → 停留滑动 → 返回。

        返回 True=有单已浏览 / False=订单页为空 / None=没到订单页（导航失败）。
        selector 已按 2026-06-05 真机截图校准（底部导航坐标兜底 + 查看全部入口）。
        """
        # 0) 先回首页：burst 结尾调用时页面停在搜索结果页（无底部导航栏），
        #    没有「个人中心」tab 可点。真人也得先退到有 tab 的页再点个人中心。
        try:
            await self._ensure_home_tab()
        except Exception:
            pass
        await _sleep_jitter(0.6, jitter=0.4)

        # 1) 进底部「个人中心」tab（含坐标兜底 + 自校验）
        if not await self._go_profile_tab():
            logger.info(f"[{self.serial}] logistics: 没进到「个人中心」页，放弃")
            return None
        await _sleep_jitter(1.0, jitter=0.3)

        # 2) 进订单列表。标题/状态入口在 PDD 上也是自定义渲染，xpath 常抓不到，
        #    _tap_text 会 xpath 不中就 OCR 认字点坐标。优先点状态入口「待收货/
        #    待发货」(直达带「查看物流」的订单列表)，兜底「查看全部/我的订单」。
        #    OCR 扫上半屏即可（这些都在个人中心页上半部）。
        if not await self._tap_text(
            ["待收货", "待发货", "查看全部", "我的订单", "全部订单"],
            region_ratio=(0.0, 0.0, 1.0, 0.6),
        ):
            logger.info(f"[{self.serial}] logistics: 没找到订单入口(待收货/查看全部/我的订单)，放弃")
            return None
        await _sleep_jitter(1.8, jitter=0.3, pace=False)  # 订单页联网加载，别压缩

        # 3) 判断有无订单
        def _detect() -> str:
            d = self._d
            for xp in (
                '//*[contains(@text,"还没有相关订单")]',
                '//*[contains(@text,"暂无订单")]',
                '//*[contains(@text,"还没有订单")]',
                '//*[contains(@text,"空空如也")]',
                '//*[contains(@text,"你还没有")]',
            ):
                try:
                    if d.xpath(xp).exists:
                        return "empty"
                except Exception:
                    continue
            # 注意：不能用「评价」判有单——它同时是订单页顶部 tab，空号也恒在。
            # 只认真实订单卡上的操作按钮。
            for xp in (
                '//*[@text="查看物流"]',
                '//*[@text="确认收货"]',
                '//*[@text="申请退款"]',
                '//*[@text="再次购买"]',
                '//*[@text="申请售后"]',
            ):
                try:
                    if d.xpath(xp).exists:
                        return "has"
                except Exception:
                    continue
            return "unknown"

        state = await asyncio.to_thread(_detect)
        # xpath 抓不到（PDD 自绘）→ OCR 兜底判有无订单
        if state == "unknown":
            empty_hit = await self._ocr_find(
                ["还没有相关订单", "暂无订单", "还没有订单", "空空如也", "你还没有"]
            )
            if empty_hit:
                state = "empty"
            else:
                has_hit = await self._ocr_find(
                    ["查看物流", "确认收货", "申请退款", "再次购买", "申请售后"]
                )
                if has_hit:
                    state = "has"
        if state == "empty":
            return False
        if state == "unknown":
            logger.info(f"[{self.serial}] logistics: 订单页状态识别不出，放弃(不冷却)")
            return None

        # 4) 有订单：点「查看物流」逛一下（xpath→OCR 兜底；点不到就停在订单列表滑两下）
        viewed = await self._tap_text(["查看物流"], region_ratio=(0.0, 0.2, 1.0, 1.0))
        if viewed:
            await _sleep_jitter(2.0, jitter=0.4)  # 看物流停留 ~1.5-3s

        try:
            def _scroll():
                w, h = self._d.window_size()
                # 起止点抖动对齐 _idle_browse_warmup：x 抖 ±30、y 起止各自随机，
                # 别每次都同一条固定竖线（固定轨迹 = 机器特征）。
                for _ in range(random.randint(1, 2)):
                    x_start = w // 2 + random.randint(-30, 30)
                    x_end = w // 2 + random.randint(-30, 30)
                    y_start = int(h * random.uniform(0.62, 0.75))
                    y_end = int(h * random.uniform(0.30, 0.42))
                    _humanize_swipe_path(
                        self._d,
                        (x_start, y_start),
                        (x_end, y_end),
                        duration_s=random.uniform(0.30, 0.70),
                    )
                    time.sleep(_pace_uniform(0.6, 1.2))
            await asyncio.to_thread(_scroll)
        except Exception:
            pass

        # 5) 退回去（back 两三次），让 cleanup 的 home press 收尾。
        #    back 间隔走 _pace_uniform 受全局节奏因子，别固定 0.5s（固定节拍 = 机器特征）。
        try:
            def _back():
                for _ in range(random.randint(2, 3)):
                    self._d.press("back")
                    time.sleep(_pace_uniform(0.4, 0.9))
            await asyncio.to_thread(_back)
        except Exception:
            pass
        return True

    async def _idle_browse_warmup(self, mode: str = "standard") -> None:
        """前置摸鱼：开 APP 后假装看下推荐流再去搜。三档强度可选：

        - ``short``：仅 1-2 次首页滚动，不进详情，**总耗时 ~3-5s**
          模拟"想搜某东西，开了 APP 顺手滑两下首页看到搜索栏就点"的真人模式
        - ``standard``：1-2 次首页滚动 + 25% 概率进详情页短停 2-4s，**总耗时 ~5-8s**
          模拟"被首页某 banner 吸引看一眼然后想起要搜东西"
        - ``deep``：1-2 次首页滚动 + 100% 进详情页 + 详情页内滚动 + 退出，**总耗时 ~10-15s**
          模拟"逛了一圈某商品才决定要搜相关词"

        旧版（Day 3.5 实现）等价于现在的 deep，**100% 任务都跑** = 单峰画像
        被 4310 死因 §6 表权重 25% 抓住。Day 4 humanization rebalance 把
        deep 占比从 100% 降到 5%，剩下 95% 分给 short/standard/direct（在
        ``search()`` 的 ``_pick_session_profile`` 里抽签）。

        失败/异常都 swallow，不影响主搜索流程——摸鱼是 best-effort。
        """
        if mode == "direct":  # 防御性：调用方应该在 search() 层就跳过本函数
            return

        if mode == "short":
            params = dict(
                scroll_times_range=(1, 2),
                inter_scroll_sleep=(0.4, 0.9),
                detail_visit_prob=0.0,
                up_scroll_times=1,
            )
        elif mode == "deep":
            params = dict(
                scroll_times_range=(2, 3),
                inter_scroll_sleep=(0.7, 1.4),
                detail_visit_prob=1.0,   # deep 必进详情
                up_scroll_times=2,
                detail_stay_sec=(1.5, 2.8),
                detail_in_scroll_prob=0.6,
                detail_in_extra_sleep=(1.5, 3.0),
            )
        else:  # standard 兜底
            mode = "standard"
            params = dict(
                scroll_times_range=(1, 2),
                inter_scroll_sleep=(0.5, 1.1),
                detail_visit_prob=0.25,  # 标准模式只有 1/4 概率进详情
                up_scroll_times=1,
                detail_stay_sec=(1.2, 2.2),
                detail_in_scroll_prob=0.40,
                detail_in_extra_sleep=(1.0, 2.0),
            )

        try:
            def _do_sync():
                w, h = self._d.window_size()

                # ── 首页下滑（看推荐流）
                scroll_lo, scroll_hi = params["scroll_times_range"]
                scroll_times = random.randint(scroll_lo, scroll_hi)
                for _ in range(scroll_times):
                    x_start = w // 2 + random.randint(-35, 35)
                    x_end = w // 2 + random.randint(-35, 35)
                    y_start = int(h * random.uniform(0.65, 0.78))
                    y_end = int(h * random.uniform(0.22, 0.35))
                    _humanize_swipe_path(
                        self._d,
                        (x_start, y_start),
                        (x_end, y_end),
                        duration_s=random.uniform(0.30, 0.70),
                    )
                    time.sleep(_pace_uniform(*params["inter_scroll_sleep"]))

                # ── 是否进详情页（standard 25% / deep 100% / short 0%）
                clicked_detail = False
                if random.random() < params["detail_visit_prob"]:
                    try:
                        cards = self._d.xpath(
                            '//android.widget.ImageView['
                            '@resource-id="com.xunmeng.pinduoduo:id/pdd"]'
                        ).all()
                        clickable_cards = []
                        for c in cards:
                            try:
                                info = c.info
                                b = info.get("bounds") or {}
                                left = b.get("left", 0)
                                right = b.get("right", 0)
                                top = b.get("top", 0)
                                if (right - left) > w * 0.25 and top > h * 0.18:
                                    clickable_cards.append(c)
                            except Exception:
                                continue
                        if clickable_cards:
                            target = random.choice(clickable_cards[: min(8, len(clickable_cards))])
                            tinfo = target.info or {}
                            tbounds = tinfo.get("bounds") or {}
                            if tbounds:
                                tx, ty = _jittered_point_in_bounds(tbounds, jitter_px=15)
                                self._d.click(tx, ty)
                            else:
                                target.click()
                            clicked_detail = True

                            time.sleep(_pace_uniform(*params["detail_stay_sec"]))
                            if random.random() < params["detail_in_scroll_prob"]:
                                detail_x = w // 2 + random.randint(-25, 25)
                                _humanize_swipe_path(
                                    self._d,
                                    (detail_x, int(h * random.uniform(0.62, 0.78))),
                                    (detail_x + random.randint(-20, 20), int(h * random.uniform(0.20, 0.35))),
                                    duration_s=random.uniform(0.45, 0.85),
                                )
                                time.sleep(_pace_uniform(*params["detail_in_extra_sleep"]))
                            else:
                                # 不滑也要多看一会才走（避免"点开秒回"）
                                time.sleep(_pace_uniform(0.8, 1.8))
                            self._d.press("back")
                            time.sleep(_pace_uniform(0.5, 1.0))
                    except Exception:
                        pass

                # ── 上滑回顶（让搜索栏可见）
                for _ in range(params["up_scroll_times"]):
                    x_start = w // 2 + random.randint(-35, 35)
                    x_end = w // 2 + random.randint(-35, 35)
                    y_start = int(h * random.uniform(0.25, 0.35))
                    y_end = int(h * random.uniform(0.70, 0.82))
                    _humanize_swipe_path(
                        self._d,
                        (x_start, y_start),
                        (x_end, y_end),
                        duration_s=random.uniform(0.22, 0.40),
                    )
                    time.sleep(_pace_uniform(0.25, 0.50))

                return {"scrolls": scroll_times, "clicked_detail": clicked_detail}

            stats = await asyncio.to_thread(_do_sync)
            logger.info(
                f"[{self.serial}] warmup mode={mode}: "
                f"scrolls={stats['scrolls']} detail_visited={stats['clicked_detail']}"
            )
        except Exception as exc:
            logger.warning(
                f"[{self.serial}] warmup({mode}) skipped: "
                f"{type(exc).__name__}: {exc}"
            )

    async def browse_detail_and_harvest(
        self,
        *,
        min_screens: int = 3,
        max_screens: int = 6,
        capture_dir: Any = None,
    ) -> dict[str, Any]:
        """**假设当前已在某商品详情页**（调用方负责点卡片进入）：像真人一样通览
        整页并收割可被动获取的字段，**绝不"进去秒退"**。

        动作序列（全程拟人、受全局节奏因子控制）：
          1. 首屏停留 → `dumpsys activity top` 抓 goods_id / 主图 / 唤起链接（被动读）
          2. 随机 N 屏（min~max）逐屏下滑：每屏曲线滑动 + 停留看一会，
             模拟真人"翻图看详情看评价"
          3. 返回收割结果（不负责 press back，由调用方收尾）

        :param capture_dir: 给定 Path 时，每屏存一张截图（screen_00.png ...）+ 首屏
            dumpsys 文本，供 OCR 区域标定/调试。生产环境留空（不落盘）。
        :return: {"goods_id", "thumb_url", "detail_url", "screens"(实际滑动屏数)}
        """
        from pathlib import Path as _Path

        def _do_sync() -> dict[str, Any]:
            d = self._d
            w, h = d.window_size()
            out: dict[str, Any] = {
                "goods_id": None, "thumb_url": None, "detail_url": None, "screens": 0,
            }

            # ── 首屏停留 + 抓 goods_id（dumpsys 被动读取）
            time.sleep(_pace_uniform(1.6, 2.8))
            try:
                resp = d.shell("dumpsys activity top")
                top_txt = getattr(resp, "output", None) or (
                    resp if isinstance(resp, str) else str(resp)
                )
            except Exception as exc:  # noqa: BLE001
                top_txt = ""
                logger.debug(f"[{self.serial}] detail dumpsys failed: {exc!r}")
            meta = extract_goods_meta(top_txt)
            out.update(meta)
            if capture_dir:
                try:
                    cap = _Path(capture_dir)
                    cap.mkdir(parents=True, exist_ok=True)
                    (cap / "detail_dumpsys_top.txt").write_text(
                        top_txt or "", encoding="utf-8"
                    )
                    d.screenshot().save(str(cap / "screen_00.png"))
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[{self.serial}] capture screen_00 failed: {exc!r}")

            # ── 随机多屏下滑通览（真人翻图看详情/评价，不是滑一屏就走）
            n = random.randint(max(1, min_screens), max(min_screens, max_screens))
            for i in range(n):
                x = w // 2 + random.randint(-30, 30)
                _humanize_swipe_path(
                    d,
                    (x, int(h * random.uniform(0.66, 0.80))),
                    (x + random.randint(-25, 25), int(h * random.uniform(0.20, 0.34))),
                    duration_s=random.uniform(0.45, 0.85),
                )
                # 每屏停留看一会（偶尔停久一点，像在认真看某段）
                if random.random() < 0.30:
                    time.sleep(_pace_uniform(2.0, 3.6))
                else:
                    time.sleep(_pace_uniform(1.0, 2.0))
                out["screens"] = i + 1
                if capture_dir:
                    try:
                        d.screenshot().save(
                            str(_Path(capture_dir) / f"screen_{i + 1:02d}.png")
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            f"[{self.serial}] capture screen_{i + 1} failed: {exc!r}"
                        )
            return out

        result = await asyncio.to_thread(_do_sync)
        logger.info(
            f"[{self.serial}] detail harvest: goods_id={result.get('goods_id')} "
            f"thumb={'y' if result.get('thumb_url') else 'n'} "
            f"screens={result.get('screens')}"
        )
        return result

    async def _scroll_up_one(self) -> None:
        """结果页向上回滚一屏（手指下划），用于"回头"找刚逛过的卡。"""
        def _do_sync():
            w, h = self._d.window_size()
            x = w // 2 + random.randint(-30, 30)
            _humanize_swipe_path(
                self._d,
                (x, int(h * random.uniform(0.24, 0.34))),
                (x + random.randint(-25, 25), int(h * random.uniform(0.70, 0.82))),
                duration_s=random.uniform(0.30, 0.55),
            )
        await asyncio.to_thread(_do_sync)
        await _sleep_jitter(0.5, 0.4)

    async def _scroll_back_and_tap_title(self, title: str, max_up: int) -> bool:
        """从当前位置向上回滚，逐屏找标题=title 的卡并点进详情。命中返回 True。

        真人"回头看刚才那个"——不是按死坐标，而是滑回去重新认那张卡（标题是
        PDD 唯一可靠锚点 tv_title），所以多屏采集滚走后也能稳定重定位。
        """
        for _ in range(max(1, max_up)):
            cards = await self._dump_visible_cards()
            for c in cards:
                ct = (c.get("title") or "").strip()
                if not ct:
                    continue
                if ct == title or (len(title) >= 6 and title in ct) or (len(ct) >= 6 and ct in title):
                    bounds = c.get("card_bounds") or c.get("bounds")
                    if not bounds:
                        continue
                    def _tap():
                        tx, ty = _jittered_point_in_bounds(
                            {"left": bounds[0], "top": bounds[1],
                             "right": bounds[2], "bottom": bounds[3]},
                            jitter_px=14,
                        )
                        self._d.click(tx, ty)
                    await asyncio.to_thread(_tap)
                    await _sleep_jitter(2.4, 0.4)  # 等详情页渲染
                    return True
            await self._scroll_up_one()
        return False

    async def browse_results_with_dips(
        self,
        *,
        max_dips: int,
        chunk_min: int = 2,
        chunk_max: int = 3,
        capture_dir: Any = None,
    ) -> list[dict[str, Any]]:
        """搜索结果页拟人浏览 + 分段"回头点进详情"收割。

        节奏（贴合真人逛店）：逛 ``chunk_min~chunk_max`` 屏 → 回头挑这段里信号
        最强（badges 多 + 销量高）的一条进详情通览收割 → 返回结果页 → 接续再逛
        一段 → 再挑一条，最多 dip ``max_dips`` 次或列表逛完为止。

        全程 **best-effort**：某次定位/进入失败就跳过该次 dip 继续，绝不抛出、
        不打断采集主流程。

        :return: 收割到的详情列表 [{title, goods_id, thumb_url, detail_url, screens}]
        """
        harvested: list[dict[str, Any]] = []
        visited: set[str] = set()
        dip = 0
        guard = 0  # 防御：逛了好几段都没可点的就收手
        while dip < max_dips and guard < max_dips + 3:
            guard += 1
            # 1. 逛一段：滚 n 屏，沿途记下每屏可见卡（取本段最强未访问卡）
            n = random.randint(max(1, chunk_min), max(chunk_min, chunk_max))
            chunk: dict[str, dict[str, Any]] = {}
            for _ in range(n):
                try:
                    cards = await self._dump_visible_cards()
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[{self.serial}] dips dump failed: {exc!r}")
                    cards = []
                for c in cards:
                    t = (c.get("title") or "").strip()
                    if not t or t in visited:
                        continue
                    badges = c.get("badges") or []
                    sales = c.get("sales") or 0
                    chunk[t] = {"badges": len(badges), "sales": sales}
                await _sleep_jitter(random.uniform(0.9, 1.6), 0.35)
                await self._human_scroll_down()

            if not chunk:
                # 这一段没有新的可点卡（可能到底了）
                if dip == 0:
                    continue
                break

            # 2. 选本段最强：badges 数优先，其次销量
            best_title = max(
                chunk, key=lambda k: (chunk[k]["badges"], chunk[k]["sales"])
            )
            visited.add(best_title)

            # 3. 回头滑回去重新认那张卡并点进
            found = await self._scroll_back_and_tap_title(best_title, max_up=n + 1)
            if not found:
                logger.info(
                    f"[{self.serial}] dips: 回头没找到「{best_title[:14]}」，跳过本次"
                )
                continue

            # 4. 详情页通览收割
            cap = None
            if capture_dir:
                from pathlib import Path as _Path
                cap = _Path(capture_dir) / f"dip{dip + 1:02d}"
            try:
                meta = await self.browse_detail_and_harvest(
                    min_screens=2, max_screens=4, capture_dir=cap,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{self.serial}] dips harvest failed: {exc!r}")
                meta = {}
            meta["title"] = best_title
            harvested.append(meta)
            dip += 1

            # 5. 返回结果页（press back），继续下一段
            try:
                await asyncio.to_thread(self._d.press, "back")
            except Exception:
                pass
            await _sleep_jitter(random.uniform(1.0, 1.8), 0.35)

        logger.info(
            f"[{self.serial}] browse_results_with_dips: dips={len(harvested)} "
            f"goods_ids={[h.get('goods_id') for h in harvested]}"
        )
        return harvested

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

        # 2026-05-27 morning test 踩坑：PDD 首页搜索栏在 XML 里长这样：
        #   <node class="android.widget.TextView" content-desc="搜索"
        #         text="<上次搜过的词>" bounds="[477,181][669,238]"
        #         clickable="false" focusable="false" .../>
        # 元素真的存在、可见、有 bounds，但 d.xpath("//*[@content-desc=...]").exists
        # 返回 False。**uiautomator2 的 xpath() 对 CJK content-desc 属性匹配
        # 在某些 PDD/u2 版本组合上彻底不工作**（不是 PDD 改 UI！）。
        #
        # 双策略修复：
        #   ① UiSelector(description=...) 走 Android 原生选择器（不经过 u2 的 xpath 引擎）
        #   ② dump XML + 正则提 bounds + d.click(x,y)（绝对兜底，因为 re.search 已证明能匹配）
        def _do_sync() -> tuple[bool, str]:
            d = self._d

            # 策略 1：UiSelector + className（不经过 xpath 引擎，更可靠）
            try:
                sel = d(description="搜索", className="android.widget.TextView")
                if sel.exists:
                    try:
                        info = sel.info or {}
                    except Exception:
                        info = {}
                    b = info.get("bounds") or {}
                    if b:
                        x, y = _jittered_point_in_bounds(b, jitter_px=10)
                        d.click(x, y)
                        return True, f"ui_selector@({x},{y})"
                    sel.click()
                    return True, "ui_selector_default_click"
            except Exception as exc:
                logger.debug(f"[{self.serial}] ui_selector failed: {exc}")

            # 策略 2：dump XML + 正则匹配 bounds（绝对兜底）
            try:
                xml = d.dump_hierarchy()
                # 匹配 class=TextView 且 content-desc 精确为"搜索"（不含拍照搜索等）的 node。
                # XML 里 content-desc 和 bounds 顺序可能反转，两种都试。
                patterns = [
                    re.compile(
                        r'<node[^>]*?class="android\.widget\.TextView"[^>]*?'
                        r'content-desc="搜索"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
                    ),
                    re.compile(
                        r'<node[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*?'
                        r'class="android\.widget\.TextView"[^>]*?content-desc="搜索"'
                    ),
                ]
                for pat in patterns:
                    m = pat.search(xml)
                    if m:
                        l, t, r, b2 = (int(g) for g in m.groups())
                        bounds = {"left": l, "top": t, "right": r, "bottom": b2}
                        x, y = _jittered_point_in_bounds(bounds, jitter_px=10)
                        d.click(x, y)
                        return True, f"xml_parse@({x},{y})_bounds=[{l},{t}][{r},{b2}]"
            except Exception as exc:
                logger.debug(f"[{self.serial}] xml_parse failed: {exc}")

            return False, "all_strategies_failed"

        clicked, how = await asyncio.to_thread(_do_sync)
        if clicked:
            logger.info(f"[{self.serial}] tapped search entry via: {how}")
            await _sleep_jitter(0.8)
            return

        # 全部失败时 dump 当前 hierarchy 用于复盘
        try:
            from datetime import datetime
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dump_path = f"tap_search_failed_{stamp}.xml"
            xml = await asyncio.to_thread(lambda: self._d.dump_hierarchy())
            with open(dump_path, "w", encoding="utf-8") as f:
                f.write(xml)
            cur = await asyncio.to_thread(lambda: self._d.app_current())
            logger.error(
                f"[{self.serial}] _tap_search_entry FAILED  "
                f"current_pkg={cur.get('package')} "
                f"activity={cur.get('activity')} "
                f"dump_saved={dump_path}"
            )
        except Exception as dump_exc:
            logger.error(
                f"[{self.serial}] _tap_search_entry FAILED, "
                f"also failed to dump hierarchy: {dump_exc}"
            )
        raise RuntimeError("找不到首页搜索入口 —— 检查 PDD 是否在首页且 UI 没大改")

    async def _type_keyword(self, keyword: str) -> None:
        """在搜索输入框里逐字敲关键词，模拟人类打字节奏。

        反爬关键点：
        1. PDD 拿不到原始 IME 事件，但能监听 EditText.text 变化的速率。
           一次 send_keys("机械键盘") → text 一帧内从空变成 4 个字 = 机器人。
           真用户每字间 200-500ms。
        2. PDD 可以通过 InputMethodManager.getCurrentInputMethodSubtype()
           读到"当前输入法是 com.github.uiautomator.adbkeyboard" =
           **直接命中 uiautomator 爬虫指纹**。所以输入完一定要切回默认输入法，
           让 PDD 即使去查也只能在 ~输入耗时窗口内看到 ATX，平时是用户的
           正常输入法。

        实现：
        - set_fastinput_ime(True) 切到 ATX 输入法（中文必须）
        - clear_text 清掉占位符
        - 每字 send_keys(clear=False) 追加 + 随机 sleep（中文比 ASCII 慢）
        - 偶尔 10% 概率多停顿 0.5-1.2s（模仿"想词"）
        - 输入完 sleep 0.5-1.5s
        - **finally**：set_fastinput_ime(False) 恢复用户默认输入法

        每字间隔分布：
        - 中文字符：0.22-0.55s（输入法选词时间）
        - 数字/ASCII：0.10-0.28s（按键直接出字符）
        """
        if not keyword:
            return

        def _setup_ime():
            self._d.set_fastinput_ime(True)
            self._d.clear_text()

        await asyncio.to_thread(_setup_ime)
        try:
            await _sleep_jitter(0.4, jitter=0.5)

            for i, ch in enumerate(keyword):
                await asyncio.to_thread(
                    lambda c=ch: self._d.send_keys(c, clear=False)
                )
                is_last = (i == len(keyword) - 1)
                if is_last:
                    continue
                if "\u4e00" <= ch <= "\u9fff":
                    base_delay = random.uniform(0.22, 0.55)
                else:
                    base_delay = random.uniform(0.10, 0.28)
                if random.random() < 0.10:
                    base_delay += random.uniform(0.5, 1.2)
                await asyncio.sleep(base_delay)

            await asyncio.sleep(random.uniform(0.5, 1.5))
        finally:
            # 关键：还原默认 IME，让 PDD 看到的"当前输入法"不是 uiautomator-adbkeyboard。
            # 这一步失败不抛——再不济搜索还是发出去了。
            try:
                await asyncio.to_thread(lambda: self._d.set_fastinput_ime(False))
            except Exception as exc:
                logger.debug(f"[{self.serial}] restore default IME failed: {exc}")

    async def _submit_search(self) -> None:
        """提交搜索。优先点页面上的"搜索"按钮，回退到键盘 Enter。

        2026-05-27 同 _tap_search_entry 一样的踩坑：uiautomator2 的 xpath()
        对 CJK text 匹配可能失败。改用 UiSelector + XML 兜底双策略。

        如果两种策略都点不到"搜索"按钮，回退到 d.press("enter")。但要注意：
        PDD 的搜索建议页可能把回车键当成"关键盘"而不是"提交搜索"，所以 enter
        是最后兜底，不可靠。
        """
        def _do_sync() -> tuple[bool, str]:
            d = self._d

            # 策略 1：UiSelector（不经过 xpath 引擎）
            for cls in ("android.widget.TextView", "android.widget.Button"):
                try:
                    sel = d(text="搜索", className=cls)
                    if sel.exists:
                        try:
                            info = sel.info or {}
                        except Exception:
                            info = {}
                        b = info.get("bounds") or {}
                        if b:
                            x, y = _jittered_point_in_bounds(b, jitter_px=10)
                            d.click(x, y)
                            return True, f"ui_selector[{cls}]@({x},{y})"
                        sel.click()
                        return True, f"ui_selector[{cls}]_default_click"
                except Exception as exc:
                    logger.debug(
                        f"[{self.serial}] submit ui_selector[{cls}] failed: {exc}"
                    )

            # 策略 2：dump XML + 正则匹配 bounds
            try:
                xml = d.dump_hierarchy()
                # 搜索建议页通常有一个 text="搜索" 且 clickable=true 的 button/textview。
                # 因为 PDD 把"搜索"这个 text 复用在多个地方（包括搜索栏 placeholder），
                # 这里**只匹配 clickable=true 的**，避免点回搜索栏本身。
                pattern = re.compile(
                    r'<node[^>]*?text="搜索"[^>]*?'
                    r'clickable="true"[^>]*?'
                    r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
                )
                m = pattern.search(xml)
                if not m:
                    # 顺序可能反转
                    pattern2 = re.compile(
                        r'<node[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*?'
                        r'text="搜索"[^>]*?clickable="true"'
                    )
                    m = pattern2.search(xml)
                if m:
                    l, t, r, b2 = (int(g) for g in m.groups())
                    bounds = {"left": l, "top": t, "right": r, "bottom": b2}
                    x, y = _jittered_point_in_bounds(bounds, jitter_px=10)
                    d.click(x, y)
                    return True, f"xml_parse@({x},{y})_bounds=[{l},{t}][{r},{b2}]"
            except Exception as exc:
                logger.debug(f"[{self.serial}] submit xml_parse failed: {exc}")

            return False, "all_strategies_failed"

        clicked, how = await asyncio.to_thread(_do_sync)
        if clicked:
            logger.info(f"[{self.serial}] tapped submit via: {how}")
            return

        # 全部失败时 dump 当前 hierarchy 用于复盘
        try:
            from datetime import datetime
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dump_path = f"submit_search_failed_{stamp}.xml"
            xml = await asyncio.to_thread(lambda: self._d.dump_hierarchy())
            with open(dump_path, "w", encoding="utf-8") as f:
                f.write(xml)
            logger.warning(
                f"[{self.serial}] _submit_search FAILED — falling back to press(enter)  "
                f"dump_saved={dump_path}"
            )
        except Exception as dump_exc:
            logger.warning(
                f"[{self.serial}] _submit_search FAILED + dump failed: {dump_exc}"
            )
        # 最后兜底：硬按 enter（可能被 PDD 当关键盘而非提交，不保证生效）
        await asyncio.to_thread(self._d.press, "enter")

    async def _detect_risk_walls(self) -> str | None:
        """识别风控/登录墙信号。命中返回信号名，否则 None。

        旧实现是 11 个 XPath 顺序 wait(timeout=0.5s)，最坏 5.5s 全花在"没风控
        也得逐个查"上。这部分耗时被 4310 死因复盘列为"操作偏慢"贡献项之一。

        现在改成**单次 dump_hierarchy + 内存 substring 扫描**：~150ms dump +
        ~5ms regex + ~5us substring × 11 关键词 = 总耗时 ≈ 0.15-0.2s，跟旧版
        相比快 3-30 倍。

        匹配只在 ``text="X"`` 和 ``content-desc="X"`` 这两类 attribute value 上做，
        不会跨属性 / 不会匹配到 class 名或 resource-id，假阳性风险极低。

        实名认证 / 身份验证 = PDD 对账号本身的风控（不是设备级），命中说明
        这个账号已经污染了，应该 quarantine 换号；继续在同一设备上重登一个
        新号通常能恢复。
        """
        # 信号名 → 关键词组（任一命中即返回该信号名）
        risk_signatures: list[tuple[str, tuple[str, ...]]] = [
            ("slide_verify",   ("拖动滑块", "向右滑动")),
            ("captcha",        ("验证码",)),
            ("login_wall",     ("登录拼多多", "请先登录")),
            ("rate_limited",   ("操作过于频繁", "稍后再试")),
            # real_name_wall：触发条件多为账号被风控、异地登录、短时间内大量搜索
            ("real_name_wall", ("实名认证", "身份验证", "请完成实名", "上传身份证")),
        ]

        try:
            xml = await asyncio.to_thread(lambda: self._d.dump_hierarchy())
        except Exception as exc:
            logger.debug(f"[{self.serial}] risk wall dump failed: {exc}")
            return None

        # 把所有 text="X" 和 content-desc="X" 的 X 抠出来拼成一个大串。
        # 在这个串上做 substring 匹配等价于旧版 contains(@text, "X") / 
        # contains(@content-desc, "X")，但不会把 resource-id 或 class 名里
        # 偶然出现的"实名"之类的字符当成命中。
        text_values = re.findall(r'text="([^"]*)"', xml)
        desc_values = re.findall(r'content-desc="([^"]*)"', xml)
        text_blob = " \x00 ".join(text_values + desc_values)

        for sig, keywords in risk_signatures:
            for kw in keywords:
                if kw in text_blob:
                    return sig
        return None

    async def _wait_search_results(self) -> None:
        """等结果列表稳定。

        旧版（Day 3 实现）无脑做"下滑 + 上滑暖屏" 4.5-7s 强制 PDD 渲染所有
        卡片，假定 lazy-render 一定要触发才能拿到价格。Day 4 重新审视后
        发现：**``_dump_with_lazy_recovery`` 已经会在发现 ≥ 50% 缺价时自动
        做 micro-scroll 兜底**，这里再无脑暖屏属于重复劳动。

        现在改成只做 1.5-2.5s static wait（让首屏稳定，不滑屏），剩下交给
        下游的 lazy-recovery。预期单任务省 3-5s humanization overhead。

        如果实测下来 lazy-recovery 兜不住（例如新版 PDD 把首屏渲染拖到
        > 3s），下面这行的 sleep 上限可以再调。
        """
        await _sleep_jitter(2.0, jitter=0.25)

    async def _collect_items(
        self, target_count: int, scroll_screens: int
    ) -> list[dict[str, Any]]:
        """抓商品卡片。先 dump 当前屏的所有 item，再滚动 N 次合并去重。

        每屏内部如果发现 ≥ 50% 卡片缺价（lazy-render 未完成），延迟 1s 再 dump
        一次，按 title 合并、更晚 dump 的非零价格覆盖较早的零价格。

        每屏 dump+lazy-recovery 完后再跑一次 OCR 兜底（``_ocr_missing_prices``），
        把"百亿补贴 Canvas 卡片"那种 XML 里完全看不到价格的卡片救回来。OCR 一定
        在跨屏合并之前做——不然滚走后截图已经对不上了。
        """
        seen_titles: dict[str, dict[str, Any]] = {}  # title → 最新数据

        # PDD 每次"滚一屏"实际只滑约半屏（见 _human_scroll_down），相邻两屏
        # 重叠近半，去重后每屏净增的新卡片有限。所以这里不按固定屏数收，而是
        # 滚到凑够 target_count 为止：
        #   - scroll_screens 只当"至少滚这么多屏"的下限提示；
        #   - max_screens 硬上限，防在结果稀少的词上无限滚；
        #   - 连续两屏一个新卡片都没有 → 视为到底/卡住，提前结束。
        max_screens = max(int(scroll_screens), 10)
        stagnant = 0
        screen_idx = 0
        while screen_idx < max_screens:
            cards = await self._dump_with_lazy_recovery()
            # OCR 兜底必须用当前屏的实时截图，所以放在跨屏合并之前
            cards = await self._ocr_missing_prices(cards)
            # 主图裁剪同理：必须在滚走之前，用当前屏截图按 image_bounds 裁
            cards = await self._attach_card_images(cards)

            new_this_screen = 0
            for card in cards:
                title = card.get("title", "").strip()
                if not title:
                    continue
                if title in seen_titles:
                    # 已存在 → 非零字段补全（lazy-render 二次抓到的值优先）
                    existing = seen_titles[title]
                    if not existing.get("price") and card.get("price"):
                        existing["price"] = card["price"]
                        # 同步带过来 OCR 相关元数据，方便后续 backend 评估
                        for opt_k in (
                            "price_source", "ocr_confidence",
                            "ocr_raw_text", "ocr_reason",
                        ):
                            if opt_k in card:
                                existing[opt_k] = card[opt_k]
                    if not existing.get("sales") and card.get("sales"):
                        existing["sales"] = card["sales"]
                    continue
                seen_titles[title] = card
                new_this_screen += 1
                if len(seen_titles) >= target_count:
                    return list(seen_titles.values())

            # 连续两屏零新增 → 多半到底了或列表卡住，别再无意义地滚
            if new_this_screen == 0:
                stagnant += 1
                if stagnant >= 2:
                    logger.info(
                        f"[{self.serial}] 连续 {stagnant} 屏无新增，提前结束"
                        f"（已采 {len(seen_titles)}/{target_count}）"
                    )
                    break
            else:
                stagnant = 0

            screen_idx += 1
            if screen_idx < max_screens:
                await self._human_scroll_down()
                await _sleep_jitter(1.0)

        logger.info(
            f"[{self.serial}] 采集结束：{len(seen_titles)}/{target_count} "
            f"（滚了 {screen_idx} 屏，上限 {max_screens}）"
        )
        return list(seen_titles.values())

    async def _ocr_missing_prices(
        self, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """对没拿到价格的卡片用 OCR 兜底（百亿补贴 Canvas 价格走这条）。

        步骤：
        1. 给所有卡片打 ``price_source`` 标签（xml / missing）
        2. 如果至少有一个 missing，截一次屏（``d.screenshot(format='opencv')``）
        3. 每个 missing 卡片：以"标题底边 → 标题底边 + 220px"为 y 区间，
           ``card_bounds`` 的 x 区间为水平范围，crop 截图丢给 ``ocr.extract_price_async``
        4. 命中 → ``price_source='ocr'`` + 填 ``ocr_confidence``/``ocr_raw_text``
        5. 不命中 → ``price_source='missing'`` 或 ``'ocr_error'``，记 ``ocr_reason``

        OCR 模块 import 失败 / 截图失败 / Reader init 失败 → 全部 swallow，
        worker 主流程继续。"我们能拿到的价格少一点"远比"OCR 把整个任务搞挂"
        优先级低。
        """
        # 先打默认标签（保留对已有 xml 价格的标记）
        for it in items:
            if it.get("price"):
                it.setdefault("price_source", "xml")
            else:
                it.setdefault("price_source", "missing")

        missing = [it for it in items if it.get("price_source") == "missing"]
        if not missing:
            return items

        # 截图 + 加载 OCR 模块（两者任意失败都 swallow）
        try:
            from pdd_app_worker import ocr as ocr_module
        except ImportError as exc:
            logger.warning(
                f"[{self.serial}] OCR 模块不可用，跳过兜底（{len(missing)} 个 missing）: {exc}"
            )
            return items
        try:
            screenshot = await asyncio.to_thread(
                lambda: self._d.screenshot(format="opencv")
            )
        except Exception as exc:
            logger.warning(
                f"[{self.serial}] OCR 截图失败，跳过兜底: {type(exc).__name__}: {exc}"
            )
            return items
        if screenshot is None:
            logger.warning(f"[{self.serial}] OCR 截图返回 None，跳过")
            return items

        try:
            h, w = screenshot.shape[:2]
        except Exception:
            logger.warning(f"[{self.serial}] OCR 截图 shape 异常，跳过")
            return items

        n_ok = 0
        n_fail = 0
        confs: list[float] = []

        for it in missing:
            # 优先用 card_bounds 框出整个商品卡片的横向范围（左右列对齐 PDD 实测）；
            # 老数据没 card_bounds 就回退到 title bounds + 50px padding。
            cb = it.get("card_bounds")
            tb = it.get("bounds") or [0, 0, 0, 0]
            if cb and len(cb) == 4:
                x1, _y_min_card, x2, _y_max_card = cb
            else:
                x1, x2 = tb[0] - 50, tb[2] + 50
            # 价格扫描垂直范围：标题底边 → +220px。PDD 双列布局每张卡片高
            # ~600-700px，标题正下方 50-200px 是价格区，220 包住有点冗余但
            # OCR 抗干扰能力够。
            y1 = tb[3]
            y2 = tb[3] + 220
            # 边界保护：到屏底就截到屏底
            x1 = max(0, int(x1) - 5)
            x2 = min(w, int(x2) + 5)
            y1 = max(0, int(y1))
            y2 = min(h, int(y2))
            if x2 - x1 < 30 or y2 - y1 < 30:
                # 卡片可能已经滚出屏外/标题在屏底 → 没法 OCR
                it["price_source"] = "missing"
                it["ocr_reason"] = "region_too_small"
                n_fail += 1
                continue

            price, meta = await ocr_module.extract_price_async(
                screenshot, (x1, y1, x2, y2)
            )
            reason = meta.get("reason", "unknown")
            if price is not None:
                it["price"] = price
                it["price_source"] = "ocr"
                if "confidence" in meta:
                    it["ocr_confidence"] = meta["confidence"]
                    confs.append(float(meta["confidence"]))
                if "raw_text" in meta:
                    it["ocr_raw_text"] = meta["raw_text"]
                n_ok += 1
            else:
                if reason in ("ocr_error", "ocr_init_error", "bad_image"):
                    it["price_source"] = "ocr_error"
                else:
                    it["price_source"] = "missing"
                it["ocr_reason"] = reason
                n_fail += 1

        if missing:
            avg_conf = sum(confs) / len(confs) if confs else 0.0
            logger.info(
                f"[{self.serial}] OCR fallback: filled {n_ok}/{len(missing)} "
                f"(still missing {n_fail}, avg_conf={avg_conf:.2f})"
            )
        return items

    async def _attach_card_images(
        self, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """给每张卡片裁一张主图缩略图（base64 data URL）塞进 ``item["image"]``。

        PDD APP 控件树没有图片 URL（图是渲染位图），只能截屏裁剪。必须在跨屏
        滚动之前调用——和 OCR 兜底一样，滚走后截图就跟 bounds 对不上了。

        任何一步失败都 swallow，绝不影响价格/销量主流程。
        """
        if not _CAPTURE_IMAGES:
            return items
        targets = [
            it for it in items
            if it.get("image_bounds") and not it.get("image")
        ]
        if not targets:
            return items

        try:
            import cv2  # noqa: PLC0415 — 延迟导入，环境没装也不影响采集
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{self.serial}] cv2 不可用，跳过主图裁剪: {exc}")
            return items
        try:
            screenshot = await asyncio.to_thread(
                lambda: self._d.screenshot(format="opencv")
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[{self.serial}] 主图截图失败，跳过: {type(exc).__name__}: {exc}"
            )
            return items
        if screenshot is None:
            return items
        try:
            h, w = screenshot.shape[:2]
        except Exception:  # noqa: BLE001
            return items

        n_ok = 0
        for it in targets:
            ib = it["image_bounds"]
            try:
                x1, y1, x2, y2 = int(ib[0]), int(ib[1]), int(ib[2]), int(ib[3])
            except Exception:  # noqa: BLE001
                continue
            x1 = max(0, x1)
            y1 = max(0, y1)
            x2 = min(w, x2)
            y2 = min(h, y2)
            if x2 - x1 < 60 or y2 - y1 < 60:
                continue
            try:
                crop = screenshot[y1:y2, x1:x2]
                ch, cw = crop.shape[:2]
                scale = _THUMB_MAX_PX / float(max(ch, cw))
                if scale < 1.0:
                    crop = cv2.resize(
                        crop,
                        (max(1, int(cw * scale)), max(1, int(ch * scale))),
                        interpolation=cv2.INTER_AREA,
                    )
                ok, buf = cv2.imencode(
                    ".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, _THUMB_JPEG_Q]
                )
                if not ok:
                    continue
                b64 = base64.b64encode(buf.tobytes()).decode("ascii")
                it["image"] = f"data:image/jpeg;base64,{b64}"
                n_ok += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[{self.serial}] crop image failed: {exc}")
                continue

        logger.info(f"[{self.serial}] 主图裁剪 {n_ok}/{len(targets)}")
        return items

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

        # 微滚动：让 RecyclerView 重 bind 所有 ViewHolder。
        # 起点 / 距离 / 时长 / 路径曲率全部随机，避免 PDD 把"暖屏后还
        # 小滑两下"当成爬虫的固定 fingerprint。
        def _micro_scroll():
            w, h = self._d.window_size()
            x_down = w // 2 + random.randint(-25, 25)
            x_up = w // 2 + random.randint(-25, 25)
            start_y = int(h * random.uniform(0.55, 0.68))
            shift = random.randint(120, 200)
            _humanize_swipe_path(
                self._d, (x_down, start_y), (x_down, start_y - shift),
                duration_s=random.uniform(0.22, 0.42),
            )
            time.sleep(_pace_uniform(0.45, 0.85))
            _humanize_swipe_path(
                self._d, (x_up, start_y - shift), (x_up, start_y),
                duration_s=random.uniform(0.22, 0.42),
            )
            time.sleep(_pace_uniform(0.8, 1.3))

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

    # debug：每个 search() 进度递增，区分同关键词下的多屏 dump
    _debug_dump_seq: int = 0
    _debug_dump_keyword: str = ""

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

        # 调试落盘：DEBUG_DUMP_LAST_SEARCH_XML=1 时把每屏 XML 保存到工作目录，
        # 名字带关键词 + 屏序号，便于事后采样分析（店铺名、SKU 痕迹等）。
        # 不开关时纯 no-op，无性能开销。
        if os.environ.get("DEBUG_DUMP_LAST_SEARCH_XML"):
            try:
                self._debug_dump_seq += 1
                kw = self._debug_dump_keyword or "unknown"
                safe_kw = re.sub(r"[^\w\u4e00-\u9fff]+", "_", kw)[:20]
                fname = f"dump_search_{safe_kw}_seq{self._debug_dump_seq}.xml"
                with open(fname, "w", encoding="utf-8") as f:
                    f.write(xml_str)
                logger.info(f"[{self.serial}] debug dump saved: {fname}")
            except Exception as exc:
                logger.warning(f"[{self.serial}] debug dump save failed: {exc}")

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

        # 商品主图候选：所有 ImageView。每张卡的主图都在标题正上方、卡片列宽
        # 内、近正方形的大图，下面按卡片逐个匹配（取"标题正上方、最靠近标题
        # 底边的够大 ImageView"）。
        image_nodes = [e for e in all_nodes if "ImageView" in e["class"]]

        # 2. 每个标题 → 一个商品卡片
        CARD_UP_EXTEND = 70     # 标题上方延伸 70px 捕获"广告"标识
        CARD_DOWN_EXTEND = 250  # 标题向下延伸 250px 算同卡片
        CARD_X_PAD = 50         # x 方向容差，应对子元素稍微出格

        # 价格小 TextView 候选：要么含 ¥/￥，要么是 1-5 位纯数字（可能带小数点）
        _price_token_re = re.compile(r"^(\d+(?:\.\d+)?|¥|￥)$")
        # badge 黑名单：包含这些 token 的 TextView 不算 badge（用于排除价格/销量/广告/价签）
        _badge_blacklist_re = re.compile(
            r"[¥￥]|^\d+(\.\d+)?$|已拼|已售|总售|^广告$|^券后$|^限\d+件$|^立省"
        )

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

            # 主图 bounds：标题正上方、列宽内、够大的 ImageView，取底边最贴近
            # 标题顶边的那个（即正上方那张图，排除上一张卡片的图/小角标）。
            image_bounds = None
            best_bottom = -1
            for e in image_nodes:
                ix1, iy1, ix2, iy2 = e["bounds"]
                if iy2 > ty1 + 10:           # 必须在标题之上
                    continue
                if iy1 < ty1 - 900:          # 离标题太远，多半是上一张卡片的图
                    continue
                ecx = (ix1 + ix2) // 2
                if not (card_x_min <= ecx <= card_x_max):
                    continue
                if (ix2 - ix1) < 100 or (iy2 - iy1) < 100:  # 滤掉小图标/角标
                    continue
                if iy2 > best_bottom:
                    best_bottom = iy2
                    image_bounds = [ix1, iy1, ix2, iy2]

            # 几何兜底：PDD 部分卡片把商品图当自绘视图（跟价格 Canvas 同理），
            # 控件树里没有对应 ImageView → 上面匹配为 None。这种情况按 PDD 搜索
            # 页固定版式推算：商品图是标题正上方、列宽见方的正方形。用标题
            # bounds 的横向范围当列宽，正方形高度=列宽，底边贴标题顶边上方 8px。
            if image_bounds is None:
                col_w = tx2 - tx1
                if col_w >= 120:
                    g_y2 = ty1 - 8
                    g_y1 = g_y2 - col_w
                    if g_y1 >= 0:
                        image_bounds = [tx1, g_y1, tx2, g_y2]

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

            # 抓 badges：标题正下方一行的"无 rid 短 TextView"，PDD 用来塞
            # 店铺信任标(五星好店/未发货秒退/先用后付)、人气标(X人收藏/X人拼
            # 单)、营销标(立减5元/即将恢复原价)等。对选品判断有用，原本被
            # 丢掉非常浪费。
            #
            # 识别规则：在 [title_bottom+5, title_bottom+95] y 范围内，
            # 是 TextView、text 长度 2-20、不是价格/销量/广告标志/title 本身。
            badge_y_min = ty2 + 5
            badge_y_max = ty2 + 95  # 限在 title 下方约 90px 内，避开价格行
            badges = []
            for e in all_nodes:
                if not _in_card(e):
                    continue
                if "TextView" not in e["class"]:
                    continue
                btext = e["text"].strip()
                if not btext or len(btext) > 20:
                    continue
                # 过滤价格 token、销量、广告标识
                if _badge_blacklist_re.search(btext):
                    continue
                # Y 范围
                ey_center = (e["bounds"][1] + e["bounds"][3]) // 2
                if not (badge_y_min <= ey_center <= badge_y_max):
                    continue
                # 过滤 title 本身（PDD 的 tv_title.text 是截断版，可能匹到 badge 行的东西，保险起见排除）
                if btext == title or (len(btext) >= 5 and btext in title):
                    continue
                if btext in badges:  # 同一 badge 不要重复
                    continue
                badges.append(btext)

            items.append({
                "title": title,
                "price": price or 0.0,
                "sales": sales,
                "is_ad": is_ad,
                "badges": badges,
                "bounds": list(t["bounds"]),  # title bounds, JSON-friendly
                "card_bounds": [card_x_min, card_y_min, card_x_max, card_y_max],
                "image_bounds": image_bounds,  # 主图 ImageView bounds（None=没找到）
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
        """人类化向下滑动一屏，触发 RecyclerView 懒加载。

        X / Y 起终点都加抖动，避免每次滑屏走同一条直线被 PDD 抓固定特征。
        """
        size = await asyncio.to_thread(lambda: self._d.window_size())
        w, h = size
        start_y_ratio = random.uniform(0.70, 0.82)
        end_y_ratio = random.uniform(0.22, 0.36)
        start = (w // 2 + random.randint(-30, 30), int(h * start_y_ratio))
        end = (w // 2 + random.randint(-30, 30), int(h * end_y_ratio))
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
