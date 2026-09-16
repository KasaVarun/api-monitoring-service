"""Database-backed users and opaque cookie sessions."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings
from app.deps import get_app_settings, get_session
from app.models import User, UserSession

logger = logging.getLogger(__name__)

SESSION_COOKIE = "api_monitor_session"
PUBLIC_PATHS = frozenset(
    {"/health", "/login", "/signup", "/api/auth/login", "/api/auth/signup"}
)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=128)


class SignupIn(BaseModel):
    username: str = Field(min_length=3, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.strip().lower()


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    created_at: datetime


def _password_hash(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, expected_hex = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
        return hmac.compare_digest(actual, bytes.fromhex(expected_hex))
    except (TypeError, ValueError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_user(session: AsyncSession, username: str, password: str) -> User:
    now = datetime.now(UTC)
    user = User(
        username=username.strip().lower(),
        password_hash=_password_hash(password),
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ValueError("Username is already registered") from exc
    await session.refresh(user)
    return user


async def authenticate_user(session: AsyncSession, username: str, password: str) -> User | None:
    result = await session.execute(
        select(User).where(User.username == username.strip().lower())
    )
    user = result.scalar_one_or_none()
    if user is None:
        _password_hash(password, salt=b"\x00" * 16)
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


async def issue_session(session: AsyncSession, user: User, settings: Settings) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    await session.execute(delete(UserSession).where(UserSession.expires_at <= now))
    session.add(
        UserSession(
            token_hash=_token_hash(token),
            user_id=user.id,
            created_at=now,
            expires_at=now + timedelta(days=settings.session_lifetime_days),
        )
    )
    await session.commit()
    return token


async def bootstrap_configured_user(session: AsyncSession, settings: Settings) -> None:
    username = settings.app_username.strip().lower()
    password = settings.secret_password()
    if not username or not password:
        return
    existing = await session.execute(select(User.id).where(User.username == username))
    if existing.scalar_one_or_none() is not None:
        return
    await create_user(session, username, password)
    logger.info("Created the configured bootstrap user %s", username)


def _set_session_cookie(
    response: Response,
    token: str,
    request: Request,
    settings: Settings,
) -> None:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    secure = request.url.scheme == "https" or forwarded_proto == "https"
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_lifetime_days * 24 * 60 * 60,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax")


def _is_public(path: str) -> bool:
    normalized = path.rstrip("/") or "/"
    return normalized in PUBLIC_PATHS or normalized.startswith("/static/")


def _unauthenticated(path: str) -> Response:
    if path.startswith("/api/"):
        return JSONResponse(
            status_code=401,
            content={
                "error": "unauthorized",
                "message": "Please log in to continue",
                "details": None,
            },
        )
    target = quote(path if path.startswith("/") else "/", safe="/")
    return RedirectResponse(url=f"/login?next={target}", status_code=303)


class SessionAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        settings = getattr(getattr(scope.get("app"), "state", None), "settings", None)
        if settings is None or not settings.auth_required or _is_public(request.url.path):
            await self.app(scope, receive, send)
            return

        token = request.cookies.get(SESSION_COOKIE)
        session_factory = getattr(scope["app"].state, "session_factory", None)
        user = None
        if token and session_factory is not None:
            async with session_factory() as database:
                result = await database.execute(
                    select(User)
                    .join(UserSession, UserSession.user_id == User.id)
                    .where(
                        UserSession.token_hash == _token_hash(token),
                        UserSession.expires_at > datetime.now(UTC),
                    )
                )
                user = result.scalar_one_or_none()

        if user is None:
            response = _unauthenticated(request.url.path)
            await response(scope, receive, send)
            return

        request.state.user = user
        request.state.session_token_hash = _token_hash(token)
        await self.app(scope, receive, send)


def create_auth_router() -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["authentication"])

    @router.post("/signup", response_model=UserOut, status_code=201)
    async def signup(
        payload: SignupIn,
        request: Request,
        response: Response,
        session: AsyncSession = Depends(get_session),
        settings: Settings = Depends(get_app_settings),
    ) -> User:
        try:
            user = await create_user(session, payload.username, payload.password)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        token = await issue_session(session, user, settings)
        _set_session_cookie(response, token, request, settings)
        return user

    @router.post("/login", response_model=UserOut)
    async def login(
        payload: LoginIn,
        request: Request,
        response: Response,
        session: AsyncSession = Depends(get_session),
        settings: Settings = Depends(get_app_settings),
    ) -> User:
        user = await authenticate_user(session, payload.username, payload.password)
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid username or password")
        token = await issue_session(session, user, settings)
        _set_session_cookie(response, token, request, settings)
        return user

    @router.post("/logout", status_code=204)
    async def logout(
        request: Request,
        response: Response,
        session: AsyncSession = Depends(get_session),
    ) -> None:
        token_hash = getattr(request.state, "session_token_hash", None)
        if token_hash:
            await session.execute(delete(UserSession).where(UserSession.token_hash == token_hash))
            await session.commit()
        _clear_session_cookie(response)

    @router.get("/me", response_model=UserOut)
    async def me(request: Request) -> User:
        user = getattr(request.state, "user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Please log in to continue")
        return user

    return router
