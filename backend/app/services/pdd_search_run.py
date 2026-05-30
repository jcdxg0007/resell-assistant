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

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal
from app.models.pdd_run import PddSearchRun
from app.models.selection import Keyword

logger = logging.getLogger(__name__)

# 用户在中国，「今日」按东八区日界算（数据库 created_at 是带时区的 UTC，
# 与带时区的 day_start 直接比较即可）。
_CN_TZ = timezone(timedelta(hours=8))
_PDD_KW_FILTER = text("selection_keywords.target_platforms::jsonb @> '[\"pdd\"]'::jsonb")


def _cn_day_start() -> datetime:
    now_cn = datetime.now(_CN_TZ)
    return now_cn.replace(hour=0, minute=0, second=0, microsecond=0)


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
    items: list | None = None,
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
                items=items or None,
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


async def console_data(db: AsyncSession) -> dict[str, Any]:
    """「今日搜索任务」控制台：今日统计 + 待采集池 + 已采集池 + 商品量范围。

    - 今日：按东八区日界
    - 待采集池：词库里 pdd_safe+is_active+schedule_enabled 且 'pdd'∈平台、
      但今天还没跑过（按 keyword_id 判定）的词，按最久没跑优先
    - 已采集池：今天跑过的词（按 keyword_text 去重，取每词今天最新一条）
    """
    day_start = _cn_day_start()

    # ── 今日统计 ──
    stat_stmt = (
        select(PddSearchRun.status, func.count(),
               func.coalesce(func.sum(PddSearchRun.items_count), 0))
        .where(PddSearchRun.created_at >= day_start)
        .group_by(PddSearchRun.status)
    )
    by_status: dict[str, int] = {}
    items_total = 0
    for st, cnt, items in (await db.execute(stat_stmt)).all():
        by_status[st] = cnt
        items_total += int(items or 0)
    today_total = sum(by_status.values())
    ok_like = by_status.get("ok", 0) + by_status.get("partial", 0)
    success_rate = round(ok_like / today_total * 100, 1) if today_total else None

    # ── 今日 PDD 采集（按 keyword_text 去重取最新一条）──
    runs_today_stmt = (
        select(PddSearchRun)
        .where(PddSearchRun.created_at >= day_start)
        .order_by(PddSearchRun.created_at.desc())
    )
    pdd_map: dict[str, dict[str, Any]] = {}  # text -> 该词今日最新 PDD 记录
    done_pdd_keyword_ids: set[str] = set()
    for r in (await db.execute(runs_today_stmt)).scalars().all():
        if r.keyword_id:
            done_pdd_keyword_ids.add(str(r.keyword_id))
        if r.keyword_text in pdd_map:
            continue
        pdd_map[r.keyword_text] = {
            "status": r.status,
            "items_count": r.items_count,
            "last_at": r.created_at.isoformat() if r.created_at else None,
            "ts": r.created_at,
            "run_id": str(r.id),
            "category_name": r.category_name,
        }

    # ── 今日 闲鱼 采集（按 category=关键词聚合 today 入库商品）──
    from app.models.product import Product
    xy_rows = (await db.execute(
        select(Product.category, func.count(), func.max(Product.created_at))
        .where(Product.source_platform == "xianyu")
        .where(Product.category.isnot(None))
        .where(Product.created_at >= day_start)
        .group_by(Product.category)
    )).all()
    xianyu_map: dict[str, dict[str, Any]] = {}  # text -> {count, last_at, ts}
    for cat, cnt, ts in xy_rows:
        xianyu_map[cat] = {
            "items_count": int(cnt or 0),
            "last_at": ts.isoformat() if ts else None,
            "ts": ts,
        }
    xianyu_done_texts: set[str] = set(xianyu_map.keys())

    # ── 已采集池：两边并到一行，各带平台标签 + 时间 ──
    collected: list[dict[str, Any]] = []
    for text_ in set(pdd_map) | set(xianyu_map):
        p = pdd_map.get(text_)
        x = xianyu_map.get(text_)
        ts_list = [t for t in (p["ts"] if p else None, x["ts"] if x else None) if t]
        last_ts = max(ts_list) if ts_list else None
        collected.append({
            "keyword_text": text_,
            "category_name": (p or {}).get("category_name"),
            "run_id": (p or {}).get("run_id"),
            "pdd": {
                "status": p["status"], "items_count": p["items_count"],
                "last_at": p["last_at"],
            } if p else None,
            "xianyu": {
                "items_count": x["items_count"], "last_at": x["last_at"],
            } if x else None,
            "last_run_at": last_ts.isoformat() if last_ts else None,
            "_sort_ts": last_ts,
        })
    collected.sort(key=lambda c: c["_sort_ts"] or day_start, reverse=True)
    for c in collected:
        c.pop("_sort_ts", None)

    # ── 待采集池（词库里今天还没采的词，按平台标注 pending）──
    # 不再只看 PDD：闲鱼用 xianyu_safe 单独控制，所以两边都拉出来。
    pending_stmt = (
        select(Keyword)
        .options(selectinload(Keyword.category))
        .where(Keyword.is_active.is_(True))
        .where(Keyword.schedule_enabled.is_(True))
        .order_by(Keyword.pdd_last_searched_at.asc().nullsfirst(), Keyword.text)
        .limit(500)
    )
    # 批量任务的预计开始时刻（开始任务时写入 Redis），算每个待采集词的预估倒计时
    from app.services.pdd_app_queue import get_batch_plan
    plan = await get_batch_plan()
    import time as _time
    now_ts = _time.time()

    def _eta_sec(ts: float | None) -> int | None:
        if not ts:
            return None
        return max(0, int(ts - now_ts))

    pending: list[dict[str, Any]] = []
    for k in (await db.execute(pending_stmt)).scalars().all():
        tp = k.target_platforms or []
        pdd_enabled = bool(k.pdd_safe) and ("pdd" in tp)
        xianyu_enabled = bool(k.xianyu_safe)
        pdd_pending = pdd_enabled and str(k.id) not in done_pdd_keyword_ids
        xianyu_pending = xianyu_enabled and k.text not in xianyu_done_texts
        if not (pdd_pending or xianyu_pending):
            continue
        p = plan.get(k.text) or {}
        pending.append({
            "keyword_id": str(k.id),
            "text": k.text,
            "category_name": k.category.name if k.category else None,
            "pdd_mode": k.pdd_mode,
            "pdd_pending": pdd_pending,
            "xianyu_pending": xianyu_pending,
            "pdd_eta_sec": _eta_sec(p.get("pdd")),
            "xianyu_eta_sec": _eta_sec(p.get("xianyu")),
        })

    # ── 今日风控命中（重点关注）──
    risk_stmt = (
        select(PddSearchRun)
        .where(PddSearchRun.created_at >= day_start)
        .where(PddSearchRun.status == "risk_blocked")
        .order_by(PddSearchRun.created_at.desc())
        .limit(10)
    )
    recent_risk = [
        {
            "id": str(r.id),
            "keyword_text": r.keyword_text,
            "risk_signals": r.risk_signals or [],
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in (await db.execute(risk_stmt)).scalars().all()
    ]

    # ── 商品量范围（来自 worker 运行时配置）──
    from app.services.pdd_worker_config import get_runtime_config
    cfg = await get_runtime_config(db)

    # ── 全自动跑批状态（前端「全自动采集」卡显示，PDD / 闲鱼 各一套）──
    from app.services.pdd_app_queue import get_auto_next_ts, get_xianyu_auto_next_ts

    def _ts_to_cn_iso(ts: float | None) -> str | None:
        return datetime.fromtimestamp(ts, tz=_CN_TZ).isoformat() if ts else None

    auto_next_at = _ts_to_cn_iso(await get_auto_next_ts())
    xianyu_auto_next_at = _ts_to_cn_iso(await get_xianyu_auto_next_ts())

    return {
        "stats": {
            "total": today_total,
            "items_total": items_total,
            "success_rate": success_rate,
            "risk_blocked": by_status.get("risk_blocked", 0),
        },
        "target_count_min": cfg.get("target_count_min"),
        "target_count_max": cfg.get("target_count_max"),
        "auto_batch_enabled": bool(cfg.get("auto_batch_enabled")),
        "auto_next_at": auto_next_at,
        "xianyu_auto_batch_enabled": bool(cfg.get("xianyu_auto_batch_enabled")),
        "xianyu_auto_next_at": xianyu_auto_next_at,
        "pending": pending,
        "collected": collected,
        "recent_risk": recent_risk,
    }


async def keyword_items(db: AsyncSession, keyword_text: str) -> dict[str, Any]:
    """取某关键词今天最新一条采集记录的逐条商品，给前端结果区展示。"""
    day_start = _cn_day_start()
    stmt = (
        select(PddSearchRun)
        .where(PddSearchRun.created_at >= day_start)
        .where(PddSearchRun.keyword_text == keyword_text)
        .order_by(PddSearchRun.created_at.desc())
        .limit(1)
    )
    r = (await db.execute(stmt)).scalar_one_or_none()
    if r is None:
        return {"keyword_text": keyword_text, "items": [], "found": False}
    return {
        "keyword_text": keyword_text,
        "found": True,
        "run_id": str(r.id),
        "status": r.status,
        "items_count": r.items_count,
        "price_min": r.price_min,
        "price_median": r.price_median,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "items": r.items or [],
    }


async def paginated_items(
    db: AsyncSession,
    keyword_text: str | None,
    *,
    page: int = 1,
    page_size: int = 10,
) -> dict[str, Any]:
    """给前端比价页结果区用：今日采集到的逐条商品，分页。

    - 给 keyword_text：取该词今天最新一条采集记录的逐条商品；
    - 不给：把今日每个词的最新一条记录的商品全合并（每条带上 keyword_text），
      和闲鱼侧"不选词就看全部"对齐。
    """
    day_start = _cn_day_start()
    flat: list[dict[str, Any]] = []
    if keyword_text:
        r = (await db.execute(
            select(PddSearchRun)
            .where(PddSearchRun.created_at >= day_start)
            .where(PddSearchRun.keyword_text == keyword_text)
            .order_by(PddSearchRun.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        for it in ((r.items if r else None) or []):
            d = dict(it)
            d.setdefault("keyword_text", keyword_text)
            flat.append(d)
    else:
        # 今日每个关键词的最新一条记录
        sub = (
            select(
                PddSearchRun.keyword_text,
                func.max(PddSearchRun.created_at).label("mx"),
            )
            .where(PddSearchRun.created_at >= day_start)
            .group_by(PddSearchRun.keyword_text)
            .subquery()
        )
        rows = (await db.execute(
            select(PddSearchRun)
            .join(
                sub,
                (PddSearchRun.keyword_text == sub.c.keyword_text)
                & (PddSearchRun.created_at == sub.c.mx),
            )
            .order_by(PddSearchRun.created_at.desc())
        )).scalars().all()
        for r in rows:
            for it in (r.items or []):
                d = dict(it)
                d.setdefault("keyword_text", r.keyword_text)
                flat.append(d)

    total = len(flat)
    start = max(0, (page - 1) * page_size)
    return {
        "keyword_text": keyword_text,
        "found": total > 0,
        "total": total,
        "page": page,
        "items": flat[start:start + page_size],
    }


async def clear_today(db: AsyncSession, keyword_text: str | None = None) -> int:
    """清空今日采集记录。keyword_text 给定则只清该词；否则清今日全部。返回删除条数。"""
    day_start = _cn_day_start()
    stmt = delete(PddSearchRun).where(PddSearchRun.created_at >= day_start)
    if keyword_text:
        stmt = stmt.where(PddSearchRun.keyword_text == keyword_text)
    res = await db.execute(stmt)
    await db.commit()
    return res.rowcount or 0


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
