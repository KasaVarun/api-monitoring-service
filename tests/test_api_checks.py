import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.worker import MonitorWorker
from tests.conftest import ScriptedChecker, public_resolver


@pytest.mark.asyncio
async def test_paginated_history_api(client):
    created = await client.post(
        "/api/endpoints",
        json={"name": "hist", "url": "https://example.com/health"},
    )
    endpoint_id = created.json()["id"]
    empty = await client.get(f"/api/endpoints/{endpoint_id}/checks")
    assert empty.status_code == 200
    payload = empty.json()
    assert payload["items"] == []
    assert payload["total"] == 0
    assert payload["page"] == 1


@pytest.mark.asyncio
async def test_manual_check_uses_worker(settings, db):
    _engine, session_factory = db
    checker = ScriptedChecker()
    worker = MonitorWorker(settings, session_factory, checker=checker)
    app = create_app(settings, resolver=public_resolver)
    task = asyncio.create_task(worker.run())
    try:
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                created = await client.post(
                    "/api/endpoints",
                    json={"name": "live", "url": "https://example.com/health"},
                )
                endpoint_id = created.json()["id"]
                checked = await client.post(f"/api/endpoints/{endpoint_id}/check")
                assert checked.status_code == 200, checked.text
                body = checked.json()
                assert body["availability"] == "UP"
                assert body["status_code"] == 200
                detail = await client.get(f"/api/endpoints/{endpoint_id}")
                assert detail.json()["availability"] == "UP"
                assert detail.json()["uptime"]["window_hours"] == 24
                assert detail.json()["uptime"]["total_checks"] == 1
                assert detail.json()["uptime"]["uptime_percent"] == 100.0
    finally:
        worker.request_stop()
        await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_manual_check_times_out_without_worker(settings):
    settings.manual_check_wait_seconds = 0.2
    app = create_app(settings, resolver=public_resolver)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/endpoints",
                json={"name": "lonely", "url": "https://example.com/health"},
            )
            endpoint_id = created.json()["id"]
            response = await client.post(f"/api/endpoints/{endpoint_id}/check")
            assert response.status_code == 504
