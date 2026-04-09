"""
Xiaohongshu (XHS) crawler service.
Playwright-based scraping of notes, topics, trending keywords, and comments.
"""
import asyncio
import random
import re
from datetime import datetime, timezone

from loguru import logger


async def _human_delay(min_s: float = 0.8, max_s: float = 2.5):
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _scroll_page(page, times: int = 3):
    for _ in range(times):
        await page.mouse.wheel(0, random.randint(300, 800))
        await _human_delay(0.5, 1.5)


def _parse_count(text: str) -> int:
    """Parse XHS count strings like '3.2万' into integers."""
    text = text.strip().replace(',', '')
    if '万' in text:
        return int(float(text.replace('万', '')) * 10000)
    if '亿' in text:
        return int(float(text.replace('亿', '')) * 100000000)
    try:
        return int(text)
    except (ValueError, TypeError):
        return 0


class XhsCrawler:

    async def search_notes(
        self,
        context,
        keyword: str,
        max_notes: int = 50,
        sort: str = "hot",
    ) -> list[dict]:
        """
        Search XHS for notes matching a keyword.

        Returns list of note data:
            xhs_note_id, title, author_id, author_name, cover_url,
            likes, collects, comments, has_product_link, published_at
        """
        page = await context.new_page()
        notes = []

        try:
            url = f"https://www.xiaohongshu.com/search_result?keyword={keyword}&type=1"
            if sort == "latest":
                url += "&sort=time"
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await _human_delay(2, 4)

            collected = 0
            scroll_attempts = 0
            max_scrolls = 15

            while collected < max_notes and scroll_attempts < max_scrolls:
                cards = await page.query_selector_all('[class*="note-item"], [class*="search-note"], .feeds-page .note-item')

                for card in cards[collected:]:
                    try:
                        note = await self._parse_note_card(card)
                        if note and note.get("xhs_note_id") not in {n.get("xhs_note_id") for n in notes}:
                            notes.append(note)
                            collected += 1
                            if collected >= max_notes:
                                break
                    except Exception as e:
                        logger.debug(f"Note card parse error: {e}")

                await _scroll_page(page, 2)
                scroll_attempts += 1

            logger.info(f"XHS search '{keyword}': collected {len(notes)} notes")
            return notes

        except Exception as e:
            logger.error(f"XHS search failed for '{keyword}': {e}")
            return notes
        finally:
            await page.close()

    async def _parse_note_card(self, card) -> dict | None:
        """Parse a single note card element."""
        link_el = await card.query_selector('a[href*="/explore/"], a[href*="/discovery/item/"]')
        href = await link_el.get_attribute("href") if link_el else ""
        note_id_match = re.search(r'/(?:explore|discovery/item)/([a-f0-9]+)', href or "")
        note_id = note_id_match.group(1) if note_id_match else None
        if not note_id:
            return None

        title_el = await card.query_selector('[class*="title"], .desc, .note-text')
        title = (await title_el.inner_text()).strip() if title_el else ""

        author_el = await card.query_selector('[class*="author"], .author-wrapper .name')
        author_name = (await author_el.inner_text()).strip() if author_el else ""

        cover_el = await card.query_selector('img')
        cover_url = await cover_el.get_attribute("src") if cover_el else ""

        likes_el = await card.query_selector('[class*="like"] span, [class*="like-count"]')
        likes = _parse_count(await likes_el.inner_text()) if likes_el else 0

        product_link = await card.query_selector('[class*="product"], [class*="goods"], [class*="shop"]')
        has_product = product_link is not None

        return {
            "xhs_note_id": note_id,
            "title": title,
            "author_name": author_name,
            "cover_url": cover_url,
            "likes": likes,
            "collects": 0,
            "comments": 0,
            "has_product_link": has_product,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }

    async def get_note_detail(self, context, note_id: str) -> dict:
        """Get full details of a single XHS note including engagement metrics."""
        page = await context.new_page()

        try:
            await page.goto(
                f"https://www.xiaohongshu.com/explore/{note_id}",
                wait_until="networkidle",
                timeout=30000,
            )
            await _human_delay(2, 4)

            title_el = await page.query_selector('[class*="title"], .note-text .title, h1')
            title = (await title_el.inner_text()).strip() if title_el else ""

            body_el = await page.query_selector('[class*="content"], .note-text .desc, .note-content')
            body = (await body_el.inner_text()).strip() if body_el else ""

            # Engagement metrics
            likes_el = await page.query_selector('[class*="like"] span, [class*="like-count"]')
            likes = _parse_count(await likes_el.inner_text()) if likes_el else 0

            collect_el = await page.query_selector('[class*="collect"] span, [class*="collect-count"]')
            collects = _parse_count(await collect_el.inner_text()) if collect_el else 0

            comment_el = await page.query_selector('[class*="chat"] span, [class*="comment-count"]')
            comments = _parse_count(await comment_el.inner_text()) if comment_el else 0

            # Tags
            tag_els = await page.query_selector_all('[class*="tag"] a, .tag-item')
            tags = []
            for t in tag_els:
                tag_text = (await t.inner_text()).strip().lstrip('#')
                if tag_text:
                    tags.append(tag_text)

            # Detect product link
            product_el = await page.query_selector('[class*="goods-card"], [class*="product-card"]')
            has_product = product_el is not None

            # Detect content type
            video_el = await page.query_selector('video, [class*="video-player"]')
            content_type = "video" if video_el else "image"

            # Author info
            author_el = await page.query_selector('[class*="author-name"], .author-wrapper .name')
            author_name = (await author_el.inner_text()).strip() if author_el else ""

            return {
                "xhs_note_id": note_id,
                "title": title,
                "body": body,
                "author_name": author_name,
                "likes": likes,
                "collects": collects,
                "comments": comments,
                "tags": tags,
                "has_product_link": has_product,
                "content_type": content_type,
                "interaction_total": likes + collects + comments,
            }

        except Exception as e:
            logger.error(f"Note detail failed for {note_id}: {e}")
            return {"xhs_note_id": note_id, "error": str(e)}
        finally:
            await page.close()

    async def get_note_comments(
        self, context, note_id: str, max_comments: int = 50
    ) -> list[dict]:
        """Extract comments from a note for intent analysis."""
        page = await context.new_page()
        comments = []

        try:
            await page.goto(
                f"https://www.xiaohongshu.com/explore/{note_id}",
                wait_until="networkidle",
                timeout=30000,
            )
            await _human_delay(2, 4)

            # Scroll to load comments
            await _scroll_page(page, 5)

            comment_els = await page.query_selector_all(
                '[class*="comment-item"], [class*="comment-inner"], .comment-text'
            )

            for cel in comment_els[:max_comments]:
                try:
                    text_el = await cel.query_selector('[class*="content"], .text, p')
                    text = (await text_el.inner_text()).strip() if text_el else ""
                    if text:
                        comments.append({"text": text})
                except Exception:
                    pass

            return comments

        except Exception as e:
            logger.debug(f"Comment extraction failed for {note_id}: {e}")
            return comments
        finally:
            await page.close()

    async def get_topic_data(self, context, topic_name: str) -> dict:
        """Get topic page data (view count, note count, growth)."""
        page = await context.new_page()

        try:
            await page.goto(
                f"https://www.xiaohongshu.com/search_result?keyword=%23{topic_name}",
                wait_until="networkidle",
                timeout=30000,
            )
            await _human_delay(2, 4)

            view_el = await page.query_selector('[class*="view-count"], [class*="topic-view"]')
            view_count = _parse_count(await view_el.inner_text()) if view_el else 0

            note_el = await page.query_selector('[class*="note-count"], [class*="topic-note"]')
            note_count = _parse_count(await note_el.inner_text()) if note_el else 0

            return {
                "topic_name": topic_name,
                "view_count": view_count,
                "note_count": note_count,
                "captured_at": datetime.now(timezone.utc).isoformat(),
            }

        except Exception as e:
            logger.debug(f"Topic data failed for '{topic_name}': {e}")
            return {"topic_name": topic_name, "view_count": 0, "note_count": 0}
        finally:
            await page.close()

    async def collect_category_data(
        self, context, keyword: str, max_notes: int = 100
    ) -> dict:
        """
        Collect comprehensive category data for XHS selection analysis.

        Returns aggregated data:
            notes, avg_likes, avg_collects, avg_comments,
            interaction_rate_avg, product_note_ratio,
            top10_avg_likes, purchase_intent_ratio
        """
        notes = await self.search_notes(context, keyword, max_notes=max_notes)

        if not notes:
            return {"keyword": keyword, "total_notes": 0}

        # Get detailed data for top notes (by likes)
        sorted_notes = sorted(notes, key=lambda n: n.get("likes", 0), reverse=True)
        detailed = []
        for note in sorted_notes[:20]:
            detail = await self.get_note_detail(context, note["xhs_note_id"])
            if "error" not in detail:
                detailed.append(detail)
            await _human_delay(1, 3)

        # Compute aggregates
        all_likes = [n.get("likes", 0) for n in notes]
        all_collects = [d.get("collects", 0) for d in detailed]
        all_comments = [d.get("comments", 0) for d in detailed]

        avg_likes = sum(all_likes) / len(all_likes) if all_likes else 0
        avg_collects = sum(all_collects) / len(all_collects) if all_collects else 0
        avg_comments = sum(all_comments) / len(all_comments) if all_comments else 0

        top10 = sorted_notes[:10]
        top10_avg_likes = sum(n.get("likes", 0) for n in top10) / len(top10) if top10 else 0

        product_notes = sum(1 for n in notes if n.get("has_product_link"))
        product_ratio = product_notes / len(notes) * 100 if notes else 0

        # Comment intent analysis for top 5 notes
        purchase_intent_comments = 0
        total_comments_analyzed = 0
        intent_keywords = ["怎么买", "链接", "多少钱", "已入手", "求推荐", "哪里买",
                           "求分享", "已收藏", "已下单", "想买", "有链接", "在哪买"]

        for note in sorted_notes[:5]:
            comments = await self.get_note_comments(context, note["xhs_note_id"], max_comments=30)
            total_comments_analyzed += len(comments)
            for c in comments:
                if any(kw in c.get("text", "") for kw in intent_keywords):
                    purchase_intent_comments += 1
            await _human_delay(1, 2)

        intent_ratio = (purchase_intent_comments / total_comments_analyzed * 100) if total_comments_analyzed > 0 else 0

        return {
            "keyword": keyword,
            "total_notes": len(notes),
            "avg_likes": round(avg_likes, 1),
            "avg_collects": round(avg_collects, 1),
            "avg_comments": round(avg_comments, 1),
            "top10_avg_likes": round(top10_avg_likes, 1),
            "product_note_ratio": round(product_ratio, 1),
            "purchase_intent_ratio": round(intent_ratio, 1),
            "total_comments_analyzed": total_comments_analyzed,
            "purchase_intent_comments": purchase_intent_comments,
            "detailed_notes": detailed[:10],
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }


xhs_crawler = XhsCrawler()
