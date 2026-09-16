from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.launch import api_command, resolve_port, supervise, worker_command

ROOT = str(Path(__file__).resolve().parents[1])


def test_resolve_port_defaults_and_env(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    assert resolve_port() == "8000"
    monkeypatch.setenv("PORT", "8080")
    assert resolve_port() == "8080"
    assert resolve_port("443") == "443"
    with pytest.raises(ValueError):
        resolve_port("not-a-port")


def test_api_and_worker_commands(monkeypatch):
    monkeypatch.setenv("PORT", "9000")
    api = api_command()
    assert api[-7:] == [
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "9000",
    ]
    assert worker_command()[-2:] == ["-m", "app.worker"]


def test_supervise_stops_remaining_process_when_one_exits():
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    failing = [sys.executable, "-c", "raise SystemExit(7)"]
    started = time.monotonic()
    status = supervise([sleeper, failing], poll_interval=0.05, shutdown_timeout=5)
    assert status != 0
    assert time.monotonic() - started < 8


def test_supervise_forwards_sigterm():
    wrapper = [
        sys.executable,
        "-c",
        (
            "from app.launch import supervise\n"
            "import sys\n"
            "cmd = [sys.executable, '-c', 'import time; time.sleep(30)']\n"
            "raise SystemExit(supervise([cmd, cmd], poll_interval=0.05, shutdown_timeout=5))\n"
        ),
    ]
    proc = subprocess.Popen(wrapper, cwd=ROOT, env={**os.environ, "PYTHONPATH": ROOT})
    time.sleep(0.4)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=8) == 0
