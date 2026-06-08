"""离线复跑：对已存的 dip 文件夹里的 screen_*_ocr.txt 跑字段抽取（§11.2 Step 2b）。

**不连手机、不重 OCR**——直接读 spike 落盘的 OCR 文本，聚合多屏后跑
``detail_fields.extract_detail_fields``，打印抽到的店铺/评论/价/规格等字段。
用于快速迭代抽取规则。

用法：
    python -m pdd_app_worker.smoke_detail_extract <dip 文件夹 或 其父目录> [--debug]

- 给某个 dipNN 文件夹 → 只跑那一条
- 给父目录（含多个 dipNN）→ 逐条跑
- 加 --debug → 额外打印"进店"附近的原始 OCR 块（文字/坐标/置信度），用于校准店铺名
"""
from __future__ import annotations

import sys
from pathlib import Path

from pdd_app_worker import detail_fields as df


def _dump_shopcard(blocks: list[dict]) -> None:
    """打印『进店』锚点上下 ±220px 的原始 OCR 块，校准店铺名规则用。"""
    jin = next((b for b in blocks if "进店" in b.get("text", "")), None)
    if not jin:
        print("  [debug] 未找到含『进店』的块")
        return
    jy = jin["cy"]
    near = [b for b in blocks if abs(b["cy"] - jy) <= 220]
    near.sort(key=lambda b: (b["cy"], b["cx"]))
    print(f"  [debug] 进店 锚点 cy={jy}，附近 ±220px 共 {len(near)} 块：")
    for b in near:
        flag = " <-进店" if "进店" in b["text"] else ""
        print(f"    cy={b['cy']:>5} cx={b['cx']:>5} conf={b.get('conf', 0):.2f}"
              f"  {b['text']!r}{flag}")


def _run_one(dip_dir: Path, debug: bool = False) -> None:
    ocr_files = sorted(dip_dir.glob("screen_*_ocr.txt"))
    if not ocr_files:
        return
    blocks: list[dict] = []
    # 每屏 cy 从 0 重计（屏高 ~2400），跨屏聚合须加屏偏移，否则版面 y 序错乱
    for idx, f in enumerate(ocr_files):
        try:
            scr = df.parse_ocr_dump(f.read_text(encoding="utf-8"))
            for b in scr:
                b["cy"] += idx * df.SCREEN_CY_STRIDE
            blocks.extend(scr)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! 读 {f.name} 失败: {exc!r}")
    fields = df.extract_detail_fields(blocks)

    print(f"\n=== {dip_dir.name}  （{len(ocr_files)} 屏, {len(blocks)} 文本块）===")
    print(f"  商品评价数   comment_count      = {fields['comment_count']}")
    print(f"  已拼件数     sold_count         = {fields['sold_count']}")
    print(f"  店铺名       shop_name          = {fields['shop_name']}")
    print(f"  店铺评价数   shop_review_count  = {fields['shop_review_count']}")
    print(f"  品牌评价数   brand_review_count = {fields['brand_review_count']}")
    print(f"  好评率       praise_rate        = {fields['praise_rate']}")
    print(f"  上榜         rank_badges        = {fields['rank_badges']}")
    print(f"  口碑标签     review_tags        = {fields['review_tags']}")
    print(f"  规格         specs              = {fields['specs']}")
    print(f"  券后价       coupon_price       = {fields['coupon_price']}")
    print(f"  优惠/立减    discount           = {fields['discount']}")
    if debug:
        _dump_shopcard(blocks)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    debug = "--debug" in sys.argv
    if not args:
        print("用法: python -m pdd_app_worker.smoke_detail_extract <dip 文件夹 或 父目录> [--debug]")
        return 2
    root = Path(args[0])
    if not root.exists():
        print(f"路径不存在: {root}")
        return 2

    # 自身就是 dip 文件夹（含 screen_*_ocr.txt）
    if list(root.glob("screen_*_ocr.txt")):
        _run_one(root, debug)
        return 0
    # 否则当父目录，逐个 dipNN 跑
    dips = sorted(p for p in root.glob("dip*") if p.is_dir())
    if not dips:
        print(f"{root} 下没找到 dipNN 文件夹或 screen_*_ocr.txt")
        return 1
    for d in dips:
        _run_one(d, debug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
