"""PoC: 验证第三方 PDD 数据 API 能否替代被关闭的 PDD H5 通道。

背景：见 docs/开发文档_转卖助手.md §1.4.4 + §1.4.5。

支持两个服务商，环境变量任选其一即可：
  - 万邦 onebound:      export ONEBOUND_KEY=xxx ONEBOUND_SECRET=xxx
  - JustOneAPI:         export JUSTONEAPI_TOKEN=xxx

执行：
    cd backend && python3 scripts/poc_pdd_third_party_api.py

输出：
  - 每个关键词在每个服务商上的命中条数 / 价格区间 / 百亿补贴标识检出率
  - 抽样 3 条原始 JSON 落到 logs/poc_pdd_api/<vendor>/<keyword>.json，便于人工核验
  - 总结表：哪个服务商更适合做主力

注意：本脚本在 devbox 本机或 backend pod 内都能跑，无需访问数据库。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
import httpx

# 目标品类抽样（覆盖 §1.6 P0/P1 + 高百亿补贴概率的日用品）
SAMPLE_KEYWORDS: list[str] = [
    "兔笼 摄影",            # P0 影视摄影器材配件
    "运动相机配件",          # P0 运动相机周边
    "手机支架 桌面",         # P1 桌面设备
    "显示器支架",            # P1 桌面设备
    "筋膜枪",                # P2 + 高百亿补贴概率
    "蓝牙耳机",              # 高百亿补贴概率
]

LOG_DIR = Path(__file__).resolve().parent.parent / "logs" / "poc_pdd_api"
LOG_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
}


# ---------------------------- 万邦 onebound ----------------------------

def call_onebound_search(keyword: str, key: str, secret: str) -> dict[str, Any]:
    """文档: https://open.onebound.cn/help/api/pinduoduo.item_search.html"""
    url = "https://api-gw.onebound.cn/pinduoduo/item_search/"
    params = {
        "key": key,
        "secret": secret,
        "q": keyword,
        "page": 1,
        "page_size": 40,
        "sort": "",
    }
    r = httpx.get(url, params=params, headers=HEADERS, timeout=30.0)
    r.raise_for_status()
    return r.json()


def parse_onebound_items(resp: dict[str, Any]) -> list[dict[str, Any]]:
    """归一化万邦返回。返回字段: title, price, original_price, sales, url, raw"""
    items = (resp.get("items") or {}).get("item") or []
    out = []
    for it in items:
        out.append({
            "title": it.get("title") or it.get("raw_title"),
            "price": _to_float(it.get("price") or it.get("promotion_price")),
            "original_price": _to_float(it.get("original_price")),
            "sales": _to_int(it.get("sales")),
            "url": it.get("detail_url") or it.get("item_url"),
            "subsidy": _detect_subsidy(it),  # 百亿补贴标记
            "raw": it,
        })
    return out


# ---------------------------- JustOneAPI ----------------------------

def call_justoneapi_search(keyword: str, token: str) -> dict[str, Any]:
    """文档假设路径（PDD 走商务合作流程，正式路径以销售给的为准）。"""
    base_candidates = [
        "https://api.justoneapi.com/api/pinduoduo/search-item-list/v1",
        "http://47.117.133.51:30015/api/pinduoduo/search-item-list/v1",
    ]
    last_err: Exception | None = None
    for base in base_candidates:
        try:
            r = httpx.get(
                base,
                params={"token": token, "keyword": keyword, "page": 1},
                headers=HEADERS,
                timeout=30.0,
            )
            if r.status_code == 200:
                return r.json()
            last_err = RuntimeError(f"HTTP {r.status_code} from {base}: {r.text[:200]}")
        except Exception as exc:
            last_err = exc
    raise last_err  # type: ignore[misc]


def parse_justoneapi_items(resp: dict[str, Any]) -> list[dict[str, Any]]:
    """正式字段以销售给的文档为准，这里用业内通用归一化。"""
    data = resp.get("data") or resp.get("items") or resp.get("result") or {}
    if isinstance(data, dict):
        items = data.get("items") or data.get("list") or data.get("goods_list") or []
    elif isinstance(data, list):
        items = data
    else:
        items = []

    out = []
    for it in items:
        out.append({
            "title": it.get("title") or it.get("goods_name") or it.get("name"),
            "price": _to_float(it.get("price") or it.get("min_group_price")
                               or it.get("group_price")),
            "original_price": _to_float(it.get("market_price") or it.get("min_normal_price")),
            "sales": _to_int(it.get("sales") or it.get("sold_quantity")),
            "url": it.get("url") or it.get("item_url"),
            "subsidy": _detect_subsidy(it),
            "raw": it,
        })
    return out


# ---------------------------- 通用 ----------------------------

def _to_float(x: Any) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _to_int(x: Any) -> int | None:
    try:
        return int(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _detect_subsidy(it: dict[str, Any]) -> bool:
    """判断单条记录是否带百亿补贴标识。
    PDD 体系下常见的指示：
      - activity_tags 含 7
      - title 含"百亿补贴"
      - 部分服务商会单独有 subsidy/has_subsidy 字段
    """
    tags = it.get("activity_tags") or it.get("tags") or []
    if isinstance(tags, list) and 7 in tags:
        return True
    if it.get("has_subsidy") or it.get("subsidy"):
        return True
    title = (it.get("title") or it.get("raw_title") or it.get("goods_name") or "")
    if "百亿补贴" in str(title):
        return True
    return False


def summarize(vendor: str, keyword: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {
            "vendor": vendor, "keyword": keyword, "count": 0,
            "subsidy_rate": 0.0, "price_min": None, "price_max": None,
            "price_median": None,
        }
    prices = sorted(x for x in (it["price"] for it in items) if x is not None)
    subsidy_count = sum(1 for it in items if it["subsidy"])
    return {
        "vendor": vendor,
        "keyword": keyword,
        "count": len(items),
        "subsidy_count": subsidy_count,
        "subsidy_rate": round(subsidy_count / len(items), 3),
        "price_min": prices[0] if prices else None,
        "price_max": prices[-1] if prices else None,
        "price_median": prices[len(prices) // 2] if prices else None,
    }


def dump_sample(vendor: str, keyword: str, items: list[dict[str, Any]]) -> Path:
    safe = keyword.replace(" ", "_").replace("/", "_")
    path = LOG_DIR / vendor / f"{safe}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    sample = items[:3]
    path.write_text(
        json.dumps(sample, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


# ---------------------------- 主流程 ----------------------------

def run_onebound(keywords: list[str], key: str, secret: str) -> list[dict[str, Any]]:
    rows = []
    for kw in keywords:
        try:
            resp = call_onebound_search(kw, key, secret)
            items = parse_onebound_items(resp)
            if items:
                dump_sample("onebound", kw, items)
            else:
                # 拒访/限流等异常情况，把错误响应留底
                LOG_DIR.joinpath("onebound", "_errors").mkdir(parents=True, exist_ok=True)
                (LOG_DIR / "onebound" / "_errors" / f"{kw}.json").write_text(
                    json.dumps(resp, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            rows.append(summarize("onebound", kw, items))
        except Exception as exc:
            rows.append({
                "vendor": "onebound", "keyword": kw,
                "count": 0, "subsidy_rate": 0.0,
                "error": f"{type(exc).__name__}: {exc}",
            })
        time.sleep(1.5)  # 文明调用
    return rows


def run_justoneapi(keywords: list[str], token: str) -> list[dict[str, Any]]:
    rows = []
    for kw in keywords:
        try:
            resp = call_justoneapi_search(kw, token)
            items = parse_justoneapi_items(resp)
            if items:
                dump_sample("justoneapi", kw, items)
            else:
                LOG_DIR.joinpath("justoneapi", "_errors").mkdir(parents=True, exist_ok=True)
                (LOG_DIR / "justoneapi" / "_errors" / f"{kw}.json").write_text(
                    json.dumps(resp, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            rows.append(summarize("justoneapi", kw, items))
        except Exception as exc:
            rows.append({
                "vendor": "justoneapi", "keyword": kw,
                "count": 0, "subsidy_rate": 0.0,
                "error": f"{type(exc).__name__}: {exc}",
            })
        time.sleep(1.5)
    return rows


def print_table(rows: list[dict[str, Any]]) -> None:
    cols = ["vendor", "keyword", "count", "subsidy_rate",
            "price_min", "price_median", "price_max", "error"]
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    line = " | ".join(c.ljust(widths[c]) for c in cols)
    print(line)
    print("-+-".join("-" * widths[c] for c in cols))
    for r in rows:
        print(" | ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


def main() -> int:
    onebound_key = os.getenv("ONEBOUND_KEY")
    onebound_secret = os.getenv("ONEBOUND_SECRET", "")
    justone_token = os.getenv("JUSTONEAPI_TOKEN")

    if not onebound_key and not justone_token:
        print(
            "请至少配置一个服务商的凭据：\n"
            "  export ONEBOUND_KEY=xxx ONEBOUND_SECRET=xxx\n"
            "  export JUSTONEAPI_TOKEN=xxx\n"
            "见 docs/开发文档_转卖助手.md §1.4.5 的接入门槛实测。",
            file=sys.stderr,
        )
        return 2

    all_rows: list[dict[str, Any]] = []

    if onebound_key:
        print(f"\n=== 万邦 onebound, {len(SAMPLE_KEYWORDS)} 关键词 ===")
        all_rows.extend(run_onebound(SAMPLE_KEYWORDS, onebound_key, onebound_secret))
    else:
        print("跳过万邦（未配置 ONEBOUND_KEY）")

    if justone_token:
        print(f"\n=== JustOneAPI, {len(SAMPLE_KEYWORDS)} 关键词 ===")
        all_rows.extend(run_justoneapi(SAMPLE_KEYWORDS, justone_token))
    else:
        print("跳过 JustOneAPI（未配置 JUSTONEAPI_TOKEN）")

    print("\n=== 汇总（同关键词跨厂商横向对比）===")
    print_table(all_rows)

    # 用结构化结果落一份，方便后续对比 / 跟踪
    out_path = LOG_DIR / f"summary_{int(time.time())}.json"
    out_path.write_text(
        json.dumps(all_rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n详细 JSON 已写入 {out_path}")
    print(f"3 条/关键词的样例数据落到 {LOG_DIR}/<vendor>/<keyword>.json")

    print(
        "\n下一步检查清单：\n"
        "  1. 任一 vendor 命中条数 ≥ 10/关键词，且 subsidy_rate > 0 → 可继续做付费 PoC\n"
        "  2. price_min 与 PDD APP 实测价对得上（人工核 1-2 条 url） → 数据真实可用\n"
        "  3. 抽 logs/poc_pdd_api/<vendor>/<keyword>.json 看是否含 sku / coupon / sales\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
