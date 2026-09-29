from app.persistence.records import QuotaReservation
from app.policy.models import PolicyCheck


class PolicyError(Exception):
    """Base class for policy failures."""


class QuotaExceededError(PolicyError):
    """No quota slot is available. ``checks`` lists every exhausted limit."""

    def __init__(self, checks: tuple[PolicyCheck, ...]) -> None:
        reasons = ", ".join(check.reason.value for check in checks)
        super().__init__(f"quota exhausted: {reasons}")
        self.checks = checks


class DuplicateQuotaReservationError(PolicyError):
    """The outbound message already holds a live reservation. ``existing`` is that reservation."""

    def __init__(self, existing: QuotaReservation) -> None:
        super().__init__(f"outbound {existing.outbound_id} already holds {existing.reservation_id}")
        self.existing = existing
