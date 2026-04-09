"""AI Operations Celery tasks."""
import asyncio
from loguru import logger
from app.core.celery_app import celery_app
from app.services.ai_ops.daily_check import run_daily_self_check, run_daily_report


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.ai_ops.daily_self_check")
def daily_self_check():
    """Daily 06:00 self-check: account health, products, crawl integrity."""
    logger.info("Starting daily AI self-check")
    return run_async(run_daily_self_check())


@celery_app.task(name="app.tasks.ai_ops.daily_report")
def daily_report():
    """Daily 22:00 operations report with AI suggestions."""
    logger.info("Starting daily AI operations report")
    return run_async(run_daily_report())
