# 开发进度总览

> 最后更新：2026-04-13（第二次更新）

## 项目简介

转售助手（Resell Assistant）— 多平台自动化选品、上架、订单、客服一体化管理系统。

- **前端：** React 18 + Ant Design 5 + Vite
- **后端：** Python 3.12 + FastAPI + Celery
- **数据库：** PostgreSQL + Redis
- **自动化：** Playwright（爬虫/登录/发布）
- **部署：** Docker + Sealos + GitHub Actions CI/CD

---

## 模块状态

### ✅ 已完成（可运行）

| 模块 | 后端 | 前端 | 说明 |
|------|:----:|:----:|------|
| 用户认证 | ✅ | ✅ | 管理员登录、JWT 鉴权、首次初始化 |
| 账号管理 | ✅ | ✅ | CRUD、平台登录（手机号+验证码）、会话检查、生命周期管理 |
| 代理系统 | ✅ | ✅ | 青果网络长效代理自动集成，支持 `qgnet:KEY` 格式 |
| 仪表盘 | ✅ | ✅ | 聚合订单统计、账号概览、AI 建议展示 |
| 订单管理 | ✅ | ✅ | 列表、统计、手动采购、自动/半自动采购模式切换，Celery 定时检测 |
| 系统设置 | ✅ | ✅ | 采购模式切换（自动/半自动）、钉钉 Webhook 配置、设置持久化 |

### ⚠️ 部分完成（可用但依赖数据/外部服务）

| 模块 | 后端 | 前端 | 说明 |
|------|:----:|:----:|------|
| 选品 — 闲鱼 | ✅ | ✅ | 推荐/评分/搜索已打通，搜索 → Celery 即时爬虫 → 刷新查看 |
| 选品 — 小红书 | ✅ | ✅ | 话题/关键词/推荐 API 齐全，前端页面已对接 |
| 选品 — 虚拟商品 | ✅ | ✅ | 前端已对接 `/selection/virtual/recommendations` |
| 闲鱼工作台 | ✅ | ✅ | API → Celery → Playwright 全链路打通，前端支持发布/擦亮/下架 |
| 小红书工作台 | ✅ | ✅ | 笔记 CRUD + 发布 + AI 内容生成 API 已完整，前端已对接 |
| 客服消息 | ✅ | ✅ | 会话列表、消息详情、AI 预回复、发送回复均已对接 |
| AI 运营 | ✅ | ✅ | 自检/日报/建议三个 Tab 均对接后端 API |

### 🔲 待完善

| 模块 | 说明 |
|------|------|
| 系统设置（基础/AI） | 基础设置和 AI 设置 Tab 仍为占位 |
| 客服 Playwright 实发 | 回复仅写入数据库，未通过 Playwright 发送到真实平台 |
| 小红书发布 Playwright | 发布笔记状态改为 scheduled，实际 Playwright 执行待接入 Celery 任务 |

---

## Celery 定时任务

| 任务 | 状态 | 频率 | 说明 |
|------|:----:|------|------|
| 会话巡检 | ✅ | cron | 定时检查账号登录状态，过期自动标记 |
| 闲鱼选品发现 | ✅ | cron | 爬虫抓取市场数据，写入 Product 表 |
| 小红书话题/笔记扫描 | ✅ | cron | 爬虫抓取竞品数据、热门话题 |
| 订单检测 | ✅ | 3 分钟 | 检测新订单，按采购模式走自动采购或钉钉通知 |
| 物流同步 | ✅ | 定时 | 同步物流状态更新 |
| 客服消息轮询 | ⚠️ | 3 分钟 | 仅闲鱼平台已实现 |
| 批量擦亮 | ⚠️ | cron | 任务已定义，核心逻辑 TODO |
| 发布执行 | ⚠️ | cron | 服务层实现，HTTP→Celery 触发未打通 |
| 退款检查 | ⚠️ | 定时 | 循环体内 TODO |
| 日计数重置 | ✅ | 每日 0 点 | 重置每日发布计数 |

---

## 代理系统详情

**当前方案：** 青果网络长效代理

- 配置格式：`qgnet:3DB99AC7`（填入账号的代理 IP 字段）
- 当前代理：`125.75.110.68:62473`（甘肃兰州，电信，到期 2026-04-13）
- 默认提取地区：广州(440100) + 深圳(440300)

**自动化功能：**
- ✅ 自动查询青果 API 获取当前代理 IP
- ✅ 代理到期后自动提取新 IP（指定广州/深圳）
- ✅ 容器重启后自动注册 IP 白名单
- ✅ 前端显示实际代理 IP + 城市 + 运营商

---

## 关键文件索引

### 后端核心

| 文件 | 用途 |
|------|------|
| `backend/app/main.py` | FastAPI 主入口、中间件、调试端点 |
| `backend/app/api/v1/accounts.py` | 账号 CRUD + 登录流 + 会话检查 + 代理状态 |
| `backend/app/api/v1/orders.py` | 订单管理 API（手动采购、退货、退款）|
| `backend/app/api/v1/settings.py` | 系统设置 API（采购模式、钉钉配置读写）|
| `backend/app/api/v1/selection.py` | 选品推荐/评分 API（闲鱼/小红书/虚拟商品）|
| `backend/app/api/v1/products.py` | 商品 CRUD + 搜索任务提交 |
| `backend/app/api/v1/xiaohongshu.py` | 小红书趋势/竞品/模板 API |
| `backend/app/api/v1/xianyu.py` | 闲鱼上架/发布/市场数据 API |
| `backend/app/api/v1/customer.py` | 客服会话/消息 API |
| `backend/app/api/v1/ai_ops.py` | AI 运营自检/日报 API |
| `backend/app/services/browser.py` | Playwright 浏览器管理 + Cookie 持久化 |
| `backend/app/services/proxy_service.py` | 青果代理自动解析/提取/白名单 |
| `backend/app/services/notification.py` | 多渠道通知（钉钉+邮件），含半自动订单通知模板 |
| `backend/app/services/platform_login.py` | 平台登录（手机号+验证码）|
| `backend/app/services/session_checker.py` | 离线会话检查（Cookie 分析）|
| `backend/app/models/system.py` | 用户/账号/任务/通知/配置 模型 |
| `backend/app/models/product.py` | 商品/市场数据/竞品 模型 |
| `backend/app/core/celery_app.py` | Celery 配置 + Beat 调度 |

### 前端核心

| 文件 | 用途 |
|------|------|
| `frontend/src/App.tsx` | 应用入口（ConfigProvider + AntdApp）|
| `frontend/src/router.tsx` | 路由配置 |
| `frontend/src/layouts/MainLayout.tsx` | 侧栏菜单布局 |
| `frontend/src/pages/accounts/index.tsx` | 账号管理页（登录/检查/代理显示）|
| `frontend/src/pages/dashboard/index.tsx` | 仪表盘 |
| `frontend/src/pages/orders/index.tsx` | 订单管理页（待采购高亮、一键复制地址、手动采购录入）|
| `frontend/src/pages/sales/XianyuWorkbench.tsx` | 闲鱼工作台（草稿/发布/擦亮/下架）|
| `frontend/src/pages/sales/XhsWorkbench.tsx` | 小红书工作台（笔记管理/发布）|
| `frontend/src/pages/customer/index.tsx` | 客服中心（会话列表/消息/AI 预回复/发送）|
| `frontend/src/pages/ai-ops/index.tsx` | AI 运营中枢（日报/建议/自检）|
| `frontend/src/pages/selection/VirtualSelection.tsx` | 虚拟商品选品（已对接 API）|
| `frontend/src/pages/settings/index.tsx` | 系统设置页（采购模式切换、钉钉 Webhook 配置）|
| `frontend/src/pages/selection/XianyuSelection.tsx` | 闲鱼选品页 |
| `frontend/src/pages/selection/XhsSelection.tsx` | 小红书选品页 |
| `frontend/src/services/api.ts` | Axios 实例配置 |

### 部署相关

| 文件 | 用途 |
|------|------|
| `.github/workflows/build-and-push.yml` | CI/CD（条件构建+Docker缓存+Sealos重启）|
| `backend/Dockerfile` | 后端镜像 |
| `frontend/Dockerfile` | 前端镜像 |
| `deploy/backend.yaml` | Sealos 后端部署清单 |
| `deploy/frontend.yaml` | Sealos 前端部署清单 |

---

## 已知问题

1. **`/accounts/stats/summary` 路由顺序**：写在 `/{account_id}` 之后，可能被当成 `account_id="stats"` 处理
2. **前端闲鱼搜索**：调用 `POST /products/search`，但后端不存在此路由
3. **容器 IP 不固定**：Sealos 重启后 IP 会变，已通过自动白名单解决
4. **Playwright 状态持久化**：Cookie 已同步存储到 PostgreSQL，容器重启不丢失

---

## 采购模式说明

| 模式 | 设置值 | 新订单处理 |
|------|--------|-----------|
| **半自动** | `manual`（默认） | 钉钉推送详细通知（收货地址+货源链接+利润） → 手动在货源平台下单 → 在系统录入采购单号 |
| **自动** | `auto` | 系统自动调用 Playwright 在源平台下单 → 自动录入采购信息 |

切换路径：前端「系统设置 → 采购模式」→ Switch 开关，实时生效无需重启。

---

## 建议的开发优先级

1. ~~**配置钉钉机器人**~~ ✅ 已完成
2. ~~**闲鱼/小红书工作台**~~ ✅ 已完成 — HTTP → Celery → Playwright 全链路
3. ~~**客服页面**~~ ✅ 已完成 — 会话、消息、AI 预回复
4. ~~**选品搜索修复**~~ ✅ 已完成 — `POST /products/search` → Celery 即时爬虫
5. ~~**AI 运营页面**~~ ✅ 已完成 — 自检/日报/建议
6. **客服 Playwright 实发** — 当前回复仅写库，需接入 Playwright 自动化
7. **小红书发布 Celery 任务** — 笔记发布状态已改，需补 Celery → Playwright 执行
8. **数据积累** — 启动 Celery Beat 后各模块将自动填充数据
