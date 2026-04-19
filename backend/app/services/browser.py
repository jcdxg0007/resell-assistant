"""
Playwright browser management service.
Handles multi-account browser context isolation with anti-detection.
Cookies are persisted to PostgreSQL to survive container restarts.
"""
import json
import random
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal

settings = get_settings()
STATES_DIR = Path(__file__).parent.parent.parent / "playwright_states"
STATES_DIR.mkdir(exist_ok=True)


# Rotating UA pool — realistic residential desktop Chrome on Win/Mac/Linux.
# Weighted implicitly by list length (Win most common).
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]

_VIEWPORT_POOL = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
]


def _random_fingerprint() -> dict[str, Any]:
    """Return a randomised UA + viewport combo for an anonymous session."""
    return {
        "user_agent": random.choice(_UA_POOL),
        "viewport": random.choice(_VIEWPORT_POOL),
    }


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
        self._loop_id: int | None = None

    def _current_loop_matches(self) -> bool:
        """Playwright transport is bound to the loop that created it.
        Using it from a different loop deadlocks. Detect and force restart."""
        import asyncio
        try:
            current = id(asyncio.get_running_loop())
            return self._loop_id == current
        except RuntimeError:
            return False

    async def start(self):
        import asyncio
        # Reset if the cached browser belongs to a different/closed loop.
        if self._browser is not None and not self._current_loop_matches():
            logger.info("Browser belongs to stale loop, discarding for restart")
            self._playwright = None
            self._browser = None
            self._contexts.clear()
        try:
            import shutil
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._loop_id = id(asyncio.get_running_loop())

            launch_kwargs: dict[str, Any] = {
                "headless": True,
                "args": [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-zygote",
                    "--disable-features=VizDisplayCompositor",
                ],
            }
            system_chromium = shutil.which("chromium") or shutil.which("chromium-browser")
            if system_chromium:
                launch_kwargs["executable_path"] = system_chromium
                logger.info(f"Using system Chromium: {system_chromium}")

            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
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
        if not self._browser or not self._current_loop_matches():
            await self.start()

        if account_id in self._contexts and self._current_loop_matches():
            return self._contexts[account_id]

        state_path = STATES_DIR / f"{account_id}.json"
        context_options: dict[str, Any] = {
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
        }

        raw_proxy = account_config.get("proxy_url")
        if raw_proxy:
            from app.services.proxy_service import resolve_proxy
            # Logged-in contexts don't have a platform grouping context, so
            # fall back to account_id as the group name. For short-term
            # proxies this means each logged-in account gets its own IP.
            resolved = await resolve_proxy(raw_proxy, platform=account_id)
            if resolved:
                context_options["proxy"] = resolved
                logger.info(
                    f"Proxy resolved for {account_id}: {resolved['server']} "
                    f"(auth={'yes' if resolved.get('username') else 'no'})"
                )
            else:
                logger.warning(f"Proxy resolution failed for {account_id}, proceeding without proxy")

        # Always set a realistic UA; headless Chromium defaults to "HeadlessChrome"
        # which is instantly flagged by goofish/xianyu anti-bot.
        context_options["user_agent"] = (
            account_config.get("user_agent")
            or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )

        context_options["viewport"] = (
            account_config.get("viewport") or {"width": 1366, "height": 768}
        )

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

    async def get_anonymous_context(
        self,
        proxy_url: str | None = None,
        platform: str | None = None,
    ) -> Any:
        """Create a disposable, cookie-free browser context.

        Use this for crawling public pages (search results, listings) so the
        traffic is not tied to any logged-in account. Randomises UA and
        viewport each call to reduce fingerprint concentration.

        `platform` is passed through to the proxy resolver so short-term
        proxies can pick the right IP group for risk isolation.
        """
        if not self._browser or not self._current_loop_matches():
            await self.start()

        fp = _random_fingerprint()
        context_options: dict[str, Any] = {
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "user_agent": fp["user_agent"],
            "viewport": fp["viewport"],
        }

        if proxy_url:
            from app.services.proxy_service import resolve_proxy
            resolved = await resolve_proxy(proxy_url, platform=platform)
            if resolved:
                context_options["proxy"] = resolved
                logger.info(
                    f"Anonymous context proxy: {resolved['server']}"
                    f" (auth={'yes' if resolved.get('username') else 'no'}, "
                    f"platform={platform})"
                )
            else:
                logger.warning("Anonymous context: proxy resolution failed, direct")

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
        # Track so _shutdown_per_loop_resources can close it too
        anon_key = f"_anon_{id(context)}"
        self._contexts[anon_key] = context
        logger.info(
            f"Anonymous context created (UA=...{fp['user_agent'][-30:]}, "
            f"vp={fp['viewport']['width']}x{fp['viewport']['height']})"
        )
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
