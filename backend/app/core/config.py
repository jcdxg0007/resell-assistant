from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    PROJECT_NAME: str = "转卖助手"
    VERSION: str = "0.1.0"
    API_V1_PREFIX: str = "/api/v1"
    DEBUG: bool = False

    # Auth
    SECRET_KEY: str = "change-me-in-production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days

    # PostgreSQL
    DATABASE_URL: str = "postgresql+asyncpg://postgres:cfghhm7f@resell-manager-postgresql.ns-3zn44u6p.svc:5432/postgres"
    DATABASE_ECHO: bool = False

    # Redis
    REDIS_URL: str = "redis://default:Xv01aH061L@resell--manager-redis-redis.ns-3zn44u6p.svc:6379/0"

    # Celery
    CELERY_BROKER_URL: str = "redis://default:Xv01aH061L@resell--manager-redis-redis.ns-3zn44u6p.svc:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://default:Xv01aH061L@resell--manager-redis-redis.ns-3zn44u6p.svc:6379/2"

    # DingTalk
    DINGTALK_WEBHOOK_URL: str = "https://oapi.dingtalk.com/robot/send?access_token=9665944ecd4eea9fc5a73d5fb11d140324f34b880d4993905d0213060b651812"
    DINGTALK_SECRET: str = "SEC86507aaec1945c1e0953632994190d74695b66d112caaf668f67a13ca75c0bb4"

    # Email
    SMTP_HOST: str = ""
    SMTP_PORT: int = 465
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""

    # LLM API
    LLM_API_KEY: str = ""
    LLM_API_BASE_URL: str = "https://api.openai.com/v1"
    LLM_MODEL_HEAVY: str = "gpt-4o"
    LLM_MODEL_LIGHT: str = "gpt-4o-mini"

    # Auto-payment daily limit (CNY)
    AUTO_PAY_DAILY_LIMIT: float = 2000.0
    AUTO_PAY_MAX_AMOUNT: float = 200.0

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
