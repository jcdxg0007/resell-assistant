"""PDD APP worker 运行时调度配置（前端可改 → DB → worker 拉取热更新）。

这组参数原本写死在 worker 端 `.env`（home Windows），改一次要远程桌面进
去改文件 + 重启 worker。现在搬到 backend：

  前端 PUT /api/v1/pdd-worker-config/  → 写 SystemConfig (一行 JSON)
  worker GET /api/v1/pdd-worker/runtime-config（每个心跳周期拉一次）→ 热更新

存储复用现成的 `system_configs` KV 表，key 固定 ``pdd_worker_runtime_config``，
value 是整个配置的 JSON 字符串（value_type='json'）。不新建表、无 migration。

worker 端默认值仍来自其 `.env`；DB 里**只存被前端显式改过的覆盖项**，
worker 把拉到的值盖在本地默认上。DB 没有这行（从没改过）时，
get_runtime_config 返回纯默认值，worker 行为与改造前完全一致（向后兼容）。
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system import SystemConfig

CONFIG_KEY = "pdd_worker_runtime_config"

# 全部可调参数的默认值。必须与 worker 端 .env 默认保持一致
# （worker/pdd_app_worker/.env.example + main.py 的 os.environ.get 默认）。
DEFAULT_RUNTIME_CONFIG: dict[str, Any] = {
    "burst_size_min": 3,
    "burst_size_max": 5,
    "intra_burst_gap_seconds_min": 5.0,
    "intra_burst_gap_seconds_max": 30.0,
    "inter_burst_gap_minutes_min": 5.0,
    "inter_burst_gap_minutes_max": 30.0,
    "burst_idle_timeout_seconds_min": 45.0,
    "burst_idle_timeout_seconds_max": 180.0,
    "daily_search_quota": 30,
    "emergency_priority_threshold": 8,
    "humanize_pace": 1.0,
}

# 每个参数的类型 / 范围 / 中文标签 / 分组 / 说明。
# 前端用它渲染表单 + 做客户端校验；后端 update 时按它做服务端校验。
# pair: 该字段是某个 [min,max] 区间的一端，update 时强制 *_min <= *_max。
PARAM_SPECS: dict[str, dict[str, Any]] = {
    "humanize_pace": {
        "type": "float", "min": 0.3, "max": 1.0, "step": 0.05,
        "label": "浏览节奏因子", "group": "节奏",
        "help": "1.0=原始节奏；0.7=整体快 30%。只压浏览/停留/思考间隔，"
                "不动 IME 输入、冷启动、burst 间静默。越小越快、越像爬虫。",
    },
    "burst_size_min": {
        "type": "int", "min": 1, "max": 10,
        "label": "单波最少搜索次数", "group": "阵发",
        "pair": "burst_size_max",
        "help": "一个 burst 内连搜几次的下限。真人一波查 3-5 个相关词。",
    },
    "burst_size_max": {
        "type": "int", "min": 1, "max": 15,
        "label": "单波最多搜索次数", "group": "阵发",
        "pair_min": "burst_size_min",
        "help": "一个 burst 内连搜几次的上限。",
    },
    "intra_burst_gap_seconds_min": {
        "type": "float", "min": 0.0, "max": 120.0, "step": 1.0,
        "label": "波内间隔下限(秒)", "group": "阵发",
        "pair": "intra_burst_gap_seconds_max",
        "help": "burst 内相邻两次搜索的最短间隔。模拟'想下个关键词'的思考时间。",
    },
    "intra_burst_gap_seconds_max": {
        "type": "float", "min": 0.0, "max": 300.0, "step": 1.0,
        "label": "波内间隔上限(秒)", "group": "阵发",
        "pair_min": "intra_burst_gap_seconds_min",
        "help": "burst 内相邻两次搜索的最长间隔。",
    },
    "inter_burst_gap_minutes_min": {
        "type": "float", "min": 0.0, "max": 120.0, "step": 1.0,
        "label": "波间静默下限(分)", "group": "阵发",
        "pair": "inter_burst_gap_minutes_max",
        "help": "两个 burst 之间的最短静默。这段时间 PDD 退后台，"
                "让画像像'偶尔打开 APP 的真用户'。调太短风险最大。",
    },
    "inter_burst_gap_minutes_max": {
        "type": "float", "min": 0.0, "max": 240.0, "step": 1.0,
        "label": "波间静默上限(分)", "group": "阵发",
        "pair_min": "inter_burst_gap_minutes_min",
        "help": "两个 burst 之间的最长静默。",
    },
    "burst_idle_timeout_seconds_min": {
        "type": "float", "min": 10.0, "max": 600.0, "step": 5.0,
        "label": "波收尾闲置下限(秒)", "group": "阵发",
        "pair": "burst_idle_timeout_seconds_max",
        "help": "burst 内最后一搜后多久没新任务就强制退后台。每波随机抽一值，"
                "避免'搜完正好 60s 退桌面'成为指纹。",
    },
    "burst_idle_timeout_seconds_max": {
        "type": "float", "min": 10.0, "max": 1200.0, "step": 5.0,
        "label": "波收尾闲置上限(秒)", "group": "阵发",
        "pair_min": "burst_idle_timeout_seconds_min",
        "help": "burst 收尾闲置超时上限。不宜超过波间静默下限(分×60)。",
    },
    "daily_search_quota": {
        "type": "int", "min": 1, "max": 500,
        "label": "每日搜索硬上限", "group": "配额",
        "help": "超过后所有 search 任务立即失败(daily_quota_exhausted)，"
                "UTC 0 点重置。新号建议 30，稳定一周后放 50-100。",
    },
    "emergency_priority_threshold": {
        "type": "int", "min": 1, "max": 100,
        "label": "紧急任务优先级阈值", "group": "配额",
        "help": "priority ≥ 此值的任务跳过波间静默、插队首。普通任务 priority=1。"
                "⚠ 必须与 backend pdd_app_queue 的同名配置一致，一般别改。",
    },
}


def _coerce(key: str, value: Any) -> Any:
    """按 spec 把值转成正确类型；类型不对抛 ValueError。"""
    spec = PARAM_SPECS[key]
    try:
        if spec["type"] == "int":
            return int(value)
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"参数 {key} 期望 {spec['type']}，收到 {value!r}")


def validate_patch(patch: dict[str, Any], merged: dict[str, Any]) -> dict[str, Any]:
    """校验前端提交的部分更新 patch。

    :param patch: 本次要改的字段（可只含部分 key）
    :param merged: patch 合并到现有配置后的完整配置（用于 pair 跨字段校验）
    :return: 类型规整后的 patch
    :raises ValueError: 未知 key / 类型错 / 越界 / min>max
    """
    cleaned: dict[str, Any] = {}
    for key, raw in patch.items():
        if key not in PARAM_SPECS:
            raise ValueError(f"未知参数: {key}")
        val = _coerce(key, raw)
        spec = PARAM_SPECS[key]
        if val < spec["min"] or val > spec["max"]:
            raise ValueError(
                f"参数 {key}={val} 越界，允许范围 [{spec['min']}, {spec['max']}]"
            )
        cleaned[key] = val
        merged[key] = val

    # 跨字段：所有 [min, max] 区间强制 min <= max
    for key, spec in PARAM_SPECS.items():
        hi_key = spec.get("pair")
        if hi_key and merged.get(key) is not None and merged.get(hi_key) is not None:
            if merged[key] > merged[hi_key]:
                raise ValueError(
                    f"{key}({merged[key]}) 不能大于 {hi_key}({merged[hi_key]})"
                )
    return cleaned


async def get_runtime_config(db: AsyncSession) -> dict[str, Any]:
    """读完整运行时配置：DB 覆盖项盖在默认值上。DB 没这行就返回纯默认。"""
    row = (
        await db.execute(select(SystemConfig).where(SystemConfig.key == CONFIG_KEY))
    ).scalar_one_or_none()
    config = dict(DEFAULT_RUNTIME_CONFIG)
    if row and row.value:
        try:
            stored = json.loads(row.value)
            for k, v in stored.items():
                if k in DEFAULT_RUNTIME_CONFIG:
                    config[k] = v
        except (json.JSONDecodeError, TypeError):
            pass
    return config


async def update_runtime_config(
    db: AsyncSession, patch: dict[str, Any]
) -> dict[str, Any]:
    """校验并合并一个 patch 到 DB，返回更新后的完整配置。"""
    current = await get_runtime_config(db)
    merged = dict(current)
    validate_patch(patch, merged)  # 原地校验 + 写入 merged，越界则抛

    row = (
        await db.execute(select(SystemConfig).where(SystemConfig.key == CONFIG_KEY))
    ).scalar_one_or_none()
    payload = json.dumps(merged, ensure_ascii=False)
    if row:
        row.value = payload
        row.value_type = "json"
    else:
        db.add(SystemConfig(
            key=CONFIG_KEY,
            value=payload,
            value_type="json",
            description="PDD APP worker 运行时调度参数（前端可改，worker 拉取热更新）",
        ))
    await db.commit()
    return merged


def specs_for_frontend() -> dict[str, Any]:
    """给前端的表单元数据：每个参数的范围/标签/默认/分组。"""
    return {
        "params": PARAM_SPECS,
        "defaults": DEFAULT_RUNTIME_CONFIG,
        "groups": ["节奏", "阵发", "配额"],
    }
