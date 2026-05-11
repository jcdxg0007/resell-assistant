"""
Taobao (淘宝) crawler — PC search page.

Reuses taobao.com cookies from a logged-in xianyu account (both live under
taobao's auth system). Callers pass the serialized Playwright state JSON,
we filter it to taobao-scoped cookies and inject into the given context.

Scrapes the mtop search API by listening for its JSON responses rather than
parsing the DOM (the DOM is encrypted/obfuscated anti-scrape).
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import datetime, timezone

from loguru import logger


_SEARCH_URL = "https://s.taobao.com/search?q={kw}&search_type=item"

# mtop endpoints that carry search results. 淘宝 changes version suffixes
# every few months; match on the core path fragment.
_API_PATH_HINTS = (
    "mtop.taobao.pcsearch",
    "mtop.taobao.wsearch",
    "mtop.relationrecommend",
    "mtop.tbsearch",
)

_RISK_URL_HINTS = (
    "login.taobao.com",
    "punish?x5secdata",
    "captcha",
    "/errorcheck",
)

# Keys we've seen in search result payloads.
_ITEM_LIST_KEYS = ("itemsArray", "auctions", "mainAuctions", "items", "list")
_TITLE_KEYS = ("title", "raw_title", "rawTitle", "itemTitle")
_PRICE_KEYS = ("price", "reservePrice", "view_price", "priceMoney")
_SALES_KEYS = ("view_sales", "realSales", "sold", "salesCount")


class TaobaoLoginExpired(Exception):
    """Raised when taobao redirects us to login/captcha — cookies are stale."""


def _coerce_price(raw) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        m = re.search(r"\d+(?:\.\d+)?", raw.replace(",", ""))
        if not m:
            return None
        try:
            return float(m.group(0))
        except ValueError:
            return None
    return None


def _coerce_sales(raw) -> int:
    if raw is None:
        return 0
    s = str(raw)
    m_unit = re.search(r"(\d+(?:\.\d+)?)\s*万", s)
    if m_unit:
        try:
            return int(float(m_unit.group(1)) * 10000)
        except ValueError:
            pass
    m = re.search(r"\d+", s.replace(",", ""))
    return int(m.group(0)) if m else 0


def _extract_items_from_payload(payload) -> list[dict]:
    if isinstance(payload, dict):
        for key in _ITEM_LIST_KEYS:
            if isinstance(payload.get(key), list) and payload[key]:
                cand = payload[key][0]
                if isinstance(cand, dict) and any(k in cand for k in _TITLE_KEYS):
                    return payload[key]
        for v in payload.values():
            found = _extract_items_from_payload(v)
            if found:
                return found
    elif isinstance(payload, list):
        for v in payload:
            found = _extract_items_from_payload(v)
            if found:
                return found
    return []


def _normalize_item(raw: dict) -> dict | None:
    title = next((raw.get(k) for k in _TITLE_KEYS if raw.get(k)), None)
    if not title:
        return None

    price = None
    for k in _PRICE_KEYS:
        if k in raw:
            price = _coerce_price(raw[k])
            if price:
                break

    sales = 0
    for k in _SALES_KEYS:
        if k in raw:
            sales = _coerce_sales(raw[k])
            if sales:
                break

    item_id = (
        raw.get("nid")
        or raw.get("itemId")
        or raw.get("item_id")
        or raw.get("id")
        or ""
    )
    # Strip HTML highlighting that taobao injects into titles.
    clean_title = re.sub(r"<[^>]+>", "", str(title))[:200]
    return {
        "title": clean_title,
        "price": price,
        "sales_count": sales,
        "item_id": str(item_id),
        "url": f"https://item.taobao.com/item.htm?id={item_id}" if item_id else "",
    }


def filter_taobao_cookies(state_json: str | None) -> list[dict]:
    """Parse a Playwright storage_state JSON and return only cookies whose
    domain belongs to the taobao ecosystem (.taobao.com / .tmall.com).
    """
    if not state_json:
        return []
    try:
        state = json.loads(state_json) if isinstance(state_json, str) else state_json
    except json.JSONDecodeError:
        return []
    cookies = state.get("cookies") or []
    wanted = []
    for c in cookies:
        dom = str(c.get("domain", ""))
        if "taobao.com" in dom or "tmall.com" in dom or "alipay.com" in dom:
            # Playwright requires name, value, and either url or domain+path.
            if c.get("name") and "value" in c:
                wanted.append(c)
    return wanted


class TaobaoCrawler:
    """淘宝 PC 搜索爬虫（复用闲鱼 taobao.com cookies）。"""

    async def search_products(
        self,
        context,
        keyword: str,
        cookies: list[dict] | None = None,
    ) -> list[dict]:
        """Returns a list of normalized items. Raises TaobaoLoginExpired on
        a hard login redirect so the orchestrator can surface the issue."""
        if cookies:
            try:
                await context.add_cookies(cookies)
                logger.debug(f"Injected {len(cookies)} taobao cookies")
            except Exception as e:
                logger.warning(f"Failed to inject taobao cookies: {e}")

        page = await context.new_page()
        api_items: list[dict] = []
        seen_ids: set[str] = set()
        risk_detected = False

        async def _capture(response):
            nonlocal risk_detected
            url = response.url
            if any(h in url for h in _RISK_URL_HINTS):
                risk_detected = True
                return
            if not any(p in url for p in _API_PATH_HINTS):
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" not in ct and "javascript" not in ct:
                    return
                body = await response.text()
            except Exception:
                return

            # mtop wraps JSON in `mtopjsonp123(...)` sometimes. Strip wrapper.
            m = re.match(r"^[^({]*?\((\{.*\})\)\s*;?\s*$", body.strip(), re.DOTALL)
            raw = m.group(1) if m else body
            try:
                payload = json.loads(raw)
            except Exception:
                return

            raw_items = _extract_items_from_payload(payload)
            if not raw_items:
                return
            for r in raw_items:
                if not isinstance(r, dict):
                    continue
                item = _normalize_item(r)
                if not item:
                    continue
                key = item["item_id"] or item["title"]
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                api_items.append(item)
            logger.debug(
                f"taobao API intercept: +{len(raw_items)} raw, total {len(api_items)}"
            )

        page.on("response", _capture)
        try:
            url = _SEARCH_URL.format(kw=keyword)
            logger.info(f"Taobao search loading '{keyword}'")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
            except Exception as e:
                logger.warning(f"Taobao page.goto failed: {e}")
            if risk_detected or "login.taobao.com" in page.url:
                raise TaobaoLoginExpired(
                    f"redirected to {page.url} — cookies likely expired or flagged"
                )
            # Trigger lazy load.
            for _ in range(2):
                await asyncio.sleep(random.uniform(1.5, 2.5))
                try:
                    await page.evaluate("window.scrollBy(0, 1500)")
                except Exception:
                    break
            await asyncio.sleep(2)
        finally:
            try:
                await page.close()
            except Exception:
                pass

        logger.info(f"Taobao got {len(api_items)} items for '{keyword}'")
        return api_items

    async def collect_market_data(
        self,
        context,
        keyword: str,
        cookies: list[dict] | None = None,
    ) -> dict:
        """Keyword-level market summary. Same shape family as the other
        platforms. On TaobaoLoginExpired or other errors returns an empty
        result with `active_listings=0` so the orchestrator can tag the
        dimension unavailable without killing the pipeline.

        **Compliance defence-in-depth**: unlike pdd/1688/xianyu which go
        through their factory in ``app.tasks.selection``, taobao is an
        orphan module with no scheduled task wiring it. To make sure any
        future caller (scripts, ad-hoc tools, new tasks) can't bypass
        rule 1, we run the compliance gate inline here. Callers that
        *do* gate externally will pay a single 5-25s jitter wait on the
        second hop — acceptable cost for a safety net.
        """
        # Gate first — if we're outside active hours or the minute
        # window hasn't elapsed, return the standard unavailable shape
        # before touching the browser.
        from app.services.compliance import compliance_gate
        gate = await compliance_gate("taobao", actor="scheduled")
        if not gate:
            logger.warning(
                f"Taobao collect_market_data blocked by compliance gate: "
                f"{gate.reason}"
            )
            return {
                "platform": "taobao",
                "keyword": keyword,
                "active_listings": 0,
                "price_min": None,
                "robust_price_min": None,
                "price_median": None,
                "total_sales": 0,
                "login_expired": False,
                "items": [],
                "__unavailable__": True,
                "error": f"compliance_gate:{gate.reason}",
                "captured_at": datetime.now(timezone.utc).isoformat(),
            }

        items: list[dict] = []
        login_expired = False
        try:
            items = await self.search_products(context, keyword, cookies=cookies)
        except TaobaoLoginExpired as e:
            logger.error(f"Taobao login expired for '{keyword}': {e}")
            login_expired = True
        except Exception as e:
            logger.warning(f"Taobao search error for '{keyword}': {e}")

        prices = sorted([i["price"] for i in items if i.get("price")])
        if prices:
            price_min = prices[0]
            floor_idx = max(0, int(len(prices) * 0.05))
            robust_min = prices[floor_idx]
            median = prices[len(prices) // 2]
        else:
            price_min = None
            robust_min = None
            median = None

        return {
            "platform": "taobao",
            "keyword": keyword,
            "active_listings": len(items),
            "price_min": price_min,
            "robust_price_min": robust_min,
            "price_median": median,
            "total_sales": sum(i.get("sales_count", 0) for i in items),
            "login_expired": login_expired,
            "items": items[:30],
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }


taobao_crawler = TaobaoCrawler()
