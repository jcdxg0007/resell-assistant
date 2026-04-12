# 开发进度总览

> 最后更新：2026-04-12

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
| 订单管理 | ✅ | ✅ | 列表、统计、手动采购，Celery 定时检测（部分 TODO） |

### ⚠️ 部分完成（后端可用，前端未完全对接）

| 模块 | 后端 | 前端 | 说明 |
|------|:----:|:----:|------|
| 选品 — 闲鱼 | ✅ | ⚠️ | 推荐/评分 API 可用，爬虫+定时任务已实现，前端搜索接口不匹配 |
| 选品 — 小红书 | ✅ | ⚠️ | 话题/关键词/推荐 API 齐全，前端页面有对接，数据依赖爬虫任务 |
| 客服消息 | ✅ | ❌ | 后端会话/消息 API + 定时采集就绪，前端页面仅有布局占位 |
| AI 运营 | ✅ | ❌ | 自检/日报/建议 API 存在，仪表盘有引用，独立页面未对接 |

### 🔲 脚手架（待开发）

| 模块 | 后端 | 前端 | 说明 |
|------|:----:|:----:|------|
| 闲鱼工作台 | ⚠️ | ❌ | 发布服务已实现但 HTTP 未触发 Celery，前端空表格 |
| 小红书工作台 | ⚠️ | ❌ | 发布服务存在但无 REST API 暴露，前端空表格 |
| 虚拟商品选品 | ⚠️ | ❌ | 后端仅简单查询，前端未调用 |
| 系统设置 | ❌ | ❌ | 无后端 API，前端仅禁用输入框占位 |

---

## Celery 定时任务

| 任务 | 状态 | 频率 | 说明 |
|------|:----:|------|------|
| 会话巡检 | ✅ | cron | 定时检查账号登录状态，过期自动标记 |
| 闲鱼选品发现 | ✅ | cron | 爬虫抓取市场数据，写入 Product 表 |
| 小红书话题/笔记扫描 | ✅ | cron | 爬虫抓取竞品数据、热门话题 |
| 订单检测 | ✅ | 3 分钟 | 检测新订单，触发通知 |
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
| `backend/app/api/v1/orders.py` | 订单管理 API |
| `backend/app/api/v1/selection.py` | 选品推荐/评分 API |
| `backend/app/api/v1/xiaohongshu.py` | 小红书趋势/竞品/模板 API |
| `backend/app/api/v1/xianyu.py` | 闲鱼上架/发布/市场数据 API |
| `backend/app/api/v1/customer.py` | 客服会话/消息 API |
| `backend/app/api/v1/ai_ops.py` | AI 运营自检/日报 API |
| `backend/app/services/browser.py` | Playwright 浏览器管理 + Cookie 持久化 |
| `backend/app/services/proxy_service.py` | 青果代理自动解析/提取/白名单 |
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
| `frontend/src/pages/orders/index.tsx` | 订单管理页 |
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

## 建议的开发优先级

1. **闲鱼/小红书工作台** — 打通发布链路（HTTP → Celery → Playwright）
2. **客服页面** — 前端对接已有的后端消息 API
3. **选品搜索修复** — 修复前端搜索接口不匹配问题
4. **AI 运营页面** — 前端对接自检/日报 API
5. **系统设置** — 实现配置管理后端 + 前端
