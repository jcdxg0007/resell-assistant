"""商品详情页「取数」真机调研冒烟（roadmap §11.2 Step 0）。

深度模式要"选择性进详情页"补抓 goods_id / SKU / 评论 / 历史价 / 店铺评分 /
百亿补贴真实价。**但列表页拿不到 goods_id，它到底怎么从 APP 详情页可靠抠出来
是未知数**（可能在 activity intent / 页面控件 / 要点"分享"才暴露）。

本脚本**不写死任何抽取方案**，只做一件事：自动进一条商品详情页，把 goods_id
可能藏身的所有地方 + 截图全 dump 到本地文件夹，供肉眼+正则定位。绕过采集队列、
不依赖 backend、不落库。

跑法（worker venv 激活、手机已连 USB）：
    python -m pdd_app_worker.smoke_detail
    SMOKE_KEYWORD=保温杯 python -m pdd_app_worker.smoke_detail   # 指定搜索词
    SMOKE_SERIAL=xxxx  python -m pdd_app_worker.smoke_detail      # 多机指定设备

产物（worker/_detail_spike/<时间戳>/）：
    01_results.png                  进卡片前的搜索结果页截图
    10_app_current.txt              d.app_current()（package/activity）
    11_dumpsys_activities.txt       dumpsys activity activities（找 intent/dat=goods_id）
    12_dumpsys_top.txt              dumpsys activity top（页面内 view 属性，可能含 url）
    13_detail_hierarchy.xml         详情页首屏控件树（全量 content-desc/text）
    detail_dumpsys_top.txt          收割函数抓 goods_id 用的 dumpsys 原文
    screen_00.png ~ screen_NN.png   首屏 + 逐屏下滑通览的截图（供 OCR 区域标定）
    20_share_*.xml/.png             仅 SMOKE_SHARE=1 时：点「分享」后的浮层（链接在这里）
    99_grep_hits.txt                自动在以上文本里 grep goods_id/yangkeduo/拼多多链接

**分享默认关闭**：第一次跑是纯被动 dump（等同正常浏览一个商品，零额外动作）。
先看 goods_id 是否本就躺在 intent/URL（11/12）里——在的话顺手抓、零风险。确认不在、
且确实想测分享链接路径时，再 `SMOKE_SHARE=1 python -m pdd_app_worker.smoke_detail`。

跑完把整个文件夹（尤其 11/12/13 和 99）发回来，我据此判断：能否被动拿到 goods_id /
店铺 / 评论 / 历史价 / 真实价，从而定深度模式抓哪些、要不要 OCR、要不要 goods_id。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("smoke_detail")

CARD_XPATH = (
    '//android.widget.ImageView[@resource-id="com.xunmeng.pinduoduo:id/pdd"]'
)
# goods_id 线索的正则：PDD 商品链接 / 裸 goods_id 键
CLUE_PATTERNS = [
    r"goods_id[=\":\s]+\d{6,}",
    r"goodsId[=\":\s]+\d{6,}",
    r"yangkeduo\.com[^\s\"'<>]*",
    r"pinduoduo://[^\s\"'<>]*",
    r"mobile\.yangkeduo[^\s\"'<>]*",
    r"goods\.html[^\s\"'<>]*",
]


def _write(path: Path, text: str) -> None:
    try:
        path.write_text(text or "", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"写 {path.name} 失败: {exc!r}")


def _save_png(d, path: Path) -> None:
    try:
        img = d.screenshot()  # PIL.Image（默认）
        img.save(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"截图 {path.name} 失败: {exc!r}")


def _shell(d, cmd: str) -> str:
    try:
        r = d.shell(cmd)
        return getattr(r, "output", None) or (r if isinstance(r, str) else str(r))
    except Exception as exc:  # noqa: BLE001
        return f"<shell '{cmd}' 失败: {exc!r}>"


def _dump_xml(d) -> str:
    try:
        return d.dump_hierarchy() or ""
    except Exception as exc:  # noqa: BLE001
        return f"<dump_hierarchy 失败: {exc!r}>"


def _tap_first_card(cli) -> bool:
    """在搜索结果页挑一张商品卡点进详情页。返回是否点了。"""
    import random
    from pdd_app_worker.pdd_app_client import _jittered_point_in_bounds
    d = cli._d
    w, h = d.window_size()
    try:
        cards = d.xpath(CARD_XPATH).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"找卡片失败: {exc!r}")
        return False
    clickable = []
    for c in cards:
        try:
            b = (c.info or {}).get("bounds") or {}
            if (b.get("right", 0) - b.get("left", 0)) > w * 0.25 and b.get("top", 0) > h * 0.18:
                clickable.append(c)
        except Exception:
            continue
    if not clickable:
        logger.warning("没找到可点的商品卡")
        return False
    target = clickable[0]
    tb = (target.info or {}).get("bounds") or {}
    if tb:
        tx, ty = _jittered_point_in_bounds(tb, jitter_px=12)
        d.click(tx, ty)
    else:
        target.click()
    return True


def _try_tap_share(cli) -> bool:
    """OCR 找「分享」并点（找不到就跳过，让用户手动点后自己截图）。"""
    try:
        from pdd_app_worker import ocr
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"OCR 不可用，跳过分享: {exc!r}")
        return False
    d = cli._d
    try:
        img = d.screenshot(format="opencv")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"分享前截图失败: {exc!r}")
        return False
    hits = ocr.locate_texts(img, ["分享", "分享赚", "share"], min_confidence=0.5)
    if not hits:
        logger.info("OCR 没找到「分享」入口（可能是无文字图标）——跳过，"
                    "可手动点分享后再截图")
        return False
    _, cx, cy, conf, raw = hits[0]
    logger.info(f"OCR 命中 '{raw}'(conf={conf:.2f}) → 点 ({cx},{cy})")
    d.click(cx, cy)
    return True


def _grep_clues(out_dir: Path) -> str:
    lines: list[str] = []
    pats = [re.compile(p, re.IGNORECASE) for p in CLUE_PATTERNS]
    for f in sorted(out_dir.glob("*.txt")) + sorted(out_dir.glob("*.xml")):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        found: set[str] = set()
        for p in pats:
            for m in p.findall(text):
                found.add(m if isinstance(m, str) else str(m))
        if found:
            lines.append(f"### {f.name}")
            lines.extend(sorted(found))
            lines.append("")
    return "\n".join(lines) if lines else "（未自动匹配到 goods_id / 链接线索，需肉眼翻 11/12/13/20）"


async def main() -> int:
    from pdd_app_worker.device_manager import healthy_serials
    from pdd_app_worker.pdd_app_client import PddAppClient

    print("=== 商品详情页取数 真机调研冒烟（§11.2 Step 0）===\n")

    serials = healthy_serials()
    if not serials:
        print("❌ 没有 state=device 的健康设备；先插好 USB / 允许调试。")
        return 2
    want = os.environ.get("SMOKE_SERIAL", "").strip()
    serial = want if (want and want in serials) else serials[0]
    keyword = os.environ.get("SMOKE_KEYWORD", "保温杯").strip() or "保温杯"
    print(f"✅ 设备 {serial}（健康: {serials}）；搜索词「{keyword}」\n")

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(__file__).resolve().parent.parent / "_detail_spike" / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"产物目录：{out_dir}\n")

    from pdd_app_worker.pdd_app_client import _sleep_jitter

    async with PddAppClient(serial) as cli:
        cli.set_cleanup_mode("exit")
        d = cli._d

        print("→ 前台化 PDD + 回首页 + 关弹窗 …")
        try:
            await cli._ensure_app_foreground()
            await cli._ensure_home_tab()
            await cli._dismiss_popups()
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ 前置出错（继续）: {exc!r}")

        # 搜索前先摸鱼一下（与正式 search() 的 cold-open 一致：看推荐流，不直奔搜索栏）。
        # 用 short 档（detail_visit_prob=0，不会随机点进别的详情页，避免污染本次调研）。
        print("→ 搜索前轻量 warmup（看推荐流，拟人）…")
        try:
            await cli._idle_browse_warmup(mode="short")
            await cli._ensure_home_tab()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"warmup 跳过: {exc!r}")

        print(f"→ 搜索「{keyword}」…")
        await cli._tap_search_entry()
        await cli._type_keyword(keyword)          # 内部含 IME 指纹收敛 + 逐字节奏
        await cli._submit_search()
        risk = await cli._detect_risk_walls()
        if risk:
            print(f"❌ 命中风控墙 {risk}，放弃本次调研（与正式采集一致地中止）。")
            return 1
        await cli._wait_search_results()
        await asyncio.to_thread(_save_png, d, out_dir / "01_results.png")

        print("→ 点第一张商品卡进详情页 …")
        tapped = await asyncio.to_thread(_tap_first_card, cli)
        if not tapped:
            print("❌ 没点进详情页（没找到商品卡）。把 01_results.png 发我看看。")
            return 1
        # 进详情页后像真人一样"读一会"再操作（抖动 sleep，受全局节奏因子控制）
        await _sleep_jitter(2.6, 0.4)

        # 首屏原始 dump（dumpsys / 控件树 / 截图都是**被动读取**，零额外动作风险）。
        # 这几份留作排查"店铺/评论/历史价在不在控件树、要不要 OCR"。
        print("→ 首屏 dump（intent / dumpsys / 控件树，供 OCR 字段排查）…")
        _write(out_dir / "10_app_current.txt", str(await asyncio.to_thread(d.app_current)))
        _write(out_dir / "11_dumpsys_activities.txt",
               await asyncio.to_thread(_shell, d, "dumpsys activity activities"))
        _write(out_dir / "12_dumpsys_top.txt",
               await asyncio.to_thread(_shell, d, "dumpsys activity top"))
        _write(out_dir / "13_detail_hierarchy.xml", await asyncio.to_thread(_dump_xml, d))

        # 统一收割函数（生产同款）：抓 goods_id/主图/唤起链接 + 随机多屏通览，
        # 每屏存截图（screen_00..NN.png）供 OCR 区域标定。**不秒退**。
        print("→ 拟人通览整页（随机 3-6 屏，每屏停留+截图）+ 抓 goods_id …")
        meta = await cli.browse_detail_and_harvest(
            min_screens=3, max_screens=6, capture_dir=out_dir,
        )
        print(f"   goods_id   = {meta.get('goods_id')}")
        print(f"   thumb_url  = {meta.get('thumb_url')}")
        print(f"   detail_url = {meta.get('detail_url')}")
        print(f"   实际滑了    = {meta.get('screens')} 屏")

        # 分享默认**关闭**：第一次跑只做纯被动 dump（intent/URL/控件树），完全等同
        # 正常浏览一个商品，零额外动作。先看 goods_id 是否本就躺在 intent/URL 里——
        # 在的话顺手抓、零风险，根本不需要分享。确认 intent/URL 里没有、且你确实想
        # 测分享链接这条路时，再 SMOKE_SHARE=1 重跑一次。
        if os.environ.get("SMOKE_SHARE", "").strip() in ("1", "true", "yes"):
            print("→ SMOKE_SHARE=1：尝试点「分享」抓链接浮层 …")
            shared = await asyncio.to_thread(_try_tap_share, cli)
            if shared:
                await _sleep_jitter(2.0, 0.4)
                _write(out_dir / "20_share_hierarchy.xml", await asyncio.to_thread(_dump_xml, d))
                await asyncio.to_thread(_save_png, d, out_dir / "20_share.png")
                await asyncio.to_thread(d.press, "back")   # 关掉分享浮层
                await _sleep_jitter(0.8, 0.4)
        else:
            print("→ 跳过分享（默认纯被动）。如需测分享链接路径：SMOKE_SHARE=1 重跑。")

        print("→ 自动 grep goods_id / 链接线索 …")
        _write(out_dir / "99_grep_hits.txt", _grep_clues(out_dir))

    print("\n🎉 调研完成。请把整个文件夹发回：")
    print(f"   {out_dir}")
    print("重点看 99_grep_hits.txt（自动命中的线索），没命中就翻 "
          "11/12/13/20 这几个 dump。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
