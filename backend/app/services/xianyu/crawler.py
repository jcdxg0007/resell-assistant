"""
Xianyu crawler service using Playwright.
Searches products, extracts market data, and collects listing details.
"""
import asyncio
import random
import re
from datetime import datetime, timezone
from typing import Any

from loguru import logger


async def _random_delay(min_s: float = 1.0, max_s: float = 3.0):
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _human_scroll(page, times: int = 3):
    for _ in range(times):
        await page.mouse.wheel(0, random.randint(300, 800))
        await _random_delay(0.5, 1.5)


class XianyuCrawler:
    """Crawls Xianyu search results and product details."""

    SEARCH_URL = "https://www.goofish.com/search?q={keyword}"
    ITEM_URL = "https://www.goofish.com/item?id={item_id}"

    async def search_products(self, context, keyword: str, max_items: int = 30) -> list[dict]:
        """Search Xianyu for a keyword, return list of product summaries."""
        page = await context.new_page()
        results = []
        try:
            url = self.SEARCH_URL.format(keyword=keyword)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await _random_delay(2, 4)
            await _human_scroll(page, times=5)

            items = await page.query_selector_all('[class*="item-card"], [class*="ItemCard"], [class*="feed-item"]')
            logger.info(f"Found {len(items)} raw items for keyword '{keyword}'")

            for item in items[:max_items]:
                try:
                    data = await self._extract_search_item(item)
                    if data:
                        results.append(data)
                except Exception as e:
                    logger.debug(f"Failed to extract item: {e}")
                    continue

            logger.info(f"Extracted {len(results)} valid items for '{keyword}'")
        except Exception as e:
            logger.error(f"Search failed for '{keyword}': {e}")
        finally:
            await page.close()
        return results

    async def _extract_search_item(self, element) -> dict | None:
        """Extract product data from a search result element."""
        title_el = await element.query_selector('[class*="title"], h3, [class*="name"]')
        price_el = await element.query_selector('[class*="price"], [class*="Price"]')
        link_el = await element.query_selector('a[href*="item"]')
        img_el = await element.query_selector('img')
        want_el = await element.query_selector('[class*="want"], [class*="Want"]')

        if not title_el or not price_el:
            return None

        title = (await title_el.inner_text()).strip()
        price_text = (await price_el.inner_text()).strip()
        price = self._parse_price(price_text)
        if price is None:
            return None

        href = await link_el.get_attribute("href") if link_el else ""
        item_id = self._extract_item_id(href)
        img_url = await img_el.get_attribute("src") if img_el else None
        want_text = (await want_el.inner_text()).strip() if want_el else "0"
        want_count = self._parse_number(want_text)

        return {
            "title": title,
            "price": price,
            "item_id": item_id,
            "url": f"https://www.goofish.com/item?id={item_id}" if item_id else href,
            "image_url": img_url,
            "want_count": want_count,
        }

    async def get_item_detail(self, context, item_id: str) -> dict | None:
        """Get detailed information for a single Xianyu listing."""
        page = await context.new_page()
        try:
            url = self.ITEM_URL.format(item_id=item_id)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await _random_delay(2, 4)

            title = await self._safe_text(page, '[class*="title"], h1')
            price = self._parse_price(await self._safe_text(page, '[class*="price"], [class*="Price"]'))
            desc = await self._safe_text(page, '[class*="desc"], [class*="description"]')
            want_text = await self._safe_text(page, '[class*="want"], [class*="Want"]')

            images = []
            img_elements = await page.query_selector_all('[class*="slider"] img, [class*="gallery"] img, [class*="main-pic"] img')
            for img in img_elements[:10]:
                src = await img.get_attribute("src")
                if src:
                    images.append(src)

            seller_el = await page.query_selector('[class*="seller"], [class*="user-info"], [class*="avatar"]')
            seller_name = await self._safe_text(page, '[class*="seller-name"], [class*="nickname"]')

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
        items = await self.search_products(context, keyword, max_items=50)
        if not items:
            return {"keyword": keyword, "active_listings": 0}

        prices = [i["price"] for i in items if i.get("price")]
        wants = [i.get("want_count", 0) for i in items]

        price_avg = sum(prices) / len(prices) if prices else 0
        price_std = (sum((p - price_avg) ** 2 for p in prices) / len(prices)) ** 0.5 if len(prices) > 1 else 0
        price_cv = (price_std / price_avg * 100) if price_avg > 0 else 0

        sorted_by_wants = sorted(items, key=lambda x: x.get("want_count", 0), reverse=True)
        top5 = sorted_by_wants[:5]

        sellers = {}
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
            "top5_sales": [{"title": i["title"], "price": i["price"], "wants": i.get("want_count", 0)} for i in top5],
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
        match = re.search(r'[\d,]+\.?\d*', text.replace(',', ''))
        return float(match.group()) if match else None

    @staticmethod
    def _extract_item_id(href: str) -> str | None:
        if not href:
            return None
        match = re.search(r'id=(\d+)', href)
        if match:
            return match.group(1)
        match = re.search(r'/item/(\d+)', href)
        return match.group(1) if match else None

    @staticmethod
    def _parse_number(text: str) -> int:
        if not text:
            return 0
        match = re.search(r'(\d+)', text.replace(',', ''))
        return int(match.group(1)) if match else 0


xianyu_crawler = XianyuCrawler()
