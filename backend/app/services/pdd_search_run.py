"""PDD 采集任务历史落库 + Ops 查询服务。

写入：persist_search_run() —— 在 search 任务跑完那一刻调一次，把结果快照
落到 pdd_search_runs。设计成"绝不抛异常"（落库失败只记日志），这样它接在
调度主流程后面也不会因为一次写库失败就把整波采集搞挂。

查询：list_runs() 分页流水、summary() 看板聚合，给前端 Ops 面板用。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.pdd_run import PddSearchRun

logger = logging.getLogger(__name__)


async def persist_search_run(
    *,
    status: str,
    keyword_text: str,
    task_id: str | None = None,
    source: str = "lib",
    keyword_id: str | None = None,
    category_name: str | None = None,
    mode: str | None = None,
    items_count: int = 0,
    price_min: float | None = None,
    price_median: float | None = None,
    risk_signals: list | None = None,
    device_serial: str | None = None,
    account_name: str | None = None,
    elapsed_ms: int | None = None,
    priority: int | None = None,
    error: str | None = None,
) -> None:
    """落一行任务历史。失败只记日志，绝不向上抛（不阻断采集主流程）。"""
    try:
        async with AsyncSessionLocal() as db:
            run = PddSearchRun(
                task_id=task_id,
                source=source,
                keyword_id=keyword_id,
                keyword_text=keyword_text[:128],
                category_name=category_name,
                mode=mode,
                status=status,
                items_count=items_count or 0,
                price_min=price_min,
                price_median=price_median,
                risk_signals=risk_signals or None,
                device_serial=device_serial,
                account_name=account_name,
                elapsed_ms=elapsed_ms,
                priority=priority,
                error=(error[:2000] if error else None),
            )
            db.add(run)
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — 落库失败不能影响采集
        logger.warning(f"persist_search_run failed (status={status} kw='{keyword_text}'): {exc}")


def _row_to_dict(r: PddSearchRun) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "task_id": r.task_id,
        "source": r.source,
        "keyword_id": str(r.keyword_id) if r.keyword_id else None,
        "keyword_text": r.keyword_text,
        "category_name": r.category_name,
        "mode": r.mode,
        "status": r.status,
        "items_count": r.items_count,
        "price_min": r.price_min,
        "price_median": r.price_median,
        "risk_signals": r.risk_signals or [],
        "device_serial": r.device_serial,
        "account_name": r.account_name,
        "elapsed_ms": r.elapsed_ms,
        "priority": r.priority,
        "error": r.error,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


async def list_runs(
    db: AsyncSession,
    *,
    status: str | None = None,
    keyword: str | None = None,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """分页查询任务流水，按时间倒序。"""
    conditions = []
    if status:
        conditions.append(PddSearchRun.status == status)
    if source:
        conditions.append(PddSearchRun.source == source)
    if keyword:
        conditions.append(PddSearchRun.keyword_text.ilike(f"%{keyword}%"))

    count_stmt = select(func.count()).select_from(PddSearchRun)
    list_stmt = select(PddSearchRun)
    for c in conditions:
        count_stmt = count_stmt.where(c)
        list_stmt = list_stmt.where(c)

    total = (await db.execute(count_stmt)).scalar_one()
    list_stmt = (
        list_stmt.order_by(PddSearchRun.created_at.desc())
        .limit(min(limit, 200))
        .offset(offset)
    )
    rows = (await db.execute(list_stmt)).scalars().all()
    return {
        "total": total,
        "items": [_row_to_dict(r) for r in rows],
    }


async def summary(db: AsyncSession, *, recent_limit: int = 15) -> dict[str, Any]:
    """看板聚合：今日各状态计数、近 7 天趋势、最近流水、近期风控。"""
    now = datetime.now(timezone.utc)
    today_start = now - timedelta(hours=24)
    week_start = (now - timedelta(days=6)).date()

    # ── 今日（近 24h）按状态计数 ──────────────────────────────
    today_stmt = (
        select(PddSearchRun.status, func.count(), func.coalesce(func.sum(PddSearchRun.items_count), 0))
        .where(PddSearchRun.created_at >= today_start)
        .group_by(PddSearchRun.status)
    )
    today_rows = (await db.execute(today_stmt)).all()
    today: dict[str, int] = {}
    items_total = 0
    for st, cnt, items in today_rows:
        today[st] = cnt
        items_total += int(items or 0)
    today_total = sum(today.values())
    ok_like = today.get("ok", 0) + today.get("partial", 0)
    success_rate = round(ok_like / today_total * 100, 1) if today_total else None

    # ── 近 7 天按天 × 状态趋势 ────────────────────────────────
    day_col = func.date(PddSearchRun.created_at)
    trend_stmt = (
        select(day_col.label("d"), PddSearchRun.status, func.count())
        .where(func.date(PddSearchRun.created_at) >= week_start)
        .group_by("d", PddSearchRun.status)
        .order_by("d")
    )
    trend_rows = (await db.execute(trend_stmt)).all()
    trend_map: dict[str, dict[str, int]] = {}
    for d, st, cnt in trend_rows:
        ds = d.isoformat() if hasattr(d, "isoformat") else str(d)
        bucket = trend_map.setdefault(ds, {"date": ds, "total": 0, "ok": 0, "risk_blocked": 0, "failed": 0, "empty": 0})
        bucket["total"] += cnt
        if st in bucket:
            bucket[st] += cnt
    trend = sorted(trend_map.values(), key=lambda x: x["date"])

    # ── 最近任务流水 ─────────────────────────────────────────
    recent_stmt = (
        select(PddSearchRun)
        .order_by(PddSearchRun.created_at.desc())
        .limit(recent_limit)
    )
    recent = [_row_to_dict(r) for r in (await db.execute(recent_stmt)).scalars().all()]

    # ── 近 24h 风控命中（需要重点关注）───────────────────────
    risk_stmt = (
        select(PddSearchRun)
        .where(PddSearchRun.status == "risk_blocked")
        .where(PddSearchRun.created_at >= today_start)
        .order_by(PddSearchRun.created_at.desc())
        .limit(10)
    )
    recent_risk = [_row_to_dict(r) for r in (await db.execute(risk_stmt)).scalars().all()]

    return {
        "today": {
            "total": today_total,
            "by_status": today,
            "items_total": items_total,
            "success_rate": success_rate,
            "risk_blocked": today.get("risk_blocked", 0),
        },
        "trend": trend,
        "recent": recent,
        "recent_risk": recent_risk,
    }
