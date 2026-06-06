import redis.asyncio as aioredis

from app.core.config import get_settings

settings = get_settings()

redis_client = aioredis.from_url(
    settings.REDIS_URL,
    decode_responses=True,
    max_connections=20,
)


async def get_redis() -> aioredis.Redis:
    return redis_client


async def reset_redis_pool() -> None:
    """断开 redis 连接池里所有连接，下次用时在「当前事件循环」重新建连。

    Celery 每个任务跑在新建的事件循环上（见 tasks 里的 run_async）。redis_client
    是模块级单例，其连接池里的连接绑死在「首次使用时的那个 loop」；换 loop 后复用
    会抛 `RuntimeError: Future attached to a different loop / Event loop is closed`。
    与 SQLAlchemy 的 engine.dispose() 同理：每个任务开跑前把池清空即可。池为空时是
    廉价 no-op。
    """
    try:
        await redis_client.connection_pool.disconnect(inuse_connections=True)
    except Exception:
        pass
