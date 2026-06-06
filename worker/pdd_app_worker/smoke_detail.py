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
    14_detail.png                   详情页首屏截图
    15_detail_scrolled.xml/.png     下滑一屏后（SKU/历史价/评论常在下面）
    20_share_*.xml/.png             点「分享」后的浮层（链接/复制链接常在这里）
    99_grep_hits.txt                自动在以上文本里 grep goods_id/yangkeduo/拼多多链接

跑完把整个文件夹（尤其 11/12/13/20 和 99）发回来，我据此定 goods_id 的可靠抠法
再写正式抽取逻辑。
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


def _scroll_down(cli) -> None:
    """详情页下滑一屏（复用拟人滑动）。"""
    from pdd_app_worker.pdd_app_client import _humanize_swipe_path
    import random
    d = cli._d
    w, h = d.window_size()
    x = w // 2 + random.randint(-25, 25)
    _humanize_swipe_path(
        d,
        (x, int(h * 0.72)),
        (x + random.randint(-20, 20), int(h * 0.26)),
        duration_s=0.6,
    )


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

        print(f"→ 搜索「{keyword}」…")
        await cli._tap_search_entry()
        await cli._type_keyword(keyword)
        await cli._submit_search()
        risk = await cli._detect_risk_walls()
        if risk:
            print(f"❌ 命中风控墙 {risk}，放弃本次调研。")
            return 1
        await cli._wait_search_results()
        await asyncio.to_thread(_save_png, d, out_dir / "01_results.png")

        print("→ 点第一张商品卡进详情页 …")
        tapped = await asyncio.to_thread(_tap_first_card, cli)
        if not tapped:
            print("❌ 没点进详情页（没找到商品卡）。把 01_results.png 发我看看。")
            return 1
        await asyncio.sleep(3.0)  # 等详情页渲染

        print("→ dump 详情页（intent / dumpsys / 控件树 / 截图）…")
        _write(out_dir / "10_app_current.txt", str(await asyncio.to_thread(d.app_current)))
        _write(out_dir / "11_dumpsys_activities.txt",
               await asyncio.to_thread(_shell, d, "dumpsys activity activities"))
        _write(out_dir / "12_dumpsys_top.txt",
               await asyncio.to_thread(_shell, d, "dumpsys activity top"))
        _write(out_dir / "13_detail_hierarchy.xml", await asyncio.to_thread(_dump_xml, d))
        await asyncio.to_thread(_save_png, d, out_dir / "14_detail.png")

        print("→ 下滑一屏再 dump（SKU/历史价/评论常在下面）…")
        await asyncio.to_thread(_scroll_down, cli)
        await asyncio.sleep(1.5)
        _write(out_dir / "15_detail_scrolled.xml", await asyncio.to_thread(_dump_xml, d))
        await asyncio.to_thread(_save_png, d, out_dir / "15_detail_scrolled.png")

        print("→ 尝试点「分享」抓链接浮层 …")
        shared = await asyncio.to_thread(_try_tap_share, cli)
        if shared:
            await asyncio.sleep(2.0)
            _write(out_dir / "20_share_hierarchy.xml", await asyncio.to_thread(_dump_xml, d))
            await asyncio.to_thread(_save_png, d, out_dir / "20_share.png")
            # 浮层里若有「复制链接」，OCR 点一下（剪贴板可能拿到 URL）
            await asyncio.to_thread(d.press, "back")

        print("→ 自动 grep goods_id / 链接线索 …")
        _write(out_dir / "99_grep_hits.txt", _grep_clues(out_dir))

    print("\n🎉 调研完成。请把整个文件夹发回：")
    print(f"   {out_dir}")
    print("重点看 99_grep_hits.txt（自动命中的线索），没命中就翻 "
          "11/12/13/20 这几个 dump。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
