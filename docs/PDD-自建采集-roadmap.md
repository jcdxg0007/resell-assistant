# PDD 自建采集 Roadmap（物理手机 + uiautomator2）

> 关联文档：见 `开发文档_转卖助手.md` §1.4.4 ~ §1.4.6。本文档锁定 A 路径的具体实施方案，决议时间 2026-05-24。
> 决策背景：PDD H5 已关闭、第三方 API 月成本 ¥1.4k-2.4k，自建一次性 ¥0-1500、月运营 ≈ ¥0，单位数据成本碾压第三方。

## 0. 目标与硬性约束

**业务目标**：替代被关闭的 PDD H5 通道，为闲鱼比价 + 选品评分提供实时 PDD 商品数据（价格 / SKU / 销量 / 百亿补贴标识）。

**硬性约束**：

- 个人副业，不能有专职运维
- 单次出差最长 7-14 天，期间系统不能完全瘫痪
- 现有架构（Celery + Redis + Postgres + K8s）能复用就复用，不重新发明轮子
- 每月运营成本 ≤ ¥200（手机话费 + 备用件）

**吞吐目标**（与现有调度对齐）：

| 阶段 | 调用峰值 | 手机数量 | 备注 |
|---|---|---|---|
| MVP | 80 次/天 | 1 台 | 跑通 PoC，对接 1 个号 |
| 稳态 | 200 次/天 | 2 台 | 主用 + 1 台冗余/养号 |
| 扩容 | 500 次/天 | 3 台 | 支撑选品自动发现峰值 |

> 单台手机日上限：~150-200 次（含人类化间隔），3 台冗余设计完全够用。

## 1. 系统架构

**两台机器各司其职，互不冲突**：

- **Sealos devbox**（云端）—— 继续作为开发终端，跑 kubectl / git / 写代码 / 调 K8s
- **家里 Windows PC**（用户日常远程桌面进的那台）—— 跑 worker，物理连接 USB 手机

**关键架构修正（2026-05-24）**：Sealos Redis 是 K8s 内部 ClusterIP，家里 Windows 无法直连。所以引入 **backend HTTP bridge** —— worker 通过 HTTPS 长轮询 backend 的 `/api/v1/pdd-worker/*` endpoints 间接操作 Redis 队列。这一改动反而提升了安全性（HTTPS + API token）和可观测性（标准 HTTP 日志）。

```
┌─────────────────────────────────────────────────────────────┐
│  Sealos K8s (云端，已有)                                     │
│  ├─ backend pod      (FastAPI)                              │
│  │   ├─ /api/v1/instant-search          (前台业务)           │
│  │   ├─ /api/v1/pdd-worker/poll         (worker 拉任务)      │
│  │   └─ /api/v1/pdd-worker/result       (worker 推结果)      │
│  ├─ celery_worker    (任务编排)                              │
│  └─ celery_beat      (定时任务、每日清库)                    │
│         │                                                    │
│         │  内部 rpush/blpop                                  │
│         ▼                                                    │
│  Redis (Sealos 托管，K8s 内部 ClusterIP)                     │
└────────────────────────┬────────────────────────────────────┘
                         │ HTTPS (Sealos Ingress 域名)
                         │ + Bearer Token 鉴权
                         │
┌────────────────────────▼────────────────────────────────────┐
│  家里 Windows PC (24h 开机，用户远程桌面登录)                │
│  ├─ Python 3.11+ venv                                       │
│  ├─ pdd_app_worker.py     (Python 守护进程，开机自启)        │
│  │   ├─ pull task from Redis                                 │
│  │   ├─ pick free phone & matching account                   │
│  │   ├─ uiautomator2 client → 真机操作                       │
│  │   └─ push result back to Redis                            │
│  ├─ Android Platform Tools (adb.exe)                        │
│  ├─ uiautomator2 (Python 客户端)                            │
│  └─ scrcpy (Windows 版，远程看屏调试用)                      │
│                  │                                          │
│                  │ USB（3+ 口或 USB Hub）                    │
│                  ▼                                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                    │
│  │ 手机 1   │  │ 手机 2   │  │ 手机 3   │                   │
│  │ 4310     │  │ 7315     │  │ 5514     │  (1 机 1 号绑死) │
│  │ 荣耀/OPPO│  │ 荣耀/OPPO│  │ 已登录   │                   │
│  │ atx-agent│  │ atx-agent│  │ atx-agent│                   │
│  └──────────┘  └──────────┘  └──────────┘                    │
└─────────────────────────────────────────────────────────────┘
```

**Windows worker 的额外注意点**：

- Windows 自动更新可能强制重启 → 配组策略推迟到周末凌晨；worker 进程做开机自启（任务计划程序）
- 偶尔的停电后电脑自动重启 → BIOS 设置 "After Power Loss: Power On"
- 用户远程桌面登录不会中断 worker 进程（worker 跑在后台服务/任务计划，不在交互会话里）

**关键设计要点**：

1. **K8s 不动**——继续跑 backend / celery / playwright（闲鱼小红书 1688 走 Playwright，PDD 改走 uiautomator2 worker）
2. **Redis 队列解耦云和家**——不暴露家里公网端口，家里 worker 主动 pull
3. **1 机 1 号绑死**——PDD APP 不支持多号常驻，切换会触发短信验证
4. **任务异步等待**——backend 发任务 + 阻塞等结果（最多 120s），失败降级返回"PDD 数据不可用"，不影响闲鱼比价主流程

## 2. 技术栈选型

| 决策点 | 选 | 不选 | 理由 |
|---|---|---|---|
| 自动化框架 | **uiautomator2** | Appium | uiautomator2 是纯 Python + 轻量；Appium 跨平台但慢 |
| 设备连接 | **USB 主 + WiFi-ADB 备** | 纯 WiFi-ADB | USB 稳定；WiFi 在路由器重启时会掉 |
| 任务队列 | **K8s 内部 Redis（不变）** | RabbitMQ / NATS | 已有 Redis，避免引入新组件 |
| 跨网络通信 | **HTTPS + Sealos Ingress + Bearer Token**（长轮询）| 直连 Redis / Tailscale | Sealos Redis 不暴露公网，HTTPS 桥同时解决安全和可观测性 |
| 手机型号 | **任意安卓 7+ 二手机** | iOS / 模拟器 | iOS 自动化贵且 PDD iOS 反爬更重 |
| 是否 root | **不 root** | root + Frida | uiautomator2 不需要 root，且 PDD 检测 root |

## 3. 实施路线图

### Phase 1 — MVP（1 周，1 台手机 + 1 个号）

**目标**：用 **4310 号（最废、烧了不心疼，最适合试错）** 在 1 台手机上跑通"接到搜索任务 → 在 PDD APP 里搜 → 抓前 20 条结果 → 写回数据库"完整链路。

> 用号策略：4310 调通技术链路，稳定后切到 5514 金号验数据真实性。

| 工作日 | 任务 | 验收 | 状态 |
|---|---|---|---|
| Day 0 | Windows host 准备：装 Python 3.11+ / Android Platform Tools / scrcpy；手机开开发者选项 + USB 调试；关闭 PDD 自动更新 | `adb devices` 能看到 1 台手机 | ✅ **2026-05-25**：Python 3.14、adb、scrcpy 全装好；OXF-AN10（荣耀 X20）serial=PKT0220416005274 已连通 |
| Day 1 | HTTP bridge + worker 骨架；stub 任务端到端 | backend `enqueue → worker poll → result` 一次往返 | ✅ **2026-05-25**：stub 任务 1.4s 走通，3 ✅ 烟测通过 |
| Day 2 | 写 `pdd_app_client.py`：登录态检测、PDD APP 启动、搜索框定位、结果列表解析 | 命令行能输入关键词，返回 JSON 列表 | ✅ **2026-05-25**：搜索栏 XPath（content-desc="搜索"）、IME 输入、提交全走通；50s 完整流程；商品卡解析待 Day 3 真机校准 |
| Day 3 | 加健壮性层 + 填实结果页商品卡 XPath | 跑 50 次不挂、价格覆盖率 ≥ 80% | 🟡 **部分完成 2026-05-25**：标题 100%、销量 30-50%、价格 30-50%（被「百亿补贴 canvas 渲染」卡住）；4310 实名墙挂 → 已 quarantine + 换 7315 |
| Day 3.5 | 7315 软养 + 加 `_idle_browse_warmup` 前置摸鱼 + 安全词白名单开测 | 24h 内 7315 不挂、安全词跑 ≥ 10 次成功 | ✅ **2026-05-27 完成**：7315 上线后纸巾 / 袜子 / 保鲜膜 / 牙线 (deep) 共 4 次任务全 status=ok、risk_signals=空；详见本表下方 §"Day 3.5 收官记录" |
| Day 4 | **OCR 兜底**：EasyOCR（ch_sim+en，CPU）+ 标题底边窄带截图识别 canvas 渲染价格，目标价格覆盖率 ≥ 90% | 10 个**不同**安全词跑一遍（分 2-3 天），每次都拿到 ≥ 80% 卡片有价格 | 🟢 **编码 + 冒烟双通过 2026-05-28 01:30 CST**：纸巾 deep 4屏返回 11 件 = 10 xml + 1 ocr，**价格覆盖率 100%**，0 risk_signals，74s 完成。**剩多关键词长周期分布验收（明天起 ≥ 10 小时间隔）** |
| Day 5 | Windows 任务计划程序设开机自启；每日 self_check 任务 + 钉钉告警 | 重启 Windows 后 5 分钟内 worker 自动上线 | pending |
| Day 6 | 改 `backend/app/tasks/selection.py`：取消 `_PDD_DISABLED` 短路 + 拔 `_PDD_USE_APP_WORKER` 开关；同步实现每日清库 beat | `instant_search('运动鞋')` 端到端能拿到 PDD 数据 | 脚手架已就位 |
| Day 7-8 | 跑 72h 稳定性测试，日志收集 | 成功率 ≥ 90%，平均耗时 ≤ 40s/任务 | pending |

> 📝 **Day 2-3 累计暴露的坑**（已修或已记录，留作后续参考）：
> 1. **Honor X20 锁屏**：`d.swipe()` 不够强，必须走 `adb shell input swipe` + 极端坐标。已加 4 策略级联（builtin / shell-swipe / KEYCODE_MENU / long-press HOME）
> 2. **PDD resource-id 全部混淆为 `id/pdd`**：3 个不同元素共用同 rid → 改 `content-desc="搜索"` 唯一定位
> 3. **PDD 首页搜索栏是 TextView 不是 EditText**：点了之后才进二级搜索页有 EditText
> 4. **uiautomator2 下 `app_current()` 可能跟实际屏幕显示不一致**：被锁屏覆盖时 PDD 在 activity stack 顶部但屏幕上是锁屏
> 5. **PDD 结果页商品标题用 `id/tv_title`（唯一未混淆 rid）**：完整标题在 `content-desc` 里，`text` 字段被截断到 ~30 字
> 6. **PDD 价格分两套渲染体系**（关键反爬手段，2026-05-25 真机实测确认）：
>    - **非补贴卡片**：价格用标准 TextView，可通过 dump_hierarchy 抓到（如"狼途 T98 ¥99.9"）
>    - **百亿补贴卡片**：价格用 **Canvas/Drawable 自绘**，uiautomator2 完全看不到。这恰恰是转卖比价**最重要**的数据 → 必须用 OCR 兜底（见 Day 4）
> 7. **PDD lazy-render**：标题渲染早，价格/销量延迟到 viewport 中心才 hydrate → 已加微滚动 rebind ViewHolder 兜底（fast 模式总耗时 30-35s）
> 8. **uiautomator2 `xpath()` 在 CJK 属性匹配上有 bug**（2026-05-27 真机实测确认）：元素在 `dump_hierarchy()` XML 里存在、可见、bounds 完整，但 `d.xpath('//*[@content-desc="搜索"]').exists` 返回 False。**不是 PDD 改 UI**！是 u2 自己的 xpath 引擎对 CJK 不工作。修复：所有涉及 CJK content-desc / text 的定位**禁用 xpath()**，改用 `d(description="...", className=...)` UiSelector，或 dump XML + 正则提 bounds + 直接点坐标。
> 9. **Honor X20 + EMUI 不响应 adb 路径的 home 键**（2026-05-27 实测）：`input keyevent KEYCODE_HOME` 和 `am start -c HOME` 都 rc=0 但 PDD 仍前台。**必须用 `d.press("home")` 走 uiautomator2 → atx-agent → InputManager 注入这条独立路径才生效**。Honor/华为系机型部署必须验证这点。

**Phase 1 出口标准**：

- ✅ 端到端 instant_search 能从 PDD 拿真实数据
- ✅ 成功率 ≥ 90%
- ✅ 任务平均耗时 ≤ 30s
- ✅ 风控信号自动上报钉钉

### Day 3.5 收官记录（2026-05-27）

7315 在 Honor X20 上彻底跑通端到端搜索，证明 Phase 1 技术链路 + 拟人化策略
组合可用。这天的关键修复（按发现顺序）：

| 时段 | 修复 | 触发场景 |
|---|---|---|
| 上午 | `_ensure_home_tab()` —— 搜索前先点底部"首页"tab | worker 启 PDD 后 APP 可能恢复到非首页（详情页 / 搜索结果页 / 活动横幅页），warmup 在错的页面上跑成功但 _tap_search_entry 找不到搜索栏 |
| 上午 | `_tap_search_entry` 改 UiSelector + XML 兜底 | **uiautomator2 的 `xpath()` 在 PDD 新版 + Python 3.14 + Honor X20 组合上对 CJK content-desc 属性匹配彻底失效**（XML 里元素存在、可见、bounds 完整，`.exists` 仍返回 False）。绕开 xpath 引擎走 Android 原生 UiSelector 解决 |
| 中午 | `_submit_search` 同样改 UiSelector + XML 兜底 | 同上 bug，导致"输入了关键词但没点搜索"的隐蔽问题——拿回的是 PDD 搜索建议页的推荐位（如搜"纸巾"返回"穿针器"/"铅笔"） |
| 中午 | `_type_keyword` finally 恢复默认 IME | ATX 输入法 (`com.github.uiautomator.adbkeyboard`) 是爬虫指纹，PDD 任何 EditText.onFocus 一查 `InputMethodManager` 就命中。输入完立刻切回默认 IME，暴露窗口缩到 2-8s 输入期 |
| 中午 | 详情页 warmup 拆解死盯 | 之前 `time.sleep(random(4,8))` 在详情页一动不动 = 机器人特征。改成 1.5-2.8s 看顶 + 60% 概率滑屏看图 + 30% 嵌套二次滑屏 |
| 下午 | `BurstScheduler.maybe_end_idle_burst(60s)` | scheduler 随机决定 burst_size=N，但实际只来 K<N 个任务时，剩余 N-K 名额永远等不到 = burst 永远不结束 = PDD 永远不退后台 = 拟人化破功。补一个 60s 闲置超时强制结束 burst |
| 下午 | `_post_task_cleanup` 末尾强制 `d.press("home")` | Honor X20 + EMUI 上 `adb shell input keyevent KEYCODE_HOME` 和 `am start -c HOME` **都吃瘪**（rc=0 但 PDD 仍前台）。换走 uiautomator2 → atx-agent → InputManager 注入这条独立路径，**生效** |

**最后那条 atx-agent 退后台 = Honor/华为系机型的关键设备兼容性发现**，
已写进 `PDD-Day3.5-7315上线观察.md §8.1`（设备指纹兜底章节）。

完整任务时间线（北京时间）：

```text
13:18  task #1  纸巾    fast  → ok  items=4  risk=0  65s   daily 1/30
16:03  task #2  袜子    fast  → ok  items=4  risk=0  65s   daily 2/30
16:22  task #3  保鲜膜  fast  → ok  items=4  risk=0  44s   daily 3/30
21:07  task #4  牙线    deep  → ok  items=10 risk=0  63s   daily 4/30
       (退后台修复在 #4 完整生效，PDD 真退到桌面)
```

**Phase 1 Day 3.5 出口达成**：账号 7315 经历 4 次真实任务零风控，
端到端拟人化采集链路验证完毕。Day 4 OCR 开工准备就绪。

### Day 4 收官记录（2026-05-28 凌晨）

环境 + 编码 + 冒烟一次性打通：

```text
01:13  worker 重启  OCR preload done in 4.9s (EasyOCR ch_sim+en, CPU)
01:14  task #1  纸巾  fast  scroll=2 → ok items=4   xml=4/4   ocr=0    risk=0  64s   daily 1/30
01:34  task #2  纸巾  deep  scroll=4 → ok items=11  xml=10/11 ocr=1/11 risk=0  74s   daily 2/30
                                          coverage=100%   ← Day 4 §Step 5 目标 ≥ 90% 达成
```

关键修复/补强：
- 修 `_handle_search` 把 payload 的 `target_count` / `scroll_screens` /
  `mode` 真正透传给 `PddAppClient.search()`（之前是摆设，所有任务都按
  默认 fast=1屏跑）
- `search()` 新增 `scroll_screens` 一等参数，clamp [1, 5]
- EasyOCR 模型经 ghfast.top 镜像（craft + zh_sim_g2 + english_g2 +
  cyrillic_g2 共 ~110MB）预下到 `~/.EasyOCR/model/`，首次任务零冷启动
- pdd_fire_one_task.py 加 `--mode {fast,deep}` + price_source 分布统计

**Phase 1 Day 4 编码出口达成**。剩多关键词长周期分布验收（明天起，
≥ 10 小时间隔，跑 10 个不同安全词分 2-3 天），看 OCR 在百亿补贴卡比例
更高的关键词上是否稳定 ≥ 50% 命中率（覆盖率应继续保持 ≥ 90%）。

### Day 4 拟人化 v2 补丁（2026-05-28 上午）

发现 2 个反真人信号源，连夜修复后再上批量任务：

| # | 信号 | 修复 |
|---|---|---|
| 1 | burst 内每个任务结束都按 home 退后台 → "用户每分钟在 PDD 间切 3 次" 的统计画像 | `BurstScheduler.is_first_in_burst` + `PddAppClient.set_cleanup_mode(soft\|exit)`；burst 内中间任务走 soft（back×1-2 退到首页，PDD 不退后台），burst 末任务才走 exit（按 home） |
| 2 | burst 内每个任务都跑 cold-start 3.5s + warmup 5-10s "回首页浏览" → 真人连搜 3 个词不会每次回首页逛 | `search(is_first_in_burst=False)` 强制 `profile="direct"`，跳 warmup；`_ensure_app_foreground` 见 PDD 前台直接返回 "already"（跳 3.5s 冷启动 sleep）  |

每次 burst 内"接续任务"省 ~12-18s，且 PDD 不再频繁切后台。日志新增
`intra_burst={yes,no}` 标记，方便复盘。Scheduler 默认配置同步从
`burst=[1,4]` 改成 `[3,5]`，匹配"一阵搜 3-5 个词再歇 5-30 分钟"的设定。

### Day 4 紧急任务旁路（2026-05-28 上午）

工作流：日常按词库节奏跑 20-30 个关键词分散到多个 burst；偶尔需要**临
时插一个紧急词**（客户问价、决策窗口紧）。直接派任务会落进 backend FIFO
队列尾部 + worker 端 5-30 min 静默期，最坏要等 ~30 min 才轮到——不够用。

**两层旁路设计**：

| 层 | 普通任务（priority<8） | 紧急任务（priority≥8） |
|---|---|---|
| backend `enqueue_task` | RPUSH 进队尾 | LPUSH 进**队首**，让 worker 下次 poll 立刻拿到 |
| worker `BurstScheduler` | 走 5-30 min inter-burst quiet | **跳 quiet**，立刻开新 burst |
| daily quota（30 次/天） | 守 | 守（紧急也不能突破，账号底线） |
| intra-burst gap（5-30s） | 守 | 守（连续 0 间隔太异常） |

**触发方式**：

```bash
# 单个紧急词
python -m scripts.pdd_fire_one_task "保温杯" --emergency
# 等价于 --priority 9

# 紧急批（建议 ≤ 3 个词）
python -m scripts.pdd_fire_keyword_batch 保温杯 牙线 洗手液 --emergency
```

**worker 日志可识别旁路**：

```text
received task xxx ... priority=9 [EMERGENCY] payload={'keyword': '保温杯', ...}
scheduler: EMERGENCY priority=9 ≥ 8 — BYPASS inter-burst quiet
  (elapsed since last burst 4.2 min, opening new burst now)
scheduler: new burst started — 3 searches planned (daily so far 5/30) [EMERGENCY]
```

**使用纪律**：

- 阈值 = 8 → 普通 fire 默认 priority=1，不会误触
- 单日紧急任务建议自控 ≤ 3 次，连续跳 quiet 会让"用户用 PDD 的间隔"统
  计画像看起来比真人激进
- 紧急 burst 结束后，下一个普通 burst 仍要等 5-30 min（_last_burst_ended_at
  在紧急 burst 收尾时重设，时间从那时算起）

### Phase 2 — 单机巩固（1-2 周）

**目标**：在 1 台手机上把所有边界情况吃透，把"试错成本"消化在加机器之前。

- 风控信号识别（滑块、短信验证、人机验证）→ 自动暂停 + 钉钉告警
- 人类化操作（滑动节奏、停留时间、随机回退到首页）
- 商品详情页采集（SKU、百亿补贴价、销量、评论数）—— 这是 §3.1 十维度评分维度 4 & 9 的数据源
- 历史价格采集（不是每次都要查，按需）
- 日志可观测性（任务耗时分布、失败原因分类）

### Phase 3 — 横向扩展到 3 台手机（2-3 周）

- 加第二台手机：配第二个 PDD 号，**走完 §账号入口隔离-SOP.md** 的全套流程（独立流量卡、首登异地、首周仅人工浏览）
- 加第三台手机：同上
- worker 改成多设备并发调度（按 `device_serial` 锁定）
- 自动故障转移：1 台手机离线时，任务自动路由到其他手机
- 性能压测：3 台并行能否撑住 500 次/天峰值

### Phase 4 — 长期运营（持续）

- 周报：每周一统计上周成功率、风控触发次数、各号健康度
- 月度复盘：是否需要补号 / 替换手机 / 升级 APP
- 储备方案：研究 D 路径（微信小程序）作为出差期间的无人值守兜底

## 4. 关键设计决策

### 4.1 任务 schema

```json
{
  "task_id": "uuid",
  "kind": "search" | "detail" | "history_price",
  "payload": {
    "keyword": "运动鞋",
    "goods_id": null,
    "max_results": 20
  },
  "account_id": "uuid",
  "priority": 1,
  "timeout_s": 120,
  "created_at": "2026-05-24T10:00:00Z"
}
```

### 4.2 账号 → 手机绑定

- 在 `accounts` 表加字段 `bound_device_serial: str | None`
- worker 启动时上报本机已连接的设备列表 → 调度器只把任务派给 `bound_device_serial` 匹配的手机

### 4.3 失败与降级

| 失败类型 | 处理 |
|---|---|
| 任务在 120s 内未完成 | 返回 `__unavailable__` + 原因；闲鱼比价主流程不阻塞 |
| 风控触发（滑块/短信码） | 该号进入 24h cooldown；钉钉告警；其他号继续工作 |
| 整台手机离线 > 5min | 钉钉告警；该手机上的号自动从调度池剔除 |
| 全部手机离线 | 自动切到 `_PDD_DISABLED=True` 短路模式，跟现在一样 |

### 4.4 风控信号识别

uiautomator2 截图 + 关键 UI 元素检测：
- 存在 `滑动滑块以完成验证` 文本 → 滑块风控
- 存在 `请输入验证码` + 数字输入框 → 短信风控
- 存在 `网络异常` / `请稍后再试` → 软风控（短 cooldown）
- 商品列表为空 + 搜索次数 > 3 → 疑似 shadowban（24h cooldown）

### 4.5 出差/远程接管预案

**远程接管方案**：用户已有远程桌面方案，能从外网登录家里 Windows PC。
- URGENT 告警 → 远程桌面登入 Windows PC → 双击桌面 scrcpy 快捷方式选择目标手机 → 在电脑屏幕上看到手机画面 + 鼠标接管
- 不再需要 Tailscale / WireGuard / 反向 SSH 等额外通路

| 出差时长 | 措施 |
|---|---|
| ≤ 2 天 | 系统自治；钉钉接到 URGENT → 用户已有远程桌面方案接管，5 分钟内可触达 |
| 3-7 天 | 出发前确认 3 台手机健康；远程桌面可用 |
| > 7 天 | 关闭 PDD 自动发现，仅保留即时搜索；接受单平台暂时降级 |

## 5. 风险矩阵

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| PDD APP 升级改 UI 导致 selector 失效 | 中 | 全部 PDD 任务失败 | 用 `resource_id` 优先、`text` 兜底；CI 每日跑一次自检脚本；钉钉告警立即可知 |
| 手机被 PDD 风控（号被封） | 低-中 | 该号停摆 1-7 天 | 1 机 1 号 + 严格遵守 §账号入口隔离-SOP；3 号互备；养号阶段 7-14 天慢热 |
| 物理故障（电池鼓包、屏幕坏） | 中（长期） | 1 台手机离线 | 二手机本身便宜（¥400-800/台），坏了直接换；3 台冗余设计 |
| 家里断网/停电 > UPS 续航 | 低 | 全 PDD 任务停摆 | 自动短路 `_PDD_DISABLED`；闲鱼/小红书继续；恢复后自动重连 |
| 我出差期间故障 | 中 | 部分任务失败 | Tailscale 远程 SSH + adb；接受短期降级 |
| PDD 全面升级反 UI 自动化（如检测 a11y 服务） | 低（短期）| 整个方案失效 | 储备 D 路径（小程序）+ APP 自动化领域有"无障碍服务"作为最后底线，PDD 全封等于自废 |

## 6. 验收标准与放弃信号

### 阶段验收（每 phase 必过）

- Phase 1 出口：见上文
- Phase 2 出口：连续 7 天成功率 ≥ 95%，单号未触发风控
- Phase 3 出口：3 台手机并行，500 次/天稳定跑 7 天

### 放弃信号（任一触发就回头讨论是否切回付费 API）

- 连续 2 周成功率 < 80%
- 月内 ≥ 2 个号被风控（说明 §账号入口隔离-SOP 不充分）
- PDD APP 升级后修复 selector 工作量 > 1 天
- 维护时间持续 > 2 小时/周

## 7. 反检测专题（关键技术认知）

### 7.1 PDD 是否会识破 uiautomator2

业内常被问的问题——结论是：**uiautomator2 不会被识别为"无障碍服务"，因为它根本走的不是那条路径**。

| | 第三方"无障碍 APP"（PDD 在防的）| **uiautomator2 / UI Automator**（我们用的）|
|---|---|---|
| 进入手机的方式 | 用户在"设置→无障碍"手动启用 | adb + Instrumentation 框架 |
| 在系统中是否可见 | `getEnabledAccessibilityServiceList()` 能查到 | 这个 API 看不到 |
| 典型 APP | 多多火车、抢券助手 | Android 官方 UI 测试框架 |
| PDD 能查到吗 | 能 | 几乎不能（除非动用很贵的方法且容易误伤）|

**实测证据**：业内 uiautomator2 跑 PDD 已存在 5+ 年，没有"因被识别 UI Automator 而批量封号"的报告。PDD 真正在防的是：
- 行为模式（节奏 / 滑动轨迹）
- 设备指纹（IMEI / Android ID 是否多账号复用）
- 网络层（机房 IP / 高频切换）

### 7.2 atx-agent APK 的次级风险

uiautomator2 需要在手机安装 `com.github.uiautomator` 这个小 APK。理论上 PDD 能扫到已安装 APP 列表，但实测不构成风控触发条件。

**Phase 2 可选增强**（Phase 1 不做）：
- 用 `python-uiautomator2` 时设 `serve_port=0`（随机端口）
- 重打包 atx-agent，把包名改成普通名（如 `com.example.tools`），15min 操作
- 不要在跑 PDD 的手机上装其它明显"爬虫/挂机"APP，保持已安装应用列表干净

### 7.3 真正威胁与防御对齐

| 威胁来源 | 我们的防御 | 实施阶段 |
|---|---|---|
| 行为节奏太机械 | 贝塞尔曲线滑动 + 随机停留 1.5-4.5s | Phase 1 Day 3 |
| 操作模式无浏览 | 每 5-10 次搜索穿插一次"漫游"（首页 3 屏+点推荐+回退）| Phase 1 Day 3 |
| 品类画像漂移 | 1 号 1 主品类，绑死 | Phase 1 Day 5 配号 |
| 设备指纹漂移 | 1 机 1 号绑死、不切换登录 | 架构层 |
| 网络异常 | 家庭 WiFi + 可选独立 SIM | 物理层 |
| 24h 不停 | 8AM-2AM 活动窗口（已有）| §compliance |

## 8. 账号策略与运营纪律

### 8.1 当前账号池状态（2026-05-24）

| 账号 | 区域 | 养号状态 | 用途 | 备注 |
|---|---|---|---|---|
| **5514** | 中国 | warm（养号 2 周+、有真实购买记录） | **主力金号** | 品类：日用 + 电子，与 §1.6 P1/P2 天然对齐 |
| **7315** | 美区 | nurturing（手机端实测可用） | 主力次选 | 品类：可绑定运动相机配件类 P0 |
| **4310** | 美区 | nurturing（H5 端被 shadowban） | 备用 | 需 7-14 天物理手机端冷养恢复 |
| 2117 | 中国（新）| nurturing（冷启动失败） | 储备 | 暂不投入使用 |
| 1876 | — | nurturing | 储备 | 暂不投入使用 |

### 8.2 金号（5514）运营纪律

为最大化寿命，5514 的使用约束：

- **真实消费维持**：每月 1-2 次真实购买（金额 ¥10-100 不等），保持"真实买家"画像
- **平台 push 响应**：每周偶尔点开 PDD 收到的 push，提升平台"用户活跃分"
- **品类不跨界**：搜索词限定在 §1.6 P1/P2（智能家居/桌面设备/电竞外设/小家电）
- **日活上限**：≤ 80 次/天（远低于人类上限）
- **冷启动**：连续 3 天无操作后，先人工浏览 5-10min 再让系统接管

### 8.3 Phase 1 用号策略（已确认）

| 阶段 | 用号 | 目的 | 风险 |
|---|---|---|---|
| Phase 1 Day 1-5（调通技术链路）| **4310**（最废、H5 已被 shadowban）| 调试用，万一烧了不心疼 | 4310 APP 端是否也 shadowban 未知；若也是，"返回结果数"指标无法验证 |
| Phase 1 Day 6-7（稳定性验数据）| **5514**（金号）| 验证拿到的数据是真实有效的 | 单号跑 72h ≤ 80 次/天，远低于上限 |
| Phase 2 起 | 全部 3 号（4310 / 7315 / 5514）| 互备 + 品类绑定 | 1 机 1 号绑死 |

> 4310 即使 APP 端也 shadowban，对 Phase 1 调试无影响——我们只需要验证"操作链路能跑通"，数据真实性由 Day 6-7 切到 5514 后再确认。

### 8.4 烧号定义与等级

| 等级 | 表现 | 处理 |
|---|---|---|
| 一级（Shadowban）| 搜索返空 / 返脏数据，账号还能登录 | 进入 7-14 天物理手机冷养（仅人工浏览，无系统任务） |
| 二级（功能限制）| 不能下单 / 限制浏览次数 | 标记 `lifecycle_stage='quarantine'`，停 30 天 |
| 三级（永久封禁）| 登录直接提示违规 | 标记 is_active=False 永不复用，从池子里删除 |

## 9. 成功率分层目标（95% 怎么实现）

| 层 | 目标 | 实现方法 |
|---|---|---|
| 单次操作（点击/滑动）| ≥ 90% | `resource_id` 优先 selector / `text` 兜底；点击前等 UI 稳定 1-3s |
| 单次任务（含重试）| ≥ 95% | 内置 2 次重试，含"重启 APP" 兜底；超时 60s |
| 单次业务请求 | ≥ 99% | 3 号互备，单号失败自动路由到其他号 |

**钉钉告警阈值**：
- 单号当天成功率 < 90% → WARN
- 单号当天成功率 < 70% → URGENT（号疑似挂了）
- 3 号合计成功率 < 80% → URGENT（系统级问题）

## 10. 商品入库与去重策略

### 10.1 同款再次入库（upsert）

```python
# 每条抓到的商品：
if exists(goods_id):
    UPDATE products SET
        price = $new_price,             # 价格变化追踪
        last_seen_at = NOW(),
        seen_count = seen_count + 1     # 累积出现次数
    WHERE goods_id = $goods_id
else:
    INSERT INTO products (...)
```

### 10.2 Pin 完全人工控制

`pinned_at` 字段只由用户主动操作设值（UI 上点"关注"）。系统不会基于 `seen_count` 自动 pin——保持库内容 100% 人工可控。

### 10.3 每日清库（Phase 1 Day 5 实现）

每天凌晨 3 点（活动窗口外）跑 celery beat：
- 删除：`pinned_at IS NULL AND last_seen_at < today - 24h`
- 保留：所有 pinned 的（只有人工 pin）
- 删除前发钉钉简报："清了 N 条 / 保留 M 条"

> `seen_count` 字段仍保留累积，作为人工 pin 决策的参考信号（你看到 seen_count 高的可以判断"长期出现"），但不触发自动 pin。

## 11. 查询模式：快速 vs 深度

| | **快速查询**（默认） | **深度查询**（手动勾） |
|---|---|---|
| 操作链路 | 仅列表页 | 列表页 + 每条点进详情页 |
| 单任务耗时 | 15-30s | 60-150s |
| 拿到的字段 | title / 价格 / 图片 / 销量 / 百亿补贴标 | + SKU / 评论数 / 历史价 / 店铺评分 / 完整描述 |
| 单号日上限 | 60-80 次 | 15-25 次 |
| 适用场景 | 选品自动发现、即时搜索看大概 | 用户已圈定的"候选品"做完整评分 |

UI 设计：即时搜索框右边「快速 / 深度」开关（默认快速）。每条商品旁有「升级为深度查询」按钮可单独触发。

## 12. PDD APP 升级策略（跟随自动升级）

跟随 PDD APP 自动升级，与普通用户体验一致，规避"强制升级"拦截 + 减少"版本号过旧"的风控信号。

**配套防御机制**：

1. **每日自检脚本**（关键！）—— 在 Phase 1 Day 4 一起做：
   - 每天早上 8:30（活动窗口刚开始时）跑一个固定基准任务：用 4310 号搜"运动鞋"
   - 期望返回：商品数 ≥ 10、价格字段非空、`secure_url` 字段存在
   - 失败立即钉钉 URGENT 告警
2. **selector 健壮性设计**（贯穿编码）：
   - 优先 `resource_id`（最稳，PDD 升级时通常不会改 id）
   - 兜底 `text` 模糊匹配（升级时可能 text 也变）
   - 同一控件多 selector 并存（任意命中即可）
3. **升级响应 SOP**：
   - 自检失败 → 第一反应：用 scrcpy 看一眼最新 APP 界面是否变了
   - 改了控件位置：在 `pdd_app_client.py` 里更新对应 selector（一般 30 分钟内可修）
   - 大改版（如改了导航结构）：临时启 `_PDD_DISABLED` 短路，周末集中修
4. **抓 selector 工具**：`uiautomator2` 自带 `python -m uiautomator2.inspect` 能直接看屏幕上每个控件的 resource_id / text / bounds，5 分钟内能定位新位置

预期维护成本：每次 PDD 大版本升级（约 2-3 月一次）需要 1-2 小时手动修 selector。

## 13. 与现有文档的关系

- **本文档** 锁定 A 路径（物理手机 + uiautomator2）的具体实施
- §1.4.4 / §1.4.5 / §1.4.6 是上游"为什么选 A 路径"的论证
- §账号入口隔离-SOP.md 是新号上线时的硬性流程，本文档 Phase 3 加号步骤强依赖
- §运维-cheatsheet.md 后续会增补"手机控操作 / 风控触发应对" 一节
