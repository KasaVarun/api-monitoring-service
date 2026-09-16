"""Alert delivery helpers."""

from __future__ import annotations

import logging

import httpx

from app.config import Settings
from app.models import AlertEvent

logger = logging.getLogger(__name__)


async def send_alert_webhook(
    alert: AlertEvent,
    endpoint_name: str,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[bool | None, str | None]:
    """Send a generic JSON webhook without exposing the monitored URL."""
    url = settings.secret_alert_webhook_url()
    if not url:
        return None, None

    payload = {
        "text": alert.message,
        "event": alert.kind,
        "endpoint_id": alert.endpoint_id,
        "endpoint_name": endpoint_name,
        "status_code": alert.status_code,
        "consecutive_failures": alert.consecutive_failures,
        "created_at": alert.created_at.isoformat(),
    }
    try:
        async with httpx.AsyncClient(
            timeout=settings.alert_webhook_timeout_seconds,
            follow_redirects=False,
            verify=True,
            trust_env=False,
            transport=transport,
        ) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
    except httpx.TimeoutException:
        return False, "Webhook timed out"
    except httpx.HTTPStatusError as exc:
        return False, f"Webhook returned HTTP {exc.response.status_code}"
    except httpx.HTTPError:
        return False, "Webhook request failed"

    logger.info("Delivered %s alert for endpoint %s", alert.kind, alert.endpoint_id)
    return True, None
