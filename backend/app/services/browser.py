"""
Playwright browser management service.
Handles multi-account browser context isolation with anti-detection.
"""
import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

from app.core.config import get_settings

settings = get_settings()
STATES_DIR = Path(__file__).parent.parent.parent / "playwright_states"
STATES_DIR.mkdir(exist_ok=True)


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
            self._browser = await self._playwright.chromium.launch(headless=True)
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

        if state_path.exists():
            context_options["storage_state"] = str(state_path)

        context = await self._browser.new_context(**context_options)

        # Apply stealth settings
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
        """Persist cookies and storage for an account."""
        if account_id not in self._contexts:
            return
        state_path = STATES_DIR / f"{account_id}.json"
        state = await self._contexts[account_id].storage_state()
        state_path.write_text(json.dumps(state, ensure_ascii=False))
        logger.info(f"Browser state saved for account {account_id}")

    async def close_context(self, account_id: str):
        """Close and remove a specific account context."""
        if account_id in self._contexts:
            await self.save_state(account_id)
            await self._contexts[account_id].close()
            del self._contexts[account_id]


browser_manager = BrowserManager()
