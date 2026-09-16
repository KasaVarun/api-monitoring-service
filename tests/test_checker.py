import httpx
import pytest

from app.checker import HttpChecker
from tests.conftest import make_checker


def _handler(status: int, delay: float = 0, error: Exception | None = None):
    def inner(request: httpx.Request) -> httpx.Response:
        if delay:
            raise httpx.TimeoutException("timeout")
        if error is not None:
            raise error
        return httpx.Response(status, text="ok")

    return inner


@pytest.mark.asyncio
async def test_successful_http_is_up(settings):
    checker = make_checker(settings, _handler(200))
    outcome = await checker.check("https://example.com/health")
    assert outcome.availability == "UP"
    assert outcome.status_code == 200
    assert outcome.response_time_ms is not None
    assert outcome.response_time_ms >= 0
    assert outcome.error_message is None


@pytest.mark.asyncio
async def test_non_success_http_is_down_with_latency(settings):
    checker = make_checker(settings, _handler(503))
    outcome = await checker.check("https://example.com/health")
    assert outcome.availability == "DOWN"
    assert outcome.status_code == 503
    assert outcome.response_time_ms is not None
    assert "503" in (outcome.error_message or "")


@pytest.mark.asyncio
async def test_timeout_has_no_latency(settings):
    checker = make_checker(settings, _handler(200, delay=1))
    outcome = await checker.check("https://example.com/health")
    assert outcome.availability == "DOWN"
    assert outcome.status_code is None
    assert outcome.response_time_ms is None
    assert outcome.error_message == "Request timed out"


@pytest.mark.asyncio
async def test_connection_error_has_no_latency(settings):
    checker = make_checker(settings, _handler(200, error=httpx.ConnectError("refused")))
    outcome = await checker.check("https://example.com/health")
    assert outcome.availability == "DOWN"
    assert outcome.status_code is None
    assert outcome.response_time_ms is None
    assert outcome.error_message == "Connection failed"


@pytest.mark.asyncio
async def test_connects_to_validated_ip_and_sets_host_header(settings):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    checker = make_checker(settings, handler)
    outcome = await checker.check("https://example.com/v1?x=1")
    assert outcome.availability == "UP"
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "example.com"
    assert seen[0].url.path == "/v1"
    assert "sni_hostname" in seen[0].extensions
    assert seen[0].extensions["sni_hostname"] == "example.com"


@pytest.mark.asyncio
async def test_redirects_are_not_followed(settings):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/steal"})

    checker = make_checker(settings, handler)
    outcome = await checker.check("https://example.com/redirect")
    assert len(seen) == 1
    assert outcome.availability == "DOWN"
    assert outcome.status_code == 302
    assert outcome.response_time_ms is not None


@pytest.mark.asyncio
async def test_dns_failure_during_check(settings):
    async def fail(_host, _port):
        from app.security import URLValidationError

        raise URLValidationError("DNS resolution failed")

    checker = HttpChecker(settings, resolver=fail)
    outcome = await checker.check("https://missing.example/health")
    assert outcome.availability == "DOWN"
    assert outcome.response_time_ms is None
    assert "DNS" in outcome.error_message


@pytest.mark.asyncio
async def test_private_resolution_during_check_is_down(settings):
    import ipaddress

    async def private(_host, _port):
        return [ipaddress.ip_address("10.0.0.8")]

    checker = HttpChecker(settings, resolver=private)
    outcome = await checker.check("https://internal.example/health")
    assert outcome.availability == "DOWN"
    assert "not allowed" in outcome.error_message.lower()
