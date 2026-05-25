"""Day 3 校准：自动跑搜索流程到结果页，dump 商品卡片 UI。

跟 dump_screen.py 不同的是：这个会自己做完整搜索（点搜索栏 → 输入 →
提交 → 等结果），然后在结果页 dump。

用法（先 Ctrl+C 停掉 main.py）：
    python -m pdd_app_worker.dump_search_result
    python -m pdd_app_worker.dump_search_result 机械键盘
    python -m pdd_app_worker.dump_search_result 蓝牙耳机
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

import uiautomator2 as u2  # noqa: E402


SERIAL = "PKT0220416005274"
PDD_PACKAGE = "com.xunmeng.pinduoduo"


def main() -> int:
    keyword = sys.argv[1] if len(sys.argv) > 1 else "机械键盘"
    print(f"=== Search-result dump for '{keyword}' on {SERIAL} ===\n")

    print("[1/7] 连接手机...")
    d = u2.connect(SERIAL)
    if not d.info.get("screenOn"):
        d.screen_on()
        time.sleep(0.8)

    print("[2/7] 启动 PDD APP（如果已在前台会复用）...")
    d.app_start(PDD_PACKAGE, use_monkey=False, wait=True)
    time.sleep(3.0)
    cur = d.app_current()
    print(f"      activity={cur.get('activity', '')[:60]}")

    print("[3/7] 点搜索栏...")
    if not d.xpath('//android.widget.TextView[@content-desc="搜索"]').click_exists(timeout=3):
        if not d.xpath('//*[@content-desc="搜索"]').click_exists(timeout=2):
            print("❌ 找不到首页搜索栏（PDD 可能不在首页）")
            return 1
    time.sleep(2.0)

    print("[4/7] 切 ATX 输入法并输入关键词...")
    d.set_fastinput_ime(True)
    d.clear_text()
    d.send_keys(keyword, clear=True)
    time.sleep(1.5)

    print("[5/7] 提交搜索...")
    submitted = (
        d.xpath('//android.widget.TextView[@text="搜索"]').click_exists(timeout=2)
        or d.xpath('//android.widget.Button[@text="搜索"]').click_exists(timeout=2)
    )
    if not submitted:
        d.press("enter")
    time.sleep(4.0)  # 给结果页加载时间

    print("[6/7] dump 结果页 UI...")
    xml = d.dump_hierarchy()
    full_path = Path("dump_search_result.xml")
    full_path.write_text(xml, encoding="utf-8")
    cur = d.app_current()
    print(f"      当前 activity={cur.get('activity', '')[:60]}")
    print(f"      XML 写入 {full_path.resolve()}（{len(xml)} 字节）")

    print("[7/7] 提取候选元素...")
    node_re = re.compile(r"<node\s+([^>]+?)/?>", re.MULTILINE)
    attr_re = re.compile(r'(\w[\w-]*)="([^"]*)"')

    elements: list[dict[str, str]] = []
    for m in node_re.finditer(xml):
        attrs = dict(attr_re.findall(m.group(1)))
        elements.append(attrs)
    print(f"      解析到 {len(elements)} 个节点\n")

    def fmt(e: dict[str, str]) -> str:
        return (
            f"  class={e.get('class', '?'):<35} "
            f"rid={e.get('resource-id', ''):<48} "
            f"text={e.get('text', '')[:30]!r:<32} "
            f"desc={e.get('content-desc', '')[:20]!r:<22} "
            f"bounds={e.get('bounds', '')}"
        )

    lines: list[str] = [f"=== Search result for '{keyword}' / {cur.get('activity', '')} ===\n"]

    # ━━━ 段 A：含 "¥" 或 "￥" 的元素（价格）━━━
    lines.append("### A. 价格元素（含 ¥ 或 ￥）###")
    seen = set()
    for e in elements:
        text = e.get("text", "")
        if "¥" in text or "￥" in text:
            sig = (e.get("class"), e.get("resource-id"), text[:10])
            if sig in seen:
                continue
            seen.add(sig)
            lines.append(fmt(e))
            if len(seen) >= 10:  # 截前 10 个样本
                lines.append("  ... (更多省略)")
                break

    # ━━━ 段 B：含"已拼"或"件已拼"的元素（销量）━━━
    lines.append("\n### B. 销量元素（含'已拼'/'人已拼'）###")
    seen.clear()
    for e in elements:
        text = e.get("text", "")
        if "已拼" in text or "已售" in text:
            sig = (e.get("class"), e.get("resource-id"), text[:15])
            if sig in seen:
                continue
            seen.add(sig)
            lines.append(fmt(e))
            if len(seen) >= 10:
                lines.append("  ... (更多省略)")
                break

    # ━━━ 段 C：所有 TextView，按行展示前 30 个（找标题）━━━
    lines.append("\n### C. TextView 元素（前 30 个，找商品标题）###")
    count = 0
    for e in elements:
        if "TextView" not in e.get("class", ""):
            continue
        text = e.get("text", "").strip()
        if not text or len(text) < 2:  # 跳过空和 1 字符
            continue
        if text.isdigit() and len(text) < 4:  # 跳过纯数字短文本
            continue
        # 跳过 systemui
        if "android.systemui" in e.get("resource-id", ""):
            continue
        lines.append(fmt(e))
        count += 1
        if count >= 30:
            lines.append("  ... (更多省略)")
            break

    # ━━━ 段 D：RecyclerView / ListView 容器 ━━━
    lines.append("\n### D. RecyclerView / ListView 容器 ###")
    for e in elements:
        cls = e.get("class", "")
        if "RecyclerView" in cls or "ListView" in cls:
            lines.append(fmt(e))

    # ━━━ 段 E：风控墙特征 ━━━
    lines.append("\n### E. 风控墙信号（理论上应该为空）###")
    risk_kws = ["滑块", "拖动", "验证", "登录", "频繁", "稍后", "请先", "无网络"]
    for e in elements:
        text = e.get("text", "") + " " + e.get("content-desc", "")
        if any(k in text for k in risk_kws):
            lines.append(fmt(e))

    summary = "\n".join(lines)
    Path("dump_search_result_summary.txt").write_text(summary, encoding="utf-8")
    print(summary)
    print(f"\n=== summary 已保存到 dump_search_result_summary.txt ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
