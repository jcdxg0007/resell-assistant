"""
Pinduoduo (拼多多) crawler — H5 mobile search.

Goal: expose `collect_market_data(context, keyword)` returning the same
shape our selection pipeline can consume. Used by product_scoring to fill
`pdd_min_price` for the 利润率 dimension.

Scraping strategy:
- Require a logged-in crawler小号 (cookies injected from accounts table
  where ``platform='pdd_crawler'``). 2026 PDD rejects guest search with
  a 403 → login.html redirect.
- **Primary path**: parse ``window.rawData.stores.store.data.ssrListData.list``.
  PDD H5 is an SSR app; after hydration (we scroll once to trigger it) the
  full product list is exposed as a structured JS object on window — no
  XHR to intercept, no canvas/webfont de-obfuscation needed.
- Fallback: parse the inline ``window.rawData = {...}`` script tag from the
  initial HTML response if ``page.evaluate`` fails for any reason.
- Wrap navigation with anti_risk.human_delay / scroll_like_human so the
  traffic doesn't look like a uniform scrape.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from loguru import logger

from app.services import anti_risk


_SEARCH_URL = (
    "https://mobile.yangkeduo.com/search_result.html?search_key={kw}&source=index"
)


# ─── sales-tip parsing ────────────────────────────────────────────────
_SALES_UNIT_W = re.compile(r"(\d+(?:\.\d+)?)\s*万")


def _parse_sales_tip(text: str | None) -> int:
    """"总售48.5万+件" / "拼单2.3万" / "1000+人付款" → integer count.
    Falls back to 0 when nothing parseable is found."""
    if not text:
        return 0
    m = _SALES_UNIT_W.search(text)
    if m:
        try:
            return int(float(m.group(1)) * 10_000)
        except ValueError:
            pass
    m = re.search(r"\d+", text.replace(",", ""))
    return int(m.group(0)) if m else 0


# ─── price normalisation ──────────────────────────────────────────────

def _price_fen_to_yuan(raw) -> float | None:
    """PDD ssrListData stores price as 分 (integer). Convert; defensively
    handle rare strings like "¥1225" / "1225.00"."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        # Heuristic: >1000 almost always means the value is in 分.
        return round(val / 100, 2) if val >= 1000 else val
    s = str(raw).replace(",", "").replace("¥", "").strip()
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


# ─── rawData item → our standard shape ────────────────────────────────

def _normalize_ssr_item(raw: dict) -> dict | None:
    """Accepts one element of ``ssrListData.list`` and flattens it.

    Historically PDD stamped real hits with ``itemType=1`` and "你可能会
    喜欢" recommendations with ``itemType=3``. As of 2026 PDD labels
    EVERY search result card as ``itemType=3, recTitle="你可能会喜欢"``
    while still showing real search hits in the DOM (it's a ranking
    dark-pattern — a disclaimer they can point to). So we no longer
    filter on those fields; goodsName + price are the real signal.

    Skipped cards:
    - ``itemType == 0`` ad / filler slots with no goodsName
    - empty goodsName (header/banner placeholders)
    """
    if not isinstance(raw, dict):
        return None

    title = raw.get("goodsName") or raw.get("goods_name") or ""
    if not title:
        return None

    goods_id = (
        raw.get("goodsID")
        or raw.get("goods_id")
        or raw.get("goodsId")
        or ""
    )
    # Prefer the fen integer, then the string "priceInfo" (already in 元).
    price = _price_fen_to_yuan(raw.get("price"))
    if price is None:
        price = _price_fen_to_yuan(raw.get("priceInfo"))

    sales = _parse_sales_tip(raw.get("salesTip") or raw.get("sales_tip"))

    mall_entrance = raw.get("mallEntrance") or {}
    mall_id = mall_entrance.get("mall_id") if isinstance(mall_entrance, dict) else None

    link_url = raw.get("linkURL") or raw.get("link_url") or ""
    if link_url and not link_url.startswith("http"):
        link_url = f"https://mobile.yangkeduo.com/{link_url.lstrip('/')}"
    if not link_url and goods_id:
        link_url = f"https://mobile.yangkeduo.com/goods.html?goods_id={goods_id}"

    return {
        "title": str(title)[:200],
        "price": price,
        "sales_count": sales,
        "goods_id": str(goods_id),
        "mall_id": str(mall_id) if mall_id else "",
        "url": link_url,
    }


# ─── inline <script>window.rawData=…</script> fallback parser ─────────

_RAWDATA_RE = re.compile(
    r"window\.rawData\s*=\s*(\{.*?\});\s*window\.",
    re.DOTALL,
)


def _parse_rawdata_from_html(html: str) -> list[dict]:
    """When ``page.evaluate`` fails (rare), fall back to regexing the
    inline <script>window.rawData = {...}</script> out of the raw HTML."""
    m = _RAWDATA_RE.search(html)
    if not m:
        return []
    try:
        payload = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    try:
        return (
            payload.get("stores", {})
            .get("store", {})
            .get("data", {})
            .get("ssrListData", {})
            .get("list", [])
        )
    except Exception:
        return []


# ─── crawler ──────────────────────────────────────────────────────────

class PddCrawler:
    """拼多多 H5 搜索爬虫 — 依赖 pdd_crawler 小号 cookies。"""

    async def search_products(
        self,
        context,
        keyword: str,
        cookies: list[dict] | None = None,
    ) -> tuple[list[dict], list[anti_risk.RiskSignal]]:
        """Return (items, risk_signals)."""
        if cookies:
            try:
                await context.add_cookies(cookies)
                logger.debug(f"Injected {len(cookies)} PDD crawler cookies")
            except Exception as e:
                logger.warning(f"Failed to inject PDD cookies: {e}")
        else:
            logger.warning(
                "PDD crawler called without cookies — search will likely "
                "return 0 items (403 login wall)."
            )

        page = await context.new_page()
        items: list[dict] = []
        risks: list[anti_risk.RiskSignal] = []
        seen_ids: set[str] = set()

        try:
            url = _SEARCH_URL.format(kw=keyword)
            logger.info(f"PDD search loading '{keyword}'")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
            except Exception as e:
                logger.warning(f"PDD goto failed: {e}")
                risks.append(anti_risk.RiskSignal(
                    platform="pdd", signal_type="goto_failed",
                    detail=str(e)[:120], url=url,
                ))
                return items, risks

            # Human interaction simulation (mouse drift + burst scroll).
            # This overlaps with the landing delay / hydration wait so
            # it doesn't add linear latency on top.
            from app.services.human_behavior import humanize_page
            await humanize_page(page, scroll_px=500)
            # The initial SSR `list` array is empty; hydration runs when
            # the user scrolls, and only then does the list get filled in
            # place. We combine (a) a burst of scrolls to kick hydration,
            # (b) a poll loop reading window.rawData until the list is
            # populated — up to ~12s total before giving up.
            await anti_risk.scroll_like_human(page, total_scrolls=4)

            raw_list: list = []
            import asyncio as _asyncio
            _js_read = (
                "() => (window.rawData && window.rawData.stores "
                "&& window.rawData.stores.store && window.rawData.stores.store.data "
                "&& window.rawData.stores.store.data.ssrListData "
                "&& window.rawData.stores.store.data.ssrListData.list) || []"
            )
            for poll in range(12):
                try:
                    raw_list = await page.evaluate(_js_read)
                except Exception as e:
                    logger.warning(f"PDD rawData evaluate failed: {e}")
                    raw_list = []
                if raw_list:
                    logger.debug(f"PDD hydration ready after {poll+1} polls")
                    break
                # Nudge scroll halfway through to re-trigger lazy hydration
                # if the first burst didn't stick.
                if poll in (3, 6):
                    try:
                        await page.evaluate("window.scrollBy(0, 1200)")
                    except Exception:
                        pass
                await _asyncio.sleep(1.0)

            # Fallback: if hydration didn't populate in time, parse the
            # inline rawData script from the raw HTML.
            if not raw_list:
                try:
                    html = await page.content()
                    raw_list = _parse_rawdata_from_html(html)
                    if raw_list:
                        logger.debug(f"PDD rawData fallback yielded {len(raw_list)}")
                except Exception:
                    pass

            for raw in raw_list:
                item = _normalize_ssr_item(raw)
                if not item:
                    continue
                key = item["goods_id"] or item["title"]
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                items.append(item)

            # Risk detection is independent of item parsing — PDD sometimes
            # silently returns 0 items when the account is flagged.
            page_risks = await anti_risk.detect_risk_in_page("pdd", page)
            risks.extend(page_risks)
            if not items and not page_risks:
                # Zero items with no visible risk keywords usually means
                # login-wall redirect or anti-bot empty response. Record it
                # so the orchestrator can escalate via DingTalk.
                risks.append(anti_risk.RiskSignal(
                    platform="pdd", signal_type="empty_result",
                    detail="ssrListData.list empty after hydration", url=url,
                ))
        except Exception as e:
            logger.warning(f"PDD search failed for '{keyword}': {e}")
        finally:
            try:
                await page.close()
            except Exception:
                pass

        logger.info(
            f"PDD got {len(items)} items for '{keyword}' (risks={len(risks)})"
        )
        return items, risks

    async def collect_market_data(
        self,
        context,
        keyword: str,
        cookies: list[dict] | None = None,
    ) -> dict:
        """Return a keyword-level market summary for PDD.

        Always returns a dict (never raises); an empty result produces
        ``active_listings=0`` and downstream code flags the dimension as
        unavailable.
        """
        items, risks = await self.search_products(context, keyword, cookies=cookies)
        prices = sorted([i["price"] for i in items if i.get("price")])
        if prices:
            price_min = prices[0]
            # Robust floor: drop the bottom 5% to avoid "一元购"/bait items.
            floor_idx = max(0, int(len(prices) * 0.05))
            robust_min = prices[floor_idx]
            median = prices[len(prices) // 2]
        else:
            price_min = None
            robust_min = None
            median = None

        return {
            "platform": "pdd",
            "keyword": keyword,
            "active_listings": len(items),
            "price_min": price_min,
            "robust_price_min": robust_min,
            "price_median": median,
            "total_sales": sum(i.get("sales_count", 0) for i in items),
            "items": items[:30],
            "risk_signals": risks,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }


pdd_crawler = PddCrawler()
