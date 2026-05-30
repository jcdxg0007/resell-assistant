@echo off
REM ============================================================
REM PDD worker 启动脚本 - 手机 1
REM 双击运行即可。每台手机一个脚本，三项必须各不相同：
REM   ADB_SERIAL         手机序列号（adb devices 看）
REM   WORKER_NAME        worker 唯一名（随便起，别重复）
REM   BOUND_PDD_ACCOUNT  这台手机登录的 PDD 账号
REM 其它配置（backend 地址 / token）共用 worker\.env
REM ============================================================
cd /d C:\resell\worker
REM 自动找虚拟环境（依次探测三个常见位置）
if exist "C:\resell\worker\pdd_app_worker\.venv\Scripts\activate.bat" (
  call "C:\resell\worker\pdd_app_worker\.venv\Scripts\activate.bat"
) else if exist "C:\resell\worker\.venv\Scripts\activate.bat" (
  call "C:\resell\worker\.venv\Scripts\activate.bat"
) else if exist "C:\resell\.venv\Scripts\activate.bat" (
  call "C:\resell\.venv\Scripts\activate.bat"
) else (
  echo [警告] 没找到 .venv，将用全局 python。若报 ModuleNotFoundError 请改本行 venv 路径。
)

set ADB_SERIAL=PKT0220416005274
set WORKER_NAME=phone-1
set BOUND_PDD_ACCOUNT=pdd_crawler_7315

python -m pdd_app_worker.main
pause
