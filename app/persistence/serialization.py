"""Deterministic JSON serialization for stored records. JSON only; never pickle.

- Models are dumped in pydantic JSON mode: enums by value, datetimes as ISO 8601 with
  their original offset (UTC stays UTC), timedeltas as ISO 8601 durations.
- Keys are sorted and separators fixed, so equal values always produce identical text.
- Timestamps projected into SQL columns use a fixed-width UTC format, so text ordering
  equals chronological ordering.
"""

import json
from datetime import UTC, datetime
from typing import TypeVar

from pydantic import BaseModel, JsonValue

M = TypeVar("M", bound=BaseModel)


def dumps_json(value: JsonValue) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def loads_json(text: str) -> JsonValue:
    loaded: JsonValue = json.loads(text)
    return loaded


def model_to_json(model: BaseModel) -> str:
    return dumps_json(model.model_dump(mode="json"))


def model_from_json(model_type: type[M], text: str) -> M:
    """Rebuild and re-validate a model. Raises pydantic.ValidationError on invalid data."""
    return model_type.model_validate_json(text)


def to_utc_text(value: datetime) -> str:
    """Fixed-width UTC text, e.g. ``2026-01-01T12:00:00.000000+00:00``."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetimes cannot be stored")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def optional_utc_text(value: datetime | None) -> str | None:
    return None if value is None else to_utc_text(value)


def from_utc_text(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"stored timestamp is not timezone-aware: {text!r}")
    return parsed.astimezone(UTC)
