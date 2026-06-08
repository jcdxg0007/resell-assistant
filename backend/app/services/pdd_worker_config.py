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
    "target_count_min": 8,
    "target_count_max": 20,
    # ── 深度采集：deep 关键词搜完进 K 个详情页被动收割（K 在 [min,max] 随机取）──
    "deep_harvest_dips_min": 3,
    "deep_harvest_dips_max": 6,
    # ── 「查物流」拟人行为（worker 读，roadmap §11.4）──
    "logistics_browse_enabled": False,
    "logistics_browse_prob": 0.25,   # A：每个 burst 结尾触发概率
    "logistics_quiet_prob": 0.35,    # B：inter-burst 静默期中段触发概率
    # ── PDD 全自动跑批（backend celery beat 读，worker 不用）──
    "auto_batch_enabled": False,
    "auto_both_platforms": False,  # 已弃用：闲鱼有独立自动开关，默认关避免双跑
    "auto_active_start_hour": 9,
    "auto_active_end_hour": 23,
    "auto_interval_min_minutes": 40,
    "auto_interval_max_minutes": 120,
    "auto_batch_count": 3,
    # ── 闲鱼 全自动采集（与 PDD 独立的一套，backend celery beat 读）──
    "xianyu_auto_batch_enabled": False,
    "xianyu_auto_active_start_hour": 9,
    "xianyu_auto_active_end_hour": 23,
    "xianyu_auto_interval_min_minutes": 40,
    "xianyu_auto_interval_max_minutes": 120,
    "xianyu_auto_batch_count": 3,
    # ── 数据清理（backend celery beat 读）──
    "pdd_runs_retention_days": 30,  # PDD 采集流水保留天数，每日 03:10 删更早的
    "xianyu_runs_retention_days": 30,  # 闲鱼采集流水保留天数，每日 03:12 删更早的
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
    "target_count_min": {
        "type": "int", "min": 1, "max": 100,
        "label": "单词商品量下限", "group": "采集量",
        "pair": "target_count_max",
        "help": "每次采集一个关键词，目标商品数在[下限,上限]之间随机取。"
                "数越大滚屏越多、暴露面越大，新号建议保守。",
    },
    "target_count_max": {
        "type": "int", "min": 1, "max": 100,
        "label": "单词商品量上限", "group": "采集量",
        "pair_min": "target_count_min",
        "help": "每次采集一个关键词的目标商品数上限。",
    },
    "deep_harvest_dips_min": {
        "type": "int", "min": 0, "max": 8,
        "label": "深度词进详情数下限(K)", "group": "采集量",
        "pair": "deep_harvest_dips_max",
        "help": "仅对深度(list_deep)关键词生效：搜完在结果页『边逛边点』进 K 个"
                "商品详情页，被动收割 goods_id / 店铺名 / 规格 / 券后价 / 评论数等，"
                "合并回采集结果。每个任务的 K 在[下限,上限]之间随机取——真人看一个"
                "词通常也会点开三五个商品，随机化更去指纹。0=不进详情(只采列表)。"
                "worker 端硬上限 8。",
    },
    "deep_harvest_dips_max": {
        "type": "int", "min": 0, "max": 8,
        "label": "深度词进详情数上限(K)", "group": "采集量",
        "pair_min": "deep_harvest_dips_min",
        "help": "深度词每任务进详情页数量的上限。K 在[下限,上限]随机取。建议 6 上下。",
    },
    "logistics_browse_enabled": {
        "type": "bool",
        "label": "查物流拟人行为", "group": "拟人行为",
        "help": "总开关。开启后会按下面两个概率去「我的订单→查看物流」逛一下，"
                "提升行为多样性。每日首次触发会先确认该号有真实订单：有则当日继续"
                "随机查，没有则当日冷却不再尝试。⚠ 仅对有真实购买记录的号有意义。",
    },
    "logistics_browse_prob": {
        "type": "float", "min": 0.0, "max": 1.0, "step": 0.05,
        "label": "查物流概率·burst 结尾", "group": "拟人行为",
        "help": "每个 burst（一波搜索）结束时触发查物流的概率（0~1）。"
                "0=关闭这条触发。建议 0.2~0.3，太高反而异常。",
    },
    "logistics_quiet_prob": {
        "type": "float", "min": 0.0, "max": 1.0, "step": 0.05,
        "label": "查物流概率·静默期", "group": "拟人行为",
        "help": "两波搜索之间的静默期（5-30min）中段触发查物流的概率（0~1）。"
                "0=关闭这条触发。更像真人空闲时点亮手机看眼快递，建议 0.3~0.4。",
    },
    "auto_batch_enabled": {
        "type": "bool",
        "label": "全自动跑批", "group": "自动跑批",
        "help": "开启后 backend 定时唤醒，在活跃时段内按随机间隔自动从词库挑词派任务。"
                "「暂停任务」会一并停掉它（共用同一暂停标志）。",
    },
    "auto_both_platforms": {
        "type": "bool",
        "label": "自动跑批同时跑闲鱼", "group": "自动跑批",
        "help": "自动派 PDD 词的同时错峰触发一次闲鱼采集。",
    },
    "auto_active_start_hour": {
        "type": "int", "min": 0, "max": 23,
        "label": "活跃时段起(点)", "group": "自动跑批",
        "help": "几点开始允许自动跑批（按北京时间）。避免凌晨搜索这种非真人时段。"
                "起>止视为跨夜（如 22→2）。",
    },
    "auto_active_end_hour": {
        "type": "int", "min": 0, "max": 24,
        "label": "活跃时段止(点)", "group": "自动跑批",
        "help": "几点停止自动跑批（按北京时间）。起==止视为全天；填 24 表示到当天 23:59。",
    },
    "auto_interval_min_minutes": {
        "type": "int", "min": 5, "max": 720,
        "label": "两波最短间隔(分)", "group": "自动跑批",
        "pair": "auto_interval_max_minutes",
        "help": "两次自动派词之间的最短间隔。实际间隔在[最短,最长]随机取，"
                "避免每天固定钟点上线被识别为机器。",
    },
    "auto_interval_max_minutes": {
        "type": "int", "min": 5, "max": 1440,
        "label": "两波最长间隔(分)", "group": "自动跑批",
        "pair_min": "auto_interval_min_minutes",
        "help": "两次自动派词之间的最长间隔。",
    },
    "auto_batch_count": {
        "type": "int", "min": 1, "max": 10,
        "label": "每波派词数", "group": "自动跑批",
        "help": "每次自动派几个词（同品类聚集）。建议 ≤ 单波最多搜索次数，"
                "让一波正好在一个 burst 内消化。",
    },
    "xianyu_auto_batch_enabled": {
        "type": "bool",
        "label": "闲鱼全自动采集", "group": "闲鱼自动",
        "help": "开启后 backend 定时在活跃时段内按随机间隔，从词库里 xianyu_safe 的词"
                "自动派闲鱼采集。与 PDD 自动跑批互相独立。",
    },
    "xianyu_auto_active_start_hour": {
        "type": "int", "min": 0, "max": 23,
        "label": "闲鱼活跃时段起(点)", "group": "闲鱼自动",
        "help": "几点开始允许闲鱼自动采集（北京时间）。起>止视为跨夜。",
    },
    "xianyu_auto_active_end_hour": {
        "type": "int", "min": 0, "max": 24,
        "label": "闲鱼活跃时段止(点)", "group": "闲鱼自动",
        "help": "几点停止闲鱼自动采集（北京时间）。起==止视为全天；填 24 表示到当天 23:59。",
    },
    "xianyu_auto_interval_min_minutes": {
        "type": "int", "min": 5, "max": 720,
        "label": "闲鱼两波最短间隔(分)", "group": "闲鱼自动",
        "pair": "xianyu_auto_interval_max_minutes",
        "help": "两次闲鱼自动派词之间的最短间隔，实际在[最短,最长]随机取。",
    },
    "xianyu_auto_interval_max_minutes": {
        "type": "int", "min": 5, "max": 1440,
        "label": "闲鱼两波最长间隔(分)", "group": "闲鱼自动",
        "pair_min": "xianyu_auto_interval_min_minutes",
        "help": "两次闲鱼自动派词之间的最长间隔。",
    },
    "xianyu_auto_batch_count": {
        "type": "int", "min": 1, "max": 10,
        "label": "闲鱼每波派词数", "group": "闲鱼自动",
        "help": "每次闲鱼自动派几个词。闲鱼有自己的合规闸(≥60s/40h)，会自动错峰。",
    },
    "pdd_runs_retention_days": {
        "type": "int", "min": 1, "max": 365,
        "label": "PDD流水保留天数", "group": "数据清理",
        "help": "PDD 采集流水(pdd_search_runs，也是「任务记录」数据源)保留天数。"
                "每日 03:10 物理删掉更早的流水，保留最近 N 天任务历史又给表封顶。"
                "收藏的 PDD 快照在独立表，不受影响。",
    },
    "xianyu_runs_retention_days": {
        "type": "int", "min": 1, "max": 365,
        "label": "闲鱼流水保留天数", "group": "数据清理",
        "help": "闲鱼采集流水(xianyu_search_runs，也是「任务记录」数据源)保留天数。"
                "每日 03:12 物理删掉更早的流水，保留最近 N 天任务历史又给表封顶。"
                "闲鱼收藏的商品另存，不受影响。",
    },
}


def _coerce(key: str, value: Any) -> Any:
    """按 spec 把值转成正确类型；类型不对抛 ValueError。"""
    spec = PARAM_SPECS[key]
    try:
        if spec["type"] == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(int(value))
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
        if spec.get("min") is not None and (val < spec["min"] or val > spec["max"]):
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
        "groups": ["节奏", "阵发", "配额", "采集量", "拟人行为", "自动跑批", "闲鱼自动", "数据清理"],
    }
