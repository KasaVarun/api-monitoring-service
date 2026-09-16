import pytest

from app.security import (
    URLValidationError,
    is_blocked_ip,
    parse_url,
    unwrap_ip,
    validate_destination,
)
from tests.conftest import public_resolver


def test_rejects_non_http_schemes():
    with pytest.raises(URLValidationError, match="HTTP and HTTPS"):
        parse_url("ftp://example.com/file")


def test_rejects_embedded_credentials():
    with pytest.raises(URLValidationError, match="credentials"):
        parse_url("https://user:pass@example.com/health")
    with pytest.raises(URLValidationError, match="credentials"):
        parse_url("https://user@example.com/health")


def test_rejects_localhost_hostname():
    with pytest.raises(URLValidationError, match="hostname"):
        parse_url("http://localhost/health")
    with pytest.raises(URLValidationError, match="hostname"):
        parse_url("http://foo.localhost/health")


def test_rejects_loopback_and_private_literals():
    for url in (
        "http://127.0.0.1/health",
        "http://10.0.0.5/health",
        "http://192.168.1.1/health",
        "http://172.16.0.1/health",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/health",
        "http://[fe80::1]/health",
        "http://[fc00::1]/health",
        "http://[::ffff:127.0.0.1]/health",
        "http://[::ffff:10.1.1.1]/health",
        "http://[2001:db8::1]/health",
    ):
        with pytest.raises(URLValidationError, match="not allowed"):
            parse_url(url)


def test_rejects_decimal_and_octal_ip_tricks():
    with pytest.raises(URLValidationError):
        parse_url("http://2130706433/health")
    with pytest.raises(URLValidationError):
        parse_url("http://0177.0.0.1/health")


def test_accepts_public_http_and_https():
    parsed = parse_url("https://example.com:8443/v1/health?ready=1")
    assert parsed.scheme == "https"
    assert parsed.hostname == "example.com"
    assert parsed.port == 8443
    assert parsed.path == "/v1/health"
    assert parsed.query == "ready=1"


@pytest.mark.asyncio
async def test_rejects_resolved_private_and_ipv6_loopback():
    async def resolver(_host, _port):
        return [__import__("ipaddress").ip_address("127.0.0.1")]

    with pytest.raises(URLValidationError, match="not allowed"):
        await validate_destination("http://rebind.example/health", resolver=resolver)

    async def resolver_v6(_host, _port):
        return [__import__("ipaddress").ip_address("::1")]

    with pytest.raises(URLValidationError, match="not allowed"):
        await validate_destination("http://rebind6.example/health", resolver=resolver_v6)


@pytest.mark.asyncio
async def test_rejects_if_any_resolved_address_is_private():
    import ipaddress

    async def mixed(_host, _port):
        return [ipaddress.ip_address("93.184.216.34"), ipaddress.ip_address("10.0.0.1")]

    with pytest.raises(URLValidationError, match="not allowed"):
        await validate_destination("http://mixed.example/health", resolver=mixed)


@pytest.mark.asyncio
async def test_pins_connection_to_validated_ip():
    target = await validate_destination("https://example.com/health", resolver=public_resolver)
    assert str(target.chosen) == "93.184.216.34"
    assert target.request_url.startswith("https://93.184.216.34:443/")
    assert target.host_header == "example.com"


def test_unwraps_nat64_and_mapped_addresses():
    import ipaddress

    mapped = ipaddress.ip_address("::ffff:169.254.169.254")
    assert is_blocked_ip(mapped)
    nat64 = ipaddress.ip_address("64:ff9b::169.254.169.254")
    assert is_blocked_ip(nat64)
    unwrapped = unwrap_ip(nat64)
    assert str(unwrapped) == "169.254.169.254"


@pytest.mark.asyncio
async def test_dns_failure_is_validation_error():
    async def boom(_host, _port):
        raise __import__("app.security", fromlist=["URLValidationError"]).URLValidationError(
            "DNS resolution failed"
        )

    with pytest.raises(URLValidationError, match="DNS"):
        await validate_destination("https://missing.example/health", resolver=boom)
