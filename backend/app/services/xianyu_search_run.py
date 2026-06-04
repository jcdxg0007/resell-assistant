"""闲鱼采集任务历史落库 + 查询服务（与 pdd_search_run 对称）。

写入：persist_xianyu_run() —— 在 instant_search(platform='xianyu') 跑完调一次。
设计成"绝不抛异常"（落库失败只记日志），不阻断采集主流程。

查询：list_xianyu_runs() 分页流水，给「任务记录」抽屉合并展示。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.xianyu_run import XianyuSearchRun

logger = logging.getLogger(__name__)


async def persist_xianyu_run(
    *,
    status: str,
    keyword_text: str,
    source: str = "manual",
    keyword_id: str | None = None,
    category_name: str | None = None,
    items_count: int = 0,
    saved_count: int | None = None,
    risk_signals: list | None = None,
    elapsed_ms: int | None = None,
    error: str | None = None,
) -> None:
    """落一行闲鱼任务历史。失败只记日志，绝不向上抛。"""
    try:
        async with AsyncSessionLocal() as db:
            db.add(XianyuSearchRun(
                source=source,
                keyword_id=keyword_id,
                keyword_text=keyword_text[:128],
                category_name=category_name,
                status=status,
                items_count=items_count or 0,
                saved_count=saved_count,
                risk_signals=risk_signals or None,
                elapsed_ms=elapsed_ms,
                error=(error[:2000] if error else None),
            ))
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — 落库失败不能影响采集
        logger.warning(f"persist_xianyu_run failed (status={status} kw='{keyword_text}'): {exc}")


def row_to_dict(r: XianyuSearchRun) -> dict[str, Any]:
    """归一成与 PDD run 同构的「任务记录」行（platform='xianyu'）。"""
    return {
        "id": str(r.id),
        "platform": "xianyu",
        "task_id": None,
        "source": r.source,
        "keyword_id": str(r.keyword_id) if r.keyword_id else None,
        "keyword_text": r.keyword_text,
        "category_name": r.category_name,
        "mode": None,
        "status": r.status,
        "items_count": r.items_count,
        "account_name": None,
        "device_serial": None,
        "elapsed_ms": r.elapsed_ms,
        "priority": None,
        "error": r.error,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


async def list_xianyu_runs(
    db: AsyncSession,
    *,
    status: str | None = None,
    keyword: str | None = None,
    source: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """取最近的闲鱼任务流水（不分页，给合并接口做归并源；上限 limit）。"""
    stmt = select(XianyuSearchRun)
    if status:
        stmt = stmt.where(XianyuSearchRun.status == status)
    if source:
        stmt = stmt.where(XianyuSearchRun.source == source)
    if keyword:
        stmt = stmt.where(XianyuSearchRun.keyword_text.ilike(f"%{keyword}%"))
    stmt = stmt.order_by(XianyuSearchRun.created_at.desc()).limit(min(limit, 400))
    rows = (await db.execute(stmt)).scalars().all()
    return [row_to_dict(r) for r in rows]


async def count_xianyu_runs(
    db: AsyncSession,
    *,
    status: str | None = None,
    keyword: str | None = None,
    source: str | None = None,
) -> int:
    stmt = select(func.count()).select_from(XianyuSearchRun)
    if status:
        stmt = stmt.where(XianyuSearchRun.status == status)
    if source:
        stmt = stmt.where(XianyuSearchRun.source == source)
    if keyword:
        stmt = stmt.where(XianyuSearchRun.keyword_text.ilike(f"%{keyword}%"))
    return (await db.execute(stmt)).scalar_one()
