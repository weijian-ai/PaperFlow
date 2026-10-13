from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator

import pytest


@pytest.fixture
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return port  # socket released; daemon can bind to this port


@pytest.fixture
async def running_daemon(free_port: int) -> AsyncGenerator[subprocess.Popen[bytes], None]:
    env = os.environ.copy()
    env["PAPERFLOW_PORT"] = str(free_port)
    env["PAPERFLOW_LOG_FILE"] = ""
    env["PAPERFLOW_LOG_LEVEL"] = "WARNING"

    proc = subprocess.Popen([sys.executable, "-m", "paperflow.core"], env=env)

    # Windows 上解释器冷启动 + 依赖导入可能超过 3 秒，这里放宽到 20 秒
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            proc.wait()
            pytest.fail(f"Daemon exited during startup with code {proc.returncode}")
        await asyncio.sleep(0.05)
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
            writer.close()
            await writer.wait_closed()
            break
        except (ConnectionRefusedError, OSError):
            pass
    else:
        proc.terminate()
        proc.wait()
        pytest.fail("Daemon did not start within 20 seconds")

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
