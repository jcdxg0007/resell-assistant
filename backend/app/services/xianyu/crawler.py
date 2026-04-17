"""
Xianyu crawler service using Playwright.
Searches products via homepage search bar (bypasses anti-bot on direct URL access),
extracts market data, and collects listing details.
"""
import asyncio
import random
import re
from datetime import datetime, timezone

from loguru import logger


async def _random_delay(min_s: float = 1.0, max_s: float = 3.0):
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _human_scroll(page, times: int = 3):
    for _ in range(times):
        await page.mouse.wheel(0, random.randint(300, 800))
        await _random_delay(0.5, 1.5)


async def _dismiss_goofish_modal(page):
    """Dismiss the login/announcement modal that goofish.com shows to visitors."""
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
        await page.evaluate(
            '() => document.querySelectorAll(".ant-modal-wrap, .ant-modal-mask")'
            ".forEach(el => el.remove())"
        )
    except Exception:
        pass


def _extract_card_item(card: dict) -> dict | None:
    """Extract product data from goofish's nested card structure.

    Card path: data.item.main.exContent / data.item.main.clickParam.args
    """
    try:
        item_node = card.get("data", {}).get("item", {}).get("main", {})
        ex = item_node.get("exContent", {})
        args = item_node.get("clickParam", {}).get("args", {})

        item_id = str(ex.get("itemId") or args.get("item_id") or args.get("id") or "")
        title = ex.get("title") or args.get("title", "")
        price_str = (
            args.get("price")
            or args.get("displayPrice")
            or ex.get("detailParams", {}).get("soldPrice", "")
        )
        pic_url = ex.get("picUrl", "")
        want = int(args.get("wantNum", 0) or 0)
        seller = ex.get("userNickName", "")

        if not item_id or not title:
            return None

        price = None
        if price_str:
            try:
                price = float(str(price_str).replace(",", ""))
            except ValueError:
                pass

        return {
            "title": title,
            "price": price,
            "item_id": item_id,
            "url": f"https://www.goofish.com/item?id={item_id}",
            "image_url": pic_url,
            "want_count": want,
            "seller_name": seller,
        }
    except Exception:
        return None


class XianyuCrawler:
    """Crawls Xianyu search results and product details."""

    SEARCH_URL = "https://www.goofish.com/search?q={keyword}"
    ITEM_URL = "https://www.goofish.com/item?id={item_id}"

    async def search_products(self, context, keyword: str, max_items: int = 100) -> list[dict]:
        """Search Xianyu by typing in homepage search bar.

        Navigating directly to search URL triggers anti-bot (RGV587_ERROR).
        Going through the homepage lets the security SDK initialise first.
        Scrolls repeatedly to trigger lazy-load API pages until max_items reached.
        """
        page = await context.new_page()
        api_items: list[dict] = []
        seen_ids: set[str] = set()
        api_page_count = 0

        async def _capture_search_api(response):
            nonlocal api_page_count
            url = response.url
            if "mtop." not in url or "search" not in url.lower():
                return
            try:
                data = await response.json()
                ret = (data.get("ret") or [""])[0]
                if "SUCCESS" not in ret:
                    if "ERROR" in ret or "FAIL" in ret:
                        logger.debug(f"API non-success: {ret[:60]}")
                    return
                rd = data.get("data", {})
                cards = rd.get("resultList", rd.get("cardList", []))
                if len(cards) <= 1:
                    return
                api_page_count += 1
                new_count = 0
                for card in cards:
                    item = _extract_card_item(card)
                    if item and item["item_id"] not in seen_ids:
                        seen_ids.add(item["item_id"])
                        api_items.append(item)
                        new_count += 1
                logger.info(
                    f"API page {api_page_count}: {len(cards)} cards, "
                    f"{new_count} new → total {len(api_items)} for '{keyword}'"
                )
            except Exception as e:
                logger.debug(f"API intercept error: {e}")

        page.on("response", _capture_search_api)
        try:
            logger.info(f"Loading goofish homepage before search '{keyword}' (max={max_items})")
            await page.goto(
                "https://www.goofish.com/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(4)
            await _dismiss_goofish_modal(page)
            await asyncio.sleep(1)

            search_input = page.locator(
                'input[class*="search"], input[placeholder*="搜索"]'
            ).first
            await search_input.click()
            await asyncio.sleep(0.5)
            await search_input.fill(keyword)
            await asyncio.sleep(0.3)
            await page.keyboard.press("Enter")
            logger.info(f"Search triggered for '{keyword}', waiting for first page...")

            await asyncio.sleep(8)

            max_scroll_rounds = 10
            stall_rounds = 0
            for scroll_round in range(max_scroll_rounds):
                if len(api_items) >= max_items:
                    logger.info(f"Reached {len(api_items)} items, stopping scroll")
                    break

                prev_count = len(api_items)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(random.uniform(1.5, 2.5))
                await _human_scroll(page, times=2)
                await asyncio.sleep(random.uniform(2.0, 4.0))

                if len(api_items) == prev_count:
                    stall_rounds += 1
                    if stall_rounds >= 2:
                        logger.info(
                            f"No new items after {stall_rounds} stalled rounds, "
                            f"stopping at {len(api_items)} items"
                        )
                        break
                else:
                    stall_rounds = 0

            if api_items:
                logger.info(
                    f"Got {len(api_items)} items across {api_page_count} API pages for '{keyword}'"
                )
                return api_items[:max_items]

            logger.warning(
                f"No API items for '{keyword}', page URL: {page.url[:80]}"
            )
        except Exception as e:
            logger.error(f"Search failed for '{keyword}': {e}")
        finally:
            page.remove_listener("response", _capture_search_api)
            await page.close()
        return api_items[:max_items]

    async def get_item_detail(self, context, item_id: str) -> dict | None:
        """Get detailed information for a single Xianyu listing."""
        page = await context.new_page()
        try:
            url = self.ITEM_URL.format(item_id=item_id)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await _random_delay(2, 4)

            title = await self._safe_text(page, '[class*="title"], h1')
            price = self._parse_price(
                await self._safe_text(page, '[class*="price"], [class*="Price"]')
            )
            desc = await self._safe_text(
                page, '[class*="desc"], [class*="description"]'
            )
            want_text = await self._safe_text(
                page, '[class*="want"], [class*="Want"]'
            )

            images = []
            img_elements = await page.query_selector_all(
                '[class*="slider"] img, [class*="gallery"] img, [class*="main-pic"] img'
            )
            for img in img_elements[:10]:
                src = await img.get_attribute("src")
                if src:
                    images.append(src)

            seller_name = await self._safe_text(
                page, '[class*="seller-name"], [class*="nickname"]'
            )

            return {
                "item_id": item_id,
                "title": title,
                "price": price,
                "description": desc,
                "want_count": self._parse_number(want_text),
                "image_urls": images,
                "seller_name": seller_name,
                "crawled_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            logger.error(f"Failed to get item detail {item_id}: {e}")
            return None
        finally:
            await page.close()

    async def collect_market_data(self, context, keyword: str) -> dict:
        """Collect aggregate market data for a keyword on Xianyu."""
        items = await self.search_products(context, keyword, max_items=100)
        if not items:
            return {"keyword": keyword, "active_listings": 0, "items": []}

        prices = [i["price"] for i in items if i.get("price")]
        wants = [i.get("want_count", 0) for i in items]

        price_avg = sum(prices) / len(prices) if prices else 0
        price_std = (
            (sum((p - price_avg) ** 2 for p in prices) / len(prices)) ** 0.5
            if len(prices) > 1
            else 0
        )
        price_cv = (price_std / price_avg * 100) if price_avg > 0 else 0

        sorted_by_wants = sorted(
            items, key=lambda x: x.get("want_count", 0), reverse=True
        )
        top5 = sorted_by_wants[:5]

        sellers: dict[str, int] = {}
        for item in items:
            seller = item.get("seller_name", "unknown")
            sellers[seller] = sellers.get(seller, 0) + 1

        return {
            "keyword": keyword,
            "active_listings": len(items),
            "total_wants": sum(wants),
            "price_min": min(prices) if prices else None,
            "price_max": max(prices) if prices else None,
            "price_avg": round(price_avg, 2),
            "price_cv": round(price_cv, 2),
            "top5_sales": [
                {
                    "title": i["title"],
                    "price": i["price"],
                    "wants": i.get("want_count", 0),
                }
                for i in top5
            ],
            "seller_distribution": sellers,
            "items": items,
        }

    @staticmethod
    async def _safe_text(page, selector: str) -> str:
        el = await page.query_selector(selector)
        if el:
            return (await el.inner_text()).strip()
        return ""

    @staticmethod
    def _parse_price(text: str) -> float | None:
        if not text:
            return None
        match = re.search(r"[\d,]+\.?\d*", text.replace(",", ""))
        return float(match.group()) if match else None

    @staticmethod
    def _extract_item_id(href: str) -> str | None:
        if not href:
            return None
        match = re.search(r"id=(\d+)", href)
        if match:
            return match.group(1)
        match = re.search(r"/item/(\d+)", href)
        return match.group(1) if match else None

    @staticmethod
    def _parse_number(text: str) -> int:
        if not text:
            return 0
        match = re.search(r"(\d+)", text.replace(",", ""))
        return int(match.group(1)) if match else 0


xianyu_crawler = XianyuCrawler()
