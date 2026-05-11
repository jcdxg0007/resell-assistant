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

# Mobile fingerprint pool for H5-only sites (PDD mobile, xianyu app WebView,
# etc). Using a desktop UA against mobile.yangkeduo.com immediately flags
# the session as automation and returns an empty ssrListData.list even
# with valid cookies. Viewports use common iPhone/Android DPR-corrected
# CSS pixels.
_MOBILE_UA_POOL = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
]

_MOBILE_VIEWPORT_POOL = [
    {"width": 390, "height": 844},   # iPhone 14/15
    {"width": 393, "height": 852},   # iPhone 15 Pro
    {"width": 412, "height": 915},   # Galaxy S / Pixel
]


def _random_fingerprint(mobile: bool = False) -> dict[str, Any]:
    """Return a randomised UA + viewport combo for an anonymous session.

    Set ``mobile=True`` when the target URL is an H5/mobile site — a
    mismatch between desktop UA and mobile.* host is the single biggest
    anti-bot tripwire we've seen from PDD in 2026.
    """
    if mobile:
        return {
            "user_agent": random.choice(_MOBILE_UA_POOL),
            "viewport": random.choice(_MOBILE_VIEWPORT_POOL),
            "is_mobile": True,
            "has_touch": True,
            "device_scale_factor": 3,
        }
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


# ─── Stealth init script ─────────────────────────────────────────────
#
# Playwright/CDP leaves a distinctive fingerprint (navigator.webdriver,
# missing chrome.runtime, wrong plugins count, undefined WebGL vendor,
# etc). Shipping anti-bot SDKs — 阿里风控, 拼多多 prowler, 滴答指纹 —
# rely on exactly these tells. We shadow them via an init script that
# runs in every new page before any site JS.
#
# Scope of patches (each one addresses a publicly documented 2023-2025
# detection technique):
#   1. navigator.webdriver   ── automation flag ( Playwright leaks true )
#   2. navigator.languages   ── empty or English-only means "likely bot"
#   3. navigator.plugins     ── headless Chromium ships zero plugins
#   4. hardware/screen/navigator.platform ── per-account values via
#      template substitution; without this every account reports the
#      same 8-core / 8-GB profile and the platform's aggregation queries
#      light up "one machine running many accounts"
#   5. permissions.query(notification)  ── headless returns "denied"
#      while the Notification.permission stays "default"
#   6. WebGL vendor/renderer ── leaks "Google Inc. (NVIDIA)" / SwiftShader
#   7. chrome.runtime object ── missing on headless Chromium
#   8. Canvas fingerprint    ── identical pixel-for-pixel across runs
#      gives us away; we add sub-perceptual noise per-session
#   9. AudioContext fingerprint ── similar to canvas, perturb floats
#  10. console.debug / window.outerWidth / etc. ── minor but cheap
#
# Every patch is defensive — if the underlying API doesn't exist in the
# current Chromium the try/catch falls through silently. Keep this
# script idempotent: adding a second copy (e.g. from a legacy caller)
# must not error.
#
# Template placeholders ``__HW_CONCURRENCY__`` / ``__DEVICE_MEMORY__`` /
# ``__SCREEN_WIDTH__`` / ``__SCREEN_HEIGHT__`` / ``__SCREEN_COLOR_DEPTH__``
# / ``__PLATFORM_STR__`` are replaced at call time by
# :func:`build_stealth_script` using the account's persisted fingerprint.
_STEALTH_INIT_TEMPLATE = r"""
(() => {
  const _noop = () => {};
  const safeDefine = (obj, prop, getter) => {
    try { Object.defineProperty(obj, prop, {get: getter, configurable: true}); }
    catch (e) { _noop(); }
  };

  // 1. navigator.webdriver
  safeDefine(Navigator.prototype, 'webdriver', () => undefined);
  try { delete Navigator.prototype.webdriver; } catch (e) {}

  // 2. navigator.languages — match UA locale
  safeDefine(navigator, 'languages', () => ['zh-CN', 'zh', 'en-US', 'en']);

  // 3. navigator.plugins — build realistic PluginArray/MimeTypeArray
  // Shape lifted from vanilla Chrome 131 on Windows.
  const fakePlugins = [
    {name: 'PDF Viewer', filename: 'internal-pdf-viewer',
     description: 'Portable Document Format'},
    {name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer',
     description: 'Portable Document Format'},
    {name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer',
     description: 'Portable Document Format'},
    {name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer',
     description: 'Portable Document Format'},
    {name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer',
     description: 'Portable Document Format'},
  ];
  safeDefine(navigator, 'plugins', () => {
    const arr = fakePlugins.map(p => {
      const mime = {type: 'application/pdf', suffixes: 'pdf',
                    description: p.description};
      return {...p, length: 1, 0: mime,
              item: () => mime, namedItem: () => mime};
    });
    arr.item = (i) => arr[i];
    arr.namedItem = (n) => arr.find(p => p.name === n) || null;
    arr.refresh = _noop;
    return arr;
  });
  safeDefine(navigator, 'mimeTypes', () => {
    const types = fakePlugins.map(p => ({
      type: 'application/pdf', suffixes: 'pdf',
      description: p.description, enabledPlugin: p,
    }));
    types.item = (i) => types[i];
    types.namedItem = (n) => types.find(t => t.type === n) || null;
    return types;
  });

  // 4. hardwareConcurrency / deviceMemory / screen / navigator.platform.
  //    Values are template-substituted per account (see build_stealth_script
  //    in browser.py) so every account in the pool reports a *different*
  //    yet stable hardware profile — defuses the aggregation tell where
  //    100 accounts all claim to be 8-core / 8-GB machines.
  safeDefine(navigator, 'hardwareConcurrency', () => __HW_CONCURRENCY__);
  safeDefine(navigator, 'deviceMemory', () => __DEVICE_MEMORY__);
  safeDefine(navigator, 'platform', () => '__PLATFORM_STR__');
  try {
    safeDefine(window.screen, 'width', () => __SCREEN_WIDTH__);
    safeDefine(window.screen, 'height', () => __SCREEN_HEIGHT__);
    safeDefine(window.screen, 'availWidth', () => __SCREEN_WIDTH__);
    safeDefine(window.screen, 'availHeight', () => (__SCREEN_HEIGHT__ - 40));
    safeDefine(window.screen, 'colorDepth', () => __SCREEN_COLOR_DEPTH__);
    safeDefine(window.screen, 'pixelDepth', () => __SCREEN_COLOR_DEPTH__);
  } catch (e) {}

  // 5. permissions.query — headless returns "denied" while
  //    Notification.permission is "default"; keep them consistent
  if (navigator.permissions && navigator.permissions.query) {
    const origQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (params) => {
      if (params && params.name === 'notifications') {
        return Promise.resolve({state: Notification.permission});
      }
      return origQuery(params);
    };
  }

  // 6. WebGL vendor/renderer — mimic Intel integrated GPU (very common)
  const getParameterOverrides = {
    37445: 'Intel Inc.',                      // UNMASKED_VENDOR_WEBGL
    37446: 'Intel Iris OpenGL Engine',        // UNMASKED_RENDERER_WEBGL
  };
  const patchContext = (proto) => {
    if (!proto || !proto.getParameter) return;
    const orig = proto.getParameter;
    proto.getParameter = function (p) {
      if (p in getParameterOverrides) return getParameterOverrides[p];
      return orig.call(this, p);
    };
  };
  try { patchContext(WebGLRenderingContext.prototype); } catch (e) {}
  try { patchContext(WebGL2RenderingContext.prototype); } catch (e) {}

  // 7. chrome.runtime — headless Chromium omits this; fake a minimal shape
  if (!window.chrome) {
    window.chrome = {};
  }
  if (!window.chrome.runtime) {
    window.chrome.runtime = {
      PlatformOs: {MAC: 'mac', WIN: 'win', ANDROID: 'android',
                   CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd'},
      PlatformArch: {ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64'},
      onConnect: null, onMessage: null,
    };
  }

  // 8. Canvas fingerprint noise
  // Most fingerprinters draw a fixed string and hash the resulting
  // pixel buffer. We perturb the alpha channel by ±1 on a random
  // subset of pixels — invisible to users, lethal to pixel-perfect
  // hashes. The noise is seeded once per document so repeated
  // measurements within the same session are stable (avoids the
  // opposite tell: "canvas hash changes every frame").
  try {
    const rand = (() => {
      let s = (Math.random() * 1e9) | 0;
      return () => { s = (s * 16807) % 2147483647; return s / 2147483647; };
    })();
    const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function (x, y, w, h) {
      const img = origGetImageData.call(this, x, y, w, h);
      const d = img.data;
      for (let i = 0; i < d.length; i += 4) {
        if (rand() < 0.003) {
          d[i]     = Math.max(0, Math.min(255, d[i]     + (rand() < 0.5 ? -1 : 1)));
          d[i + 1] = Math.max(0, Math.min(255, d[i + 1] + (rand() < 0.5 ? -1 : 1)));
          d[i + 2] = Math.max(0, Math.min(255, d[i + 2] + (rand() < 0.5 ? -1 : 1)));
        }
      }
      return img;
    };
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function (...args) {
      try {
        const ctx = this.getContext('2d');
        if (ctx) { ctx.getImageData(0, 0, this.width, this.height); }
      } catch (e) {}
      return origToDataURL.apply(this, args);
    };
  } catch (e) {}

  // 9. AudioContext fingerprint noise (same idea, but for Web Audio
  // fingerprinters such as FingerprintJS).
  try {
    const ctxProto = (window.AudioContext || window.webkitAudioContext)
                      && (window.AudioContext || window.webkitAudioContext).prototype;
    if (ctxProto && ctxProto.createAnalyser) {
      const origCreate = ctxProto.createAnalyser;
      ctxProto.createAnalyser = function () {
        const a = origCreate.call(this);
        const origGetFloat = a.getFloatFrequencyData;
        a.getFloatFrequencyData = function (arr) {
          origGetFloat.call(this, arr);
          for (let i = 0; i < arr.length; i++) arr[i] += (Math.random() - 0.5) * 1e-7;
        };
        return a;
      };
    }
  } catch (e) {}

  // 10. Minor: outerWidth/outerHeight, mediaDevices dummy
  try {
    if (!window.outerWidth) safeDefine(window, 'outerWidth', () => window.innerWidth);
    if (!window.outerHeight) safeDefine(window, 'outerHeight', () => window.innerHeight);
  } catch (e) {}
})();
"""


# Defaults for sessions without an account (anonymous, no persistence).
# We still inject a *consistent* random profile to avoid leaving the
# template placeholders un-substituted, which would syntactically break
# the script and is itself a tell.
_FP_DEFAULTS: dict[str, Any] = {
    "hardware_concurrency": 8,
    "device_memory": 8,
    "screen": {"width": 1920, "height": 1080, "color_depth": 24},
    "platform_str": "Win32",
}


def build_stealth_script(fingerprint: dict[str, Any] | None = None) -> str:
    """Render the stealth template with a specific hardware profile.

    ``fingerprint`` is expected to be the dict returned by
    :func:`app.services.account_fingerprint.get_or_init_fingerprint`
    (or :func:`generate_ephemeral_fingerprint` for one-off sessions).
    Missing keys fall back to ``_FP_DEFAULTS`` so callers never have to
    construct a full dict by hand.
    """
    fp = {**_FP_DEFAULTS, **(fingerprint or {})}
    screen = fp.get("screen") or _FP_DEFAULTS["screen"]
    if not isinstance(screen, dict):
        screen = _FP_DEFAULTS["screen"]
    return (
        _STEALTH_INIT_TEMPLATE
        .replace("__HW_CONCURRENCY__", str(int(fp["hardware_concurrency"])))
        .replace("__DEVICE_MEMORY__", str(int(fp["device_memory"])))
        .replace("__SCREEN_WIDTH__", str(int(screen.get("width", 1920))))
        .replace("__SCREEN_HEIGHT__", str(int(screen.get("height", 1080))))
        .replace("__SCREEN_COLOR_DEPTH__", str(int(screen.get("color_depth", 24))))
        .replace("__PLATFORM_STR__", str(fp.get("platform_str", "Win32")))
    )


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

        # V3: load this account's stable hardware fingerprint so the
        # stealth script reports values consistent across sessions for
        # the same account (varying *between* accounts).
        from app.services.account_fingerprint import get_or_init_fingerprint
        try:
            hw_fp = await get_or_init_fingerprint(account_id)
        except Exception as e:
            logger.warning(
                f"Fingerprint load failed for {account_id}, using defaults: {e}"
            )
            hw_fp = None

        context = await self._browser.new_context(**context_options)
        await context.add_init_script(build_stealth_script(hw_fp))

        self._contexts[account_id] = context
        if hw_fp:
            logger.info(
                f"Browser context for {account_id}: "
                f"hw={hw_fp.get('hardware_concurrency')}/"
                f"{hw_fp.get('device_memory')}GB, "
                f"platform={hw_fp.get('platform_str')}"
            )
        else:
            logger.info(f"Browser context created for account {account_id}")
        return context

    async def get_anonymous_context(
        self,
        proxy_url: str | None = None,
        platform: str | None = None,
        mobile: bool | None = None,
        proxy_area: str | None = None,
        account_id: str | None = None,
    ) -> Any:
        """Create a disposable, cookie-free browser context.

        Use this for crawling public pages (search results, listings) so the
        traffic is not tied to any logged-in account. Randomises UA and
        viewport each call to reduce fingerprint concentration.

        `platform` is passed through to the proxy resolver so short-term
        proxies can pick the right IP group for risk isolation.

        `proxy_area` pins the exit IP to a specific GB-T 2260 area code
        (e.g. "350000" 福建) — used when a crawler account has been
        bound to a specific geographic home via Account.bound_proxy_area.
        Overrides any ``area`` embedded in proxy_url.

        `mobile`: when True the fingerprint is iPhone/Android-shaped; when
        None we auto-pick mobile for H5-only platforms (currently just
        ``pdd``). Desktop UA against mobile.yangkeduo.com is an instant
        anti-bot flag.

        `account_id`: when supplied, the per-account *hardware*
        fingerprint (V3) is loaded from ``Account.fingerprint`` and
        injected into the stealth script so the same account reports a
        stable hw profile across runs. Anonymous calls (no account_id)
        get a one-off random profile per session — still varied between
        runs, but never the static 8/8 default.
        """
        if not self._browser or not self._current_loop_matches():
            await self.start()

        if mobile is None:
            mobile = platform in ("pdd",)

        fp = _random_fingerprint(mobile=mobile)

        # V3: account-stable hardware fingerprint when available,
        # ephemeral random one for true anonymous sessions.
        from app.services.account_fingerprint import (
            generate_ephemeral_fingerprint,
            get_or_init_fingerprint,
        )
        hw_fp: dict[str, Any] | None
        if account_id:
            try:
                hw_fp = await get_or_init_fingerprint(account_id)
            except Exception as e:
                logger.warning(
                    f"Fingerprint load failed for {account_id}, using ephemeral: {e}"
                )
                hw_fp = generate_ephemeral_fingerprint()
        else:
            hw_fp = generate_ephemeral_fingerprint()

        context_options: dict[str, Any] = {
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "user_agent": fp["user_agent"],
            "viewport": fp["viewport"],
        }
        if mobile:
            context_options["is_mobile"] = fp.get("is_mobile", True)
            context_options["has_touch"] = fp.get("has_touch", True)
            context_options["device_scale_factor"] = fp.get("device_scale_factor", 3)

        if proxy_url:
            from app.services.proxy_service import resolve_proxy
            resolved = await resolve_proxy(
                proxy_url, platform=platform, area_override=proxy_area
            )
            if resolved:
                context_options["proxy"] = resolved
                area_note = f", area={proxy_area}" if proxy_area else ""
                logger.info(
                    f"Anonymous context proxy: {resolved['server']}"
                    f" (auth={'yes' if resolved.get('username') else 'no'}, "
                    f"platform={platform}{area_note})"
                )
            else:
                logger.warning("Anonymous context: proxy resolution failed, direct")

        context = await self._browser.new_context(**context_options)
        await context.add_init_script(build_stealth_script(hw_fp))
        # Track so _shutdown_per_loop_resources can close it too
        anon_key = f"_anon_{id(context)}"
        self._contexts[anon_key] = context
        hw_tag = (
            f", hw={hw_fp.get('hardware_concurrency')}/"
            f"{hw_fp.get('device_memory')}GB"
            if hw_fp else ""
        )
        acc_tag = f", account={account_id}" if account_id else ""
        logger.info(
            f"Anonymous context created ({'mobile' if mobile else 'desktop'}, "
            f"UA=...{fp['user_agent'][-30:]}, "
            f"vp={fp['viewport']['width']}x{fp['viewport']['height']}"
            f"{hw_tag}{acc_tag})"
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
