"""Run the API and worker together for single-container platforms such as Railway."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence

logger = logging.getLogger(__name__)

DEFAULT_API_PORT = "8000"
_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def resolve_port(value: str | None = None) -> str:
    raw = value if value is not None else os.environ.get("PORT", DEFAULT_API_PORT)
    raw = str(raw).strip() or DEFAULT_API_PORT
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise ValueError("PORT must be an integer between 1 and 65535")
    return raw


def api_command(port: str | None = None) -> list[str]:
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        resolve_port(port),
    ]


def worker_command() -> list[str]:
    return [sys.executable, "-m", "app.worker"]


def _stop_process(proc: subprocess.Popen, signum: int) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signum)
    except ProcessLookupError:
        return


def _wait_process(proc: subprocess.Popen, timeout: float) -> None:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def supervise(
    commands: Sequence[Sequence[str]],
    *,
    poll_interval: float = 0.2,
    shutdown_timeout: float = 15.0,
) -> int:
    """Run required child processes until a signal or unexpected exit.

    SIGINT/SIGTERM are forwarded to every child. If any child exits on its own,
    the others are stopped and this function returns a non-zero status.
    """
    if not commands:
        raise ValueError("at least one command is required")

    processes = [subprocess.Popen(list(command)) for command in commands]
    exit_reason = "running"
    status = 0

    def handle_signal(signum: int, _frame: object) -> None:
        nonlocal exit_reason
        if exit_reason == "running":
            exit_reason = "signal"
        logger.info("Received signal %s; stopping child processes", signum)
        for proc in processes:
            _stop_process(proc, signum)

    previous_handlers = {
        signum: signal.signal(signum, handle_signal) for signum in _STOP_SIGNALS
    }
    try:
        while exit_reason == "running":
            for proc in processes:
                code = proc.poll()
                if code is None:
                    continue
                exit_reason = "child"
                status = code if code != 0 else 1
                logger.error(
                    "Required process exited unexpectedly with status %s",
                    code,
                )
                for other in processes:
                    _stop_process(other, signal.SIGTERM)
                break
            else:
                time.sleep(poll_interval)
                continue
            break
    finally:
        remaining = shutdown_timeout
        for proc in processes:
            started = time.monotonic()
            _wait_process(proc, remaining)
            remaining = max(0.1, remaining - (time.monotonic() - started))
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)

    if exit_reason == "signal":
        return 0
    return status


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    port = resolve_port()
    logger.info("Starting API and worker (port=%s)", port)
    raise SystemExit(supervise([api_command(port), worker_command()]))


if __name__ == "__main__":
    main()
