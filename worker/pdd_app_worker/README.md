# PDD APP Worker（家里 Windows）

跑在家里 Windows PC 上，物理连接 USB 手机，用 uiautomator2 操控 PDD APP。

配套文档：`docs/PDD-自建采集-roadmap.md` + `docs/PDD-Day0-准备清单.md`。

## 架构定位

```
backend (云端 K8s)  ──HTTPS──►  此 worker  ──USB──►  手机里的 PDD APP
```

worker 通过 HTTPS 长轮询拉任务、推结果，不直连 K8s 内部 Redis。

## Windows 安装

> 跑完 `docs/PDD-Day0-准备清单.md` 的 A/B 段后做这一步。

> ⚠️ **目录易踩坑**：venv 装在 `pdd_app_worker\.venv` 里，但跑 `python -m pdd_app_worker.xxx` 时
> **当前目录必须是 `worker\`**（pdd_app_worker 的**父目录**），否则会报
> `ModuleNotFoundError: No module named 'pdd_app_worker'`。

```cmd
:: 1. 把项目 clone 到 Windows（推荐位置 C:\resell）
git clone <repo url> C:\resell

:: 2. 进 worker（父目录）
cd /d C:\resell\worker

:: 3. 创建 venv（venv 放在子包目录里，方便随包一起备份/迁移）
python -m venv pdd_app_worker\.venv
pdd_app_worker\.venv\Scripts\activate

:: 4. 装依赖
pip install -r pdd_app_worker\requirements.txt

:: 5. 配置环境变量
copy pdd_app_worker\.env.example pdd_app_worker\.env
:: 用记事本打开 pdd_app_worker\.env，填好下面 4 个值：
::   BACKEND_BASE_URL  - 例如 https://jbbobxkpstwp.sealosbja.site
::   WORKER_TOKEN      - 跟 backend 上配置的 PDD_WORKER_TOKEN 完全一致
::   WORKER_NAME       - 给本机起个名（默认 windows-home）
::   LOG_LEVEL         - 默认 INFO

:: 6. 烟测：先单独跑连通性测试（始终在 worker\ 目录下跑）
python -m pdd_app_worker.smoke_test

:: 7. 真正启动 worker（先连一台手机）
python -m pdd_app_worker.main
```

成功的话你会看到类似：

```
[INFO] pdd_app_worker: connected to backend https://....sealosbja.site
[INFO] pdd_app_worker: detected devices: ['ABC123']
[INFO] pdd_app_worker: heartbeat sent
[INFO] pdd_app_worker: polling for tasks... (long-poll 25s)
```

## 开机自启（Phase 1 Day 4 配）

用 Windows 任务计划程序：

1. 开始菜单搜"任务计划程序"
2. 创建基本任务 → 名字 `PDDAppWorker`
3. 触发器：当 Windows 启动时
4. 操作：启动程序 → `C:\resell\worker\pdd_app_worker\.venv\Scripts\python.exe`
5. 添加参数：`-m pdd_app_worker.main`
6. 起始位置：`C:\resell\worker\pdd_app_worker`
7. 完成后右键→属性→"不管用户是否登录都要运行"+"使用最高权限运行"

## 日常运维

- 看实时日志：`tail -f logs/worker.log`（PowerShell 用 `Get-Content -Wait .\logs\worker.log`）
- 重启 worker：任务计划程序里右键→结束→运行
- 加新手机：直接 USB 插上 → worker 下一次心跳就会上报
- 想暂停采集（不停 worker，只是不接任务）：backend 侧把 `_PDD_DISABLED=True` 重启

## 目录结构

```
pdd_app_worker/
├── __init__.py
├── main.py              # 主循环：心跳 + 拉任务 + 派发
├── http_client.py       # backend HTTP bridge 客户端
├── device_manager.py    # adb device 列表 + 设备状态
├── pdd_app_client.py    # PDD APP 操作封装（Phase 1 Day 2 实现）
├── smoke_test.py        # 启动前烟测：连通性 + adb + 手机
├── requirements.txt
├── .env.example
└── logs/
```
