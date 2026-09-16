from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

from app.alerts import send_alert_webhook
from app.checker import CheckOutcome, utcnow
from app.config import Settings
from app.main import create_app
from app.models import AlertEvent
from app.store import create_endpoint, get_endpoint, list_alerts
from app.worker import MonitorWorker
from tests.conftest import ScriptedChecker, public_resolver


@pytest.mark.asyncio
async def test_outage_is_deduplicated_and_recovery_is_created(settings, db):
    _engine, session_factory = db
    now = utcnow()
    checker = ScriptedChecker(
        [
            CheckOutcome("DOWN", 503, 10.0, "HTTP 503", now),
            CheckOutcome("DOWN", 503, 11.0, "HTTP 503", now + timedelta(seconds=1)),
            CheckOutcome("DOWN", 503, 12.0, "HTTP 503", now + timedelta(seconds=2)),
            CheckOutcome("DOWN", 503, 13.0, "HTTP 503", now + timedelta(seconds=3)),
            CheckOutcome("UP", 200, 8.0, None, now + timedelta(seconds=4)),
        ]
    )
    worker = MonitorWorker(settings, session_factory, checker=checker)
    async with session_factory() as session:
        endpoint = await create_endpoint(
            session,
            name="Payments",
            url="https://example.com/health",
            enabled=True,
            interval_seconds=60,
        )
        endpoint_id = endpoint.id

    for expected_count in range(1, 6):
        async with session_factory() as session:
            endpoint = await get_endpoint(session, endpoint_id)
            endpoint.force_check = True
            await session.commit()
        await worker.run_check(endpoint_id)

        async with session_factory() as session:
            alerts = await list_alerts(session)
        expected_alerts = 0 if expected_count < 3 else (1 if expected_count < 5 else 2)
        assert len(alerts) == expected_alerts

    async with session_factory() as session:
        alerts = await list_alerts(session)
        assert [item.kind for item, _name in alerts] == ["RECOVERY", "OUTAGE"]
        assert alerts[1][0].consecutive_failures == 3
        assert alerts[1][0].status_code == 503
        assert alerts[0][0].consecutive_failures == 0

    app = create_app(settings, resolver=public_resolver)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/alerts?limit=10")
            assert response.status_code == 200
            body = response.json()
            assert [item["kind"] for item in body] == ["RECOVERY", "OUTAGE"]
            assert body[0]["endpoint_name"] == "Payments"


@pytest.mark.asyncio
async def test_webhook_delivery_uses_safe_payload(settings):
    settings.alert_webhook_url = SecretStr("https://hooks.example.test/alert")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(204)

    alert = AlertEvent(
        id="alert-1",
        endpoint_id="endpoint-1",
        kind="OUTAGE",
        message="Payments is DOWN after 3 consecutive failures (HTTP 503).",
        status_code=503,
        consecutive_failures=3,
        created_at=utcnow(),
    )
    delivered, error = await send_alert_webhook(
        alert,
        "Payments",
        settings,
        transport=httpx.MockTransport(handler),
    )

    assert delivered is True
    assert error is None
    assert captured["event"] == "OUTAGE"
    assert captured["endpoint_name"] == "Payments"
    assert "url" not in captured


@pytest.mark.asyncio
async def test_webhook_failure_is_reported_without_response_body(settings):
    settings.alert_webhook_url = SecretStr("https://hooks.example.test/alert")
    alert = AlertEvent(
        id="alert-1",
        endpoint_id="endpoint-1",
        kind="OUTAGE",
        message="Service is DOWN.",
        status_code=500,
        consecutive_failures=3,
        created_at=utcnow(),
    )
    delivered, error = await send_alert_webhook(
        alert,
        "Service",
        settings,
        transport=httpx.MockTransport(lambda _request: httpx.Response(500, text="secret")),
    )
    assert delivered is False
    assert error == "Webhook returned HTTP 500"
    assert "secret" not in error


def test_webhook_requires_https():
    with pytest.raises(ValidationError):
        Settings(alert_webhook_url="http://hooks.example.test/alert")
