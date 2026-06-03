@echo off
REM ============================================================
REM worker 机器专属配置模板（serial + 采集号）
REM
REM 用法（每台手机一次性）：把本文件复制成 phone_N.env.local.bat
REM   （N = 1/2/3/4，对应 start_phone_N.bat），再填下面两行真实值。
REM   *.env.local.bat 已被 .gitignore 忽略 —— git pull 永远不会覆盖它，
REM   今后更新代码不再和你的本地配置冲突（roadmap §15.4）。
REM
REM 两个值的要求：
REM   ADB_SERIAL         adb devices 看，要 device 状态的那串
REM   BOUND_PDD_ACCOUNT  必须等于 accounts 表里 platform='pdd_crawler' 的某个
REM                      account_name，且 = 这台手机【当前登录】的号。
REM                      现有号：pdd_crawler_7315(手机1) / _1876 / _2117 / _4310 / _5514
REM                      换号同步跑 backend/scripts/pdd_account_swap.py（或找后端改 DB）
REM ============================================================
set ADB_SERIAL=<adb devices 看到的序列号>
set BOUND_PDD_ACCOUNT=<pdd_crawler_xxxx，且=本机当前登录号>
