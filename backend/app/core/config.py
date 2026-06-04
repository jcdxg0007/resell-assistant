from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    PROJECT_NAME: str = "转卖助手"
    VERSION: str = "0.2.0"
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
    DINGTALK_WEBHOOK_URL: str = "https://oapi.dingtalk.com/robot/send?access_token=db4f47b563c55d361189e74de38717b6e48b3edcbd0a0e6f0df8be306d42111e"
    DINGTALK_SECRET: str = "SEC9ff09c7fd652e3cd8a407bf55b06c8e727c161f8d5458b0a8c06fa752fc44280"

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

    # Selection / profit margin defaults (CNY, unitless ratio).
    # Used in product_scoring._score_profit_margin. Can be overridden per
    # category later; start with a single-SKU baseline.
    SELECTION_LOGISTICS_COST: float = 3.5
    SELECTION_LOSS_RATE: float = 1.05

    # Crawler proxy strategy
    # ----------------------
    # All selection crawlers (xianyu/pdd/1688/xhs) MUST use this short-term
    # proxy URL — never an operation account's long-term proxy. This keeps
    # crawler traffic on a separate IP pool from the operation accounts
    # that handle listings/orders, so a crawler ban can't cascade to the
    # money-earning accounts.
    #
    # Format: 'qgshort:KEY:PWD' (青果短效), resolved by proxy_service.
    # Empty string disables proxying (dangerous in production).
    SELECTION_CRAWLER_PROXY_URL: str = "qgshort:S5NVQC4A:3AA4CD8F25C2"

    # Hard cap per platform for crawler searches within a rolling hour.
    # Redis-backed; shared across Celery workers. Tune down if a platform
    # starts flagging us.
    SELECTION_SEARCH_RATE_LIMIT_PER_HOUR: int = 40

    # ─── 合规硬性规定 (see app.services.compliance) ────────────────────
    # These four settings codify non-negotiable operational/legal
    # constraints. Lowering them is fine; raising them requires a review
    # of the compliance policy.
    #
    # 1. 同一平台最小调用间隔（秒）。低于 60 即违反"每分钟不超过 1 次"。
    COMPLIANCE_MIN_INTERVAL_SECONDS: int = 60
    # 2. 间隔通过后的随机额外 jitter 范围（秒）。让流量不呈整分钟节拍。
    COMPLIANCE_JITTER_MIN_SECONDS: float = 5.0
    COMPLIANCE_JITTER_MAX_SECONDS: float = 25.0
    # 3. 类人活跃时段（北京时间，含起、不含止；支持跨午夜窗口）。
    #    "8-2" 表示 08:00 到次日 02:00（跨午夜）。调度任务只在此窗口
    #    内执行；用户手动触发的 instant_search 不受限。
    COMPLIANCE_ACTIVE_HOURS_BEIJING: str = "8-2"
    # 4. 商品库硬上限。enforce_product_cap 按 last_crawled_at FIFO 淘汰。
    PRODUCT_LIBRARY_CAP: int = 100_000

    # ─── PDD APP worker（家里 Windows）认证 ────────────────────────────────
    # 见 docs/PDD-自建采集-roadmap.md。家里 worker 通过 HTTPS 长轮询 backend
    # 拉任务/推结果，要带这个 Bearer token。生产环境务必从 .env 覆盖。
    PDD_WORKER_TOKEN: str = "change-me-pdd-worker-token"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
