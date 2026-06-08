"""深度模式「结果页分段浏览 + 回头点进详情收割」冒烟（roadmap §11.2）。

模拟真人逛店节奏：搜索 → 逛 2-3 屏 → 回头挑这段信号最强（badges 多 + 销量高）
的一条进详情页通览收割（抓 goods_id / 主图 / 唤起链接，被动读取零风险）→ 返回
结果页 → 接续再逛一段 → 再挑一条，最多 dip K 次。**绝不"进去秒退"**。

驱动的是生产同款方法 `PddAppClient.browse_results_with_dips` +
`browse_detail_and_harvest`。绕过采集队列、不依赖 backend、不落库。

跑法（worker venv 激活、手机已连 USB）：
    python -m pdd_app_worker.smoke_detail
    SMOKE_KEYWORD=运动鞋 python -m pdd_app_worker.smoke_detail   # 指定搜索词
    SMOKE_DIPS=3        python -m pdd_app_worker.smoke_detail   # 进几条详情（K，默认3）
    SMOKE_SERIAL=xxxx   python -m pdd_app_worker.smoke_detail   # 多机指定设备

产物（worker/_detail_spike/<时间戳>/）：
    01_results.png                  结果页首屏截图
    dip01/, dip02/ ...              每次进详情的通览截图 screen_00..NN.png + dumpsys 原文
    99_grep_hits.txt                递归 grep goods_id/yangkeduo/拼多多链接

终端会直接打印每条收割到的 goods_id / thumb_url / detail_url。请把
`dip*/screen_*.png` 这些通览截图发回来，用于标定后续 OCR 区域（店铺/评论/历史价/真实价）。
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


def _grep_clues(out_dir: Path) -> str:
    lines: list[str] = []
    pats = [re.compile(p, re.IGNORECASE) for p in CLUE_PATTERNS]
    # 递归扫（每次 dip 的 dumpsys 落在 dipNN/ 子目录里）
    files = sorted(out_dir.rglob("*.txt")) + sorted(out_dir.rglob("*.xml"))
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        found: set[str] = set()
        for p in pats:
            for m in p.findall(text):
                found.add(m if isinstance(m, str) else str(m))
        if found:
            lines.append(f"### {f.relative_to(out_dir)}")
            lines.extend(sorted(found))
            lines.append("")
    return "\n".join(lines) if lines else "（未自动匹配到 goods_id / 链接线索）"


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

        # "边往下逛边遇强就点，进入概率随深度递减"（头部点得多、越往下越少）。
        # K 走 SMOKE_DIPS（默认 3），与生产 detail_top_k 同义。
        dips = int(os.environ.get("SMOKE_DIPS", "3") or "3")
        print(f"→ 结果页边逛边点（进入概率随深度递减，最多 {dips} 次 dip）…")
        harvested = await cli.browse_results_with_dips(
            max_dips=dips, capture_dir=out_dir,
        )
        if not harvested:
            print("❌ 一次详情都没收割到（回头定位都失败？）。把 01_results.png 发我看看。")
            return 1
        print(f"\n收割到 {len(harvested)} 条详情：")
        for i, m in enumerate(harvested, 1):
            print(f"  [{i}] {(m.get('title') or '')[:20]}")
            print(f"      goods_id   = {m.get('goods_id')}")
            print(f"      thumb_url  = {m.get('thumb_url')}")
            print(f"      detail_url = {m.get('detail_url')}")
            print(f"      通览屏数    = {m.get('screens')}")

        print("→ 自动 grep goods_id / 链接线索 …")
        _write(out_dir / "99_grep_hits.txt", _grep_clues(out_dir))

    print("\n🎉 调研完成。请把整个文件夹发回：")
    print(f"   {out_dir}")
    print("【Step 2 标定】重点看每个 dipNN/ 下的 screen_*_ocr.txt —— 那是详情页"
          "每屏的全屏 OCR 文本（带 y/x 坐标），用来定位店铺名/评论数/历史价/"
          "补贴价的真实标签与位置。把 dip01、dip02 两个文件夹整包发回即可。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
