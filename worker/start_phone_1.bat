@echo off
REM ============================================================
REM PDD worker 启动脚本 - 手机 1
REM 双击运行即可。每台手机一个脚本，三项必须各不相同：
REM   ADB_SERIAL         手机序列号（adb devices 看，要 device 状态）
REM   WORKER_NAME        worker 唯一名（随便起，别重复）
REM   BOUND_PDD_ACCOUNT  这台手机登录的 PDD 账号
REM 其它配置（backend 地址 / token）共用 worker\.env
REM ============================================================
cd /d C:\resell\worker

REM 直接用 venv 里的 python.exe（比 activate 稳）。依次探测常见位置。
set "PYEXE=C:\resell\worker\pdd_app_worker\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=C:\resell\worker\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=C:\resell\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
echo 使用解释器: %PYEXE%

set ADB_SERIAL=PKT0220416005274
set WORKER_NAME=phone-1
set BOUND_PDD_ACCOUNT=pdd_crawler_7315

"%PYEXE%" -m pdd_app_worker.main
pause
