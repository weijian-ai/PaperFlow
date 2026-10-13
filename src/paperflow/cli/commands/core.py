from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

from paperflow.core.config import PaperFlowConfig

_PID_FILE = Path.home() / ".paperflow" / "flow-core.pid"


# 尝试连接 daemon，成功则正常返回，失败则抛出 ConnectionRefusedError/OSError
async def _ping_check(config: PaperFlowConfig) -> None:
    _r, w = await asyncio.open_connection(config.host, config.port)
    w.close()
    await w.wait_closed()


# 判断指定 pid 的进程是否存活；Windows 上没有信号 0，改用 OpenProcess 查询
def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# 读取 PID 文件并确认进程存活，进程已消失则删除文件并返回 None
def _running_pid() -> int | None:
    if not _PID_FILE.exists():
        return None
    try:
        pid = int(_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        _PID_FILE.unlink(missing_ok=True)
        return None
    if not _pid_alive(pid):
        _PID_FILE.unlink(missing_ok=True)
        return None
    return pid


# 打印 daemon 当前状态（running / not running）
def cmd_core_status(config: PaperFlowConfig) -> None:
    try:
        asyncio.run(_ping_check(config))
        print(f"running  ({config.host}:{config.port})")
    except (ConnectionRefusedError, OSError):
        print("not running")


# 在后台启动 daemon，若已在运行则提示并退出
def cmd_core_start(config: PaperFlowConfig) -> None:
    try:
        asyncio.run(_ping_check(config))
        print(f"already running  ({config.host}:{config.port})")
        return
    except (ConnectionRefusedError, OSError):
        pass

    proc = subprocess.Popen(
        [sys.executable, "-m", "paperflow.core"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(proc.pid))
    print(f"started  pid={proc.pid}  ({config.host}:{config.port})")


# 向 daemon 发送信号停止进程，若未运行则提示；Windows 不支持 SIGTERM，用 terminate()
def cmd_core_stop(config: PaperFlowConfig) -> None:
    pid = _running_pid()
    if pid is None:
        print("not running")
        return
    if os.name == "nt":
        # Windows 无 POSIX 信号，直接终止进程树
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        os.kill(pid, signal.SIGTERM)
    _PID_FILE.unlink(missing_ok=True)
    print(f"stopped  pid={pid}")
