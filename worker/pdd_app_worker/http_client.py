"""worker → backend HTTP 客户端。

封装 4 个 endpoint 调用：poll / result / heartbeat / status。
所有请求自动带 Bearer token。失败时按指数退避重试。
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

logger = logging.getLogger(__name__)

BACKEND_BASE_URL = os.environ["BACKEND_BASE_URL"].rstrip("/")
WORKER_TOKEN = os.environ["WORKER_TOKEN"]
POLL_WAIT_SECONDS = int(os.environ.get("POLL_WAIT_SECONDS", "25"))

API_PREFIX = "/api/v1/pdd-worker"


class BackendClient:
    """长连接 backend 的 HTTP 客户端。重试策略：网络错误指数退避，4xx 直接抛。"""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=BACKEND_BASE_URL,
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
            # poll endpoint 长轮询 25s，加 buffer
            timeout=httpx.Timeout(connect=10.0, read=POLL_WAIT_SECONDS + 15, write=10.0, pool=10.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def poll_task(self) -> dict[str, Any] | None:
        """长轮询拉一个任务。无任务时返回 None。"""
        backoff = 1.0
        while True:
            try:
                r = await self._client.get(
                    f"{API_PREFIX}/poll",
                    params={"wait_s": POLL_WAIT_SECONDS},
                )
                if r.status_code == 401:
                    raise RuntimeError(
                        "401 from backend: WORKER_TOKEN 不匹配。"
                        "确认 backend .env 和 worker .env 里的 PDD_WORKER_TOKEN 一致。"
                    )
                if r.status_code == 503:
                    # 区分两种 503：
                    #  1) backend app 自己 raise 的（token 是默认值）: body 是 JSON
                    #     {"detail": "..."}
                    #  2) Sealos/k8s ingress 返回的（upstream connect error，
                    #     backend pod 在重启/未 ready）: body 是 plain text
                    #     "upstream connect error or disconnect/reset ..."
                    body = r.text[:200]
                    if "upstream connect error" in body or "no healthy upstream" in body:
                        logger.warning(
                            f"poll_task: backend ingress 暂时连不上 upstream "
                            f"（backend pod 可能在重启）；{backoff:.1f}s 后自动重试。"
                            f" body={body!r}"
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30.0)
                        continue
                    raise RuntimeError(
                        f"503 from backend (app-level): {body}. "
                        "如果提示 'PDD_WORKER_TOKEN'，去 backend .env 改成非默认值后重启 backend pod。"
                    )
                r.raise_for_status()
                return r.json().get("task")
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                logger.warning(f"poll_task transient error: {exc}; retrying in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def push_result(self, result: dict[str, Any]) -> None:
        """推结果。短重试，失败 3 次后放弃（会丢这一次结果，但避免无限阻塞 worker 主循环）。"""
        for attempt in range(3):
            try:
                r = await self._client.post(f"{API_PREFIX}/result", json=result)
                r.raise_for_status()
                return
            except Exception as exc:
                logger.warning(f"push_result attempt {attempt + 1} failed: {exc}")
                await asyncio.sleep(2 ** attempt)
        logger.error(f"push_result gave up after 3 attempts: task_id={result.get('task_id')}")

    async def send_heartbeat(self, devices: list[str]) -> None:
        """心跳上报。失败不抛，下次重试。"""
        try:
            r = await self._client.post(f"{API_PREFIX}/heartbeat", json=devices)
            r.raise_for_status()
        except Exception as exc:
            logger.warning(f"send_heartbeat failed: {exc}")

    async def get_status(self) -> dict[str, Any]:
        r = await self._client.get(f"{API_PREFIX}/status")
        r.raise_for_status()
        return r.json()

    async def fetch_runtime_config(self) -> dict[str, Any] | None:
        """拉取 backend 上的运行时调度配置。失败不抛（返回 None），
        让 worker 沿用当前内存里的配置，不影响采集主循环。"""
        try:
            r = await self._client.get(f"{API_PREFIX}/runtime-config")
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            logger.warning(f"fetch_runtime_config failed: {exc}")
            return None
