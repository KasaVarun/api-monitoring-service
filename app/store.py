"""Endpoint persistence and check-result recording."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.checker import CheckOutcome, utcnow
from app.config import Settings
from app.models import AlertEvent, CheckResult, Endpoint


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


async def get_endpoint(session: AsyncSession, endpoint_id: str) -> Endpoint | None:
    return await session.get(Endpoint, endpoint_id)


async def list_endpoints(session: AsyncSession) -> list[Endpoint]:
    result = await session.execute(select(Endpoint).order_by(Endpoint.created_at.desc()))
    return list(result.scalars())


async def create_endpoint(
    session: AsyncSession,
    *,
    name: str,
    url: str,
    enabled: bool,
    interval_seconds: int,
) -> Endpoint:
    now = utcnow()
    endpoint = Endpoint(
        name=name,
        url=url,
        enabled=enabled,
        interval_seconds=interval_seconds,
        force_check=False,
        total_failures=0,
        consecutive_failures=0,
        next_check_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(endpoint)
    await session.commit()
    await session.refresh(endpoint)
    return endpoint


async def update_endpoint(
    session: AsyncSession,
    endpoint: Endpoint,
    *,
    name: str | None = None,
    url: str | None = None,
    enabled: bool | None = None,
    interval_seconds: int | None = None,
) -> Endpoint:
    if name is not None:
        endpoint.name = name
    if url is not None:
        endpoint.url = url
    if enabled is not None:
        endpoint.enabled = enabled
    if interval_seconds is not None:
        endpoint.interval_seconds = interval_seconds
        if endpoint.last_checked_at is None:
            endpoint.next_check_at = utcnow()
        else:
            endpoint.next_check_at = _aware(endpoint.last_checked_at) + timedelta(
                seconds=endpoint.interval_seconds
            )
    endpoint.updated_at = utcnow()
    await session.commit()
    await session.refresh(endpoint)
    return endpoint


async def delete_endpoint(session: AsyncSession, endpoint: Endpoint) -> None:
    await session.delete(endpoint)
    await session.commit()


async def request_force_check(session: AsyncSession, endpoint: Endpoint) -> datetime:
    requested_at = utcnow()
    endpoint.force_check = True
    endpoint.updated_at = requested_at
    await session.commit()
    return requested_at


async def list_due_endpoints(session: AsyncSession, limit: int) -> list[Endpoint]:
    now = utcnow()
    stmt: Select[tuple[Endpoint]] = (
        select(Endpoint)
        .where(
            (Endpoint.force_check.is_(True))
            | ((Endpoint.enabled.is_(True)) & (Endpoint.next_check_at <= now))
        )
        .order_by(Endpoint.force_check.desc(), Endpoint.next_check_at.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars())


async def record_check(
    session: AsyncSession,
    endpoint: Endpoint,
    outcome: CheckOutcome,
    settings: Settings,
    *,
    consumed_force_check: bool,
) -> CheckResult:
    """Persist a check and update denormalized endpoint counters.

    Failure counts:
    - DOWN increments total_failures and consecutive_failures by 1.
    - UP resets consecutive_failures to 0 and leaves total_failures unchanged.
    - Counts only change when a check actually runs and is recorded.
    """
    result = CheckResult(
        endpoint_id=endpoint.id,
        status_code=outcome.status_code,
        availability=outcome.availability,
        response_time_ms=outcome.response_time_ms,
        error_message=outcome.error_message,
        checked_at=outcome.checked_at,
    )
    session.add(result)

    if outcome.availability == "DOWN":
        endpoint.total_failures += 1
        endpoint.consecutive_failures += 1
    else:
        endpoint.consecutive_failures = 0

    endpoint.last_availability = outcome.availability
    endpoint.last_status_code = outcome.status_code
    endpoint.last_response_time_ms = outcome.response_time_ms
    endpoint.last_error_message = outcome.error_message
    endpoint.last_checked_at = outcome.checked_at
    endpoint.next_check_at = outcome.checked_at + timedelta(seconds=endpoint.interval_seconds)
    if consumed_force_check:
        endpoint.force_check = False
    endpoint.updated_at = utcnow()

    await session.flush()
    await prune_history(session, endpoint.id, settings, now=outcome.checked_at)
    await session.commit()
    await session.refresh(result)
    await session.refresh(endpoint)
    return result


async def prune_history(
    session: AsyncSession,
    endpoint_id: str,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> None:
    cutoff = (now or utcnow()) - timedelta(days=settings.history_retention_days)
    await session.execute(
        delete(CheckResult).where(
            CheckResult.endpoint_id == endpoint_id,
            CheckResult.checked_at < cutoff,
        )
    )

    keep_stmt = (
        select(CheckResult.id)
        .where(CheckResult.endpoint_id == endpoint_id)
        .order_by(CheckResult.checked_at.desc())
        .limit(settings.history_max_records_per_endpoint)
    )
    keep_ids = set((await session.execute(keep_stmt)).scalars().all())
    if not keep_ids:
        return
    await session.execute(
        delete(CheckResult).where(
            CheckResult.endpoint_id == endpoint_id,
            CheckResult.id.not_in(keep_ids),
        )
    )


async def list_checks(
    session: AsyncSession,
    endpoint_id: str,
    *,
    page: int,
    page_size: int,
) -> tuple[list[CheckResult], int]:
    filters = CheckResult.endpoint_id == endpoint_id
    total = await session.scalar(select(func.count()).select_from(CheckResult).where(filters))
    stmt = (
        select(CheckResult)
        .where(filters)
        .order_by(CheckResult.checked_at.desc(), CheckResult.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = list((await session.execute(stmt)).scalars())
    return rows, int(total or 0)


async def latest_check_since(
    session: AsyncSession,
    endpoint_id: str,
    since: datetime,
) -> CheckResult | None:
    stmt = (
        select(CheckResult)
        .where(CheckResult.endpoint_id == endpoint_id, CheckResult.checked_at >= since)
        .order_by(CheckResult.checked_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def uptime_summary(
    session: AsyncSession,
    endpoint_id: str,
    *,
    window_hours: int = 24,
) -> tuple[int, int]:
    start = utcnow() - timedelta(hours=window_hours)
    base = select(CheckResult).where(
        CheckResult.endpoint_id == endpoint_id,
        CheckResult.checked_at >= start,
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    up = await session.scalar(
        select(func.count())
        .select_from(CheckResult)
        .where(
            CheckResult.endpoint_id == endpoint_id,
            CheckResult.checked_at >= start,
            CheckResult.availability == "UP",
        )
    )
    return int(total or 0), int(up or 0)


async def latest_alert(session: AsyncSession, endpoint_id: str) -> AlertEvent | None:
    stmt = (
        select(AlertEvent)
        .where(AlertEvent.endpoint_id == endpoint_id)
        .order_by(AlertEvent.created_at.desc(), AlertEvent.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def maybe_create_alert(
    session: AsyncSession,
    endpoint: Endpoint,
    outcome: CheckOutcome,
    settings: Settings,
) -> AlertEvent | None:
    """Create one outage alert per incident and one recovery alert afterward."""
    previous = await latest_alert(session, endpoint.id)
    alert: AlertEvent | None = None

    if (
        outcome.availability == "DOWN"
        and endpoint.consecutive_failures >= settings.alert_failure_threshold
        and (previous is None or previous.kind != "OUTAGE")
    ):
        status = (
            f"HTTP {outcome.status_code}"
            if outcome.status_code is not None
            else (outcome.error_message or "request failed")
        )
        alert = AlertEvent(
            endpoint_id=endpoint.id,
            kind="OUTAGE",
            message=(
                f"{endpoint.name} is DOWN after {endpoint.consecutive_failures} "
                f"consecutive failures ({status})."
            ),
            status_code=outcome.status_code,
            consecutive_failures=endpoint.consecutive_failures,
            created_at=outcome.checked_at,
        )
    elif outcome.availability == "UP" and previous is not None and previous.kind == "OUTAGE":
        alert = AlertEvent(
            endpoint_id=endpoint.id,
            kind="RECOVERY",
            message=f"{endpoint.name} recovered and is UP (HTTP {outcome.status_code}).",
            status_code=outcome.status_code,
            consecutive_failures=0,
            created_at=outcome.checked_at,
        )

    if alert is None:
        return None
    session.add(alert)
    await session.commit()
    await session.refresh(alert)
    return alert


async def list_alerts(session: AsyncSession, *, limit: int = 50) -> list[tuple[AlertEvent, str]]:
    stmt = (
        select(AlertEvent, Endpoint.name)
        .join(Endpoint, Endpoint.id == AlertEvent.endpoint_id)
        .order_by(AlertEvent.created_at.desc(), AlertEvent.id.desc())
        .limit(limit)
    )
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


async def record_webhook_delivery(
    session: AsyncSession,
    alert_id: str,
    *,
    delivered: bool,
    error: str | None,
) -> None:
    alert = await session.get(AlertEvent, alert_id)
    if alert is None:
        return
    alert.webhook_delivered = delivered
    alert.webhook_error = error[:256] if error else None
    await session.commit()
