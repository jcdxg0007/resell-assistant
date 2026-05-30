@echo off
REM ============================================================
REM PDD worker 启动脚本 - 手机 3
REM 下面三项改成第三台手机的真实值（adb devices 看序列号）：
REM ============================================================
cd /d C:\resell\worker
call .venv\Scripts\activate.bat

set ADB_SERIAL=<改成第三台手机序列号>
set WORKER_NAME=phone-3
set BOUND_PDD_ACCOUNT=<改成第三个PDD账号>

python -m pdd_app_worker.main
pause
