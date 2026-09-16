"""Optional HTTP Basic authentication. Credentials are never logged."""

from __future__ import annotations

import base64
import hmac
import logging
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({"/health"})
AUTH_REALM = 'Basic realm="API Monitor", charset="UTF-8"'


def auth_enabled(settings: Settings) -> bool:
    return bool(settings.app_username.strip() and settings.secret_password())


def _compare(left: str, right: str) -> bool:
    left_b = left.encode("utf-8")
    right_b = right.encode("utf-8")
    width = max(len(left_b), len(right_b), 1)
    padded_left = left_b.ljust(width, b"\x00")
    padded_right = right_b.ljust(width, b"\x00")
    same_length = hmac.compare_digest(
        len(left_b).to_bytes(4, "big"),
        len(right_b).to_bytes(4, "big"),
    )
    same_bytes = hmac.compare_digest(padded_left, padded_right)
    return same_length and same_bytes


def credentials_match(username: str, password: str, settings: Settings) -> bool:
    expected_user = settings.app_username.strip()
    expected_password = settings.secret_password()
    user_ok = _compare(username, expected_user)
    password_ok = _compare(password, expected_password)
    return user_ok and password_ok


def parse_basic_auth(header: str | None) -> tuple[str, str] | None:
    if not header:
        return None
    scheme, _, rest = header.partition(" ")
    if scheme.lower() != "basic" or not rest.strip():
        return None
    try:
        decoded = base64.b64decode(rest.strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    username, separator, password = decoded.partition(":")
    if not separator:
        return None
    return username, password


def unauthorized_response() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "error": "unauthorized",
            "message": "Authentication required",
            "details": None,
        },
        headers={"WWW-Authenticate": AUTH_REALM},
    )


def is_public_path(path: str) -> bool:
    normalized = path.rstrip("/") or "/"
    return normalized in PUBLIC_PATHS


class BasicAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        app = scope.get("app")
        settings = getattr(getattr(app, "state", None), "settings", None)
        if settings is None or not auth_enabled(settings):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        if is_public_path(request.url.path):
            await self.app(scope, receive, send)
            return

        parsed = parse_basic_auth(request.headers.get("authorization"))
        if parsed is None or not credentials_match(parsed[0], parsed[1], settings):
            logger.info("Rejected unauthenticated request for %s", request.url.path)
            response = unauthorized_response()
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
