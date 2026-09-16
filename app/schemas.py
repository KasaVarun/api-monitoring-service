"""Pydantic schemas for the public API."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Availability = Literal["UP", "DOWN", "UNKNOWN"]


class EndpointCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=8, max_length=2048)
    enabled: bool = True
    interval_seconds: int | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name must not be empty")
        return cleaned

    @field_validator("url")
    @classmethod
    def strip_url(cls, value: str) -> str:
        return value.strip()


class EndpointUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    url: str | None = Field(default=None, min_length=8, max_length=2048)
    enabled: bool | None = None
    interval_seconds: int | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name must not be empty")
        return cleaned

    @field_validator("url")
    @classmethod
    def strip_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()


class EndpointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    url: str
    enabled: bool
    interval_seconds: int
    availability: Availability
    last_status_code: int | None
    last_response_time_ms: float | None
    last_error_message: str | None
    last_checked_at: datetime | None
    total_failures: int
    consecutive_failures: int
    created_at: datetime
    updated_at: datetime


class CheckOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    endpoint_id: str
    status_code: int | None
    availability: Literal["UP", "DOWN"]
    response_time_ms: float | None
    error_message: str | None
    checked_at: datetime


class PaginatedChecks(BaseModel):
    items: list[CheckOut]
    page: int
    page_size: int
    total: int


class UptimeSummary(BaseModel):
    window_hours: int
    total_checks: int
    up_checks: int
    uptime_percent: float | None


class EndpointDetail(EndpointOut):
    uptime: UptimeSummary


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    database: Literal["ok", "error"]


class ErrorBody(BaseModel):
    error: str
    message: str
    details: list[dict] | None = None
