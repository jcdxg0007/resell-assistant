from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "resell_assistant",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    result_expires=3600,
)

celery_app.conf.beat_schedule = {
    # === Order Detection & Fulfillment ===
    "detect-new-orders": {
        "task": "app.tasks.orders.detect_new_orders",
        "schedule": 180.0,  # 3 min
    },
    "sync-logistics": {
        "task": "app.tasks.orders.sync_logistics",
        "schedule": 1800.0,  # 30 min
    },
    "check-refund-status": {
        "task": "app.tasks.orders.check_refund_status",
        "schedule": 3600.0,  # 1 hour
    },

    # === Selection Engine ===
    # Schedule design:
    # - price_monitor / discovery / xhs tasks ALL run inside
    #   COMPLIANCE_ACTIVE_HOURS_BEIJING (08:00 - 次日 02:00).
    # - price_monitor (50 keyword × ~75s gate = ~60min per round) is
    #   deliberately spaced 4h apart AND offset from discovery (hour
    #   9/21) so the two never fight for the xianyu pacing slot.
    "xianyu-price-monitor": {
        "task": "app.tasks.selection.xianyu_price_monitor",
        "schedule": crontab(minute=0, hour="10,14,18,22"),
    },
    "xianyu-product-discovery": {
        "task": "app.tasks.selection.xianyu_product_discovery",
        "schedule": crontab(minute=0, hour="9,21"),
    },
    "xhs-hot-article-scan": {
        "task": "app.tasks.selection.xhs_hot_article_scan",
        "schedule": crontab(minute=0, hour="10,22"),
    },
    "xhs-topic-trending": {
        "task": "app.tasks.selection.xhs_topic_trending",
        "schedule": crontab(minute=30, hour=10),
    },
    "source-stock-check": {
        "task": "app.tasks.selection.source_stock_check",
        "schedule": crontab(minute=0, hour=8),
    },

    # === Publishing ===
    "batch-refresh-listings": {
        "task": "app.tasks.publish.batch_refresh_listings",
        "schedule": crontab(minute=0, hour="9,13,19"),
    },
    "reset-daily-counts": {
        "task": "app.tasks.publish.reset_daily_counts",
        "schedule": crontab(minute=0, hour=0),
    },
    "listing-health-check": {
        "task": "app.tasks.publish.listing_health_check",
        "schedule": crontab(minute=0, hour="*/6"),
    },

    # === Customer & AI Ops ===
    "check-customer-messages": {
        "task": "app.tasks.customer.check_messages",
        "schedule": 180.0,
    },
    "daily-ai-check": {
        "task": "app.tasks.ai_ops.daily_self_check",
        "schedule": crontab(minute=0, hour=6),
    },
    "daily-ops-report": {
        "task": "app.tasks.ai_ops.daily_report",
        "schedule": crontab(minute=0, hour=22),
    },

    # === Session Monitoring ===
    "session-health-check": {
        "task": "app.tasks.session.check_all_sessions",
        "schedule": crontab(minute=0, hour="*/2"),
    },

    # === Compliance ===
    # Keeps the product library ≤ PRODUCT_LIBRARY_CAP (100k by default)
    # via FIFO rotation on last_crawled_at. Runs once a day in low-
    # traffic hours. See app.services.compliance for the four hard
    # rules this supports.
    "enforce-product-cap": {
        "task": "app.tasks.compliance.enforce_product_cap",
        "schedule": crontab(minute=15, hour=4),
    },

    # === PDD 全自动跑批 ===
    # 每 3 分钟唤醒一次「自带闸门」的 tick：是否真派由任务内部按 开关/暂停/
    # 活跃时段/随机下次时刻/配额/worker在线 判断。固定高频唤醒，实际派词时刻
    # 每天随机错峰，避免「每天 X 点准时上线」的机器指纹。频率/时段/词数前端可调。
    "pdd-auto-batch-tick": {
        "task": "app.tasks.pdd.auto_batch_tick",
        "schedule": crontab(minute="*/3"),
    },
}

celery_app.autodiscover_tasks([
    "app.tasks.orders",
    "app.tasks.selection",
    "app.tasks.publish",
    "app.tasks.customer",
    "app.tasks.ai_ops",
    "app.tasks.session",
    "app.tasks.compliance",
    "app.tasks.pdd",
])
