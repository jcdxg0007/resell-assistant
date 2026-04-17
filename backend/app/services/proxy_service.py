"""
Dynamic proxy resolution service.
Supports static proxy URLs and 青果网络 (qg.net) long-term proxy API.

Usage in account proxy_url field:
  - Static proxy: "http://ip:port" or "socks5://user:pass@ip:port"
  - 青果长效代理: "qgnet:YOUR_KEY"  (auto-queries assigned IP)
  - 青果长效代理+地区: "qgnet:YOUR_KEY:area=440100,440300"

Features:
  - Auto-queries assigned proxy IP from 青果 API
  - Auto-extracts new IP when current one expires (with area preference)
  - Auto-adds container IP to 青果 whitelist on startup
"""
import time
from typing import Any

import httpx
from loguru import logger

_proxy_cache: dict[str, dict[str, Any]] = {}
_whitelist_registered: dict[str, str] = {}

QG_LONGTERM_QUERY = "https://longterm.proxy.qg.net/query"
QG_LONGTERM_GET = "https://longterm.proxy.qg.net/get"
QG_WHITELIST_ADD = "https://proxy.qg.net/whitelist/add"

DEFAULT_AREA = "440100,440300"


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


async def resolve_proxy(proxy_url: str | None) -> str | None:
    """Resolve a proxy_url to an actual usable proxy address.

    Returns a URL like "http://ip:port" that Playwright can use,
    or None if no proxy is configured / resolution fails.
    """
    if not proxy_url:
        return None

    proxy_url = proxy_url.strip()

    if proxy_url.startswith("qgnet:"):
        return await _resolve_qgnet_longterm(proxy_url)

    return proxy_url


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


async def get_proxy_status(proxy_url: str | None) -> dict:
    """Get current proxy status (for API/debug display)."""
    if not proxy_url:
        return {"type": "none", "status": "未配置代理"}

    proxy_url = proxy_url.strip()

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
