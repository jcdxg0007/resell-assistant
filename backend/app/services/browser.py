"""
Playwright browser management service.
Handles multi-account browser context isolation with anti-detection.
Cookies are persisted to PostgreSQL to survive container restarts.
"""
import json
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal

settings = get_settings()
STATES_DIR = Path(__file__).parent.parent.parent / "playwright_states"
STATES_DIR.mkdir(exist_ok=True)


async def _save_cookies_to_db(account_id: str, state_json: str):
    """Persist Playwright state JSON to the database."""
    try:
        from app.models.system import Account
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(Account)
                .where(Account.id == account_id)
                .values(cookies_data=state_json)
            )
            await db.commit()
        logger.debug(f"Cookies persisted to DB for {account_id}")
    except Exception as e:
        logger.error(f"Failed to save cookies to DB for {account_id}: {e}")


async def _load_cookies_from_db(account_id: str) -> str | None:
    """Load Playwright state JSON from the database."""
    try:
        from app.models.system import Account
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Account.cookies_data).where(Account.id == account_id)
            )
            row = result.scalar_one_or_none()
            return row if row else None
    except Exception as e:
        logger.error(f"Failed to load cookies from DB for {account_id}: {e}")
        return None


def _load_cookies_from_db_sync(account_id: str) -> str | None:
    """Synchronous version for offline session checker."""
    try:
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_load_cookies_from_db(account_id))
        finally:
            loop.close()
    except Exception as e:
        logger.error(f"Sync cookie load failed for {account_id}: {e}")
        return None


class BrowserManager:
    """Manages isolated Playwright browser contexts for each account."""

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._contexts: dict[str, Any] = {}

    async def start(self):
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-zygote",
                    "--disable-features=VizDisplayCompositor",
                ],
            )
            logger.info("Playwright browser started")
        except ImportError:
            logger.warning("Playwright not installed, browser features disabled")

    async def stop(self):
        for ctx in self._contexts.values():
            await ctx.close()
        self._contexts.clear()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Playwright browser stopped")

    async def get_context(self, account_id: str, account_config: dict) -> Any:
        """Get or create an isolated browser context for an account."""
        if not self._browser:
            raise RuntimeError("Browser not started. Call start() first.")

        if account_id in self._contexts:
            return self._contexts[account_id]

        state_path = STATES_DIR / f"{account_id}.json"
        context_options: dict[str, Any] = {
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
        }

        if account_config.get("proxy_url"):
            context_options["proxy"] = {"server": account_config["proxy_url"]}

        if account_config.get("user_agent"):
            context_options["user_agent"] = account_config["user_agent"]

        if account_config.get("viewport"):
            context_options["viewport"] = account_config["viewport"]

        # Try file first, then restore from database
        if state_path.exists():
            context_options["storage_state"] = str(state_path)
        else:
            db_cookies = await _load_cookies_from_db(account_id)
            if db_cookies:
                state_path.write_text(db_cookies)
                context_options["storage_state"] = str(state_path)
                logger.info(f"Restored cookies from DB for {account_id}")

        context = await self._browser.new_context(**context_options)

        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) =>
                parameters.name === 'notifications'
                    ? Promise.resolve({state: Notification.permission})
                    : originalQuery(parameters);
            delete navigator.__proto__.webdriver;
        """)

        self._contexts[account_id] = context
        logger.info(f"Browser context created for account {account_id}")
        return context

    async def save_state(self, account_id: str):
        """Persist cookies and storage for an account (file + database)."""
        if account_id not in self._contexts:
            return
        state_path = STATES_DIR / f"{account_id}.json"
        state = await self._contexts[account_id].storage_state()
        state_json = json.dumps(state, ensure_ascii=False)

        # Save to file (fast local cache)
        state_path.write_text(state_json)
        # Save to database (survives container restarts)
        await _save_cookies_to_db(account_id, state_json)

        logger.info(f"Browser state saved for account {account_id}")

    async def close_context(self, account_id: str):
        """Close and remove a specific account context."""
        if account_id in self._contexts:
            await self.save_state(account_id)
            await self._contexts[account_id].close()
            del self._contexts[account_id]


browser_manager = BrowserManager()
