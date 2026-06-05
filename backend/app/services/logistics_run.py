"""「查快递」拟人行为事件落库 + 查询服务（roadmap §11.4，与 xianyu_search_run 对称）。

写入：persist_logistics_run() —— worker 每次查物流执行后经 POST /pdd-worker/logistics 调一次。
设计成"绝不抛异常"（落库失败只记日志），不阻断 worker 主流程。

查询：list_logistics_runs() 给「任务记录」抽屉合并展示。归一成与 PDD/闲鱼 run
同构的行（platform='logistics'），关键词列借位展示触发点+结果的中文说明。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.logistics_run import LogisticsRun

logger = logging.getLogger(__name__)

# 触发点 → 中文（借「关键词」列展示，让流水一眼能看懂）
_TRIGGER_LABEL = {"A": "burst结尾", "B": "静默期"}


async def persist_logistics_run(
    *,
    trigger: str,
    status: str,
    account_name: str | None = None,
    device_serial: str | None = None,
    elapsed_ms: int | None = None,
    note: str | None = None,
) -> None:
    """落一行查快递事件。失败只记日志，绝不向上抛。"""
    try:
        async with AsyncSessionLocal() as db:
            db.add(LogisticsRun(
                trigger=(trigger or "A")[:8],
                status=(status or "nav_failed")[:16],
                account_name=account_name,
                device_serial=device_serial,
                elapsed_ms=elapsed_ms,
                note=(note[:2000] if note else None),
            ))
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — 落库失败不能影响 worker
        logger.warning(f"persist_logistics_run failed (trigger={trigger} status={status}): {exc}")


def row_to_dict(r: LogisticsRun) -> dict[str, Any]:
    """归一成与 PDD/闲鱼 run 同构的「任务记录」行（platform='logistics'）。

    没有 keyword/品类/价格语义，关键词列借位展示「查快递·<触发点>」，
    来源列借位展示触发点（A/B）。
    """
    trig = _TRIGGER_LABEL.get(r.trigger, r.trigger or "")
    return {
        "id": str(r.id),
        "platform": "logistics",
        "task_id": None,
        "source": r.trigger,          # A / B，前端映射成"结尾/静默"
        "keyword_id": None,
        "keyword_text": f"查快递·{trig}" if trig else "查快递",
        "category_name": None,
        "mode": None,
        "status": r.status,            # viewed / empty / nav_failed
        "items_count": 0,
        "account_name": r.account_name,
        "device_serial": r.device_serial,
        "elapsed_ms": r.elapsed_ms,
        "priority": None,
        "error": r.note,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


async def list_logistics_runs(
    db: AsyncSession,
    *,
    status: str | None = None,
    keyword: str | None = None,  # 仅为合并接口签名对齐，查快递无关键词，传了也忽略
    limit: int = 50,
) -> list[dict[str, Any]]:
    """取最近的查快递事件（不分页，给合并接口做归并源；上限 limit）。"""
    if keyword:
        # 查快递没有真实关键词，关键词过滤时直接返回空，避免把它混进关键词搜索结果
        return []
    stmt = select(LogisticsRun)
    if status:
        stmt = stmt.where(LogisticsRun.status == status)
    stmt = stmt.order_by(LogisticsRun.created_at.desc()).limit(min(limit, 400))
    rows = (await db.execute(stmt)).scalars().all()
    return [row_to_dict(r) for r in rows]


async def count_logistics_runs(
    db: AsyncSession,
    *,
    status: str | None = None,
    keyword: str | None = None,
) -> int:
    if keyword:
        return 0
    stmt = select(func.count()).select_from(LogisticsRun)
    if status:
        stmt = stmt.where(LogisticsRun.status == status)
    return (await db.execute(stmt)).scalar_one()
