"""
1688 (Alibaba wholesale) crawler — guest search mode.

Replacement for the taobao crawler in the selection pipeline. 1688's
guest search page exposes wholesale price ranges and monthly deal counts
without login, which is exactly what we need for the 利润率 / 跨平台价差
dimensions: we're trying to estimate "how cheap can I source this?"

Also fits the operational plan: items found here can later be placed for
one-click dropshipping (一键代发) via 1688, which is why we keep the
``item_id`` and ``shop_id`` fields — downstream code can surface them
directly to the operator.

Guest mode caveats:
- Price shown is the public wholesale range; per-seller negotiated price
  requires login. Close enough for scoring.
- Some sellers gate visibility to registered distributors; those items
  just won't appear in the result set.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone

from loguru import logger

from app.services import anti_risk


_SEARCH_URL = "https://s.1688.com/selloffer/offer_search.htm?keywords={kw}"

# 1688 front-end preloads an SSR bootstrap JSON then hydrates. We grab
# the bootstrap first (faster, fewer anti-bot tripwires), fall back to
# XHR calls for pagination.
_XHR_PATH_HINTS = (
    "pcOfferSearch",
    "getOfferList",
    "selloffer",
    "/offer_search",
)

_TITLE_KEYS = ("title", "subject", "subjectTrans", "offerTitle")
_PRICE_KEYS = (
    "price",
    "priceInfo",
    "priceTrend",
    "minPrice",
    "displayPrice",
    "discountPrice",
)
_SALES_KEYS = ("sold", "sale30", "monthSold", "totalSold")
_SHOP_KEYS = ("companyName", "supplierName", "sellerLoginId", "memberId")


def _coerce_price(raw) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, dict):
        for k in ("minPrice", "price", "displayPrice", "realPrice"):
            if k in raw:
                got = _coerce_price(raw[k])
                if got is not None:
                    return got
        return None
    if isinstance(raw, str):
        m = re.search(r"\d+(?:\.\d+)?", raw.replace(",", "").replace("¥", ""))
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


def _walk_for_offers(payload) -> list[dict]:
    """Depth-first: find the list of offer records inside a nested payload.
    An offer is a dict that has a title-ish key AND a price-ish key.
    """
    if isinstance(payload, dict):
        for v in payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                cand = v[0]
                has_title = any(k in cand for k in _TITLE_KEYS)
                has_price = any(k in cand for k in _PRICE_KEYS)
                if has_title and has_price:
                    return v
            found = _walk_for_offers(v)
            if found:
                return found
    elif isinstance(payload, list):
        for v in payload:
            found = _walk_for_offers(v)
            if found:
                return found
    return []


def _normalize_offer(raw: dict) -> dict | None:
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

    shop = ""
    for k in _SHOP_KEYS:
        val = raw.get(k)
        if val:
            shop = str(val)
            break

    offer_id = (
        raw.get("offerId")
        or raw.get("id")
        or raw.get("offer_id")
        or ""
    )
    clean_title = re.sub(r"<[^>]+>", "", str(title))[:200]
    return {
        "title": clean_title,
        "price": price,
        "sales_count": sales,
        "offer_id": str(offer_id),
        "shop_name": shop[:120],
        "url": f"https://detail.1688.com/offer/{offer_id}.html" if offer_id else "",
    }


class Alibaba1688Crawler:
    """1688 游客搜索爬虫，用于跨平台价差 & 利润率计算。"""

    async def search_offers(
        self,
        context,
        keyword: str,
        cookies: list[dict] | None = None,
    ) -> tuple[list[dict], list[anti_risk.RiskSignal]]:
        """Return (offers, risk_signals). Always returns — never raises.

        If ``cookies`` is provided we inject a 1688_crawler 小号 session;
        1688 recently started redirecting guest search to
        login.taobao.com, so cookies are usually required to see any
        results. Without them the orchestrator will still get back an
        empty list + a ``url_redirect`` risk signal, which is enough for
        the neutral-score fallback.
        """
        if cookies:
            try:
                await context.add_cookies(cookies)
                logger.debug(f"Injected {len(cookies)} 1688 crawler cookies")
            except Exception as e:
                logger.warning(f"Failed to inject 1688 cookies: {e}")

        page = await context.new_page()
        offers: list[dict] = []
        risks: list[anti_risk.RiskSignal] = []
        seen_ids: set[str] = set()

        async def _capture(response):
            url = response.url
            if not any(h in url for h in _XHR_PATH_HINTS):
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" not in ct:
                    return
                body = await response.text()
            except Exception:
                return
            try:
                payload = json.loads(body)
            except Exception:
                return
            raw_items = _walk_for_offers(payload)
            for r in raw_items:
                if not isinstance(r, dict):
                    continue
                item = _normalize_offer(r)
                if not item:
                    continue
                k = item["offer_id"] or item["title"]
                if k in seen_ids:
                    continue
                seen_ids.add(k)
                offers.append(item)

        page.on("response", _capture)
        try:
            url = _SEARCH_URL.format(kw=keyword)
            logger.info(f"1688 search loading '{keyword}'")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
            except Exception as e:
                logger.warning(f"1688 goto failed: {e}")

            # After DOM ready, also parse the SSR bootstrap — 1688 embeds the
            # initial offer list in a <script> tag so we can harvest the first
            # page without waiting for XHR hydration.
            try:
                bootstrap = await page.evaluate(
                    "() => { const n = document.querySelector('#__next_data__, script#_bl_pss_'); "
                    "return n ? n.textContent : ''; }"
                )
                if bootstrap:
                    try:
                        bt_payload = json.loads(bootstrap)
                        for r in _walk_for_offers(bt_payload):
                            item = _normalize_offer(r)
                            if not item:
                                continue
                            k = item["offer_id"] or item["title"]
                            if k in seen_ids:
                                continue
                            seen_ids.add(k)
                            offers.append(item)
                    except Exception:
                        pass
            except Exception:
                pass

            # Human-ish scrolling to trigger lazy-loaded XHR results.
            # Human interaction simulation (mouse drift + burst scroll).
            from app.services.human_behavior import humanize_page
            await humanize_page(page, scroll_px=700)

            # DOM-level fallback: modern s.1688.com (2026) serves a full SSR
            # offer list as <a class="search-offer-wrapper"> elements with
            # no exposed XHR. offerId lives in the href + data-renderkey;
            # shop name / sales / price all sit inside the card as plain
            # text we can regex out. We read everything in a single
            # page.evaluate() to avoid per-element round-trips.
            if not offers:
                try:
                    dom_offers = await page.evaluate(r"""
() => {
  const out = [];
  // `data-tracker="offer"` disambiguates real search cards from the
  // hover-peek recommendations that share most class names.
  const cards = document.querySelectorAll(
    'a.search-offer-wrapper[data-tracker="offer"], a.search-offer-item[data-tracker="offer"]'
  );
  for (const a of cards) {
    const href = a.getAttribute('href') || '';
    // offerId lives in `?offerId=<digits>` inside the href.
    const idMatch = href.match(/[?&]offerId=(\d+)/);
    const offerId = idMatch ? idMatch[1] : '';
    if (!offerId) continue;

    // Card innerText format (stable across cards, whitespace-joined):
    //   "<title> ¥ <yuan> [.<jiao>] [红包价] <sales>+件 [退货…] 回头率NN% <店铺名>"
    // We normalise whitespace once and regex each field.
    const text = (a.innerText || '').replace(/\s+/g, ' ').trim();
    if (!text) continue;

    // Price: "¥ 8" or "¥ 2 .5" (元 + 角 split across spans)
    let price = null;
    const pm = text.match(/¥\s*(\d+)\s*(?:\.\s*(\d+))?/);
    if (pm) {
      const yuan = pm[1];
      const jiao = pm[2] || '0';
      price = parseFloat(`${yuan}.${jiao}`);
    }

    // Sales: "3.1万+件" / "1700+件" / "80+件". Prefix "全网" is optional.
    let sales = 0;
    const sm = text.match(/(?:全网)?([\d.]+)(万)?\+?\s*件/);
    if (sm) {
      sales = parseFloat(sm[1]) * (sm[2] ? 10000 : 1);
    }

    // Title = everything before the first "¥". Cheap + reliable.
    let title = text;
    const yuanIdx = text.indexOf('¥');
    if (yuanIdx > 0) title = text.slice(0, yuanIdx).trim();
    title = title.slice(0, 200);

    // Shop name: heuristic — the last chunk of innerText is the shop
    // (e.g. "深圳市维川实业有限公司"). We take the last segment after
    // the final "回头率NN%" marker if present, else the last word-run.
    let shopName = '';
    const shopMatch = text.match(/回头率\d+%\s*(.+)$/);
    if (shopMatch) {
      shopName = shopMatch[1].trim().slice(0, 120);
    }

    // Image: primary offer thumbnail.
    const img = a.querySelector('img');
    const imgSrc = img ? img.getAttribute('src') || img.getAttribute('data-src') || '' : '';

    out.push({
      offer_id: offerId,
      title,
      price,
      sales_count: Math.round(sales) || 0,
      shop_name: shopName,
      img_src: (imgSrc || '').slice(0, 200),
      detail_url: `https://detail.1688.com/offer/${offerId}.html`,
    });
  }
  return out;
}
""")
                    for rec in dom_offers or []:
                        oid = rec.get("offer_id") or ""
                        if not oid or oid in seen_ids:
                            continue
                        seen_ids.add(oid)
                        offers.append({
                            "title": re.sub(r"<[^>]+>", "", rec.get("title") or ""),
                            "price": rec.get("price"),
                            "sales_count": int(rec.get("sales_count") or 0),
                            "offer_id": oid,
                            "shop_name": rec.get("shop_name") or "",
                            "url": rec.get("detail_url") or f"https://detail.1688.com/offer/{oid}.html",
                        })
                    if offers:
                        logger.debug(f"1688 DOM fallback: {len(offers)} offers")
                except Exception as e:
                    logger.warning(f"1688 DOM fallback failed: {e}")

            # Risk detection happens regardless of success — 1688 sometimes
            # silently serves a nearly-empty page when it flags the IP.
            risks = await anti_risk.detect_risk_in_page("1688", page)
            if not offers and not risks:
                risks.append(anti_risk.RiskSignal(
                    platform="1688", signal_type="empty_result",
                    detail="No offer cards parsed from SSR HTML", url=url,
                ))
        finally:
            try:
                await page.close()
            except Exception:
                pass

        logger.info(f"1688 got {len(offers)} offers for '{keyword}'")
        return offers, risks

    async def collect_market_data(
        self,
        context,
        keyword: str,
        cookies: list[dict] | None = None,
    ) -> dict:
        """Standard collect_market_data interface used by the selection
        orchestrator. Plays the role taobao used to play: supplies the
        cheapest-sourcing-price anchor for product scoring.
        """
        offers, risks = await self.search_offers(context, keyword, cookies=cookies)
        prices = sorted([o["price"] for o in offers if o.get("price")])
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
            "platform": "1688",
            "keyword": keyword,
            "active_listings": len(offers),
            "price_min": price_min,
            "robust_price_min": robust_min,
            "price_median": median,
            "total_sales": sum(o.get("sales_count", 0) for o in offers),
            "items": offers[:30],
            "risk_signals": risks,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }


alibaba_1688_crawler = Alibaba1688Crawler()
