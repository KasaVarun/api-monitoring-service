"""URL parsing and SSRF-safe destination validation."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

Resolver = Callable[[str, int], Awaitable[list[ipaddress.IPv4Address | ipaddress.IPv6Address]]]

BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "host.docker.internal",
    "kubernetes",
    "internal",
}

BLOCKED_HOSTNAME_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".corp",
    ".home",
    ".lan",
    ".cluster.local",
)

# Extra networks not always covered by ipaddress.is_global, plus tunneled encodings.
EXTRA_BLOCKED_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
    ipaddress.ip_network("100::/64"),
    ipaddress.ip_network("2001::/23"),
    ipaddress.ip_network("2001:db8::/32"),
    ipaddress.ip_network("2002::/16"),
    ipaddress.ip_network("3fff::/20"),
)

MAX_URL_LENGTH = 2048


class URLValidationError(ValueError):
    """Raised when a URL is syntactically invalid or not allowed."""


@dataclass(frozen=True)
class ParsedURL:
    original: str
    scheme: str
    hostname: str
    port: int
    path: str
    query: str


@dataclass(frozen=True)
class ValidatedTarget:
    parsed: ParsedURL
    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]
    chosen: ipaddress.IPv4Address | ipaddress.IPv6Address

    @property
    def host_header(self) -> str:
        default_port = 443 if self.parsed.scheme == "https" else 80
        if self.parsed.port == default_port:
            return self.parsed.hostname
        return f"{self.parsed.hostname}:{self.parsed.port}"

    @property
    def request_url(self) -> str:
        """URL that connects to the already-validated IP, not a re-resolved hostname."""
        ip = self.chosen
        if ip.version == 6:
            netloc = f"[{ip}]:{self.parsed.port}"
        else:
            netloc = f"{ip}:{self.parsed.port}"
        path = self.parsed.path or "/"
        return urlunparse((self.parsed.scheme, netloc, path, "", self.parsed.query, ""))


def parse_url(url: str) -> ParsedURL:
    if not url or len(url) > MAX_URL_LENGTH:
        raise URLValidationError("URL must be between 1 and 2048 characters")
    if any(ch.isspace() for ch in url):
        raise URLValidationError("URL must not contain whitespace")

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise URLValidationError("Only HTTP and HTTPS URLs are allowed")

    if parsed.username is not None or parsed.password is not None:
        raise URLValidationError("URLs must not contain embedded credentials")
    if "@" in (parsed.netloc or ""):
        raise URLValidationError("URLs must not contain embedded credentials")

    hostname = parsed.hostname
    if not hostname:
        raise URLValidationError("URL must include a hostname")
    hostname = hostname.rstrip(".").lower()
    if not hostname:
        raise URLValidationError("URL must include a hostname")
    if hostname.startswith("[") and hostname.endswith("]"):
        hostname = hostname[1:-1]

    if _is_blocked_hostname(hostname):
        raise URLValidationError("Destination hostname is not allowed")

    literal_ip = _try_parse_ip(hostname)
    if hostname.isdigit() or _looks_like_numeric_ipv4_trick(hostname):
        raise URLValidationError("Numeric or non-standard IP literals are not allowed")

    if literal_ip is not None:
        _assert_public_ip(literal_ip)

    try:
        port = parsed.port
    except ValueError as exc:
        raise URLValidationError("URL port is invalid") from exc
    if port is None:
        port = 443 if scheme == "https" else 80
    if port < 1 or port > 65535:
        raise URLValidationError("URL port is invalid")

    path = parsed.path or "/"
    return ParsedURL(
        original=url,
        scheme=scheme,
        hostname=hostname,
        port=port,
        path=path,
        query=parsed.query,
    )


def _is_blocked_hostname(hostname: str) -> bool:
    if hostname in BLOCKED_HOSTNAMES:
        return True
    return any(hostname.endswith(suffix) for suffix in BLOCKED_HOSTNAME_SUFFIXES)


def _looks_like_numeric_ipv4_trick(hostname: str) -> bool:
    """Reject decimal/octal/hex IPv4 forms that browsers may interpret as addresses."""
    if "." not in hostname:
        return False
    parts = hostname.split(".")
    if not 1 <= len(parts) <= 4:
        return False
    for part in parts:
        if not part:
            return True
        lowered = part.lower()
        if lowered.startswith("0x"):
            try:
                int(lowered, 16)
            except ValueError:
                return False
            else:
                return True
        if part.isdigit() and (part.startswith("0") and len(part) > 1):
            return True
        if not part.isdigit():
            return False
    return False


def _try_parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def unwrap_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Expand mapped/tunneled forms so embedded private IPv4 addresses cannot hide."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return unwrap_ip(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return unwrap_ip(ip.sixtofour)
        if ip.teredo is not None:
            _server, client = ip.teredo
            return unwrap_ip(client)
        nat64 = ipaddress.ip_network("64:ff9b::/96")
        if ip in nat64:
            return ipaddress.IPv4Address(ip.packed[-4:])
    return ip


def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    candidate = unwrap_ip(ip)
    if not candidate.is_global:
        return True
    if candidate.is_multicast or candidate.is_reserved or candidate.is_unspecified:
        return True
    for network in EXTRA_BLOCKED_NETWORKS:
        try:
            if ip in network or candidate in network:
                return True
        except TypeError:
            continue
    return False


def _assert_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if is_blocked_ip(ip):
        raise URLValidationError("Destination address is not allowed")


async def default_resolver(
    host: str, port: int
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise URLValidationError("DNS resolution failed") from exc

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for info in infos:
        addr = info[4][0]
        ip = ipaddress.ip_address(addr)
        key = str(ip)
        if key not in seen:
            seen.add(key)
            addresses.append(ip)
    if not addresses:
        raise URLValidationError("DNS resolution failed")
    return addresses


async def validate_destination(
    url: str,
    resolver: Resolver | None = None,
) -> ValidatedTarget:
    parsed = parse_url(url)
    resolve = resolver or default_resolver
    literal = _try_parse_ip(parsed.hostname)
    if literal is not None:
        _assert_public_ip(literal)
        addresses = (literal,)
    else:
        addresses = tuple(await resolve(parsed.hostname, parsed.port))
        if not addresses:
            raise URLValidationError("DNS resolution failed")
        for ip in addresses:
            _assert_public_ip(ip)
    return ValidatedTarget(parsed=parsed, addresses=addresses, chosen=addresses[0])
