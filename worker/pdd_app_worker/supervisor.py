"""PDD worker 管家(supervisor)——家里 PC 上常驻的「唯一」进程。

它替前端管理所有手机的 worker 子进程，把「一台台手动点开 .bat」和「手动 git pull
重启」都收敛成前端一键操作：

  - adb devices 检测连了哪几台手机；
  - 长轮询后端控制命令（/api/v1/pdd-worker/control/poll），按命令 spawn/kill 每台
    手机的 worker 子进程（python -m pdd_app_worker.main，自动带上该机绑定的账号）；
  - 「一键更新」= git pull + 重启所有在跑的 worker；
  - 子进程意外退出自动拉起（带失败上限，避免崩溃循环）；
  - 把最新状态（设备/子进程/git commit/最近命令结果）POST 回后端，供前端面板渲染。

启动（家里 PC，整个采集系统只需常驻这一个进程）：
    python -m pdd_app_worker.supervisor

每台手机的 serial↔账号绑定来自后端 accounts 表，新增手机只需在前端绑一次，
不用再编辑本地 .bat。公共配置（BACKEND_BASE_URL / WORKER_TOKEN）仍读 .env。
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from pdd_app_worker.device_manager import list_devices

load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("supervisor")

BACKEND_BASE_URL = os.environ["BACKEND_BASE_URL"].rstrip("/")
WORKER_TOKEN = os.environ["WORKER_TOKEN"]
API_PREFIX = "/api/v1/pdd-worker"
POLL_INTERVAL_S = float(os.environ.get("SUPERVISOR_POLL_INTERVAL", "3"))

# worker/pdd_app_worker/supervisor.py → parents[1]=worker 目录, parents[2]=仓库根
WORKER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]

IS_WINDOWS = os.name == "nt"
# 同一台手机连续意外退出超过这个次数就放弃自动拉起（多半是配置/登录态坏了，
# 别无脑重启刷屏），等前端手动处理。
_MAX_CONSECUTIVE_CRASH = 5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git(*args: str, timeout: int = 120) -> tuple[bool, str]:
    """在仓库根跑一条 git 命令，返回 (ok, 合并输出)。"""
    try:
        r = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode == 0, out.strip()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


class _Child:
    """一台手机对应的 worker 子进程封装。"""

    def __init__(self, serial: str, account: str | None, name: str) -> None:
        self.serial = serial
        self.account = account
        self.name = name
        self.proc: subprocess.Popen | None = None
        self.started_at: str | None = None
        self.crash_count = 0

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def start(self) -> tuple[bool, str]:
        if self.running:
            return True, f"{self.serial} 已在跑(pid={self.pid})"
        env = dict(os.environ)
        env["ADB_SERIAL"] = self.serial
        env["WORKER_NAME"] = self.name
        if self.account:
            env["BOUND_PDD_ACCOUNT"] = self.account
        creationflags = 0
        if IS_WINDOWS:
            # 独立进程组，便于整组结束
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "pdd_app_worker.main"],
                cwd=str(WORKER_DIR),
                env=env,
                creationflags=creationflags,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"启动失败: {type(exc).__name__}: {exc}"
        self.started_at = _now_iso()
        logger.info(
            f"started worker serial={self.serial} account={self.account} "
            f"name={self.name} pid={self.pid}"
        )
        return True, f"{self.serial} 已启动(pid={self.pid}, 账号={self.account or '未绑'})"

    def stop(self) -> tuple[bool, str]:
        if not self.running:
            self.proc = None
            return True, f"{self.serial} 本就没在跑"
        pid = self.pid
        try:
            if IS_WINDOWS:
                # /T 连同子进程一起结束（python 进程下还有 adb 等）
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, text=True, timeout=15,
                )
            else:
                self.proc.terminate()
            self.proc.wait(timeout=15)
        except Exception as exc:  # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
            logger.warning(f"stop {self.serial} 强杀兜底: {exc}")
        self.proc = None
        self.started_at = None
        logger.info(f"stopped worker serial={self.serial} pid={pid}")
        return True, f"{self.serial} 已停止"


class Supervisor:
    def __init__(self) -> None:
        self.children: dict[str, _Child] = {}
        # 期望在跑的 serial（用于子进程意外退出后自动拉起）
        self.desired: set[str] = set()
        self.bindings: dict[str, str] = {}
        self.last_results: list[dict[str, Any]] = []
        self._client = httpx.Client(
            base_url=BACKEND_BASE_URL,
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        )

    # ── 命令名 → 账号解析 ───────────────────────────────────────
    def _name_for(self, serial: str, account: str | None) -> str:
        if account:
            return account
        return f"phone-{serial[-6:]}" if len(serial) > 6 else f"phone-{serial}"

    def _child_for(self, serial: str) -> _Child:
        acct = self.bindings.get(serial)
        ch = self.children.get(serial)
        if ch is None:
            ch = _Child(serial, acct, self._name_for(serial, acct))
            self.children[serial] = ch
        else:
            # 绑定可能在后端更新过，刷新账号/名字（仅在没在跑时）
            if not ch.running and acct and ch.account != acct:
                ch.account = acct
                ch.name = self._name_for(serial, acct)
        return ch

    def _record(self, cmd: dict[str, Any], ok: bool, msg: str) -> None:
        self.last_results.insert(0, {
            "id": cmd.get("id"),
            "action": cmd.get("action"),
            "serial": cmd.get("serial"),
            "ok": ok,
            "msg": msg,
            "ts": _now_iso(),
        })
        del self.last_results[10:]
        logger.info(f"cmd done action={cmd.get('action')} serial={cmd.get('serial')} ok={ok} msg={msg}")

    # ── 命令执行 ────────────────────────────────────────────────
    def _connected_serials(self) -> list[str]:
        return [d.serial for d in list_devices() if d.state == "device"]

    def handle(self, cmd: dict[str, Any]) -> None:
        action = cmd.get("action")
        serial = cmd.get("serial")
        try:
            if action == "scan":
                self._record(cmd, True, f"已连接 {len(self._connected_serials())} 台")
            elif action == "start":
                ok, msg = self._start_one(serial)
                self._record(cmd, ok, msg)
            elif action == "stop":
                self.desired.discard(serial)
                ok, msg = self._child_for(serial).stop()
                self._record(cmd, ok, msg)
            elif action == "restart":
                self._child_for(serial).stop()
                ok, msg = self._start_one(serial)
                self._record(cmd, ok, "已重启; " + msg)
            elif action == "start_all":
                self._record(cmd, True, self._start_all())
            elif action == "stop_all":
                self._record(cmd, True, self._stop_all())
            elif action == "update":
                self._record(cmd, *self._update())
            else:
                self._record(cmd, False, f"未知命令: {action}")
        except Exception as exc:  # noqa: BLE001
            self._record(cmd, False, f"执行异常: {type(exc).__name__}: {exc}")

    def _start_one(self, serial: str | None) -> tuple[bool, str]:
        if not serial:
            return False, "缺 serial"
        if serial not in self._connected_serials():
            return False, f"{serial} 未连接(adb 看不到)"
        self.desired.add(serial)
        ch = self._child_for(serial)
        ch.crash_count = 0
        return ch.start()

    def _start_all(self) -> str:
        connected = self._connected_serials()
        started, skipped = [], []
        for s in connected:
            if not self.bindings.get(s):
                skipped.append(f"{s}(未绑号)")
                continue
            self.desired.add(s)
            ch = self._child_for(s)
            ch.crash_count = 0
            ok, _ = ch.start()
            (started if ok else skipped).append(s)
        msg = f"启动 {len(started)} 台"
        if skipped:
            msg += f"；跳过 {len(skipped)}: {', '.join(skipped)}"
        return msg

    def _stop_all(self) -> str:
        self.desired.clear()
        n = 0
        for ch in self.children.values():
            if ch.running:
                ch.stop()
                n += 1
        return f"停了 {n} 台"

    def _update(self) -> tuple[bool, str]:
        running = [s for s, ch in self.children.items() if ch.running]
        for ch in self.children.values():
            if ch.running:
                ch.stop()
        ok, out = _git("pull")
        tail = out.splitlines()[-1] if out else ""
        if not ok:
            # 拉取失败也要把刚停的 worker 拉回来，别让采集白停
            for s in running:
                self._child_for(s).start()
            return False, f"git pull 失败: {tail}；已恢复 {len(running)} 台"
        for s in running:
            self._child_for(s).start()
        commit, _ = self._git_commit()
        return True, f"已更新到 {commit}({tail})；重启 {len(running)} 台"

    # ── 自愈：子进程意外退出 → 自动拉起 ────────────────────────
    def _reap(self) -> None:
        for serial in list(self.desired):
            ch = self.children.get(serial)
            if ch is None or ch.running:
                if ch and ch.running:
                    ch.crash_count = 0
                continue
            # 期望在跑却死了 → 自动拉起（带上限）
            if ch.crash_count >= _MAX_CONSECUTIVE_CRASH:
                logger.error(
                    f"{serial} 连续崩溃 {ch.crash_count} 次，停止自动拉起，等前端处理"
                )
                self.desired.discard(serial)
                continue
            ch.crash_count += 1
            logger.warning(f"{serial} worker 退出，自动拉起(第 {ch.crash_count} 次)")
            ch.start()

    # ── 状态快照 ────────────────────────────────────────────────
    def _git_commit(self) -> tuple[str, str]:
        _, commit = _git("rev-parse", "--short", "HEAD", timeout=15)
        _, branch = _git("rev-parse", "--abbrev-ref", "HEAD", timeout=15)
        return commit or "?", branch or "?"

    def _build_status(self) -> dict[str, Any]:
        commit, branch = self._git_commit()
        devices = [{"serial": d.serial, "state": d.state} for d in list_devices()]
        workers = []
        for serial, ch in self.children.items():
            workers.append({
                "serial": serial,
                "account": ch.account,
                "name": ch.name,
                "running": ch.running,
                "pid": ch.pid,
                "started_at": ch.started_at,
                "crash_count": ch.crash_count,
            })
        return {
            "host": socket.gethostname(),
            "git_commit": commit,
            "git_branch": branch,
            "devices": devices,
            "workers": workers,
            "bindings": self.bindings,
            "desired": sorted(self.desired),
            "last_results": self.last_results,
        }

    def _push_status(self) -> None:
        try:
            r = self._client.post(f"{API_PREFIX}/control/status", json=self._build_status())
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"push status failed: {exc}")

    def _poll(self) -> list[dict[str, Any]]:
        try:
            r = self._client.get(f"{API_PREFIX}/control/poll")
            r.raise_for_status()
            data = r.json()
            self.bindings = data.get("bindings") or {}
            return data.get("commands") or []
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"poll failed: {exc}")
            return []

    def run(self) -> None:
        commit, branch = self._git_commit()
        logger.info(
            f"supervisor 启动 host={socket.gethostname()} repo={REPO_ROOT} "
            f"commit={commit} branch={branch} backend={BACKEND_BASE_URL}"
        )
        while True:
            try:
                self._reap()
                commands = self._poll()
                for cmd in commands:
                    self.handle(cmd)
                self._push_status()
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"supervisor loop error: {exc}")
            time.sleep(POLL_INTERVAL_S)


def main() -> None:
    try:
        Supervisor().run()
    except KeyboardInterrupt:
        logger.info("supervisor 收到 Ctrl+C，退出（不主动停 worker 子进程，它们会继续跑）")


if __name__ == "__main__":
    main()
