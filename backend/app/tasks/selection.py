"""
Selection engine Celery tasks.
Handles periodic product discovery, price monitoring, and data collection.
"""
import asyncio
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select

from app.core.celery_app import celery_app
from app.core.database import AsyncSessionLocal
from app.models.product import Product, PriceSnapshot, Platform
from app.models.xianyu import XianyuMarketData
from app.services.xianyu.crawler import xianyu_crawler
from app.services.browser import browser_manager


def run_async(coro):
    """Helper to run async code from sync Celery tasks."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _get_crawler_context(account_id: str | None = None, platform: str = "xianyu"):
    """Get a Playwright context with login session for crawling.

    Tries to find an active account for the platform; falls back to anonymous context.
    """
    if not browser_manager._browser:
        await browser_manager.start()

    if account_id:
        config = {"proxy_url": None}
        return await browser_manager.get_context(account_id, config)

    # Find the best logged-in account for this platform
    try:
        from app.models.system import Account
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Account)
                .where(Account.platform == platform)
                .where(Account.is_active == True)
                .where(Account.session_status == "active")
                .order_by(Account.health_score.desc())
                .limit(1)
            )
            account = result.scalar_one_or_none()
            if account:
                config = {
                    "proxy_url": account.proxy_url,
                    "user_agent": account.user_agent,
                    "viewport": account.viewport,
                }
                logger.info(f"Using logged-in account '{account.account_name}' for {platform} crawling")
                return await browser_manager.get_context(str(account.id), config)
            else:
                logger.warning(f"No active logged-in {platform} account found, using anonymous context")
    except Exception as e:
        logger.error(f"Failed to find crawler account: {e}")

    config = {"proxy_url": None}
    return await browser_manager.get_context("crawler_default", config)


@celery_app.task(name="app.tasks.selection.xianyu_price_monitor")
def xianyu_price_monitor():
    """Monitor prices for products with active Xianyu listings."""
    logger.info("Starting Xianyu price monitor task")
    run_async(_xianyu_price_monitor())


async def _xianyu_price_monitor():
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Product)
            .where(Product.is_active == True)
            .where(Product.source_platform.in_([Platform.PINDUODUO, Platform.TAOBAO]))
            .order_by(Product.last_crawled_at.asc().nulls_first())
            .limit(50)
        )
        products = result.scalars().all()
        logger.info(f"Price monitor: {len(products)} products to check")

        for product in products:
            try:
                context = await _get_crawler_context(platform="xianyu")
                keyword = product.title[:20]
                market_data = await xianyu_crawler.collect_market_data(context, keyword)

                if market_data.get("active_listings", 0) > 0:
                    md = XianyuMarketData(
                        product_id=product.id,
                        keyword=keyword,
                        active_listings=market_data["active_listings"],
                        total_wants=market_data.get("total_wants", 0),
                        price_min=market_data.get("price_min"),
                        price_max=market_data.get("price_max"),
                        price_avg=market_data.get("price_avg"),
                        price_cv=market_data.get("price_cv"),
                        top5_sales=market_data.get("top5_sales"),
                        seller_distribution=market_data.get("seller_distribution"),
                        captured_at=datetime.now(timezone.utc),
                    )
                    db.add(md)

                product.last_crawled_at = datetime.now(timezone.utc)
                await db.commit()
                logger.info(f"Market data collected for: {product.title[:30]}")

            except Exception as e:
                logger.error(f"Price monitor failed for {product.id}: {e}")
                continue


@celery_app.task(name="app.tasks.selection.xianyu_product_discovery")
def xianyu_product_discovery():
    """Discover new products by searching preset category keywords."""
    logger.info("Starting Xianyu product discovery task")
    run_async(_xianyu_product_discovery())


async def _xianyu_product_discovery():
    discovery_keywords = [
        "相机兔笼", "GoPro配件", "显示器支架", "桌面收纳",
        "补光灯", "跟焦器", "筋膜枪", "迷你投影仪",
        "机械键盘配件", "氛围灯", "理线器", "便携咖啡机",
    ]

    async with AsyncSessionLocal() as db:
        context = await _get_crawler_context(platform="xianyu")
        for keyword in discovery_keywords:
            try:
                items = await xianyu_crawler.search_products(context, keyword, max_items=20)
                new_count = 0
                for item in items:
                    if not item.get("item_id"):
                        continue
                    existing = await db.execute(
                        select(Product).where(
                            Product.source_platform == Platform.XIANYU,
                            Product.source_id == item["item_id"],
                        )
                    )
                    if existing.scalar_one_or_none():
                        continue

                    product = Product(
                        source_platform=Platform.XIANYU,
                        source_url=item.get("url", ""),
                        source_id=item["item_id"],
                        title=item["title"],
                        price=item["price"],
                        image_urls=[item["image_url"]] if item.get("image_url") else None,
                        category=keyword,
                        last_crawled_at=datetime.now(timezone.utc),
                    )
                    db.add(product)
                    new_count += 1

                await db.commit()
                logger.info(f"Discovery '{keyword}': found {len(items)} items, {new_count} new")

            except Exception as e:
                logger.error(f"Discovery failed for '{keyword}': {e}")
                continue


@celery_app.task(name="app.tasks.selection.xhs_hot_article_scan")
def xhs_hot_article_scan():
    """Scan XHS for hot articles in target categories."""
    logger.info("Starting XHS hot article scan")
    run_async(_xhs_hot_article_scan())


async def _xhs_hot_article_scan():
    from app.models.xiaohongshu import XhsCompetitorNote
    from app.services.xiaohongshu.crawler import xhs_crawler
    from app.services.xiaohongshu.analyzer import analyze_comment_intent

    scan_keywords = [
        "相机配件", "摄影装备", "桌面好物", "数码配件",
        "补光灯推荐", "收纳神器", "投影仪推荐", "键盘推荐",
        "考研资料", "PPT模板", "简历模板",
    ]

    async with AsyncSessionLocal() as db:
        context = await _get_crawler_context("xhs_scanner")
        for keyword in scan_keywords:
            try:
                notes = await xhs_crawler.search_notes(context, keyword, max_notes=50)
                saved = 0
                for note in notes:
                    note_id = note.get("xhs_note_id")
                    if not note_id:
                        continue
                    existing = await db.execute(
                        select(XhsCompetitorNote).where(XhsCompetitorNote.xhs_note_id == note_id)
                    )
                    if existing.scalar_one_or_none():
                        continue

                    comp = XhsCompetitorNote(
                        keyword=keyword,
                        xhs_note_id=note_id,
                        title=note.get("title", ""),
                        likes=note.get("likes", 0),
                        collects=note.get("collects", 0),
                        comments=note.get("comments", 0),
                        has_product_link=note.get("has_product_link", False),
                        captured_at=datetime.now(timezone.utc),
                    )
                    db.add(comp)
                    saved += 1

                await db.commit()
                logger.info(f"XHS scan '{keyword}': {len(notes)} notes, {saved} new")
            except Exception as e:
                logger.error(f"XHS scan failed for '{keyword}': {e}")


@celery_app.task(name="app.tasks.selection.xhs_topic_trending")
def xhs_topic_trending():
    """Update trending topic data from XHS."""
    logger.info("Starting XHS topic trending update")
    run_async(_xhs_topic_trending())


async def _xhs_topic_trending():
    from app.models.xiaohongshu import XhsHotTopic
    from app.services.xiaohongshu.crawler import xhs_crawler

    topics_to_track = [
        ("相机配件", "3C"), ("摄影装备", "3C"), ("桌面收纳", "家居"),
        ("数码好物", "3C"), ("投影仪", "3C"), ("机械键盘", "3C"),
        ("考研", "教育"), ("备考", "教育"), ("PPT模板", "职场"),
        ("手机摄影", "摄影"), ("Vlog", "摄影"),
    ]

    async with AsyncSessionLocal() as db:
        context = await _get_crawler_context("xhs_topic_tracker")
        for topic_name, category in topics_to_track:
            try:
                data = await xhs_crawler.get_topic_data(context, topic_name)
                topic = XhsHotTopic(
                    topic_name=topic_name,
                    category=category,
                    view_count=data.get("view_count", 0),
                    note_count=data.get("note_count", 0),
                    is_trending=data.get("view_count", 0) > 10_000_000,
                    captured_at=datetime.now(timezone.utc),
                )
                db.add(topic)
            except Exception as e:
                logger.error(f"Topic tracking failed for '{topic_name}': {e}")

        await db.commit()
        logger.info(f"Topic trending updated: {len(topics_to_track)} topics")


@celery_app.task(name="app.tasks.selection.source_stock_check")
def source_stock_check():
    """Check stock availability on source platforms."""
    logger.info("Source stock check - not yet implemented")
    # TODO: Implement source platform stock checking


@celery_app.task(name="app.tasks.selection.instant_search")
def instant_search(keyword: str, platform: str = "xianyu"):
    """User-triggered instant search on a platform."""
    logger.info(f"Instant search: '{keyword}' on {platform}")
    return run_async(_instant_search(keyword, platform))


async def _instant_search(keyword: str, platform: str):
    if platform != "xianyu":
        return {"error": f"Platform {platform} not yet supported"}

    context = await _get_crawler_context(platform=platform)
    market_data = await xianyu_crawler.collect_market_data(context, keyword)
    items = market_data.get("items", [])
    if not items:
        logger.warning(f"Instant search '{keyword}': no items found")
        return market_data

    async with AsyncSessionLocal() as db:
        saved_product_ids = []
        for item in items:
            if not item.get("item_id"):
                continue
            existing = await db.execute(
                select(Product).where(
                    Product.source_platform == Platform.XIANYU,
                    Product.source_id == item["item_id"],
                )
            )
            product = existing.scalar_one_or_none()
            if product:
                product.price = item.get("price", product.price)
                product.last_crawled_at = datetime.now(timezone.utc)
            else:
                product = Product(
                    source_platform=Platform.XIANYU,
                    source_url=item.get("url", ""),
                    source_id=item["item_id"],
                    title=item["title"],
                    price=item["price"],
                    image_urls=[item["image_url"]] if item.get("image_url") else None,
                    category=keyword,
                    sales_count=item.get("want_count", 0),
                    last_crawled_at=datetime.now(timezone.utc),
                )
                db.add(product)

            await db.flush()
            saved_product_ids.append(str(product.id))

        if saved_product_ids and market_data.get("active_listings", 0) > 0:
            for pid in saved_product_ids:
                md = XianyuMarketData(
                    product_id=pid,
                    keyword=keyword,
                    active_listings=market_data["active_listings"],
                    total_wants=market_data.get("total_wants", 0),
                    price_min=market_data.get("price_min"),
                    price_max=market_data.get("price_max"),
                    price_avg=market_data.get("price_avg"),
                    price_cv=market_data.get("price_cv"),
                    top5_sales=market_data.get("top5_sales"),
                    seller_distribution=market_data.get("seller_distribution"),
                    captured_at=datetime.now(timezone.utc),
                )
                db.add(md)

        await db.commit()
        logger.info(
            f"Instant search '{keyword}': saved {len(saved_product_ids)} products, "
            f"market_data active_listings={market_data.get('active_listings', 0)}"
        )

        # Auto-score saved products
        from app.services.selection.scoring import ScoringInput, calculate_xianyu_score

        active_listings = market_data.get("active_listings", 0)
        price_cv = market_data.get("price_cv", 0)
        total_wants = market_data.get("total_wants", 0)
        price_avg = market_data.get("price_avg", 0)

        seller_dist = market_data.get("seller_distribution", {})
        top1_ratio = 0.0
        if seller_dist:
            total_sellers = sum(seller_dist.values())
            top1_count = max(seller_dist.values()) if seller_dist else 0
            top1_ratio = (top1_count / total_sellers * 100) if total_sellers > 0 else 0

        for pid in saved_product_ids:
            try:
                scoring_input = ScoringInput(
                    active_listings=active_listings,
                    price_cv=price_cv,
                    total_wants=total_wants,
                    top1_seller_ratio=top1_ratio,
                    unit_price=price_avg or 0,
                )
                score_result = calculate_xianyu_score(scoring_input)
                dim_dict = {
                    d.name: {"score": d.score, "max": d.max_score, "label": d.label}
                    for d in score_result.dimensions
                }
                from app.models.product import ProductScore
                db_score = ProductScore(
                    product_id=pid,
                    score_type="xianyu_10d",
                    total_score=score_result.total_score,
                    dimension_scores=dim_dict,
                    decision=score_result.decision,
                    scored_at=datetime.now(timezone.utc),
                )
                db.add(db_score)
            except Exception as e:
                logger.error(f"Auto-score failed for {pid}: {e}")

        await db.commit()

    market_data["saved_products"] = len(saved_product_ids)
    return market_data
