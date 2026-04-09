"""
AI Operations Center - Daily self-check & operations report.
"""
from datetime import datetime, timezone, timedelta

import httpx
from loguru import logger
from sqlalchemy import select, func

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.models.system import Account
from app.models.order import Order
from app.models.product import Product, ProductScore
from app.models.xianyu import XianyuListing
from app.services.notification import notification_service

settings = get_settings()


async def run_daily_self_check() -> dict:
    """
    Daily 06:00 AI self-check.
    Checks: account health, product status, crawl integrity, competition.
    """
    report = {
        "check_time": datetime.now(timezone.utc).isoformat(),
        "accounts": [],
        "products": {},
        "alerts": [],
    }

    async with AsyncSessionLocal() as db:
        # 1. Account health check
        accounts = (await db.execute(
            select(Account).where(Account.is_active == True)
        )).scalars().all()

        for acct in accounts:
            status = "正常"
            if acct.health_score < 70:
                status = "需关注"
                report["alerts"].append(f"账号 {acct.account_name} 健康度{acct.health_score}，低于阈值")
            if acct.lifecycle_stage == "suspended":
                status = "已暂停"

            report["accounts"].append({
                "name": acct.account_name,
                "platform": acct.platform,
                "health": acct.health_score,
                "stage": acct.lifecycle_stage,
                "status": status,
            })

        # 2. Product status
        total_products = (await db.execute(
            select(func.count()).where(Product.is_active == True)
        )).scalar() or 0

        active_listings = (await db.execute(
            select(func.count()).where(XianyuListing.status == "published")
        )).scalar() or 0

        high_score = (await db.execute(
            select(func.count())
            .where(ProductScore.total_score >= 80)
        )).scalar() or 0

        report["products"] = {
            "total_tracked": total_products,
            "active_listings": active_listings,
            "high_score_count": high_score,
        }

        # 3. Order summary (last 24h)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)

        orders_24h = (await db.execute(
            select(func.count()).where(Order.created_at >= yesterday)
        )).scalar() or 0

        revenue_24h = (await db.execute(
            select(func.sum(Order.sale_price)).where(
                Order.created_at >= yesterday,
                Order.status.in_(["purchased", "shipped", "delivered", "completed"]),
            )
        )).scalar() or 0

        profit_24h = (await db.execute(
            select(func.sum(Order.actual_profit)).where(
                Order.created_at >= yesterday,
                Order.status.in_(["purchased", "shipped", "delivered", "completed"]),
            )
        )).scalar() or 0

        error_orders = (await db.execute(
            select(func.count()).where(
                Order.status == "error",
                Order.created_at >= yesterday,
            )
        )).scalar() or 0

        if error_orders > 0:
            report["alerts"].append(f"过去24小时有{error_orders}个异常订单需处理")

        report["orders_24h"] = {
            "count": orders_24h,
            "revenue": round(revenue_24h, 2),
            "profit": round(profit_24h, 2),
            "errors": error_orders,
        }

    # Push self-check report
    alert_text = "\n".join(f"⚠️ {a}" for a in report["alerts"]) if report["alerts"] else "✅ 无异常"
    content = (
        f"**账号状态**: {len(report['accounts'])}个活跃\n"
        f"**在售商品**: {report['products']['active_listings']}个\n"
        f"**24h订单**: {report['orders_24h']['count']}单 ¥{report['orders_24h']['revenue']}\n"
        f"**24h利润**: ¥{report['orders_24h']['profit']}\n\n"
        f"**异常检查**:\n{alert_text}"
    )
    await notification_service.send_dingtalk(title="AI每日自检报告", content=content)

    return report


async def run_daily_report() -> dict:
    """
    Daily 22:00 operations report with AI-generated suggestions.
    """
    report = await run_daily_self_check()

    # Generate AI suggestions
    suggestions = await _generate_ai_suggestions(report)
    report["suggestions"] = suggestions

    # Push daily report
    suggestion_text = "\n".join(f"{i+1}. {s}" for i, s in enumerate(suggestions))
    content = (
        f"**今日数据**:\n"
        f"- 订单: {report['orders_24h']['count']}单\n"
        f"- 收入: ¥{report['orders_24h']['revenue']}\n"
        f"- 利润: ¥{report['orders_24h']['profit']}\n"
        f"- 高分选品: {report['products']['high_score_count']}个\n\n"
        f"**AI建议**:\n{suggestion_text or '暂无建议'}\n\n"
        f"[打开面板查看详情](http://localhost:3000/dashboard)"
    )
    await notification_service.notify(
        "AI运营日报",
        content,
        level="info",
    )

    return report


async def _generate_ai_suggestions(report: dict) -> list[str]:
    """Generate actionable suggestions based on report data."""
    suggestions = []

    # Rule-based suggestions
    if report.get("orders_24h", {}).get("errors", 0) > 0:
        suggestions.append(f"[订单] 有{report['orders_24h']['errors']}个异常订单待处理，请尽快查看")

    for acct in report.get("accounts", []):
        if acct.get("health", 100) < 70:
            suggestions.append(f"[风控] {acct['name']} 健康度偏低({acct['health']})，建议降低发布频率")

    if report.get("products", {}).get("high_score_count", 0) > 0:
        suggestions.append(f"[选品] 发现{report['products']['high_score_count']}个高分商品(≥80)，建议查看选品面板")

    # LLM-enhanced suggestions
    if settings.LLM_API_KEY:
        try:
            prompt = f"""基于以下电商运营数据，给出3条简短、可执行的运营建议:

24小时数据: {report.get('orders_24h', {})}
账号状态: {len(report.get('accounts', []))}个活跃
在售商品: {report.get('products', {}).get('active_listings', 0)}个
异常告警: {report.get('alerts', [])}

要求: 每条建议一行，带[类别]前缀，具体可执行，不超过30字"""

            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"{settings.LLM_API_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
                    json={
                        "model": settings.LLM_MODEL_LIGHT,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.5,
                        "max_tokens": 200,
                    },
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"].strip()
                for line in content.split("\n"):
                    line = line.strip()
                    if line and len(line) > 5:
                        suggestions.append(line)
        except Exception as e:
            logger.warning(f"AI suggestion generation failed: {e}")

    return suggestions[:8]
