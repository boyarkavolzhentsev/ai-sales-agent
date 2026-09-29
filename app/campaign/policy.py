"""Campaign outreach eligibility, from current authoritative state.

Contact-level rules come first and are the ones that keep campaign automation from ever
running in parallel with other work for the same person:
- suppression (email, email domain, company domain) and a bounced address;
- an active (non-terminal) Stage 9 conversation with the contact;
- any message to the contact (any of its leads) with an unresolved Stage 8 dispatch,
  late-acceptance conflict evidence, or an undispatched draft.
Then the lead (closed, on hold, operator-owned, open escalation), then the Stage 3 policy
for the touch's kind: FIRST_TOUCH outbound policy, or the Stage 3 follow-up policy over the
campaign FollowUpPlan for later touches (quota, window, kill switch, campaign status,
interval, caps).

Each code has one disposition: TERMINAL (the membership ends with a status), SUPERSEDED
(stale job), CANCEL (campaign not executable now), BLOCK (operator attention; the job
stops) or DEFER (temporary; the job is rescheduled).
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.core.enums import (
    CampaignMemberStatus,
    CampaignStatus,
    DNCScope,
    EmailValidity,
    EscalationStatus,
    LeadStage,
    LeadStatus,
    OutboundStatus,
)
from app.core.models import (
    TERMINAL_CONVERSATION_STATUSES,
    CampaignMember,
    DoNotContactEntry,
    OutboundMessage,
    ProspectCompany,
    ProspectContact,
)
from app.conversation.cancellation import UNDISPATCHED_STATUSES
from app.persistence import UNRESOLVED_ATTEMPT_STATES, UnitOfWork
from app.policy import PolicyReason
from app.policy.suppression import evaluate_suppression

M = CampaignMemberStatus


class CampaignBlock(StrEnum):
    CAMPAIGN_NOT_ACTIVE = "CAMPAIGN_NOT_ACTIVE"
    CAMPAIGN_ENDED = "CAMPAIGN_ENDED"
    MEMBER_NOT_READY = "MEMBER_NOT_READY"
    MEMBER_CHANGED = "MEMBER_CHANGED"
    STALE_TOUCH = "STALE_TOUCH"
    NOT_DUE = "NOT_DUE"
    CONTACT_MISSING = "CONTACT_MISSING"
    DO_NOT_CONTACT = "DO_NOT_CONTACT"
    INVALID_ADDRESS = "INVALID_ADDRESS"
    ACTIVE_LEAD_EXISTS = "ACTIVE_LEAD_EXISTS"
    PREVIOUSLY_DECLINED = "PREVIOUSLY_DECLINED"
    LEAD_CLOSED = "LEAD_CLOSED"
    LEAD_ON_HOLD = "LEAD_ON_HOLD"
    LEAD_OPERATOR_OWNED = "LEAD_OPERATOR_OWNED"
    OPERATOR_REVIEW_OPEN = "OPERATOR_REVIEW_OPEN"
    CONVERSATION_ACTIVE = "CONVERSATION_ACTIVE"
    DISPATCH_UNRESOLVED = "DISPATCH_UNRESOLVED"
    ACCEPTANCE_CONFLICT = "ACCEPTANCE_CONFLICT"
    OUTBOUND_PENDING = "OUTBOUND_PENDING"
    NO_FOLLOW_UP_PLAN = "NO_FOLLOW_UP_PLAN"
    SEQUENCE_COMPLETE = "SEQUENCE_COMPLETE"
    CLAIM_CHECK_FAILED = "CLAIM_CHECK_FAILED"


# Code -> (disposition, terminal member status when TERMINAL).
TERMINAL: dict[str, M] = {
    CampaignBlock.DO_NOT_CONTACT: M.SUPPRESSED,
    PolicyReason.DNC_EMAIL.value: M.SUPPRESSED,
    PolicyReason.DNC_DOMAIN.value: M.SUPPRESSED,
    CampaignBlock.INVALID_ADDRESS: M.SKIPPED,
    PolicyReason.INVALID_OR_BOUNCED_ADDRESS.value: M.SKIPPED,
    CampaignBlock.CONVERSATION_ACTIVE: M.SKIPPED,
    CampaignBlock.CONTACT_MISSING: M.SKIPPED,
    CampaignBlock.LEAD_CLOSED: M.CANCELLED,
    CampaignBlock.LEAD_OPERATOR_OWNED: M.CANCELLED,
    PolicyReason.LEAD_OPERATOR_OWNED.value: M.CANCELLED,
    PolicyReason.LEAD_NOT_AWAITING_REPLY.value: M.CANCELLED,
    CampaignBlock.CAMPAIGN_ENDED: M.CANCELLED,
    PolicyReason.CAMPAIGN_ENDED.value: M.CANCELLED,
    CampaignBlock.SEQUENCE_COMPLETE: M.COMPLETED,
    CampaignBlock.NO_FOLLOW_UP_PLAN: M.COMPLETED,
    PolicyReason.CONTACT_FOLLOWUP_LIMIT.value: M.COMPLETED,
    PolicyReason.FOLLOWUP_PLAN_INACTIVE.value: M.COMPLETED,
    CampaignBlock.CLAIM_CHECK_FAILED: M.FAILED,
}
SUPERSEDING = frozenset({CampaignBlock.MEMBER_NOT_READY, CampaignBlock.MEMBER_CHANGED, CampaignBlock.STALE_TOUCH})
CANCELLING = frozenset({CampaignBlock.CAMPAIGN_NOT_ACTIVE, PolicyReason.CAMPAIGN_PAUSED.value, PolicyReason.CAMPAIGN_NOT_ACTIVE.value})
BLOCKING = frozenset({CampaignBlock.ACCEPTANCE_CONFLICT})
# Everything else (window, quota, kill switch, hold, open escalation, unresolved or pending
# messages, follow-up interval, plan paused, not due) is temporary: DEFER.

# Terminal precedence: suppression first, then the rest in this order.
_TERMINAL_ORDER = (M.SUPPRESSED, M.SKIPPED, M.CANCELLED, M.COMPLETED, M.FAILED)


def terminal_status(codes: list[str]) -> tuple[M, str] | None:
    found = [(TERMINAL[c], c) for c in codes if c in TERMINAL]
    if not found:
        return None
    return min(found, key=lambda item: _TERMINAL_ORDER.index(item[0]))


@dataclass(frozen=True)
class ContactFacts:
    contact: ProspectContact | None
    company: ProspectCompany | None
    dnc_entries: tuple[DoNotContactEntry, ...]
    messages: tuple[OutboundMessage, ...]
    unresolved_ids: frozenset[str]
    conflict_ids: frozenset[str]
    active_conversations: int


def load_contact_facts(uow: UnitOfWork, contact_id: str) -> ContactFacts:
    contact = uow.contacts.get(contact_id)
    company = uow.companies.get(contact.company_id) if contact is not None and contact.company_id else None
    entries: list[DoNotContactEntry] = []
    if contact is not None:
        entries += uow.dnc.list_for_value(DNCScope.EMAIL, contact.email)
        for domain in sorted({contact.email.split("@", 1)[1], *([company.domain] if company else [])}):
            entries += uow.dnc.list_for_value(DNCScope.DOMAIN, domain)
    messages = tuple(m for lead in uow.leads.list_by_contact(contact_id) for m in uow.outbound.list_by_lead(lead.lead_id))
    unresolved: set[str] = set()
    conflicts: set[str] = set()
    for message in messages:
        if message.status is OutboundStatus.SENDING:
            unresolved.add(message.outbound_id)
        for attempt in uow.dispatch_attempts.list_for_outbound(message.outbound_id):
            if attempt.state in UNRESOLVED_ATTEMPT_STATES:
                unresolved.add(message.outbound_id)
            if attempt.late_acceptance_provider_message_id is not None:
                conflicts.add(message.outbound_id)
    active = sum(1 for c in uow.conversations.list_by_contact(contact_id) if c.status not in TERMINAL_CONVERSATION_STATUSES)
    return ContactFacts(contact, company, tuple(entries), messages, frozenset(unresolved), frozenset(conflicts), active)


def contact_blockers(facts: ContactFacts, now: datetime, *, own_outbound_id: str | None = None) -> list[str]:
    if facts.contact is None:
        return [CampaignBlock.CONTACT_MISSING]
    codes: list[str] = []
    domain = facts.company.domain if facts.company else None
    if evaluate_suppression(facts.contact.email, domain, facts.dnc_entries, now) is not None:
        codes.append(CampaignBlock.DO_NOT_CONTACT)
    if facts.contact.email_validity is EmailValidity.BOUNCED:
        codes.append(CampaignBlock.INVALID_ADDRESS)
    if facts.active_conversations:
        codes.append(CampaignBlock.CONVERSATION_ACTIVE)
    others = {m.outbound_id for m in facts.messages} - ({own_outbound_id} if own_outbound_id else set())
    if facts.unresolved_ids & others:
        codes.append(CampaignBlock.DISPATCH_UNRESOLVED)
    if facts.conflict_ids:
        codes.append(CampaignBlock.ACCEPTANCE_CONFLICT)
    if any(m.status in UNDISPATCHED_STATUSES for m in facts.messages if m.outbound_id in others):
        codes.append(CampaignBlock.OUTBOUND_PENDING)
    return codes


def lead_blockers(uow: UnitOfWork, member: CampaignMember) -> list[str]:
    lead = uow.leads.get(member.lead_id) if member.lead_id else None
    if lead is None or lead.stage is LeadStage.CLOSED:
        return [CampaignBlock.LEAD_CLOSED]
    codes: list[str] = []
    if lead.status is LeadStatus.ON_HOLD:
        codes.append(CampaignBlock.LEAD_ON_HOLD)
    elif lead.status is LeadStatus.OPERATOR_OWNED:
        codes.append(CampaignBlock.LEAD_OPERATOR_OWNED)
    if any(e.status in (EscalationStatus.OPEN, EscalationStatus.ACKNOWLEDGED) for e in uow.escalations.list_by_lead(lead.lead_id)):
        codes.append(CampaignBlock.OPERATOR_REVIEW_OPEN)
    return codes


def enrollment_blockers(uow: UnitOfWork, campaign_id: str, contact_id: str, now: datetime) -> list[str]:
    """Why a contact must not be put in the sequence at all (recorded as SKIPPED/SUPPRESSED).
    An existing open lead or a past decline is never merged or guessed around."""
    facts = load_contact_facts(uow, contact_id)
    codes = [c for c in contact_blockers(facts, now) if c in (CampaignBlock.DO_NOT_CONTACT, CampaignBlock.INVALID_ADDRESS,
                                                              CampaignBlock.CONVERSATION_ACTIVE, CampaignBlock.CONTACT_MISSING)]
    leads = uow.leads.list_by_contact(contact_id)
    if any(lead.stage is not LeadStage.CLOSED for lead in leads):
        codes.append(CampaignBlock.ACTIVE_LEAD_EXISTS)
    if any(lead.close_reason is not None and lead.close_reason.value in ("NOT_INTERESTED", "UNSUBSCRIBED") for lead in leads):
        codes.append(CampaignBlock.PREVIOUSLY_DECLINED)
    return codes


def campaign_blockers(status: CampaignStatus | None) -> list[str]:
    if status is None or status is CampaignStatus.ENDED:
        return [CampaignBlock.CAMPAIGN_ENDED]
    if status is not CampaignStatus.ACTIVE:
        return [CampaignBlock.CAMPAIGN_NOT_ACTIVE]
    return []


ENROLLMENT_TERMINAL = {
    CampaignBlock.DO_NOT_CONTACT: M.SUPPRESSED,
    CampaignBlock.INVALID_ADDRESS: M.SKIPPED,
    CampaignBlock.CONVERSATION_ACTIVE: M.SKIPPED,
    CampaignBlock.ACTIVE_LEAD_EXISTS: M.SKIPPED,
    CampaignBlock.PREVIOUSLY_DECLINED: M.SKIPPED,
}
