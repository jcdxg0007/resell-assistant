@echo off
REM ============================================================
REM PDD worker 管家(supervisor)启动脚本
REM 整个采集系统只需常驻这一个进程：它替前端检测设备、启停各手机的 worker、
REM 一键 git 更新。每台手机的 serial 不用在这里配——账号绑定从后端自动取，
REM 在前端「Worker机器」面板点按钮即可启停/更新。
REM
REM 启动后别关这个窗口（最小化即可）。想彻底无人值守，见下方「开机自启」。
REM ============================================================
cd /d C:\resell\worker

REM 直接用 venv 里的 python.exe（比 activate 稳）。依次探测常见位置。
set "PYEXE=C:\resell\worker\pdd_app_worker\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=C:\resell\worker\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=C:\resell\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
echo 使用解释器: %PYEXE%

"%PYEXE%" -m pdd_app_worker.supervisor
pause
