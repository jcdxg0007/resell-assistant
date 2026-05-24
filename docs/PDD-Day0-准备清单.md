# Phase 1 Day 0 准备清单

> 配套文档：`PDD-自建采集-roadmap.md`。Day 0 完成后即可进入 Day 1 联调。
> 预计耗时：30-60 分钟。**遇到任何卡点把屏幕截图或报错文字发我，不要硬磕。**

## A. Windows worker host 准备（家里那台电脑）

> 用你日常远程桌面进的那台电脑。需要管理员权限。

### A.1 装 Python 3.11+

1. 浏览器打开 https://www.python.org/downloads/windows/
2. 下载 "Windows installer (64-bit)" 当前最新 3.11.x 或 3.12.x（不要 3.13，部分依赖还没适配）
3. 双击安装，**第一屏务必勾选 "Add python.exe to PATH"**，然后 Install Now
4. 安装完打开 cmd（开始菜单搜 `cmd`），输入：
   ```
   python --version
   pip --version
   ```
   能看到版本号就算成功。

### A.2 装 Android Platform Tools（adb.exe 的来源）

1. 浏览器打开 https://developer.android.com/tools/releases/platform-tools
2. 下载 "Download SDK Platform-Tools for Windows" 解压到 `C:\platform-tools\`
3. 把 `C:\platform-tools` 加到系统 PATH（开始菜单搜"环境变量" → 系统变量 Path → 编辑 → 新建 → 粘贴 `C:\platform-tools` → 一路确定）
4. 重开 cmd，输入：
   ```
   adb version
   ```
   能看到版本号就成功。

### A.3 装 scrcpy（远程看屏 + 控制手机，调试和接管必备）

1. 浏览器打开 https://github.com/Genymobile/scrcpy/releases
2. 下载最新的 `scrcpy-win64-vX.X.zip`，解压到 `C:\scrcpy\`
3. 把 `C:\scrcpy` 也加到 PATH（同 A.2 步骤）
4. 重开 cmd，输入 `scrcpy --version` 确认

### A.4 装 Git（用来同步代码到 home 这边）

1. https://git-scm.com/download/win 下载安装，全程默认即可
2. 完成后 cmd 里 `git --version` 验证

> 完成 A 之后，把家里电脑当成开发机的一半——后续 Phase 1 的 worker 代码会拉到这里跑。

## B. 手机准备（先准备 1 台，对应 4310 号）

> 4310 是计划在 Phase 1 第一周用的"试错号"。先准备装着 4310 号 PDD 的那台手机就行。其余两台 Phase 2 再说。

### B.1 开开发者选项

**荣耀**：设置 → 关于手机 → 连续点 "版本号" 7 次，提示"开发者模式已开启"
**OPPO**：设置 → 关于手机 → 版本信息 → 连续点 "软件版本号" 7 次（个别机型在"版本"页）

### B.2 开 USB 调试 + 关闭"USB 安装监控"

进入 设置 → 系统/更多设置 → 开发者选项，打开以下开关：

| 选项 | 状态 |
|---|---|
| USB 调试 | 开 |
| USB 调试（安全设置）| 开（如有，允许通过 USB 调试修改权限/模拟点击）|
| 仅充电模式下允许 ADB 调试 | 开（如有）|
| USB 安装 | 开（让 adb 装 APK 不弹安装确认框）|

### B.3 USB 连接测试

1. 用数据线把这台手机连到 Windows PC（注意：必须是带数据传输的线，不是只能充电的"垃圾线"，最好用手机原装线）
2. 手机会弹"是否允许此电脑进行 USB 调试？" → **勾选"一律允许"+ 确定**
3. 在电脑 cmd 里输入：
   ```
   adb devices
   ```
   应该看到类似输出（一行设备号 + `device`）：
   ```
   List of devices attached
   ABC123DEF456    device
   ```

> 如果显示 `unauthorized` —— 手机上没点"允许"，重新拔插 USB 重试。
> 如果完全没输出 —— USB 线不行换一条 / Windows 没装手机驱动（部分机型需要厂商驱动）。

### B.4 关闭 PDD 后台限制（重要）

**荣耀**：设置 → 应用 → 应用启动管理 → PDD → 关闭"自动管理"→ 全部允许（自启动、关联启动、后台活动）
**OPPO**：设置 → 电池 → 应用耗电管理 → PDD → 关闭"应用睡眠"、关闭"后台冻结"、关闭"自动启动"管理

否则手机长时间不操作会被系统杀掉 PDD 进程，导致 worker 操作时要重新启动 APP，浪费时间。

### B.5 确认 PDD APP 已登录 4310 + 已经至少能搜出商品

打开 PDD，确认：
- 个人中心是 4310 号（不是 5514 / 7315）
- 搜索"运动鞋"能看到商品列表（如果搜空那说明 4310 在 APP 端也被 shadowban 了，调试影响小但要告诉我，会调整 Day 6-7 切到 5514 的时间）

## C. 网络与连通性

### C.1 Sealos Redis 公网可达

我会在 Day 1 前给你一条命令，让你在 Windows cmd 里跑，验证家里电脑能连到 Sealos Redis。这个等我并行做完脚手架后告诉你。

### C.2 远程桌面双因子稳定

确认你日常用的远程桌面（向日葵 / TeamViewer / Windows 自带 RDP / ToDesk）：
- 在外网（手机 4G）也能登
- 登入后看到 Windows 桌面正常
- 不需要每次都人在家里点"接受"

## D. 完成核对（每项打勾，发我截图或文字回执）

- [ ] `python --version` 输出 3.11.x 或 3.12.x
- [ ] `adb --version` 能跑
- [ ] `scrcpy --version` 能跑
- [ ] `git --version` 能跑
- [ ] 手机连 USB 后 `adb devices` 能看到设备
- [ ] PDD APP 是 4310 登录态，能搜出商品
- [ ] 远程桌面在外网能进
- [ ] PDD 后台限制已关

全部打勾后就告诉我"Day 0 done"，我们立刻进 Day 1。

## E. 卡点处理

如果遇到：

| 症状 | 原因 | 处理 |
|---|---|---|
| `adb devices` 显示 unauthorized | 手机端没点允许 | 拔插 USB，注意手机屏幕上的允许弹窗 |
| `adb devices` 完全空 | USB 线/驱动问题 | 换线；荣耀需装"HiSuite"或"华为手机助手"装驱动；OPPO 需装"OPPO 手机助手" |
| `pip` 提示 SSL 错误 | 网络/证书 | 把命令加 `-i https://pypi.tuna.tsinghua.edu.cn/simple` 走清华源 |
| 手机连 USB 一直反复弹"允许调试" | 系统时间不对/USB 调试授权过期 | 重启手机；或 cmd 里 `adb kill-server && adb start-server` |
| PDD 启动慢/转圈 | 后台被系统杀 | 按 B.4 重新关闭后台限制 |
