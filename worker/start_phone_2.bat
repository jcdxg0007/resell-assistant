@echo off
REM ============================================================
REM PDD worker 启动脚本 - 手机 2
REM 下面三项改成第二台手机的真实值（adb devices 看序列号，要 device 状态）：
REM   ADB_SERIAL         手机序列号
REM   WORKER_NAME        worker 唯一名（别和别的手机重复）
REM   BOUND_PDD_ACCOUNT  这台手机【当前登录】的 PDD 账号 account_name
REM
REM 【重要·多号路由】BOUND_PDD_ACCOUNT 现在不只是日志，backend 会按它把
REM "分配给这个号的品类"的任务发给本机（词库管理页分配）。所以它必须：
REM   1) 完全等于 accounts 表里 platform='pdd_crawler' 的某个 account_name；
REM   2) 就是这台手机此刻真正登录的那个号（不一致会采错品类）。
REM 现有可用号（截至 2026-06-03）：pdd_crawler_1876 / _2117 / _4310 / _5514
REM （pdd_crawler_7315 已绑手机1）。换号要同步跑 backend/scripts/pdd_account_swap.py
REM ============================================================
cd /d C:\resell\worker

REM 直接用 venv 里的 python.exe（比 activate 稳）。依次探测常见位置。
set "PYEXE=C:\resell\worker\pdd_app_worker\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=C:\resell\worker\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=C:\resell\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
echo 使用解释器: %PYEXE%

set ADB_SERIAL=<改成第二台手机序列号>
set WORKER_NAME=phone-2
set BOUND_PDD_ACCOUNT=<改成第二个PDD账号>

"%PYEXE%" -m pdd_app_worker.main
pause
