@echo off
REM ============================================================
REM PDD worker 启动脚本 - 手机 4
REM 下面三项改成第四台手机的真实值（adb devices 看序列号，要 device 状态）：
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
