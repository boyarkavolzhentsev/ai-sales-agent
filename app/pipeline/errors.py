"""Pipeline failures with stable codes. A failure leaves the transaction to roll back."""

from enum import StrEnum


class PipelineCode(StrEnum):
    LEAD_VERSION_CHANGED = "LEAD_VERSION_CHANGED"
    LEAD_CLOSED = "LEAD_CLOSED"
    CONTACT_SUPPRESSED = "CONTACT_SUPPRESSED"
    TRANSITION_NOT_ALLOWED = "TRANSITION_NOT_ALLOWED"
    QUALIFICATION_VERSION_CHANGED = "QUALIFICATION_VERSION_CHANGED"
    QUALIFICATION_NOT_STARTED = "QUALIFICATION_NOT_STARTED"
    QUALIFICATION_NOT_READY = "QUALIFICATION_NOT_READY"
    QUALIFICATION_DECIDED = "QUALIFICATION_DECIDED"
    QUALIFICATION_CONFLICT_OPEN = "QUALIFICATION_CONFLICT_OPEN"
    QUALIFICATION_FIELD_UNKNOWN = "QUALIFICATION_FIELD_UNKNOWN"
    CONFLICT_NOT_OPEN = "CONFLICT_NOT_OPEN"
    OPPORTUNITY_EXISTS = "OPPORTUNITY_EXISTS"
    OPPORTUNITY_REQUIRED = "OPPORTUNITY_REQUIRED"
    OPPORTUNITY_VERSION_CHANGED = "OPPORTUNITY_VERSION_CHANGED"
    OPPORTUNITY_NOT_ACTIVE = "OPPORTUNITY_NOT_ACTIVE"
    OPPORTUNITY_ACTIVE = "OPPORTUNITY_ACTIVE"
    NOT_REOPENABLE = "NOT_REOPENABLE"
    REOPEN_TARGET_NOT_ALLOWED = "REOPEN_TARGET_NOT_ALLOWED"


# Codes that mean "you acted on a version that is no longer current: re-read".
STALE_CODES = frozenset({
    PipelineCode.LEAD_VERSION_CHANGED, PipelineCode.QUALIFICATION_VERSION_CHANGED,
    PipelineCode.OPPORTUNITY_VERSION_CHANGED,
})


class PipelineError(Exception):
    """The requested pipeline change is not allowed on the current state. Nothing is
    written (the caller's transaction rolls back)."""

    def __init__(self, *codes: PipelineCode) -> None:
        self.codes = codes
        super().__init__(", ".join(code.value for code in codes))

    @property
    def stale(self) -> bool:
        return bool(STALE_CODES & set(self.codes))


class PipelineNotFoundError(Exception):
    """The referenced lead, qualification conflict or opportunity does not exist."""
