@echo off
REM ============================================================
REM 国内拉取最新代码（走 GitHub 镜像）。
REM 直连 github.com 在国内常 Connection reset / 443 超时，用镜像稳。
REM 镜像偶尔会失效/限速，连不上就把下面 MIRROR 换一个再跑：
REM   https://ghfast.top          https://gh-proxy.com
REM   https://mirror.ghproxy.com  https://ghproxy.net
REM 真实 serial/采集号在 phone_N.env.local.bat 里，已被 .gitignore 忽略，
REM 本次 pull 不会动它（roadmap §15.4）。
REM ============================================================
set "MIRROR=https://ghfast.top"
set "REPO=https://github.com/jcdxg0007/resell-assistant.git"
cd /d C:\resell
echo 走镜像拉取: %MIRROR%/%REPO%  main
git pull %MIRROR%/%REPO% main
pause
