from datetime import UTC, datetime, timedelta

import pytest

from app.core.validation import (
    ensure_after,
    ensure_not_before,
    normalize_domain,
    normalize_email,
    require_non_blank,
    unique_items,
)

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Sales@Example.COM", "sales@example.com"),
        ("  partners@acme.io ", "partners@acme.io"),
        ("first.last+tag@sub.example.co.uk", "first.last+tag@sub.example.co.uk"),
        ("ops@xn--80ak6aa92e.com", "ops@xn--80ak6aa92e.com"),
    ],
)
def test_normalize_email_accepts_and_normalizes(raw: str, expected: str) -> None:
    assert normalize_email(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "no-at-sign.example.com",
        "two@@example.com",
        "a@b@example.com",
        "@example.com",
        "user@",
        "user name@example.com",
        "Name <user@example.com>",
        ".user@example.com",
        "user.@example.com",
        "us..er@example.com",
        "user@localhost",
        "user@example.com.",
        "user@exa_mple.com",
        "user@-example.com",
        "user@пример.com",
        "user@127.0.0.1",
    ],
)
def test_normalize_email_rejects_ambiguous_or_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_email(raw)


def test_normalize_domain() -> None:
    assert normalize_domain(" Example.COM ") == "example.com"
    for bad in ["", "example", "example.com.", "ex ample.com", "a" * 64 + ".com"]:
        with pytest.raises(ValueError):
            normalize_domain(bad)


def test_require_non_blank_returns_value_unchanged() -> None:
    assert require_non_blank("  x  ") == "  x  "
    with pytest.raises(ValueError):
        require_non_blank(" \t\n")


def test_unique_items() -> None:
    assert unique_items((1, 2, 3)) == (1, 2, 3)
    with pytest.raises(ValueError, match="duplicate"):
        unique_items(("a", "b", "a"))


def test_time_ordering_helpers() -> None:
    later = T0 + timedelta(seconds=1)
    ensure_not_before(T0, T0, "a", "b")
    ensure_not_before(None, T0, "a", "b")
    ensure_after(later, T0, "a", "b")
    with pytest.raises(ValueError, match="must not precede"):
        ensure_not_before(T0, later, "a", "b")
    with pytest.raises(ValueError, match="must be after"):
        ensure_after(T0, T0, "a", "b")
