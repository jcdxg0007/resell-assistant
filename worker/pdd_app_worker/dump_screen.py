"""Day 3 校准辅助：连手机 → 打开 PDD → 等 8 秒 → dump UI XML。

输出两份：
  - dump_full.xml：完整 UI 树（>50KB，给我做 XPath 用）
  - dump_summary.txt：候选元素列表（搜索框/弹窗关闭按钮/底部导航栏）— 你贴这个给我即可

指定设备（多机时）：SMOKE_SERIAL=xxxx 或命令行第一个参数；都没有则用下面的默认值。

用法（先 Ctrl+C 停掉 main.py，再跑）：
    set SMOKE_SERIAL=J7JJOVHQYHU8HMHE
    python -m pdd_app_worker.dump_screen
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

import uiautomator2 as u2  # noqa: E402


SERIAL = (
    (sys.argv[1] if len(sys.argv) > 1 else "")
    or os.environ.get("SMOKE_SERIAL", "").strip()
    or "PKT0220416005274"
)
PDD_PACKAGE = "com.xunmeng.pinduoduo"
WAIT_AFTER_OPEN = 8.0  # 等 splash + 弹窗完全展开


def main() -> int:
    print(f"=== Day 3 UI dump for {SERIAL} ===\n")
    print(f"[1/4] 连接手机...")
    d = u2.connect(SERIAL)
    info = d.info
    print(f"      sdk={info.get('sdkInt')} display={info.get('displaySizeDpX')}x{info.get('displaySizeDpY')}")

    print(f"[2/4] 启动 PDD APP（如果已经在前台会复用）...")
    d.app_start(PDD_PACKAGE, use_monkey=False, wait=True)
    print(f"      等 {WAIT_AFTER_OPEN}s 让 splash/弹窗展开...")
    time.sleep(WAIT_AFTER_OPEN)
    current = d.app_current()
    print(f"      当前包名: {current.get('package')}  activity: {current.get('activity', '')[:60]}")

    print(f"[3/4] dump UI XML...")
    xml = d.dump_hierarchy()
    full_path = Path("dump_full.xml")
    full_path.write_text(xml, encoding="utf-8")
    print(f"      完整 XML 写入 {full_path.resolve()}（{len(xml)} 字节）")

    print(f"[4/4] 提取关键元素 → dump_summary.txt")
    summary_lines: list[str] = []
    summary_lines.append(f"=== UI summary for {current.get('package')} / {current.get('activity', '')} ===\n")

    # 简单 regex 抽取（避免 ET 依赖）
    node_re = re.compile(
        r'<node\s+([^>]+?)/?>'
        , re.MULTILINE
    )
    attr_re = re.compile(r'(\w[\w-]*)="([^"]*)"')

    elements: list[dict[str, str]] = []
    for m in node_re.finditer(xml):
        attrs = dict(attr_re.findall(m.group(1)))
        elements.append(attrs)
    print(f"      解析到 {len(elements)} 个节点")

    def fmt(e: dict[str, str]) -> str:
        return (
            f"  class={e.get('class', '?'):<35} "
            f"rid={e.get('resource-id', ''):<60} "
            f"text={e.get('text', '')!r:<30} "
            f"desc={e.get('content-desc', '')!r:<30} "
            f"bounds={e.get('bounds', '')}"
        )

    # 段 1：所有 EditText（搜索栏候选）
    summary_lines.append("\n### 1. EditText 元素（搜索栏候选）###")
    for e in elements:
        if "EditText" in e.get("class", ""):
            summary_lines.append(fmt(e))

    # 段 2：含"搜索"字样的所有元素
    summary_lines.append("\n### 2. 含 '搜索' 字样的元素 ###")
    for e in elements:
        text = e.get("text", "") + e.get("content-desc", "")
        if "搜索" in text:
            summary_lines.append(fmt(e))

    # 段 3：resource-id 含 search 的元素
    summary_lines.append("\n### 3. resource-id 含 search 的元素 ###")
    for e in elements:
        rid = e.get("resource-id", "").lower()
        if "search" in rid:
            summary_lines.append(fmt(e))

    # 段 4：所有 clickable 的 ImageView/Button 含关闭/取消（弹窗关闭候选）
    summary_lines.append("\n### 4. 弹窗关闭按钮候选 ###")
    close_keywords = ["关闭", "取消", "跳过", "暂不", "稍后", "以后", "close", "cancel", "skip"]
    for e in elements:
        text = e.get("text", "") + e.get("content-desc", "")
        rid = e.get("resource-id", "").lower()
        if any(k in text for k in close_keywords) or any(
            f"{k}" in rid for k in ("close", "cancel", "skip", "btn_close")
        ):
            summary_lines.append(fmt(e))

    # 段 5：屏幕顶部 200px 内的所有元素（一般搜索栏在顶部）
    summary_lines.append("\n### 5. 屏幕顶部 200px 内的非空 TextView/EditText ###")
    for e in elements:
        bounds = e.get("bounds", "")
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not m:
            continue
        _, y1, _, y2 = map(int, m.groups())
        if y1 < 200 and ("TextView" in e.get("class", "") or "EditText" in e.get("class", "")):
            text_or_desc = (e.get("text") or e.get("content-desc") or "").strip()
            if text_or_desc:
                summary_lines.append(fmt(e))

    # 屏幕高度：取所有 bounds 的最大 y2 作为近似屏高，用于"底部导航栏"判定
    screen_h = 0
    for e in elements:
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", e.get("bounds", ""))
        if m:
            screen_h = max(screen_h, int(m.group(4)))
    bottom_threshold = int(screen_h * 0.82) if screen_h else 0

    # 段 6：底部导航栏候选（屏幕底部 18% 内的非空 text/desc 元素）——找「个人中心」tab
    summary_lines.append(
        f"\n### 6. 底部导航栏候选（y1 ≥ {bottom_threshold}，屏高≈{screen_h}）—— 找『个人中心』tab ###"
    )
    for e in elements:
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", e.get("bounds", ""))
        if not m:
            continue
        y1 = int(m.group(2))
        if bottom_threshold and y1 >= bottom_threshold:
            text_or_desc = (e.get("text") or e.get("content-desc") or "").strip()
            if text_or_desc or "clickable" in fmt(e):
                summary_lines.append(fmt(e))

    # 段 7：tab 文案命中（首页/个人中心/我的/聊天/多多视频/购物车/订单 等）
    summary_lines.append("\n### 7. 命中 tab/订单 文案的元素（看『个人中心』究竟叫什么）###")
    tab_words = ["个人中心", "我的", "首页", "聊天", "多多视频", "视频", "购物车", "我的订单", "订单"]
    for e in elements:
        text = (e.get("text", "") + "|" + e.get("content-desc", ""))
        if any(w in text for w in tab_words):
            summary_lines.append(fmt(e))

    summary = "\n".join(summary_lines)
    Path("dump_summary.txt").write_text(summary, encoding="utf-8")
    print(f"      写入 dump_summary.txt（{len(summary)} 字节）")

    print(f"\n=== 把下面 dump_summary.txt 的内容完整贴给我 ===\n")
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
