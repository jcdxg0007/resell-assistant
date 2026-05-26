# PDD APP Day 3.5：7315 上线观察 SOP

> 创建时间：2026-05-26
> 触发事件：4310 在 Day 3 联调中被实名认证墙击穿；账号已 quarantine，换 7315 上岗。
> 目标：用 7315 跑通"能采集 + 不挂"的稳定窗口，把 Day 3 真正收掉。
>
> 历史背景见 `PDD-自建采集-roadmap.md` 第 6-7 条踩坑记录 + 本文 §6 "4310 死因复盘"。

## §1 7315 当前的"账号画像"（PDD 看到的）

| 维度 | 状态 | 风险评估 |
|---|---|---|
| 账龄 | 老号（last_used 2026-05-23）| ✅ 不是新号 |
| H5 历史 | 11 cookies，bound_area=370000 | ⚠️ 之前在青果代理 IP 上活动过 |
| APP 登录历史 | 0（2026-05-26 首次） | ⚠️ 设备类型迁移信号 |
| 今日真人行为 | ¥10 订单 + 搜索 + 比价 + 浏览 | ✅✅ 比 4310 的几毛钱订单强 10 倍 |
| 物理设备 | Honor X20 (OXF-AN10) | ⚠️ 家人此前用过，多账号史 |
| 物理 IP | 家里 WiFi | ⚠️ 跟 H5 历史的青果代理不一致 |

**综合**：起点优于 4310，但有 24h 观察期。

## §2 软养 24h 清单（2026-05-26 当天 + 隔夜）

```text
当天剩余时间（你方便就做，不勉强）：
  □ 每隔 2-3 小时打开 PDD APP 滑 1-2 分钟首页推荐流
  □ 不搜任何东西，纯浏览
  □ 如果刚好要买生活用品（牙膏/纸巾/零食），就用 7315 下单
  □ ❌ 不要打开高利润类目（机械键盘/耳机/球鞋/潮玩/手办/数码）
  □ ❌ 不要薅 0.1-0.5 元的羊毛订单（PDD 已经把这种识别为套利模板了）

睡觉前：
  □ 手机插着电、APP 留在 PDD 首页（不用退出，不用关）
  □ Worker 不启
  □ Honor X20 屏幕"休眠永不"保持开启

明早起床第一件事：
  □ 打开 PDD 看首页有没有任何弹窗
  □ 关注：「实名认证」「身份验证」「异地登录」「设备异常」「账号风险」
  □ 没弹窗 → §3 可以开测；有任何弹窗 → §5 应急处置
```

## §3 明早开测 SOP（严格遵守）

### 启动前自检

```cmd
:: Windows worker 端
cd /d C:\resell\worker
.venv\Scripts\activate   ← 必须激活 venv
type pdd_app_worker\.env | findstr BOUND_PDD_ACCOUNT
::    应该看到 BOUND_PDD_ACCOUNT=pdd_crawler_7315

python -m pdd_app_worker.smoke_test
::    三个 ✅：环境 / adb / backend
```

### 启 worker

```cmd
python -m pdd_app_worker.main
```

期望首屏日志：

```text
pdd_app_worker windows-home starting (BOUND_PDD_ACCOUNT=pdd_crawler_7315)
initial heartbeat sent: devices=['PKT0220416005274']
```

### 派任务的关键词白名单（按风险从低到高，依次开）

| 阶段 | 关键词 | 时段 | 累计次数上限 |
|---|---|---|---|
| 第 1 波 ✅ 安全词 | "纸巾"、"袜子"、"保鲜膜"、"矿泉水"、"棉签" | 上午 | 总共 3-5 次 |
| 第 2 波（首波过了）| "保温杯"、"牙膏"、"垃圾袋"、"洗手液" | 中午 | +3-5 次 |
| 第 3 波（前两波都过了）| "U 盘"、"数据线"、"插线板" | 下午 | +3 次 |
| 第 4 波（24h 全通）| 才可以试 "机械键盘"、"耳机" | 明天后天 | 谨慎，单次单屏 |

**绝对禁区（至少 7 天内别碰）：**

```text
❌ 球鞋 / 运动鞋 / Nike / 阿迪
❌ 潮玩 / Bearbrick / 泡泡玛特 / 手办
❌ 平价手机 / iPhone / Apple
❌ 任何"XX 元包邮"明显套利的搜索词
```

### 任务节奏（worker 内置 Burst 调度自动管控）

worker 现在自动模拟真人的阵发式搜索节奏，**不需要你手动控制派任务的间隔**：

```text
默认配置（.env 里 BURST_SIZE_* / INTRA_BURST_* / INTER_BURST_* / DAILY_SEARCH_QUOTA）：
  burst_size            = 1-4 次/burst（随机）
  burst 内任务间隔      = 5-30s（随机）
  burst 之间静默期      = 5-30 分钟（随机，期间 PDD 自动退后台）
  每日 quota            = 30 次（到 UTC 0 点重置）

派任务后 worker 自动决定：
  □ 上次任务后 < 30s 且当前 burst 没用完 → 短间隔，立刻执行
  □ 当前 burst 用完 → sleep 5-30 分钟 + adb home 把 PDD 退后台 → 开新 burst
  □ 今日已搜 ≥ 30 次 → 立刻返回 risk_signals=["daily_quota_exhausted"]
```

观察日志里这几行就能知道当前调度状态：

```text
[INFO] scheduler: new burst started — 3 searches planned (daily so far 0/30)
[INFO] scheduler: intra-burst gap — sleeping 18.7s (2 left in this burst)
[INFO] scheduler: burst ended — daily total 3/30
[INFO] [PKT0220416005274] PDD pushed to background (KEYCODE_HOME)
[INFO] scheduler: inter-burst quiet — sleeping 12.3 min before new burst
```

**如果连续 3 次任务全部 risk_blocked → 立刻停 worker，进 §5 应急。**

## §4 观察哪些指标 = 健康 / 不健康

### ✅ 健康信号（继续放心跑）

```text
- task status="ok"
- items 数 ≥ 4
- elapsed < 60s
- 风控信号空数组
- worker 日志看到 "warmup done: scrolls=2 detail_visited=True"
- 物理手机 APP 没有任何弹窗
```

### ⚠️ 黄灯信号（停下来观察）

```text
- task 偶尔出现 items=[]（empty_result）但没风控信号
  → 可能是关键词冷门，换个关键词再试
- elapsed > 90s
  → 网络抖动 / lazy-render 没救回来，问题不大
- detail_visited=False 连续 3 次以上
  → warmup 路径上首页推荐流没被识别出商品卡片，需 dump 排查
```

### ❌ 红灯信号（立刻停 worker，进 §5）

```text
- status="risk_blocked" 且 risk_signals 包含：
    "real_name_wall"  ← 实名认证墙（最严重，号大概率废了）
    "slide_verify"    ← 滑块验证
    "captcha"         ← 图形验证码
    "login_wall"      ← 突然要求重新登录
- 同一关键词连续 2 次返回 active_listings=0
- 物理手机上 PDD 弹任何"账号风险 / 异地登录 / 异常行为"提示
```

## §5 应急处置（如果 7315 也挂了）

按严重程度顺序：

### 5.1 看到 real_name_wall

```text
1. 立刻 Ctrl+C 停 worker
2. 物理手机上不要点"去认证"！点了就坐实，号直接废
3. 选择：
   A. 退出 PDD APP 卸载重装（清掉 _nano_fp）+ 重新登 7315 → 50% 概率可恢复
   B. 直接 quarantine 7315，换下一个备用号
4. 如果选 B，跑：
   python scripts/pdd_account_swap.py \
       --device-serial PKT0220416005274 \
       --old 7315 --new <下个备用号尾号> \
       --reason "real_name_wall_2026-05-27"
```

### 5.2 看到 slide_verify

```text
没那么严重，账号还能救：
1. 在物理手机上手动过一次滑块验证
2. 等 30 分钟再让 worker 继续跑
3. 把这天的任务上限砍半（10 次/天）
```

### 5.3 看到 login_wall

```text
账号 cookies 失效或 session 过期：
1. 物理手机上重新登 7315（短信验证码）
2. 登录后手动浏览 5 分钟养号
3. 再启 worker
```

## §6 4310 死因复盘（写进项目记忆，下次避雷）

时间线：

```text
Day -10 左右   4310 注册（账龄 < 30 天）
Day -7 左右    4310 在某物理手机上零星下了几笔 0.1-0.5 元订单
Day 0 (5/24)   4310 绑到 Honor X20，开始 Phase 1 Day 1
Day 2 (5/25)   4310 跑 worker，过了一波搜索测试
Day 3 (5/25 晚) 4310 手动搜"机械键盘"，滚到第 3 屏 → 实名墙
```

**死因因子权重（按 PDD 风控模型估算）：**

| 因子 | 权重 | 教训 |
|---|---|---|
| **手机已有家人 PDD 账号历史** | 35% | 一机一号铁律——**装 worker 前必须先把手机恢复出厂 + 不让任何其他号登过** |
| **几毛钱订单 = "测试性消费"标签** | 25% | **真用户消费要 ≥ ¥3，最好 ≥ ¥10**。0.1-0.5 元订单是 PDD 训练过的套利模板 |
| **关键词类目（机械键盘）属高商业意图** | 15% | 新号头 7 天严禁碰电子产品/球鞋/潮玩 |
| **新号 < 30 天** | 10% | 时间换不来，等就完事了 |
| **滚 3 屏没点击 = 异常浏览模式** | 8% | Worker 已加 `_idle_browse_warmup` 缓解 |
| **其他（IP、路径、节奏）** | 7% | — |

**预防 checklist（每次新号上线必查）：**

```text
□ 手机有没有其他人登过 PDD？（有 = 先恢复出厂）
□ 这个号的下单史是不是都是 ¥0.5 以下？（是 = 先用号本人手动下一笔 ¥10+ 的）
□ 关键词第一周白名单是不是只有日用品？
□ Worker 启动后第一次跑的关键词是不是从最安全词开始？
□ 一上手是不是滑了 ≥ 2 屏推荐 + 点了 ≥ 1 个商品看详情？（worker 的 warmup 已做）
```

## §7 长期演化路线

```text
今天剩下时间      → 软养 24h
明早 (5/27)       → §3 SOP 开测
2 天内 (5/28-29)  → 跑通安全词，验证 7315 稳定
3-7 天内 (5/30+)  → 上 Day 4 PaddleOCR 解决"百亿补贴" canvas 价格盲点
7-14 天内         → 上 Day 5 Windows 开机自启 + 钉钉告警
14 天后           → 拔 _PDD_USE_APP_WORKER 开关，正式接入 instant_search
```
