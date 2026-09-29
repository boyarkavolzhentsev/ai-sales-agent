from datetime import datetime


def ensure_not_before(
    later: datetime | None, earlier: datetime | None, later_name: str, earlier_name: str
) -> None:
    """Raise if both are set and ``later`` precedes ``earlier``. Equal instants are allowed."""
    if later is not None and earlier is not None and later < earlier:
        raise ValueError(f"{later_name} must not precede {earlier_name}")


def ensure_after(
    later: datetime | None, earlier: datetime | None, later_name: str, earlier_name: str
) -> None:
    """Raise if both are set and ``later`` is not strictly after ``earlier``."""
    if later is not None and earlier is not None and later <= earlier:
        raise ValueError(f"{later_name} must be after {earlier_name}")
