"""FastAPI application factory."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.checker import HttpChecker
from app.config import Settings, get_settings
from app.database import init_db, setup_database
from app.routes import create_api_router
from app.schemas import HealthOut
from app.security import Resolver, default_resolver

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    settings: Settings | None = None,
    *,
    resolver: Resolver | None = None,
    checker: HttpChecker | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    resolve = resolver or default_resolver
    http_checker = checker or HttpChecker(settings, resolver=resolve)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(
            level=settings.log_level.upper(),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        engine, session_factory = setup_database(settings)
        await init_db(engine)
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.resolver = resolve
        app.state.checker = http_checker
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title="API Monitor",
        description=(
            "HTTP endpoint monitoring service. Authentication is not included; "
            "protect public deployments with a reverse proxy or network policy."
        ),
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings
    app.state.resolver = resolve
    app.state.checker = http_checker

    app.include_router(create_api_router())

    @app.get("/health", response_model=HealthOut, tags=["health"])
    async def health(request: Request) -> HealthOut | JSONResponse:
        engine = getattr(request.app.state, "engine", None)
        if engine is None:
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "database": "error"},
            )
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "database": "error"},
            )
        return HealthOut(status="ok", database="ok")

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "message": "Invalid request",
                "details": json.loads(json.dumps(exc.errors(), default=str)),
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        message = detail if isinstance(detail, str) else "Request failed"
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": _error_code(exc.status_code), "message": message, "details": None},
        )

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def dashboard() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

        @app.get("/endpoints/{endpoint_id}", include_in_schema=False)
        async def dashboard_detail(endpoint_id: str) -> FileResponse:  # noqa: ARG001
            return FileResponse(STATIC_DIR / "index.html")

    return app


def _error_code(status_code: int) -> str:
    return {
        400: "bad_request",
        404: "not_found",
        409: "conflict",
        422: "validation_error",
        503: "unavailable",
        504: "timeout",
    }.get(status_code, "error")


app = create_app()
