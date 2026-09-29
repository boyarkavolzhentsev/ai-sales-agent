"""Time zones, local policy dates and sending windows (stdlib zoneinfo)."""

from datetime import UTC, date, datetime, time, timedelta
from enum import IntEnum
from typing import Annotated, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, Field, model_validator

from app.core.models.base import CoreModel
from app.core.validation import unique_items


def _require_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA time zone: {name!r}") from exc
    return name


# An explicit IANA time zone name such as "Europe/Kyiv" or "UTC".
TimezoneName = Annotated[str, AfterValidator(_require_timezone)]


class Weekday(IntEnum):
    """Values match ``datetime.weekday()``."""

    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4
    SATURDAY = 5
    SUNDAY = 6


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


def local_date(now: datetime, timezone: str) -> date:
    """The calendar date of ``now`` in ``timezone``."""
    return _require_aware(now).astimezone(ZoneInfo(timezone)).date()


def local_day_bounds_utc(day: date, timezone: str) -> tuple[datetime, datetime]:
    """UTC [start, end) of a local calendar day. DST-safe: a day may be 23 or 25 hours."""
    zone = ZoneInfo(timezone)
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


class SendingWindow(CoreModel):
    """Local working hours in which automated sending is allowed.

    Same-day windows only in V1: ``start_local_time`` must be before ``end_local_time``
    (overnight windows are rejected). The end is exclusive. Times are local wall-clock
    times and must be naive.
    """

    timezone: TimezoneName
    working_days: Annotated[tuple[Weekday, ...], Field(min_length=1), AfterValidator(unique_items)]
    start_local_time: time
    end_local_time: time

    @model_validator(mode="after")
    def _check_times(self) -> Self:
        if self.start_local_time.tzinfo is not None or self.end_local_time.tzinfo is not None:
            raise ValueError("window times are local wall-clock times and must be naive")
        if self.start_local_time >= self.end_local_time:
            raise ValueError("start_local_time must be before end_local_time (no overnight windows)")
        return self


def is_within_sending_window(now_utc: datetime, window: SendingWindow) -> bool:
    local = _require_aware(now_utc).astimezone(ZoneInfo(window.timezone))
    if Weekday(local.weekday()) not in window.working_days:
        return False
    return window.start_local_time <= local.time() < window.end_local_time
