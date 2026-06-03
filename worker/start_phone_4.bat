@echo off
REM ============================================================
REM PDD worker 启动脚本 - 手机 4
REM 下面三项改成第四台手机的真实值（adb devices 看序列号，要 device 状态）：
REM   ADB_SERIAL / WORKER_NAME / BOUND_PDD_ACCOUNT
REM
REM 【重要·多号路由】BOUND_PDD_ACCOUNT 现在决定 backend 把哪些品类的任务
REM 发给本机（词库管理页分配）。它必须等于 accounts 表里 platform='pdd_crawler'
REM 的某个 account_name，且就是这台手机此刻真正登录的号。
REM 现有可用号（截至 2026-06-03）：pdd_crawler_1876 / _2117 / _4310 / _5514
REM （7315 已绑手机1）。换号同步跑 backend/scripts/pdd_account_swap.py
REM ============================================================
cd /d C:\resell\worker

REM 直接用 venv 里的 python.exe（比 activate 稳）。依次探测常见位置。
set "PYEXE=C:\resell\worker\pdd_app_worker\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=C:\resell\worker\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=C:\resell\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
echo 使用解释器: %PYEXE%

set ADB_SERIAL=<改成第四台手机序列号>
set WORKER_NAME=phone-4
set BOUND_PDD_ACCOUNT=<改成第四个PDD账号>

"%PYEXE%" -m pdd_app_worker.main
pause
