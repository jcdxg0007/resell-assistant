"""Shared async runner for Celery tasks.

Each Celery prefork worker creates a new event loop per task. SQLAlchemy's
async engine caches asyncpg connections in its pool, and those connections
are bound to the loop that created them. Without a pre-task dispose, the
engine may hand out a connection whose `_waiter` future lives on a dead
loop, producing `got Future ... attached to a different loop` errors.

Calling `engine.dispose()` before running the coroutine is cheap when the
pool is already empty and guarantees every task starts with fresh
connections.
"""
import asyncio
from loguru import logger


def run_async(coro):
    """Run an async coroutine from a sync Celery task on a fresh event loop.

    Disposes the SQLAlchemy async engine before running the coroutine to
    drop any stale asyncpg connections inherited from the fork parent or a
    previous task's dead loop.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        try:
            from app.core.database import engine
            loop.run_until_complete(engine.dispose())
        except Exception as e:
            logger.debug(f"Pre-task engine dispose ignored: {e}")
        # 同理重置 redis 连接池，避免复用死 loop 上的连接报
        # 'Future attached to a different loop / Event loop is closed'
        try:
            from app.core.redis import reset_redis_pool
            loop.run_until_complete(reset_redis_pool())
        except Exception as e:
            logger.debug(f"Pre-task redis reset ignored: {e}")
        return loop.run_until_complete(coro)
    finally:
        try:
            from app.core.database import engine
            loop.run_until_complete(engine.dispose())
        except Exception:
            pass
        loop.close()
        asyncio.set_event_loop(None)
