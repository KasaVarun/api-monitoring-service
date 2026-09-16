"""REST routes for endpoint CRUD, history, and manual checks."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.deps import get_app_settings, get_resolver, get_session
from app.schemas import (
    AlertOut,
    CheckOut,
    EndpointCreate,
    EndpointDetail,
    EndpointOut,
    EndpointUpdate,
    PaginatedChecks,
    UptimeSummary,
)
from app.security import URLValidationError, validate_destination
from app.store import (
    create_endpoint,
    delete_endpoint,
    get_endpoint,
    latest_check_since,
    list_alerts,
    list_checks,
    list_endpoints,
    request_force_check,
    update_endpoint,
    uptime_summary,
)


def _availability(endpoint) -> str:
    if endpoint.last_checked_at is None or endpoint.last_availability is None:
        return "UNKNOWN"
    return endpoint.last_availability


def to_out(endpoint) -> EndpointOut:
    return EndpointOut(
        id=endpoint.id,
        name=endpoint.name,
        url=endpoint.url,
        enabled=endpoint.enabled,
        interval_seconds=endpoint.interval_seconds,
        availability=_availability(endpoint),
        last_status_code=endpoint.last_status_code,
        last_response_time_ms=endpoint.last_response_time_ms,
        last_error_message=endpoint.last_error_message,
        last_checked_at=endpoint.last_checked_at,
        total_failures=endpoint.total_failures,
        consecutive_failures=endpoint.consecutive_failures,
        created_at=endpoint.created_at,
        updated_at=endpoint.updated_at,
    )


def _interval_or_400(value: int, settings: Settings) -> int:
    if value < settings.min_check_interval_seconds or value > settings.max_check_interval_seconds:
        raise HTTPException(
            status_code=422,
            detail=(
                "interval_seconds must be between "
                f"{settings.min_check_interval_seconds} and "
                f"{settings.max_check_interval_seconds}"
            ),
        )
    return value


async def _validate_url(url: str, resolver) -> None:
    try:
        await validate_destination(url, resolver=resolver)
    except URLValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def create_api_router() -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.post("/endpoints", response_model=EndpointOut, status_code=201, tags=["endpoints"])
    async def create_endpoint_route(
        payload: EndpointCreate,
        session: AsyncSession = Depends(get_session),
        settings: Settings = Depends(get_app_settings),
        resolver=Depends(get_resolver),
    ) -> EndpointOut:
        await _validate_url(payload.url, resolver)
        interval = (
            settings.default_check_interval_seconds
            if payload.interval_seconds is None
            else _interval_or_400(payload.interval_seconds, settings)
        )
        endpoint = await create_endpoint(
            session,
            name=payload.name,
            url=payload.url,
            enabled=payload.enabled,
            interval_seconds=interval,
        )
        return to_out(endpoint)

    @router.get("/endpoints", response_model=list[EndpointOut], tags=["endpoints"])
    async def list_endpoints_route(
        session: AsyncSession = Depends(get_session),
    ) -> list[EndpointOut]:
        endpoints = await list_endpoints(session)
        return [to_out(item) for item in endpoints]

    @router.get("/endpoints/{endpoint_id}", response_model=EndpointDetail, tags=["endpoints"])
    async def get_endpoint_route(
        endpoint_id: str,
        session: AsyncSession = Depends(get_session),
    ) -> EndpointDetail:
        endpoint = await get_endpoint(session, endpoint_id)
        if endpoint is None:
            raise HTTPException(status_code=404, detail="Endpoint not found")
        total, up = await uptime_summary(session, endpoint.id, window_hours=24)
        percent = round((up / total) * 100, 1) if total else None
        base = to_out(endpoint)
        return EndpointDetail(
            **base.model_dump(),
            uptime=UptimeSummary(
                window_hours=24,
                total_checks=total,
                up_checks=up,
                uptime_percent=percent,
            ),
        )

    @router.patch("/endpoints/{endpoint_id}", response_model=EndpointOut, tags=["endpoints"])
    async def update_endpoint_route(
        endpoint_id: str,
        payload: EndpointUpdate,
        session: AsyncSession = Depends(get_session),
        settings: Settings = Depends(get_app_settings),
        resolver=Depends(get_resolver),
    ) -> EndpointOut:
        endpoint = await get_endpoint(session, endpoint_id)
        if endpoint is None:
            raise HTTPException(status_code=404, detail="Endpoint not found")
        if payload.url is not None:
            await _validate_url(payload.url, resolver)
        interval = None
        if payload.interval_seconds is not None:
            interval = _interval_or_400(payload.interval_seconds, settings)
        endpoint = await update_endpoint(
            session,
            endpoint,
            name=payload.name,
            url=payload.url,
            enabled=payload.enabled,
            interval_seconds=interval,
        )
        return to_out(endpoint)

    @router.delete("/endpoints/{endpoint_id}", status_code=204, tags=["endpoints"])
    async def delete_endpoint_route(
        endpoint_id: str,
        session: AsyncSession = Depends(get_session),
    ) -> None:
        endpoint = await get_endpoint(session, endpoint_id)
        if endpoint is None:
            raise HTTPException(status_code=404, detail="Endpoint not found")
        await delete_endpoint(session, endpoint)

    @router.post(
        "/endpoints/{endpoint_id}/check",
        response_model=CheckOut,
        tags=["endpoints"],
    )
    async def trigger_check_route(
        endpoint_id: str,
        session: AsyncSession = Depends(get_session),
        settings: Settings = Depends(get_app_settings),
    ) -> CheckOut:
        endpoint = await get_endpoint(session, endpoint_id)
        if endpoint is None:
            raise HTTPException(status_code=404, detail="Endpoint not found")
        requested_at = await request_force_check(session, endpoint)
        deadline = asyncio.get_event_loop().time() + settings.manual_check_wait_seconds
        while asyncio.get_event_loop().time() < deadline:
            result = await latest_check_since(session, endpoint_id, requested_at)
            if result is not None:
                return CheckOut.model_validate(result)
            await asyncio.sleep(0.1)
        raise HTTPException(
            status_code=504,
            detail="Timed out waiting for the worker to run the check",
        )

    @router.get(
        "/endpoints/{endpoint_id}/checks",
        response_model=PaginatedChecks,
        tags=["checks"],
    )
    async def list_checks_route(
        endpoint_id: str,
        session: AsyncSession = Depends(get_session),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
    ) -> PaginatedChecks:
        endpoint = await get_endpoint(session, endpoint_id)
        if endpoint is None:
            raise HTTPException(status_code=404, detail="Endpoint not found")
        items, total = await list_checks(session, endpoint_id, page=page, page_size=page_size)
        return PaginatedChecks(
            items=[CheckOut.model_validate(item) for item in items],
            page=page,
            page_size=page_size,
            total=total,
        )

    @router.get("/alerts", response_model=list[AlertOut], tags=["alerts"])
    async def list_alerts_route(
        limit: int = Query(default=50, ge=1, le=100),
        session: AsyncSession = Depends(get_session),
    ) -> list[AlertOut]:
        rows = await list_alerts(session, limit=limit)
        return [
            AlertOut(
                id=alert.id,
                endpoint_id=alert.endpoint_id,
                endpoint_name=endpoint_name,
                kind=alert.kind,
                message=alert.message,
                status_code=alert.status_code,
                consecutive_failures=alert.consecutive_failures,
                webhook_delivered=alert.webhook_delivered,
                webhook_error=alert.webhook_error,
                created_at=alert.created_at,
            )
            for alert, endpoint_name in rows
        ]

    return router
