"""HTTP check execution with pinned destination addresses."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import httpx

from app.config import Settings
from app.security import Resolver, URLValidationError, ValidatedTarget, validate_destination

logger = logging.getLogger(__name__)

Availability = Literal["UP", "DOWN"]

TransportFactory = Callable[[], httpx.AsyncBaseTransport]


@dataclass(frozen=True)
class CheckOutcome:
    availability: Availability
    status_code: int | None
    response_time_ms: float | None
    error_message: str | None
    checked_at: datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def _safe_error(message: str) -> str:
    cleaned = " ".join(message.split())
    return cleaned[:200]


class HttpChecker:
    """Performs a single GET probe against a validated destination.

    DNS is resolved and every address is checked against the SSRF policy
    *before* connecting. The TCP connection is opened to that already-validated
    IP, with the original hostname used for the Host header and TLS SNI.
    Redirects are not followed. Response bodies are truncated.
    """

    def __init__(
        self,
        settings: Settings,
        resolver: Resolver | None = None,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self.settings = settings
        self.resolver = resolver
        self.transport_factory = transport_factory

    async def check(self, url: str) -> CheckOutcome:
        checked_at = utcnow()
        try:
            target = await validate_destination(url, resolver=self.resolver)
        except URLValidationError as exc:
            return CheckOutcome(
                availability="DOWN",
                status_code=None,
                response_time_ms=None,
                error_message=_safe_error(str(exc)),
                checked_at=checked_at,
            )

        return await self._probe(target, checked_at)

    async def _probe(self, target: ValidatedTarget, checked_at: datetime) -> CheckOutcome:
        timeout = httpx.Timeout(self.settings.request_timeout_seconds)
        headers = {
            "Host": target.host_header,
            "User-Agent": "api-monitor/1.0",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        extensions: dict = {}
        if target.parsed.scheme == "https":
            extensions["sni_hostname"] = target.parsed.hostname

        transport = self.transport_factory() if self.transport_factory else None
        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                verify=True,
                transport=transport,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "GET",
                    target.request_url,
                    headers=headers,
                    extensions=extensions,
                ) as response:
                    status_code = response.status_code
                    await self._drain_limited(response)
                    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        except httpx.TimeoutException:
            return CheckOutcome("DOWN", None, None, "Request timed out", checked_at)
        except httpx.ConnectError:
            return CheckOutcome("DOWN", None, None, "Connection failed", checked_at)
        except httpx.UnsupportedProtocol:
            return CheckOutcome("DOWN", None, None, "Unsupported protocol", checked_at)
        except httpx.HTTPError:
            logger.warning("HTTP error while checking endpoint %s", target.parsed.hostname)
            return CheckOutcome("DOWN", None, None, "Request failed", checked_at)
        except OSError:
            return CheckOutcome("DOWN", None, None, "Connection failed", checked_at)

        if 200 <= status_code <= 299:
            return CheckOutcome("UP", status_code, elapsed_ms, None, checked_at)
        return CheckOutcome(
            "DOWN",
            status_code,
            elapsed_ms,
            _safe_error(f"HTTP {status_code}"),
            checked_at,
        )

    async def _drain_limited(self, response: httpx.Response) -> None:
        remaining = self.settings.max_response_bytes
        if remaining <= 0:
            await response.aclose()
            return
        async for chunk in response.aiter_bytes():
            remaining -= len(chunk)
            if remaining <= 0:
                break
