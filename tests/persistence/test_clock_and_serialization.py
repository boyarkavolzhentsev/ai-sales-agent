import json
from datetime import datetime, timedelta, timezone

import pytest

from app.core.enums import LeadStage
from app.core.models import Campaign, Lead
from app.persistence import Clock, FrozenClock, SystemClock
from app.persistence.serialization import (
    dumps_json,
    from_utc_text,
    loads_json,
    model_from_json,
    model_to_json,
    to_utc_text,
)
from tests.persistence import factories as f

KYIV_WINTER = timezone(timedelta(hours=2))

# ---- Clock ------------------------------------------------------------------


def test_frozen_clock_is_deterministic() -> None:
    clock = FrozenClock(f.T0)
    assert clock.now() == clock.now() == f.T0
    clock.advance(timedelta(minutes=5))
    assert clock.now() == f.T0 + timedelta(minutes=5)
    clock.set(f.T0)
    assert clock.now() == f.T0


def test_frozen_clock_rejects_naive_and_backwards_and_normalizes_to_utc() -> None:
    with pytest.raises(ValueError):
        FrozenClock(datetime(2026, 1, 1))
    clock = FrozenClock(datetime(2026, 1, 1, 14, 0, tzinfo=KYIV_WINTER))
    assert clock.now() == f.T0
    assert clock.now().utcoffset() == timedelta(0)
    with pytest.raises(ValueError):
        clock.advance(timedelta(seconds=-1))


def test_system_clock_returns_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
    assert isinstance(SystemClock(), Clock)
    assert isinstance(FrozenClock(f.T0), Clock)


# ---- Serialization ------------------------------------------------------------


def test_model_json_is_deterministic_sorted_and_enum_by_value() -> None:
    lead = f.lead()
    text = model_to_json(lead)
    assert text == model_to_json(f.lead())
    parsed = json.loads(text)
    assert list(parsed) == sorted(parsed)
    assert parsed["stage"] == "NEW"
    assert " " not in text.replace("Q1 partnerships", "")


def test_model_json_roundtrip_is_exact() -> None:
    for model in (f.lead(), f.campaign(), f.audit_event(), f.inbound_message(), f.operator_response()):
        restored = model_from_json(type(model), model_to_json(model))
        assert restored == model
        assert model_to_json(restored) == model_to_json(model)


def test_datetimes_keep_utc_and_offsets_roundtrip() -> None:
    lead = f.lead(created_at=datetime(2026, 1, 1, 14, 0, tzinfo=KYIV_WINTER), updated_at=f.T0 + timedelta(hours=1))
    restored = model_from_json(Lead, model_to_json(lead))
    assert restored.created_at == lead.created_at
    assert restored.created_at.utcoffset() == timedelta(hours=2)
    assert restored.updated_at.utcoffset() == timedelta(0)


def test_timedelta_and_tuples_roundtrip() -> None:
    campaign = f.campaign()
    restored = model_from_json(Campaign, model_to_json(campaign))
    assert restored.min_interval_between_follow_ups == timedelta(days=3)
    assert isinstance(restored.allowed_knowledge_domains, tuple)


def test_json_is_utf8_text_not_escaped() -> None:
    assert "ціна" in model_to_json(f.inbound_message())


def test_dumps_json_rejects_nan_and_sorts_keys() -> None:
    assert dumps_json({"b": 1, "a": [1, "x"]}) == '{"a":[1,"x"],"b":1}'
    assert loads_json('{"a":1}') == {"a": 1}
    with pytest.raises(ValueError):
        dumps_json(float("nan"))


def test_utc_text_is_fixed_width_and_orders_chronologically() -> None:
    earlier = datetime(2026, 1, 1, 13, 59, tzinfo=KYIV_WINTER)  # 11:59 UTC
    later = f.T0  # 12:00 UTC
    assert to_utc_text(later) == "2026-01-01T12:00:00.000000+00:00"
    assert to_utc_text(earlier) < to_utc_text(later)
    assert len(to_utc_text(earlier)) == len(to_utc_text(later))
    assert from_utc_text(to_utc_text(earlier)) == earlier


def test_utc_text_rejects_naive() -> None:
    with pytest.raises(ValueError):
        to_utc_text(datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        from_utc_text("2026-01-01T12:00:00")


def test_stage_enum_restored_as_enum() -> None:
    assert model_from_json(Lead, model_to_json(f.lead())).stage is LeadStage.NEW
