from fastapi import APIRouter

from app.api.v1 import (
    auth, products, selection, xianyu, xiaohongshu, orders,
    accounts, ai_ops, customer, settings, pdd_worker, pdd_worker_config,
    pdd_runs, pdd_keywords,
)

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["认证"])
api_router.include_router(products.router, prefix="/products", tags=["商品"])
api_router.include_router(selection.router, prefix="/selection", tags=["选品"])
api_router.include_router(xianyu.router, prefix="/xianyu", tags=["闲鱼"])
api_router.include_router(xiaohongshu.router, prefix="/xhs", tags=["小红书"])
api_router.include_router(orders.router, prefix="/orders", tags=["订单"])
api_router.include_router(accounts.router, prefix="/accounts", tags=["账号"])
api_router.include_router(ai_ops.router, prefix="/ai-ops", tags=["AI运营"])
api_router.include_router(customer.router, prefix="/customer", tags=["客服"])
api_router.include_router(settings.router, prefix="/settings", tags=["设置"])
api_router.include_router(pdd_worker.router, prefix="/pdd-worker", tags=["PDD APP Worker"])
api_router.include_router(pdd_worker_config.router, prefix="/pdd-worker-config", tags=["PDD Worker 调度配置"])
api_router.include_router(pdd_runs.router, prefix="/pdd-runs", tags=["PDD 采集历史"])
api_router.include_router(pdd_keywords.router, prefix="/pdd-keywords", tags=["PDD 词库管理"])
