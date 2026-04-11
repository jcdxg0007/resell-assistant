from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from loguru import logger

from app.core.config import get_settings
from app.core.database import engine, Base
from app.core.redis import redis_client
from app.api.v1.router import api_router

settings = get_settings()


class ForceHTTPSSchemeMiddleware(BaseHTTPMiddleware):
    """Force HTTPS scheme when behind TLS-terminating proxy (e.g. Sealos/Envoy)."""

    async def dispatch(self, request: Request, call_next):
        if request.headers.get("x-forwarded-proto") == "https":
            request.scope["scheme"] = "https"
        response = await call_next(request)
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting {settings.PROJECT_NAME} v{settings.VERSION}")
    await redis_client.ping()
    logger.info("Redis connected")
    yield
    await redis_client.close()
    await engine.dispose()
    logger.info("Shutdown complete")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan,
)

app.add_middleware(ForceHTTPSSchemeMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/health")
async def health_check():
    return {"status": "ok", "version": settings.VERSION}


@app.get("/debug/network")
async def debug_network():
    """Test outbound network from this container."""
    import socket
    import time as _time
    results = {}
    targets = [
        ("creator.xiaohongshu.com", 443),
        ("www.baidu.com", 443),
        ("www.google.com", 443),
    ]
    for host, port in targets:
        try:
            t0 = _time.time()
            ip = socket.getaddrinfo(host, port, socket.AF_INET)[0][4][0]
            dns_ms = round((_time.time() - t0) * 1000)
            t1 = _time.time()
            s = socket.create_connection((host, port), timeout=10)
            s.close()
            tcp_ms = round((_time.time() - t1) * 1000)
            results[host] = {"ip": ip, "dns_ms": dns_ms, "tcp_ms": tcp_ms, "ok": True}
        except Exception as e:
            results[host] = {"error": str(e)[:120], "ok": False}

    # Also test HTTP via httpx if available
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            t0 = _time.time()
            resp = await client.get("https://creator.xiaohongshu.com/")
            http_ms = round((_time.time() - t0) * 1000)
            results["httpx_xiaohongshu"] = {"status": resp.status_code, "ms": http_ms, "ok": True}
    except Exception as e:
        results["httpx_xiaohongshu"] = {"error": str(e)[:120], "ok": False}

    return results
