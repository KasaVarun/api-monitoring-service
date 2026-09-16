from __future__ import annotations

import base64

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import credentials_match, parse_basic_auth
from app.config import Settings
from app.main import create_app
from tests.conftest import public_resolver


def _auth_settings(tmp_path, username: str = "ops", password: str = "s3cret") -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}",
        app_username=username,
        app_password=password,
    )


def test_parse_basic_auth_round_trip():
    token = base64.b64encode(b"ops:p@ss:word").decode()
    assert parse_basic_auth(f"Basic {token}") == ("ops", "p@ss:word")
    assert parse_basic_auth("Bearer abc") is None
    assert parse_basic_auth(None) is None


def test_credentials_match_does_not_accept_wrong_password(tmp_path):
    settings = _auth_settings(tmp_path)
    assert credentials_match("ops", "s3cret", settings)
    assert not credentials_match("ops", "wrong", settings)
    assert not credentials_match("other", "s3cret", settings)


@pytest.mark.asyncio
async def test_auth_protects_app_and_keeps_health_public(tmp_path):
    settings = _auth_settings(tmp_path)
    app = create_app(settings, resolver=public_resolver)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json()["status"] == "ok"

            for path in (
                "/",
                "/docs",
                "/redoc",
                "/openapi.json",
                "/static/app.js",
                "/endpoints/demo",
                "/api/endpoints",
            ):
                response = await client.get(path)
                assert response.status_code == 401, path
                assert "WWW-Authenticate" in response.headers
                assert response.json()["error"] == "unauthorized"

            denied = await client.get(
                "/api/endpoints",
                auth=("ops", "wrong"),
            )
            assert denied.status_code == 401

            allowed = await client.get("/api/endpoints", auth=("ops", "s3cret"))
            assert allowed.status_code == 200
            assert allowed.json() == []

            docs = await client.get("/docs", auth=("ops", "s3cret"))
            assert docs.status_code == 200


@pytest.mark.asyncio
async def test_auth_disabled_when_credentials_missing(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'open.db'}",
        app_username="only-user",
        app_password="",
    )
    app = create_app(settings, resolver=public_resolver)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            listed = await client.get("/api/endpoints")
            assert listed.status_code == 200
