"""Session health check Celery task."""
import asyncio
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select

from app.core.celery_app import celery_app
from app.core.database import AsyncSessionLocal
from app.models.system import Account


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _check_all_sessions():
    from app.services.session_checker import check_all_sessions
    from app.services.notification import notification_service

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Account).where(Account.is_active == True)
        )
        accounts = result.scalars().all()

    if not accounts:
        logger.info("No active accounts to check sessions for")
        return {"checked": 0}

    account_dicts = [
        {
            "id": str(a.id),
            "platform": a.platform,
            "account_name": a.account_name,
            "proxy_url": a.proxy_url,
            "user_agent": a.user_agent,
            "viewport": a.viewport,
        }
        for a in accounts
    ]

    summary = await check_all_sessions(account_dicts)
    now = datetime.now(timezone.utc)

    # Update DB with results
    expired_names = []
    async with AsyncSessionLocal() as db:
        for detail in summary["details"]:
            account_id = detail["account_id"]
            status = detail["status"]
            hint = detail.get("hint")

            result = await db.execute(
                select(Account).where(Account.id == account_id)
            )
            account = result.scalar_one_or_none()
            if not account:
                continue

            account.session_checked_at = now

            if status == "active":
                account.session_status = "active"
                account.session_expires_hint = None
                account.last_active_at = now
            elif status == "expired":
                account.session_status = "expired"
                account.session_expires_hint = hint
                account.health_score = max(0, account.health_score - 10)
                expired_names.append(f"{account.account_name} ({account.platform})")
            # "none" — leave session_status as-is

        await db.commit()

    # Send notification for expired sessions
    if expired_names:
        names_str = "\n".join(f"- {n}" for n in expired_names)
        await notification_service.notify(
            "会话过期提醒",
            f"以下 {len(expired_names)} 个账号会话已过期，请尽快重新登录：\n\n{names_str}",
            level="warning",
        )

    return {
        "checked": summary["checked"],
        "active": summary["active"],
        "expired": summary["expired"],
        "skipped": summary["skipped"],
    }


@celery_app.task(name="app.tasks.session.check_all_sessions")
def check_all_sessions_task():
    """Periodic task: check all account sessions are still valid."""
    logger.info("Starting session health check for all accounts")
    return run_async(_check_all_sessions())
