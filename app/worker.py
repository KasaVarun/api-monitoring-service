"""Background worker that schedules and executes HTTP checks."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.checker import CheckOutcome, HttpChecker, utcnow
from app.config import Settings, get_settings
from app.database import init_db, setup_database
from app.store import get_endpoint, list_due_endpoints, record_check

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]


class MonitorWorker:
    """Single-process check scheduler.

    Only this process performs HTTP probes. The API sets `force_check` on an
    endpoint; the worker claims due work, runs probes under a per-endpoint lock
    and a global concurrency semaphore, then writes results.

    One worker instance is required. Overlap prevention is in-process (asyncio
    locks). Scaling to multiple workers would need a distributed lease, e.g. an
    `UPDATE ... WHERE check_claimed_until < now` row lock.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        checker: HttpChecker | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.checker = checker or HttpChecker(settings)
        self.clock = clock or utcnow
        self._stop = asyncio.Event()
        self._locks: dict[str, asyncio.Lock] = {}
        self._semaphore = asyncio.Semaphore(settings.check_concurrency)
        self._in_flight: set[asyncio.Task] = set()
        self._scheduled: set[str] = set()
        self._active_checks = 0
        self.max_active_checks = 0

    def request_stop(self) -> None:
        self._stop.set()

    def lock_for(self, endpoint_id: str) -> asyncio.Lock:
        lock = self._locks.get(endpoint_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[endpoint_id] = lock
        return lock

    async def run(self) -> None:
        logger.info("Worker starting (concurrency=%s)", self.settings.check_concurrency)
        try:
            while not self._stop.is_set():
                try:
                    await self.tick()
                except Exception:
                    logger.exception("Worker tick failed; continuing")
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self.settings.worker_poll_seconds,
                    )
                except TimeoutError:
                    pass
        finally:
            await self._drain()
            logger.info("Worker stopped")

    async def tick(self) -> None:
        slots = self.settings.check_concurrency - len(self._in_flight)
        if slots <= 0:
            return

        async with self.session_factory() as session:
            due = await list_due_endpoints(session, limit=slots * 2)

        started = 0
        for endpoint in due:
            if started >= slots:
                break
            if endpoint.id in self._scheduled or self.lock_for(endpoint.id).locked():
                continue
            self._scheduled.add(endpoint.id)
            task = asyncio.create_task(
                self._guarded_check(endpoint.id),
                name=f"check-{endpoint.id}",
            )
            self._in_flight.add(task)
            task.add_done_callback(lambda _t, eid=endpoint.id: self._on_task_done(eid))
            started += 1

    def _on_task_done(self, endpoint_id: str) -> None:
        self._scheduled.discard(endpoint_id)
        done = {task for task in self._in_flight if task.done()}
        self._in_flight.difference_update(done)

    async def _guarded_check(self, endpoint_id: str) -> None:
        try:
            await self.run_check(endpoint_id)
        except Exception:
            logger.exception("Check failed unexpectedly for endpoint %s", endpoint_id)

    async def run_check(self, endpoint_id: str) -> CheckOutcome | None:
        lock = self.lock_for(endpoint_id)
        if lock.locked():
            return None
        async with lock:
            return await self._execute_check(endpoint_id)

    async def _execute_check(self, endpoint_id: str) -> CheckOutcome | None:
        async with self.session_factory() as session:
            endpoint = await get_endpoint(session, endpoint_id)
            if endpoint is None:
                return None
            now = self.clock()
            is_force = bool(endpoint.force_check)
            is_due = endpoint.enabled and endpoint.next_check_at <= now
            if not is_force and not is_due:
                return None
            if is_force:
                endpoint.force_check = False
                await session.commit()
            url = endpoint.url

        async with self._semaphore:
            self._active_checks += 1
            self.max_active_checks = max(self.max_active_checks, self._active_checks)
            try:
                try:
                    outcome = await self.checker.check(url)
                except Exception:
                    logger.exception("Checker crashed for endpoint %s", endpoint_id)
                    outcome = CheckOutcome(
                        availability="DOWN",
                        status_code=None,
                        response_time_ms=None,
                        error_message="Internal check error",
                        checked_at=self.clock(),
                    )
            finally:
                self._active_checks -= 1

        async with self.session_factory() as session:
            endpoint = await get_endpoint(session, endpoint_id)
            if endpoint is None:
                return outcome
            await record_check(
                session,
                endpoint,
                outcome,
                self.settings,
                consumed_force_check=False,
            )
        return outcome

    async def _drain(self, timeout: float = 15.0) -> None:
        if not self._in_flight:
            return
        logger.info("Waiting for %s in-flight check(s) to finish", len(self._in_flight))
        done, pending = await asyncio.wait(set(self._in_flight), timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        _ = done


def _install_signal_handlers(worker: MonitorWorker) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda _s, _f: worker.request_stop())


async def run_worker(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    engine, session_factory = setup_database(settings)
    await init_db(engine)
    worker = MonitorWorker(settings, session_factory)
    _install_signal_handlers(worker)
    try:
        await worker.run()
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
