from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.auth import verify_password
from app.config import Settings
from app.main import create_app
from app.models import User
from tests.conftest import public_resolver


def _auth_settings(tmp_path, username: str = "", password: str = "") -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}",
        auth_required=True,
        app_username=username,
        app_password=password,
        session_lifetime_days=7,
    )


@pytest.mark.asyncio
async def test_login_pages_and_health_are_public_but_app_is_protected(tmp_path):
    app = create_app(_auth_settings(tmp_path), resolver=public_resolver)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/login")).status_code == 200
            assert (await client.get("/signup")).status_code == 200
            assert (await client.get("/static/auth.js")).status_code == 200

            dashboard = await client.get("/", follow_redirects=False)
            assert dashboard.status_code == 303
            assert dashboard.headers["location"] == "/login?next=/"

            api = await client.get("/api/endpoints")
            assert api.status_code == 401
            assert api.json()["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_signup_creates_hashed_user_and_authenticated_session(tmp_path):
    app = create_app(_auth_settings(tmp_path), resolver=public_resolver)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            signup = await client.post(
                "/api/auth/signup",
                json={"username": "Varun.K", "password": "strong-pass-123"},
            )
            assert signup.status_code == 201
            assert signup.json()["username"] == "varun.k"
            cookie = signup.headers["set-cookie"]
            assert "api_monitor_session=" in cookie
            assert "HttpOnly" in cookie
            assert "SameSite=lax" in cookie

            assert (await client.get("/api/endpoints")).status_code == 200
            assert (await client.get("/docs")).status_code == 200
            assert (await client.get("/api/auth/me")).json()["username"] == "varun.k"

            async with app.state.session_factory() as session:
                user = (await session.execute(select(User))).scalar_one()
                assert user.password_hash != "strong-pass-123"
                assert verify_password("strong-pass-123", user.password_hash)
                assert not verify_password("wrong-password", user.password_hash)


@pytest.mark.asyncio
async def test_duplicate_signup_bad_login_and_logout(tmp_path):
    app = create_app(_auth_settings(tmp_path), resolver=public_resolver)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            payload = {"username": "operator", "password": "strong-pass-123"}
            assert (await client.post("/api/auth/signup", json=payload)).status_code == 201
            duplicate = await client.post("/api/auth/signup", json=payload)
            assert duplicate.status_code == 409

            await client.post("/api/auth/logout")
            assert (await client.get("/api/endpoints")).status_code == 401

            wrong = await client.post(
                "/api/auth/login",
                json={"username": "operator", "password": "wrong-password"},
            )
            assert wrong.status_code == 401

            login = await client.post("/api/auth/login", json=payload)
            assert login.status_code == 200
            assert (await client.get("/api/endpoints")).status_code == 200


@pytest.mark.asyncio
async def test_configured_credentials_bootstrap_first_user(tmp_path):
    settings = _auth_settings(tmp_path, username="ops", password="s3cret-pass")
    app = create_app(settings, resolver=public_resolver)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            login = await client.post(
                "/api/auth/login",
                json={"username": "ops", "password": "s3cret-pass"},
                headers={"x-forwarded-proto": "https"},
            )
            assert login.status_code == 200
            assert "Secure" in login.headers["set-cookie"]


@pytest.mark.asyncio
async def test_auth_can_be_disabled_for_local_or_test_environments(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'open.db'}",
        auth_required=False,
    )
    app = create_app(settings, resolver=public_resolver)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/api/endpoints")).status_code == 200
