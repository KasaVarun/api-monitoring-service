from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.checker import CheckOutcome, HttpChecker, utcnow
from app.config import Settings
from app.database import init_db, setup_database
from app.main import create_app

PUBLIC_IP = ipaddress.IPv4Address("93.184.216.34")
PUBLIC_IPV6 = ipaddress.IPv6Address("2606:4700:4700::1111")


async def public_resolver(
    host: str, _port: int
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    if host.endswith(".ipv6.test"):
        return [PUBLIC_IPV6]
    return [PUBLIC_IP]


class ScriptedChecker:
    def __init__(self, outcomes: list[CheckOutcome] | None = None) -> None:
        self.outcomes = list(outcomes or [])
        self.calls: list[str] = []
        self.hold = False
        self.gate = None
        self.current = 0
        self.max_current = 0

    async def check(self, url: str) -> CheckOutcome:

        self.calls.append(url)
        self.current += 1
        self.max_current = max(self.max_current, self.current)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.outcomes:
                return self.outcomes.pop(0)
            return CheckOutcome("UP", 200, 12.0, None, utcnow())
        finally:
            self.current -= 1


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    db = tmp_path / "monitor.db"
    return Settings(
        database_url=f"sqlite+aiosqlite:///{db}",
        request_timeout_seconds=1.0,
        min_check_interval_seconds=10,
        max_check_interval_seconds=3600,
        default_check_interval_seconds=60,
        check_concurrency=2,
        history_retention_days=7,
        history_max_records_per_endpoint=20,
        worker_poll_seconds=0.05,
        manual_check_wait_seconds=3.0,
        max_response_bytes=2048,
    )


@pytest_asyncio.fixture
async def db(settings: Settings):
    engine, session_factory = setup_database(settings)
    await init_db(engine)
    try:
        yield engine, session_factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings, resolver=public_resolver)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as async_client:
            yield async_client


def make_checker(settings: Settings, handler) -> HttpChecker:
    import httpx

    return HttpChecker(
        settings,
        resolver=public_resolver,
        transport_factory=lambda: httpx.MockTransport(handler),
    )
