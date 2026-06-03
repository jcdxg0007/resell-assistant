@echo off
REM ============================================================
REM PDD worker 启动脚本 - 手机 1
REM 机器专属值（ADB_SERIAL + BOUND_PDD_ACCOUNT）放在 git 忽略的本地文件
REM   phone_1.env.local.bat —— git pull 永不覆盖（roadmap §15.4）。
REM 首次部署：复制 phone.env.example.bat 为 phone_1.env.local.bat 再填真实值。
REM ============================================================
cd /d C:\resell\worker

REM 直接用 venv 里的 python.exe（比 activate 稳）。依次探测常见位置。
set "PYEXE=C:\resell\worker\pdd_app_worker\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=C:\resell\worker\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=C:\resell\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
echo 使用解释器: %PYEXE%

set WORKER_NAME=phone-1
REM 载入本机专属 serial + 采集号（不在 git 里）
if not exist "%~dp0phone_1.env.local.bat" (
  echo [错误] 缺少 phone_1.env.local.bat —— 请复制 phone.env.example.bat 改名并填真实值
  pause
  exit /b 1
)
call "%~dp0phone_1.env.local.bat"

"%PYEXE%" -m pdd_app_worker.main
pause
