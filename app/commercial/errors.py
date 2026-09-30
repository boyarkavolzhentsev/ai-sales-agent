"""Commercial failures with stable codes. A failure leaves the transaction to roll back."""

from enum import StrEnum


class CommercialCode(StrEnum):
    LEAD_CLOSED = "LEAD_CLOSED"
    CONTACT_SUPPRESSED = "CONTACT_SUPPRESSED"
    OPPORTUNITY_NOT_OPEN = "OPPORTUNITY_NOT_OPEN"
    OPPORTUNITY_VERSION_CHANGED = "OPPORTUNITY_VERSION_CHANGED"
    QUALIFICATION_NOT_APPROVED = "QUALIFICATION_NOT_APPROVED"
    PROPOSAL_EXISTS = "PROPOSAL_EXISTS"
    REVISION_VERSION_CHANGED = "REVISION_VERSION_CHANGED"
    REVISION_NOT_EDITABLE = "REVISION_NOT_EDITABLE"
    REVISION_NOT_CURRENT = "REVISION_NOT_CURRENT"
    REVISION_STATUS_INVALID = "REVISION_STATUS_INVALID"
    PROPOSAL_NOT_READY = "PROPOSAL_NOT_READY"
    CURRENCY_NOT_ALLOWED = "CURRENCY_NOT_ALLOWED"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    TERM_VALUE_INVALID = "TERM_VALUE_INVALID"
    TERM_VERSION_CHANGED = "TERM_VERSION_CHANGED"
    DISCOUNT_NOT_ALLOWED = "DISCOUNT_NOT_ALLOWED"
    DISCOUNT_ABOVE_LIMIT = "DISCOUNT_ABOVE_LIMIT"
    REQUEST_VERSION_CHANGED = "REQUEST_VERSION_CHANGED"
    REQUEST_NOT_OPEN = "REQUEST_NOT_OPEN"
    OBJECTION_VERSION_CHANGED = "OBJECTION_VERSION_CHANGED"
    OBJECTION_NOT_OPEN = "OBJECTION_NOT_OPEN"
    SIGNAL_VERSION_CHANGED = "SIGNAL_VERSION_CHANGED"
    SIGNAL_NOT_OPEN = "SIGNAL_NOT_OPEN"


STALE_CODES = frozenset({
    CommercialCode.OPPORTUNITY_VERSION_CHANGED, CommercialCode.REVISION_VERSION_CHANGED,
    CommercialCode.TERM_VERSION_CHANGED, CommercialCode.REQUEST_VERSION_CHANGED,
    CommercialCode.OBJECTION_VERSION_CHANGED, CommercialCode.SIGNAL_VERSION_CHANGED,
})


class CommercialError(Exception):
    """The requested commercial change is not allowed on the current state."""

    def __init__(self, *codes: CommercialCode | str) -> None:
        self.codes = tuple(str(code) for code in codes)
        super().__init__(", ".join(self.codes))

    @property
    def stale(self) -> bool:
        return bool({str(c) for c in STALE_CODES} & set(self.codes))


class CommercialNotFoundError(Exception):
    """The referenced opportunity, revision, request, objection or signal does not exist."""
