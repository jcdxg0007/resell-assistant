@echo off
REM ============================================================
REM PDD worker 启动脚本 - 手机 2
REM 下面三项改成第二台手机的真实值（adb devices 看序列号）：
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

set ADB_SERIAL=<改成第二台手机序列号>
set WORKER_NAME=phone-2
set BOUND_PDD_ACCOUNT=<改成第二个PDD账号>

python -m pdd_app_worker.main
pause
