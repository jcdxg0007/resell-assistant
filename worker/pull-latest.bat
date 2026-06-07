@echo off
REM ============================================================
REM Pull latest code via a GitHub mirror (for use inside China).
REM Direct github.com often fails (connection reset / 443 timeout),
REM so a mirror is more reliable. If the mirror is down or slow,
REM switch MIRROR below to another one and re-run:
REM   https://ghfast.top          https://gh-proxy.com
REM   https://mirror.ghproxy.com  https://ghproxy.net
REM Real serial / collector id live in phone_N.env.local.bat,
REM which is gitignored, so this pull never touches it (roadmap 15.4).
REM ============================================================
set "MIRROR=https://ghfast.top"
set "REPO=https://github.com/jcdxg0007/resell-assistant.git"
cd /d C:\resell
echo Pulling via mirror: %MIRROR%/%REPO%  main
git pull %MIRROR%/%REPO% main
pause
