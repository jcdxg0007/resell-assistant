from fastapi import APIRouter, Depends
from app.api.deps import get_current_user
from app.models.system import User
from app.services.ai_ops.daily_check import run_daily_self_check, run_daily_report

router = APIRouter()


@router.get("/self-check", summary="触发AI自检")
async def trigger_self_check(user: User = Depends(get_current_user)):
    report = await run_daily_self_check()
    return report


@router.get("/daily-report", summary="触发运营日报")
async def trigger_daily_report(user: User = Depends(get_current_user)):
    report = await run_daily_report()
    return report


@router.get("/suggestions", summary="获取AI运营建议")
async def get_suggestions(user: User = Depends(get_current_user)):
    report = await run_daily_self_check()
    from app.services.ai_ops.daily_check import _generate_ai_suggestions
    suggestions = await _generate_ai_suggestions(report)
    return {"suggestions": suggestions, "report_summary": report.get("orders_24h", {})}
