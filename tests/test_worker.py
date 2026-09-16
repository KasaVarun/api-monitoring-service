import asyncio
from datetime import timedelta

import pytest

from app.checker import CheckOutcome, utcnow
from app.store import create_endpoint, get_endpoint, list_checks
from app.worker import MonitorWorker
from tests.conftest import ScriptedChecker


@pytest.mark.asyncio
async def test_failure_counts_and_recovery(settings, db):
    _engine, session_factory = db
    now = utcnow()
    outcomes = [
        CheckOutcome("DOWN", 500, 11.0, "HTTP 500", now),
        CheckOutcome("DOWN", None, None, "Request timed out", now + timedelta(seconds=1)),
        CheckOutcome("UP", 200, 9.0, None, now + timedelta(seconds=2)),
        CheckOutcome("DOWN", 404, 8.0, "HTTP 404", now + timedelta(seconds=3)),
    ]
    checker = ScriptedChecker(outcomes)
    worker = MonitorWorker(settings, session_factory, checker=checker)

    async with session_factory() as session:
        endpoint = await create_endpoint(
            session,
            name="api",
            url="https://example.com/health",
            enabled=True,
            interval_seconds=60,
        )
        endpoint_id = endpoint.id

    for _ in range(4):
        async with session_factory() as session:
            row = await get_endpoint(session, endpoint_id)
            row.force_check = True
            await session.commit()
        await worker.run_check(endpoint_id)

    async with session_factory() as session:
        row = await get_endpoint(session, endpoint_id)
        assert row.total_failures == 3
        assert row.consecutive_failures == 1
        assert row.last_availability == "DOWN"
        assert row.last_status_code == 404


@pytest.mark.asyncio
async def test_disabled_endpoints_are_not_scheduled(settings, db):
    _engine, session_factory = db
    checker = ScriptedChecker()
    worker = MonitorWorker(settings, session_factory, checker=checker)
    async with session_factory() as session:
        await create_endpoint(
            session,
            name="off",
            url="https://example.com/health",
            enabled=False,
            interval_seconds=10,
        )
    await worker.tick()
    await asyncio.sleep(0.05)
    assert checker.calls == []


@pytest.mark.asyncio
async def test_interval_prevents_immediate_reschedule(settings, db):
    _engine, session_factory = db
    checker = ScriptedChecker()
    worker = MonitorWorker(settings, session_factory, checker=checker)
    async with session_factory() as session:
        endpoint = await create_endpoint(
            session,
            name="on",
            url="https://example.com/health",
            enabled=True,
            interval_seconds=60,
        )
        endpoint_id = endpoint.id
    await worker.run_check(endpoint_id)
    assert len(checker.calls) == 1
    await worker.tick()
    await asyncio.sleep(0.05)
    assert len(checker.calls) == 1


@pytest.mark.asyncio
async def test_force_check_runs_when_disabled(settings, db):
    _engine, session_factory = db
    checker = ScriptedChecker()
    worker = MonitorWorker(settings, session_factory, checker=checker)
    async with session_factory() as session:
        endpoint = await create_endpoint(
            session,
            name="off",
            url="https://example.com/health",
            enabled=False,
            interval_seconds=60,
        )
        endpoint.force_check = True
        await session.commit()
        endpoint_id = endpoint.id
    await worker.run_check(endpoint_id)
    assert checker.calls == ["https://example.com/health"]


@pytest.mark.asyncio
async def test_overlap_prevention(settings, db):
    _engine, session_factory = db
    checker = ScriptedChecker()
    checker.gate = asyncio.Event()
    worker = MonitorWorker(settings, session_factory, checker=checker)
    async with session_factory() as session:
        endpoint = await create_endpoint(
            session,
            name="slow",
            url="https://example.com/health",
            enabled=True,
            interval_seconds=10,
        )
        endpoint_id = endpoint.id
        endpoint.force_check = True
        await session.commit()

    first = asyncio.create_task(worker.run_check(endpoint_id))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(worker.run_check(endpoint_id))
    await asyncio.sleep(0.05)
    assert worker.lock_for(endpoint_id).locked()
    assert len(checker.calls) == 1
    checker.gate.set()
    results = await asyncio.gather(first, second)
    assert results.count(None) == 1
    assert len(checker.calls) == 1


@pytest.mark.asyncio
async def test_bounded_concurrency(settings, db):
    _engine, session_factory = db
    settings.check_concurrency = 2
    checker = ScriptedChecker()
    checker.gate = asyncio.Event()
    worker = MonitorWorker(settings, session_factory, checker=checker)
    async with session_factory() as session:
        for i in range(5):
            await create_endpoint(
                session,
                name=f"e{i}",
                url=f"https://example.com/{i}",
                enabled=True,
                interval_seconds=10,
            )
    await worker.tick()
    await asyncio.sleep(0.1)
    assert len(worker._in_flight) <= 2
    assert checker.max_current <= 2
    assert len(checker.calls) == 2
    checker.gate.set()
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_checker_exception_does_not_stop_worker(settings, db):
    _engine, session_factory = db

    class Boom:
        calls = 0

        async def check(self, url: str):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
            return CheckOutcome("UP", 200, 1.0, None, utcnow())

    boom = Boom()
    worker = MonitorWorker(settings, session_factory, checker=boom)
    async with session_factory() as session:
        a = await create_endpoint(
            session, name="a", url="https://example.com/a", enabled=True, interval_seconds=60
        )
        b = await create_endpoint(
            session, name="b", url="https://example.com/b", enabled=True, interval_seconds=60
        )
    await worker.run_check(a.id)
    await worker.run_check(b.id)
    async with session_factory() as session:
        first = await get_endpoint(session, a.id)
        second = await get_endpoint(session, b.id)
        assert first.last_availability == "DOWN"
        assert first.last_error_message == "Internal check error"
        assert second.last_availability == "UP"


@pytest.mark.asyncio
async def test_worker_startup_shutdown_and_persistence(settings, tmp_path):
    from app.database import init_db, setup_database
    from app.store import create_endpoint, get_endpoint

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'persist.db'}"
    settings.database_url = db_url
    engine, session_factory = setup_database(settings)
    await init_db(engine)
    checker = ScriptedChecker()
    worker = MonitorWorker(settings, session_factory, checker=checker)
    async with session_factory() as session:
        endpoint = await create_endpoint(
            session,
            name="persist",
            url="https://example.com/health",
            enabled=True,
            interval_seconds=60,
        )
        endpoint_id = endpoint.id
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.2)
    worker.request_stop()
    await asyncio.wait_for(task, timeout=2)
    await engine.dispose()

    engine2, session_factory2 = setup_database(settings)
    await init_db(engine2)
    async with session_factory2() as session:
        restored = await get_endpoint(session, endpoint_id)
        assert restored is not None
        assert restored.name == "persist"
        assert restored.last_availability == "UP"
        items, total = await list_checks(session, endpoint_id, page=1, page_size=10)
        assert total == 1
        assert items[0].availability == "UP"
    await engine2.dispose()


@pytest.mark.asyncio
async def test_history_ordering_and_pagination(settings, db):
    _engine, session_factory = db
    now = utcnow()
    outcomes = [
        CheckOutcome("UP", 200, 1.0, None, now + timedelta(seconds=i)) for i in range(5)
    ]
    checker = ScriptedChecker(outcomes)
    worker = MonitorWorker(settings, session_factory, checker=checker)
    async with session_factory() as session:
        endpoint = await create_endpoint(
            session,
            name="hist",
            url="https://example.com/health",
            enabled=True,
            interval_seconds=10,
        )
        endpoint_id = endpoint.id
    for _ in range(5):
        async with session_factory() as session:
            row = await get_endpoint(session, endpoint_id)
            row.force_check = True
            await session.commit()
        await worker.run_check(endpoint_id)

    async with session_factory() as session:
        page1, total = await list_checks(session, endpoint_id, page=1, page_size=2)
        page2, _ = await list_checks(session, endpoint_id, page=2, page_size=2)
        page3, _ = await list_checks(session, endpoint_id, page=3, page_size=2)
        assert total == 5
        assert [row.checked_at for row in page1] == sorted(
            (row.checked_at for row in page1), reverse=True
        )
        times = [row.checked_at for row in page1 + page2 + page3]
        assert times == sorted(times, reverse=True)
        assert len(page1) == 2
        assert len(page3) == 1
