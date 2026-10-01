"""Provider-neutral result of one operator-channel pass (Stage 17). Kept apart from the
Telegram package so the runtime can report it without loading any provider code."""

from enum import StrEnum

from app.core.models.base import CoreModel


class OperatorSyncStatus(StrEnum):
    OK = "OK"
    PARTIAL = "PARTIAL"  # updates handled, but some failed or the card pass stopped early
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"


class OperatorSyncResult(CoreModel):
    """Counts and codes only: no update content, no token."""

    status: OperatorSyncStatus
    reason: str | None = None
    updates: int = 0
    outcomes: dict[str, int] = {}
    failed_updates: tuple[int, ...] = ()
    notifications_sent: int = 0
    notifications_failed: int = 0
    cursor: int | None = None
