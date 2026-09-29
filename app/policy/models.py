"""Policy reason codes, checks, decision results and the kill-switch state.

Each reason maps to exactly one OutboundDecision through a fixed table, so a reason
can never produce a different decision at the call site.
"""

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, model_validator

from app.core.decisions import combine_outbound_decisions
from app.core.enums import OutboundDecision
from app.core.models.base import CoreModel
from app.core.models.types import NonEmptyStr


class PolicyReason(StrEnum):
    """Stable machine-readable policy reasons."""

    DNC_EMAIL = "DNC_EMAIL"
    DNC_DOMAIN = "DNC_DOMAIN"
    INVALID_OR_BOUNCED_ADDRESS = "INVALID_OR_BOUNCED_ADDRESS"
    CAMPAIGN_NOT_ACTIVE = "CAMPAIGN_NOT_ACTIVE"
    CAMPAIGN_PAUSED = "CAMPAIGN_PAUSED"
    CAMPAIGN_ENDED = "CAMPAIGN_ENDED"
    KILL_SWITCH = "KILL_SWITCH"
    OUTSIDE_SENDING_WINDOW = "OUTSIDE_SENDING_WINDOW"
    GLOBAL_DAILY_LIMIT = "GLOBAL_DAILY_LIMIT"
    MAILBOX_DAILY_LIMIT = "MAILBOX_DAILY_LIMIT"
    CAMPAIGN_DAILY_LIMIT = "CAMPAIGN_DAILY_LIMIT"
    NEW_CONTACT_DAILY_LIMIT = "NEW_CONTACT_DAILY_LIMIT"
    FOLLOWUP_DAILY_LIMIT = "FOLLOWUP_DAILY_LIMIT"
    CONTACT_FOLLOWUP_LIMIT = "CONTACT_FOLLOWUP_LIMIT"
    FOLLOWUP_INTERVAL = "FOLLOWUP_INTERVAL"
    FOLLOWUP_PLAN_PAUSED = "FOLLOWUP_PLAN_PAUSED"
    FOLLOWUP_PLAN_INACTIVE = "FOLLOWUP_PLAN_INACTIVE"
    LEAD_NOT_AWAITING_REPLY = "LEAD_NOT_AWAITING_REPLY"
    LEAD_ON_HOLD = "LEAD_ON_HOLD"
    LEAD_OPERATOR_OWNED = "LEAD_OPERATOR_OWNED"
    # Reserved: defined for stable codes, evaluated in later stages once their inputs exist.
    ACTIVE_LEAD_EXISTS = "ACTIVE_LEAD_EXISTS"
    RECENT_CROSS_CAMPAIGN_CONTACT = "RECENT_CROSS_CAMPAIGN_CONTACT"
    KNOWLEDGE_REVIEW_REQUIRED = "KNOWLEDGE_REVIEW_REQUIRED"
    COMPLIANCE_REVIEW_REQUIRED = "COMPLIANCE_REVIEW_REQUIRED"


RESERVED_REASONS: frozenset[PolicyReason] = frozenset(
    {
        PolicyReason.ACTIVE_LEAD_EXISTS,
        PolicyReason.RECENT_CROSS_CAMPAIGN_CONTACT,
        PolicyReason.KNOWLEDGE_REVIEW_REQUIRED,
        PolicyReason.COMPLIANCE_REVIEW_REQUIRED,
    }
)

_SKIP, _ESCALATE, _HOLD = OutboundDecision.SKIP, OutboundDecision.ESCALATE, OutboundDecision.HOLD

REASON_DECISIONS: Mapping[PolicyReason, OutboundDecision] = {
    PolicyReason.DNC_EMAIL: _SKIP,
    PolicyReason.DNC_DOMAIN: _SKIP,
    PolicyReason.INVALID_OR_BOUNCED_ADDRESS: _SKIP,
    PolicyReason.CAMPAIGN_NOT_ACTIVE: _HOLD,
    PolicyReason.CAMPAIGN_PAUSED: _HOLD,
    PolicyReason.CAMPAIGN_ENDED: _SKIP,
    PolicyReason.KILL_SWITCH: _HOLD,
    PolicyReason.OUTSIDE_SENDING_WINDOW: _HOLD,
    PolicyReason.GLOBAL_DAILY_LIMIT: _HOLD,
    PolicyReason.MAILBOX_DAILY_LIMIT: _HOLD,
    PolicyReason.CAMPAIGN_DAILY_LIMIT: _HOLD,
    PolicyReason.NEW_CONTACT_DAILY_LIMIT: _HOLD,
    PolicyReason.FOLLOWUP_DAILY_LIMIT: _HOLD,
    PolicyReason.CONTACT_FOLLOWUP_LIMIT: _SKIP,
    PolicyReason.FOLLOWUP_INTERVAL: _HOLD,
    PolicyReason.FOLLOWUP_PLAN_PAUSED: _HOLD,
    PolicyReason.FOLLOWUP_PLAN_INACTIVE: _SKIP,
    PolicyReason.LEAD_NOT_AWAITING_REPLY: _SKIP,
    PolicyReason.LEAD_ON_HOLD: _HOLD,
    PolicyReason.LEAD_OPERATOR_OWNED: _SKIP,
    PolicyReason.ACTIVE_LEAD_EXISTS: _SKIP,
    PolicyReason.RECENT_CROSS_CAMPAIGN_CONTACT: _SKIP,
    PolicyReason.KNOWLEDGE_REVIEW_REQUIRED: _ESCALATE,
    PolicyReason.COMPLIANCE_REVIEW_REQUIRED: _ESCALATE,
}


class PolicyCheck(CoreModel):
    """One failed policy check. Its decision is fixed by its reason."""

    reason: PolicyReason
    detail: NonEmptyStr

    @property
    def decision(self) -> OutboundDecision:
        return REASON_DECISIONS[self.reason]


class PolicyDecisionResult(CoreModel):
    """Outcome of a policy evaluation.

    ``checks`` holds every failed check in evaluation order. With no failures the
    decision is SEND; otherwise it is the highest-precedence decision among them
    (SKIP > ESCALATE > HOLD > SEND).
    """

    decision: OutboundDecision
    checks: tuple[PolicyCheck, ...] = ()

    @model_validator(mode="after")
    def _check_decision(self) -> Self:
        if self.decision is not _expected_decision(self.checks):
            raise ValueError(f"decision {self.decision} does not follow from the failed checks")
        return self

    @classmethod
    def from_checks(cls, checks: Iterable[PolicyCheck]) -> Self:
        failed = tuple(checks)
        return cls(decision=_expected_decision(failed), checks=failed)

    @property
    def reasons(self) -> tuple[PolicyReason, ...]:
        return tuple(check.reason for check in self.checks)


def _expected_decision(checks: tuple[PolicyCheck, ...]) -> OutboundDecision:
    if not checks:
        return OutboundDecision.SEND
    return combine_outbound_decisions(check.decision for check in checks)


class KillSwitchState(CoreModel):
    """Global stop for automated sending. Configuration/operator state; never LLM-controlled."""

    enabled: bool
    reason: NonEmptyStr | None = None
    changed_at: AwareDatetime
    changed_by: NonEmptyStr

    @model_validator(mode="after")
    def _check_reason(self) -> Self:
        if self.enabled and self.reason is None:
            raise ValueError("an enabled kill switch requires a reason")
        return self
