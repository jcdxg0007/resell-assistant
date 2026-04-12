"""
Dynamic proxy resolution service.
Supports static proxy URLs and 青果网络 (qg.net) long-term proxy API.

Usage in account proxy_url field:
  - Static proxy: "http://ip:port" or "socks5://user:pass@ip:port"
  - 青果长效代理: "qgnet:YOUR_KEY"  (auto-queries assigned IP)
  - 青果长效代理+地区: "qgnet:YOUR_KEY:area=441900"
"""
import time
from typing import Any

import httpx
from loguru import logger

_proxy_cache: dict[str, dict[str, Any]] = {}

QG_LONGTERM_API = "https://longterm.proxy.qg.net/query"


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
    """Query 青果网络 long-term proxy API for assigned proxy IP.

    Format: "qgnet:KEY" or "qgnet:KEY:task=xxx"
    """
    parts = proxy_url.split(":", 2)
    key = parts[1] if len(parts) >= 2 else ""
    extra_params = parts[2] if len(parts) >= 3 else ""

    if not key:
        logger.error("青果代理配置格式错误，缺少 key")
        return None

    cached = _proxy_cache.get(key)
    if cached:
        remaining = cached["deadline"] - time.time()
        if remaining > 60:
            logger.debug(f"Using cached proxy {cached['server']} (expires in {remaining:.0f}s)")
            return cached["proxy_formatted"]

    params: dict[str, str] = {"key": key}
    if extra_params:
        for pair in extra_params.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[k.strip()] = v.strip()

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(QG_LONGTERM_API, params=params)
            data = resp.json()

        if data.get("code") != "SUCCESS":
            logger.error(f"青果长效代理查询失败: {data.get('code')} - {data}")
            return None

        ip_list = data.get("data", [])
        if not ip_list:
            logger.error("青果长效代理返回空列表，可能没有可用的代理通道")
            return None

        ip_info = ip_list[0]
        server = ip_info["server"]  # e.g. "125.75.110.68:62473"
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
            f"青果长效代理查询成功: {server} ({ip_info.get('area', '')} {ip_info.get('isp', '')}) "
            f"有效至 {deadline_str}"
        )
        return proxy_formatted

    except Exception as e:
        logger.error(f"青果长效代理查询异常: {e}")
        return None


async def get_proxy_status(proxy_url: str | None) -> dict:
    """Get current proxy status (for API/debug display)."""
    if not proxy_url:
        return {"type": "none", "status": "未配置代理"}

    proxy_url = proxy_url.strip()

    if proxy_url.startswith("qgnet:"):
        parts = proxy_url.split(":", 2)
        key = parts[1] if len(parts) >= 2 else ""
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
