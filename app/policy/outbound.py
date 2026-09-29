"""Deterministic outbound policy evaluation. Pure: no I/O, no LLM, no state changes.

Stage 3 never returns ESCALATE: no deterministic Stage 3 condition needs operator
review. Knowledge and compliance escalations belong to later stages.
"""

from typing import Self

from pydantic import AwareDatetime, model_validator

from app.core.enums import CampaignStatus, EmailValidity, OutboundKind
from app.core.models import Campaign, DoNotContactEntry, ProspectContact
from app.core.models.base import CoreModel
from app.core.models.types import DomainName
from app.policy.limits import LimitPolicy
from app.policy.models import KillSwitchState, PolicyCheck, PolicyDecisionResult, PolicyReason
from app.policy.quota import QuotaSnapshot, evaluate_quota
from app.policy.suppression import evaluate_suppression
from app.policy.windows import SendingWindow, is_within_sending_window


class PolicyContext(CoreModel):
    """Every fact the deterministic outbound evaluator needs, gathered by the caller."""

    now: AwareDatetime
    kind: OutboundKind
    contact: ProspectContact
    company_domain: DomainName | None = None
    campaign: Campaign | None = None
    suppression_entries: tuple[DoNotContactEntry, ...] = ()
    kill_switch: KillSwitchState
    window: SendingWindow
    limits: LimitPolicy
    quota: QuotaSnapshot

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.kind in (OutboundKind.FIRST_TOUCH, OutboundKind.FOLLOW_UP) and self.campaign is None:
            raise ValueError(f"{self.kind} evaluation requires the campaign")
        if self.quota.contact_id != self.contact.contact_id:
            raise ValueError("quota snapshot is for a different contact")
        expected_campaign = self.campaign.campaign_id if self.campaign is not None else None
        if self.quota.campaign_id != expected_campaign:
            raise ValueError("quota snapshot is for a different campaign")
        if self.campaign is not None and self.quota.mailbox != self.campaign.sending_mailbox:
            raise ValueError("quota snapshot is for a different mailbox than the campaign's")
        return self


def evaluate_outbound_policy(context: PolicyContext) -> PolicyDecisionResult:
    return PolicyDecisionResult.from_checks(outbound_checks(context))


def outbound_checks(context: PolicyContext) -> list[PolicyCheck]:
    """All failed deterministic checks, in a fixed evaluation order."""
    checks: list[PolicyCheck] = []

    match = evaluate_suppression(
        context.contact.email, context.company_domain, context.suppression_entries, context.now
    )
    if match is not None:
        checks.append(
            PolicyCheck(reason=match.policy_reason, detail=f"DNC {match.scope} {match.value} ({match.reason})")
        )
    if context.contact.email_validity is EmailValidity.BOUNCED:
        checks.append(PolicyCheck(reason=PolicyReason.INVALID_OR_BOUNCED_ADDRESS, detail="address bounced"))

    if context.campaign is not None:
        checks.extend(campaign_checks(context.campaign, context))

    if context.kill_switch.enabled:
        checks.append(
            PolicyCheck(reason=PolicyReason.KILL_SWITCH, detail=f"kill switch: {context.kill_switch.reason}")
        )
    if not is_within_sending_window(context.now, context.window):
        checks.append(PolicyCheck(reason=PolicyReason.OUTSIDE_SENDING_WINDOW, detail="outside sending window"))

    checks.extend(evaluate_quota(context.quota, context.limits, context.kind, context.campaign))
    return checks


def campaign_checks(campaign: Campaign, context: PolicyContext) -> list[PolicyCheck]:
    """DRAFT or not yet started -> HOLD; PAUSED -> HOLD; ENDED or past end_at -> SKIP."""
    status = campaign.status
    if status is CampaignStatus.ENDED or (campaign.end_at is not None and campaign.end_at <= context.now):
        return [PolicyCheck(reason=PolicyReason.CAMPAIGN_ENDED, detail=f"campaign {campaign.campaign_id} ended")]
    if status is CampaignStatus.PAUSED:
        return [PolicyCheck(reason=PolicyReason.CAMPAIGN_PAUSED, detail=f"campaign {campaign.campaign_id} paused")]
    if status is CampaignStatus.DRAFT:
        return [
            PolicyCheck(reason=PolicyReason.CAMPAIGN_NOT_ACTIVE, detail=f"campaign {campaign.campaign_id} is a draft")
        ]
    if campaign.start_at is not None and campaign.start_at > context.now:
        return [
            PolicyCheck(
                reason=PolicyReason.CAMPAIGN_NOT_ACTIVE, detail=f"campaign {campaign.campaign_id} not started"
            )
        ]
    return []
