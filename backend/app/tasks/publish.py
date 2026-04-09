"""
Publishing Celery tasks.
Handles scheduled publishing, batch refresh, and listing health monitoring.
"""
import asyncio
import random
from datetime import datetime, timezone, timedelta

from loguru import logger
from sqlalchemy import select, and_

from app.core.celery_app import celery_app
from app.core.database import AsyncSessionLocal
from app.models.xianyu import XianyuListing
from app.models.system import Account
from app.services.publish.xianyu_publisher import (
    publish_listing, refresh_listing, is_safe_time,
)
from app.services.notification import notification_service


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.publish.execute_publish")
def execute_publish(listing_id: str):
    """Execute a single listing publish via Playwright."""
    logger.info(f"Publishing listing {listing_id}")
    return run_async(_execute_publish(listing_id))


async def _execute_publish(listing_id: str):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(XianyuListing).where(XianyuListing.id == listing_id)
        )
        listing = result.scalar_one_or_none()
        if not listing or listing.status != "pending_review":
            return {"error": "listing not in pending_review status"}

        # Get account
        acct_result = await db.execute(
            select(Account).where(Account.id == listing.account_id)
        )
        account = acct_result.scalar_one_or_none()
        if not account or not account.is_active:
            listing.status = "error"
            listing.error_message = "发布账号不可用"
            await db.commit()
            return {"error": "account unavailable"}

        # Check daily limit
        if account.daily_published_count >= account.daily_publish_limit:
            logger.warning(f"Account {account.account_name} daily limit reached")
            listing.status = "draft"
            await db.commit()
            return {"error": "daily limit reached"}

        # Check safe time window
        if not is_safe_time():
            logger.info("Not in safe publish window, deferring")
            listing.status = "draft"
            await db.commit()
            return {"error": "outside safe time window"}

        config = {
            "proxy_url": account.proxy_url,
            "user_agent": account.user_agent,
            "viewport": account.viewport,
        }

        pub_result = await publish_listing(
            account_id=str(account.id),
            account_config=config,
            listing_data={
                "title": listing.title,
                "description": listing.description,
                "price": listing.price,
                "image_paths": listing.image_paths or [],
            },
        )

        if pub_result["success"]:
            listing.status = "published"
            listing.xianyu_item_id = pub_result.get("xianyu_item_id")
            listing.published_at = datetime.now(timezone.utc)
            account.daily_published_count += 1
            account.last_active_at = datetime.now(timezone.utc)
            await db.commit()
            logger.info(f"Published: {listing.title[:30]}")
        else:
            listing.status = "error"
            listing.error_message = pub_result.get("error", "未知错误")
            await db.commit()
            await notification_service.notify(
                "发布失败",
                f"商品: {listing.title[:30]}\n账号: {account.account_name}\n错误: {pub_result.get('error')}",
                level="warning",
            )

        return pub_result


@celery_app.task(name="app.tasks.publish.batch_refresh_listings")
def batch_refresh_listings():
    """Batch refresh (擦亮) published listings for exposure boost."""
    logger.info("Starting batch refresh")
    run_async(_batch_refresh())


async def _batch_refresh():
    if not is_safe_time():
        logger.info("Not in safe time window for refresh")
        return

    async with AsyncSessionLocal() as db:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
        result = await db.execute(
            select(XianyuListing).where(
                XianyuListing.status == "published",
                XianyuListing.xianyu_item_id.isnot(None),
                (XianyuListing.last_refreshed_at < cutoff) | (XianyuListing.last_refreshed_at.is_(None)),
            ).limit(15)
        )
        listings = result.scalars().all()

        for listing in listings:
            acct_result = await db.execute(select(Account).where(Account.id == listing.account_id))
            account = acct_result.scalar_one_or_none()
            if not account or not account.is_active:
                continue

            config = {"proxy_url": account.proxy_url, "user_agent": account.user_agent, "viewport": account.viewport}

            ref_result = await refresh_listing(
                account_id=str(account.id),
                account_config=config,
                xianyu_item_id=listing.xianyu_item_id,
            )

            if ref_result["success"]:
                listing.last_refreshed_at = datetime.now(timezone.utc)
                await db.commit()

            await asyncio.sleep(random.uniform(30, 90))


@celery_app.task(name="app.tasks.publish.reset_daily_counts")
def reset_daily_counts():
    """Reset daily publish counts for all accounts (runs at midnight)."""
    run_async(_reset_daily_counts())


async def _reset_daily_counts():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Account))
        accounts = result.scalars().all()
        for account in accounts:
            account.daily_published_count = 0
        await db.commit()
        logger.info(f"Reset daily counts for {len(accounts)} accounts")


@celery_app.task(name="app.tasks.publish.listing_health_check")
def listing_health_check():
    """Check listing health: views, wants, exposure trends."""
    logger.info("Listing health check - to be implemented with actual metrics")
    # TODO: Crawl each listing's stats and detect anomalies
