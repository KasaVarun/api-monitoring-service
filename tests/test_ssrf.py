import ipaddress

import httpx
import pytest

from app.checker import HttpChecker
from app.security import URLValidationError, validate_destination


@pytest.mark.asyncio
async def test_api_rejects_ssrf_urls(client):
    for url in (
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data",
        "http://192.168.0.20/admin",
        "https://localhost/health",
        "https://user:secret@example.com/x",
    ):
        response = await client.post("/api/endpoints", json={"name": "bad", "url": url})
        assert response.status_code == 422, url


@pytest.mark.asyncio
async def test_api_rejects_dns_rebinding_to_private(settings):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    async def rebind(_host, _port):
        return [ipaddress.ip_address("169.254.169.254")]

    app = create_app(settings, resolver=rebind)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/endpoints",
                json={"name": "rebind", "url": "http://metadata.example/latest"},
            )
            assert response.status_code == 422


@pytest.mark.asyncio
async def test_ipv6_unique_local_and_link_local_blocked():
    for host in ("fd00::1", "fe80::ffff", "ff02::1"):
        with pytest.raises(URLValidationError):
            await validate_destination(f"http://[{host}]/")


@pytest.mark.asyncio
async def test_redirect_to_private_is_not_requested(settings):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(301, headers={"Location": "http://[::1]/metadata"})

    async def resolver(_host, _port):
        return [ipaddress.ip_address("93.184.216.34")]

    checker = HttpChecker(
        settings,
        resolver=resolver,
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    outcome = await checker.check("https://example.com/go")
    assert len(seen) == 1
    assert seen[0].url.host == "93.184.216.34"
    assert outcome.status_code == 301
    assert outcome.availability == "DOWN"
