"""
Dynamic proxy resolution service.
Supports static proxy URLs, 青果 long-term, and 青果 short-term (按量) proxies.

Usage in account proxy_url field:
  - Static proxy: "http://ip:port" or "socks5://user:pass@ip:port"
  - 青果长效代理: "qgnet:YOUR_KEY" (auto-queries, area support)
  - 青果短效代理: "qgshort:KEY:PWD" (auth-based, shared-pool, platform-grouped)

Short-term proxy platform groups — keeps risk isolation between platforms
that share the same risk-control system (Alibaba sees xianyu+taobao as one):
  - group_a: xianyu, pdd           (single IP)
  - group_b: taobao, xiaohongshu   (single IP)
  - group_c: jd                    (single IP)
Tasks of the same group reuse the group's live IP; tasks of different groups
get different IPs, so Alibaba never sees xianyu and taobao from one IP.

Features:
  - Auto-queries / auto-extracts / auto-releases 青果 long-term proxy
  - Auto-rotates 青果 short-term proxy when IP is <90s from expiry
  - Thread-safe per-group IP cache with asyncio locks
  - Auto-adds container IP to 青果 whitelist (long-term only)
"""
import asyncio
import time
from typing import Any

import httpx
from loguru import logger

_proxy_cache: dict[str, dict[str, Any]] = {}
_whitelist_registered: dict[str, str] = {}

# Short-term proxy pool is stored in Redis (shared across Celery workers).
# See _SHORT_POOL_REDIS_PREFIX / _SHORT_LOCK_REDIS_PREFIX below.

QG_LONGTERM_QUERY = "https://longterm.proxy.qg.net/query"
QG_LONGTERM_GET = "https://longterm.proxy.qg.net/get"
QG_SHORT_GET = "https://share.proxy.qg.net/get"
QG_WHITELIST_ADD = "https://proxy.qg.net/whitelist/add"

DEFAULT_AREA = "440100,440300"

# Platform -> risk-isolation group. See module docstring.
PLATFORM_GROUP: dict[str, str] = {
    "xianyu": "group_a",
    "pdd": "group_a",
    "taobao": "group_b",
    "xiaohongshu": "group_b",
    "jd": "group_c",
}
DEFAULT_GROUP = "group_default"

# When cached IP has less than this many seconds left, rotate preemptively
# so mid-task IP expiry never happens.
SHORT_MIN_REMAINING_SECONDS = 90


def _parse_qgnet_config(proxy_url: str) -> tuple[str, dict[str, str]]:
    """Parse 'qgnet:KEY' or 'qgnet:KEY:area=xxx&isp=1' into (key, params)."""
    parts = proxy_url.split(":", 2)
    key = parts[1] if len(parts) >= 2 else ""
    extra = parts[2] if len(parts) >= 3 else ""

    params: dict[str, str] = {}
    if extra:
        for pair in extra.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[k.strip()] = v.strip()

    return key, params


async def resolve_proxy(
    proxy_url: str | None,
    platform: str | None = None,
) -> dict[str, str] | None:
    """Resolve a proxy_url to a Playwright-compatible proxy config.

    Returns a dict like {"server": "http://host:port"} (optionally with
    "username"/"password") that can be passed directly to Playwright's
    new_context(proxy=...). Returns None if no proxy / resolution failed.

    The `platform` argument is used for short-term proxies to pick the
    right IP group (see PLATFORM_GROUP). Ignored for static / long-term.
    """
    if not proxy_url:
        return None

    proxy_url = proxy_url.strip()

    if proxy_url.startswith("qgshort:"):
        return await _resolve_qgnet_short(proxy_url, platform)

    if proxy_url.startswith("qgnet:"):
        server = await _resolve_qgnet_longterm(proxy_url)
        return {"server": server} if server else None

    return {"server": proxy_url}


def _parse_qgshort_config(proxy_url: str) -> tuple[str, str, dict[str, str]]:
    """Parse 'qgshort:KEY:PWD' or 'qgshort:KEY:PWD:area=xxx' into (key, pwd, params)."""
    parts = proxy_url.split(":", 3)
    key = parts[1] if len(parts) >= 2 else ""
    pwd = parts[2] if len(parts) >= 3 else ""
    extra = parts[3] if len(parts) >= 4 else ""

    params: dict[str, str] = {}
    if extra:
        for pair in extra.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[k.strip()] = v.strip()

    return key, pwd, params


async def _extract_qgnet_short(key: str, area: str | None) -> dict | None:
    """Extract one short-term IP via 青果 share API (按量 billing).

    Consumes one IP from the daily quota each successful call.
    """
    params: dict[str, str] = {"key": key, "num": "1", "format": "json"}
    if area:
        params["area"] = area
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(QG_SHORT_GET, params=params)
            data = resp.json()
        if data.get("code") != "SUCCESS":
            logger.error(
                f"青果短效提取失败: {data.get('code')} - {data.get('msg') or data.get('message')}"
            )
            return None
        ip_list = data.get("data", [])
        if not ip_list:
            logger.error("青果短效提取返回空列表")
            return None
        info = ip_list[0]
        logger.info(
            f"青果短效 IP 已提取: {info.get('server')} (出口 {info.get('proxy_ip')}, "
            f"{info.get('area', '')} {info.get('isp', '')}, 到期 {info.get('deadline', '')})"
        )
        return info
    except Exception as e:
        logger.error(f"青果短效提取异常: {e}")
        return None


_SHORT_POOL_REDIS_PREFIX = "proxy:short:pool:"
_SHORT_LOCK_REDIS_PREFIX = "proxy:short:lock:"


class _PerCallRedis:
    """Context manager that gives a fresh redis client and closes it afterwards.

    Avoids the module-level singleton binding to a stale event loop when
    Celery tasks run across different loops.
    """
    async def __aenter__(self):
        import redis.asyncio as aioredis
        from app.core.config import get_settings
        self._client = aioredis.from_url(
            get_settings().REDIS_URL,
            decode_responses=True,
            max_connections=5,
        )
        return self._client

    async def __aexit__(self, exc_type, exc, tb):
        try:
            await self._client.aclose()
        except Exception:
            pass


async def _resolve_qgnet_short(
    proxy_url: str,
    platform: str | None,
) -> dict[str, str] | None:
    """Resolve short-term proxy with per-group IP sharing (Redis-backed).

    Tasks in the same PLATFORM_GROUP reuse the group's active IP until it
    expires; tasks in different groups get different IPs. Shared across all
    Celery worker processes via Redis.
    """
    key, pwd, extra = _parse_qgshort_config(proxy_url)
    if not key or not pwd:
        logger.error("青果短效代理配置错误: 需要 qgshort:KEY:PWD")
        return None
    area = extra.get("area")

    group = PLATFORM_GROUP.get(platform or "", DEFAULT_GROUP)
    pool_key = f"{_SHORT_POOL_REDIS_PREFIX}{group}"
    lock_key = f"{_SHORT_LOCK_REDIS_PREFIX}{group}"

    async with _PerCallRedis() as redis_client:
        # Fast path: cached IP still fresh, return without locking
        cached = await _read_pool_entry(redis_client, pool_key)
        now = time.time()
        if cached and cached["deadline_ts"] - now > SHORT_MIN_REMAINING_SECONDS:
            return _short_pool_to_playwright(cached, key, pwd)

        # Slow path: acquire Redis lock so only one worker calls /get per group
        acquired = await _acquire_redis_lock(redis_client, lock_key, ttl_s=20)
        try:
            if not acquired:
                for _ in range(16):
                    await asyncio.sleep(0.5)
                    cached = await _read_pool_entry(redis_client, pool_key)
                    if cached and cached["deadline_ts"] - time.time() > SHORT_MIN_REMAINING_SECONDS:
                        return _short_pool_to_playwright(cached, key, pwd)
                logger.warning(f"青果短效池 [{group}] 等锁超时，强制重取")

            cached = await _read_pool_entry(redis_client, pool_key)
            now = time.time()
            if cached and cached["deadline_ts"] - now > SHORT_MIN_REMAINING_SECONDS:
                return _short_pool_to_playwright(cached, key, pwd)

            info = await _extract_qgnet_short(key, area)
            if not info:
                return None

            try:
                from datetime import datetime
                deadline_ts = datetime.strptime(
                    info.get("deadline", ""), "%Y-%m-%d %H:%M:%S"
                ).timestamp()
            except Exception:
                deadline_ts = now + 180

            entry = {
                "server": info["server"],
                "proxy_ip": info.get("proxy_ip", ""),
                "area": info.get("area", ""),
                "isp": info.get("isp", ""),
                "deadline_ts": deadline_ts,
            }
            await _write_pool_entry(redis_client, pool_key, entry, deadline_ts)
            logger.info(
                f"青果短效池 [{group}]: {entry['server']} → 出口 {entry['proxy_ip']} "
                f"({entry['area']}), {int(deadline_ts - now)}s 可用"
            )
            return _short_pool_to_playwright(entry, key, pwd)
        finally:
            if acquired:
                await _release_redis_lock(redis_client, lock_key)


async def invalidate_short_group(platform: str | None):
    """Drop the cached IP for the platform's group.

    Call this after a crawling failure that you suspect was IP-related, so
    the next task forces a fresh IP instead of reusing a bad one.
    """
    group = PLATFORM_GROUP.get(platform or "", DEFAULT_GROUP)
    async with _PerCallRedis() as redis_client:
        await redis_client.delete(f"{_SHORT_POOL_REDIS_PREFIX}{group}")
    logger.info(f"青果短效池 [{group}] 已失效，下次重新提取")


async def _read_pool_entry(redis_client, pool_key: str) -> dict | None:
    import json
    raw = await redis_client.get(pool_key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


async def _write_pool_entry(redis_client, pool_key: str, entry: dict, deadline_ts: float):
    import json
    ttl = max(10, int(deadline_ts - time.time()))
    await redis_client.set(pool_key, json.dumps(entry), ex=ttl)


async def _acquire_redis_lock(redis_client, lock_key: str, ttl_s: int) -> bool:
    # SET NX EX — returns None/False if key already exists
    result = await redis_client.set(lock_key, "1", nx=True, ex=ttl_s)
    return bool(result)


async def _release_redis_lock(redis_client, lock_key: str):
    try:
        await redis_client.delete(lock_key)
    except Exception:
        pass


def _short_pool_to_playwright(entry: dict, key: str, pwd: str) -> dict[str, str]:
    return {
        "server": f"http://{entry['server']}",
        "username": key,
        "password": pwd,
    }


async def _resolve_qgnet_longterm(proxy_url: str) -> str | None:
    """Query 青果 long-term proxy; auto-release & re-extract if expired or wrong area."""
    key, extra_params = _parse_qgnet_config(proxy_url)
    desired_area = extra_params.get("area", DEFAULT_AREA)

    if not key:
        logger.error("青果代理配置格式错误，缺少 key")
        return None

    cached = _proxy_cache.get(key)
    if cached:
        remaining = cached["deadline"] - time.time()
        if remaining > 60:
            logger.debug(f"Using cached proxy {cached['server']} (expires in {remaining:.0f}s)")
            return cached["proxy_formatted"]

    await _ensure_whitelist(key)

    result = await _query_existing_proxy(key)

    if result:
        deadline_str = result.get("deadline", "")
        area_code = str(result.get("area_code", ""))
        desired_codes = [c.strip() for c in desired_area.split(",") if c.strip()]
        is_expired = _is_proxy_expired(deadline_str)
        is_wrong_area = bool(desired_codes) and bool(area_code) and area_code not in desired_codes

        if is_expired or is_wrong_area:
            reason = "expired" if is_expired else f"wrong area ({result.get('area', '')})"
            logger.info(f"Current proxy {result.get('server')} {reason}, releasing...")
            await _release_proxy(key, result.get("proxy_ip", ""))
            result = None

    if not result:
        logger.info(f"No suitable proxy, auto-extracting new IP (area={desired_area})")
        result = await _extract_new_proxy(key, desired_area)

    if not result:
        return None

    return _cache_proxy_result(key, result)


def _is_proxy_expired(deadline_str: str) -> bool:
    """Check if proxy has passed its deadline (青果 returns Beijing time)."""
    if not deadline_str:
        return False
    try:
        from datetime import datetime, timezone, timedelta
        deadline_local = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M:%S")
        deadline_utc = deadline_local.replace(tzinfo=timezone(timedelta(hours=8)))
        return datetime.now(timezone.utc) > deadline_utc
    except Exception:
        return False


async def _release_proxy(key: str, proxy_ip: str):
    """Release an existing proxy IP so a new one can be extracted."""
    if not proxy_ip:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://proxy.qg.net/release",
                params={"Key": key, "IP": proxy_ip},
            )
            data = resp.json()
        if data.get("Code") == 0:
            logger.info(f"青果代理已释放: {proxy_ip}")
            _proxy_cache.pop(key, None)
        else:
            logger.warning(f"青果代理释放返回: {data}")
    except Exception as e:
        logger.warning(f"青果代理释放失败: {e}")


async def _query_existing_proxy(key: str) -> dict | None:
    """Query existing assigned proxy via /query API."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(QG_LONGTERM_QUERY, params={"key": key})
            data = resp.json()

        if data.get("code") != "SUCCESS":
            logger.warning(f"青果代理查询: {data.get('code')} - {data.get('message', '')}")
            return None

        ip_list = data.get("data", [])
        if not ip_list:
            logger.info("青果代理查询返回空列表，当前无可用代理")
            return None

        return ip_list[0]

    except Exception as e:
        logger.error(f"青果代理查询异常: {e}")
        return None


async def _extract_new_proxy(key: str, area: str) -> dict | None:
    """Extract a new proxy IP via /get API with area preference."""
    try:
        params: dict[str, str] = {"key": key, "area": area}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(QG_LONGTERM_GET, params=params)
            data = resp.json()

        if data.get("code") != "SUCCESS":
            logger.error(f"青果代理提取失败: {data.get('code')} - {data.get('message', '')}")
            return None

        ip_list = data.get("data", [])
        if not ip_list:
            logger.error("青果代理提取返回空列表")
            return None

        ip_info = ip_list[0]
        logger.info(
            f"青果代理自动提取成功: {ip_info.get('server')} "
            f"({ip_info.get('area', '')} {ip_info.get('isp', '')})"
        )
        return ip_info

    except Exception as e:
        logger.error(f"青果代理提取异常: {e}")
        return None


def _cache_proxy_result(key: str, ip_info: dict) -> str:
    """Cache proxy info and return formatted proxy URL."""
    server = ip_info["server"]
    proxy_formatted = f"http://{server}"

    deadline_str = ip_info.get("deadline", "")
    try:
        from datetime import datetime
        deadline_ts = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        deadline_ts = time.time() + 3600

    _proxy_cache[key] = {
        "server": server,
        "proxy_formatted": proxy_formatted,
        "deadline": deadline_ts,
        "area": ip_info.get("area", ""),
        "proxy_ip": ip_info.get("proxy_ip", ""),
        "isp": ip_info.get("isp", ""),
    }

    logger.info(
        f"青果代理就绪: {server} ({ip_info.get('area', '')} {ip_info.get('isp', '')}) "
        f"有效至 {deadline_str}"
    )
    return proxy_formatted


async def force_rotate_qgnet(proxy_url: str) -> dict:
    """Manually release the current 青果 IP and extract a new one.

    Useful when risk-control is suspected, or when preparing a fresh session.
    Currently consumes daily release+extract quota — use sparingly on a
    single-channel account. With multi-channel plans this is the per-keyword
    rotation hook.
    """
    if not proxy_url or not proxy_url.startswith("qgnet:"):
        return {"status": "error", "message": "not a qgnet proxy"}

    key, extra = _parse_qgnet_config(proxy_url)
    desired_area = extra.get("area", DEFAULT_AREA)
    if not key:
        return {"status": "error", "message": "invalid qgnet config"}

    await _ensure_whitelist(key)

    current = await _query_existing_proxy(key)
    if current and current.get("proxy_ip"):
        await _release_proxy(key, current["proxy_ip"])

    new_ip = await _extract_new_proxy(key, desired_area)
    if not new_ip:
        return {"status": "error", "message": "failed to extract new ip (quota?)"}

    _cache_proxy_result(key, new_ip)
    return {
        "status": "ok",
        "server": new_ip.get("server"),
        "area": new_ip.get("area"),
        "isp": new_ip.get("isp"),
    }


async def get_proxy_status(proxy_url: str | None) -> dict:
    """Get current proxy status (for API/debug display)."""
    if not proxy_url:
        return {"type": "none", "status": "未配置代理"}

    proxy_url = proxy_url.strip()

    if proxy_url.startswith("qgshort:"):
        now = time.time()
        groups: dict[str, dict] = {}
        known_groups = set(PLATFORM_GROUP.values()) | {DEFAULT_GROUP}
        async with _PerCallRedis() as redis_client:
            for g in known_groups:
                entry = await _read_pool_entry(redis_client, f"{_SHORT_POOL_REDIS_PREFIX}{g}")
                if not entry:
                    continue
                groups[g] = {
                    "server": entry.get("server"),
                    "proxy_ip": entry.get("proxy_ip"),
                    "area": entry.get("area"),
                    "isp": entry.get("isp"),
                    "remaining_seconds": max(0, int(entry.get("deadline_ts", 0) - now)),
                }
        return {
            "type": "qgnet_short",
            "status": "active" if groups else "idle",
            "groups": groups,
            "platform_mapping": PLATFORM_GROUP,
        }

    if proxy_url.startswith("qgnet:"):
        key, _ = _parse_qgnet_config(proxy_url)
        cached = _proxy_cache.get(key)

        if cached and cached["deadline"] - time.time() > 0:
            remaining = cached["deadline"] - time.time()
            return {
                "type": "qgnet_longterm",
                "status": "active",
                "server": cached["server"],
                "proxy_ip": cached["proxy_ip"],
                "area": cached["area"],
                "isp": cached.get("isp", ""),
                "remaining_seconds": max(0, int(remaining)),
            }

        resolved = await _resolve_qgnet_longterm(proxy_url)
        if resolved:
            cached = _proxy_cache.get(key)
            if cached:
                return {
                    "type": "qgnet_longterm",
                    "status": "active",
                    "server": cached["server"],
                    "proxy_ip": cached["proxy_ip"],
                    "area": cached["area"],
                    "isp": cached.get("isp", ""),
                    "remaining_seconds": max(0, int(cached["deadline"] - time.time())),
                }
        return {"type": "qgnet_longterm", "status": "error", "message": "无法获取代理"}

    return {"type": "static", "status": "active", "server": proxy_url}


async def _get_outbound_ip() -> str | None:
    """Detect this container's outbound IP."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get("https://d.qg.net/ip")
            return resp.text.strip()
    except Exception:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get("https://httpbin.org/ip")
                return resp.json().get("origin", "").strip()
        except Exception:
            return None


async def _ensure_whitelist(key: str):
    """Auto-add container IP to 青果 whitelist on first use."""
    outbound_ip = await _get_outbound_ip()
    if not outbound_ip:
        return

    if _whitelist_registered.get(key) == outbound_ip:
        return

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                QG_WHITELIST_ADD,
                params={"Key": key, "IP": outbound_ip},
            )
            data = resp.json()

        if data.get("Code") == 0:
            _whitelist_registered[key] = outbound_ip
            logger.info(f"青果白名单已自动添加: {outbound_ip} (Key={key[:6]}...)")
        else:
            logger.warning(f"青果白名单添加返回: {data}")
    except Exception as e:
        logger.warning(f"青果白名单自动添加失败: {e}")
