"""Follow-up policy: whether and when the next follow-up of a conversation may exist.

Deterministic and evaluated from current authoritative state (never from a conversation
mirror alone): lead status and stage, suppression, open escalations, every outbound
message of the lead and its Stage 8 attempts, the conversation's last messages and count.

Hard rule (Stage 8): while any outbound message to the contact (any of its leads) has an
unresolved dispatch attempt (CLAIMED/UNKNOWN), is SENDING, or carries late-acceptance
conflict evidence, no follow-up is scheduled, executed or dispatched.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated

from pydantic import Field

from app.core.enums import (
    ConversationStatus,
    DNCScope,
    EscalationStatus,
    LeadStage,
    LeadStatus,
    OutboundStatus,
)
from app.core.models import TERMINAL_CONVERSATION_STATUSES, Conversation, DoNotContactEntry, FollowUpJob, Lead, OutboundMessage, ProspectContact
from app.core.models.base import CoreModel
from app.conversation.cancellation import UNDISPATCHED_STATUSES
from app.llm import SenderIdentity
from app.persistence import UNRESOLVED_ATTEMPT_STATES, UnitOfWork
from app.policy import KillSwitchState, LimitPolicy, SendingWindow
from app.policy.suppression import evaluate_suppression

S = ConversationStatus


class FollowUpConfig(CoreModel):
    sender: SenderIdentity
    # Stage 3 inputs, re-checked when a follow-up is executed (and again by Stage 8 at dispatch).
    limits: LimitPolicy
    window: SendingWindow
    kill_switch: KillSwitchState
    # Follow-ups per conversation; the effective cap is also bounded by the Stage 3
    # ``max_follow_ups_per_contact``.
    max_follow_ups: Annotated[int, Field(ge=1, le=5)] = 2
    first_delay: Annotated[timedelta, Field(gt=timedelta(0))] = timedelta(days=3)
    interval: Annotated[timedelta, Field(gt=timedelta(0))] = timedelta(days=4)
    lease: Annotated[timedelta, Field(gt=timedelta(0), le=timedelta(hours=1))] = timedelta(minutes=5)
    # A temporary Stage 3 hold (window, kill switch, quota) defers the job by this much.
    defer_delay: Annotated[timedelta, Field(gt=timedelta(0))] = timedelta(hours=1)


class FollowUpBlock(StrEnum):
    CONVERSATION_NOT_WAITING = "CONVERSATION_NOT_WAITING"
    CONVERSATION_PAUSED = "CONVERSATION_PAUSED"
    CONVERSATION_CLOSED = "CONVERSATION_CLOSED"
    ASSOCIATION_UNCERTAIN = "ASSOCIATION_UNCERTAIN"
    DO_NOT_CONTACT = "DO_NOT_CONTACT"
    LEAD_CLOSED = "LEAD_CLOSED"
    LEAD_ON_HOLD = "LEAD_ON_HOLD"
    LEAD_OPERATOR_OWNED = "LEAD_OPERATOR_OWNED"
    OPERATOR_REVIEW_OPEN = "OPERATOR_REVIEW_OPEN"
    NO_ACCEPTED_OUTBOUND = "NO_ACCEPTED_OUTBOUND"
    NO_INBOUND_CONTEXT = "NO_INBOUND_CONTEXT"
    NEWER_INBOUND = "NEWER_INBOUND"
    OUTBOUND_PENDING = "OUTBOUND_PENDING"
    DISPATCH_UNRESOLVED = "DISPATCH_UNRESOLVED"
    ACCEPTANCE_CONFLICT = "ACCEPTANCE_CONFLICT"
    MAX_FOLLOW_UPS = "MAX_FOLLOW_UPS"
    FOLLOW_UP_ALREADY_SCHEDULED = "FOLLOW_UP_ALREADY_SCHEDULED"
    NOT_DUE = "NOT_DUE"
    STALE_ANCHOR = "STALE_ANCHOR"
    CONVERSATION_CHANGED = "CONVERSATION_CHANGED"
    CLAIM_CHECK_FAILED = "CLAIM_CHECK_FAILED"


# Codes meaning "newer activity made this follow-up stale" (SUPERSEDED rather than BLOCKED).
STALE_CODES = frozenset({FollowUpBlock.NEWER_INBOUND, FollowUpBlock.STALE_ANCHOR, FollowUpBlock.CONVERSATION_CHANGED})


@dataclass(frozen=True)
class FollowUpFacts:
    conversation: Conversation
    lead: Lead | None
    contact: ProspectContact | None
    company_domain: str | None
    dnc_entries: tuple[DoNotContactEntry, ...]
    open_escalations: int
    lead_messages: tuple[OutboundMessage, ...]
    unresolved_outbound_ids: frozenset[str]
    conflict_outbound_ids: frozenset[str]
    anchor: OutboundMessage | None
    open_job: FollowUpJob | None


def load_facts(uow: UnitOfWork, conversation: Conversation) -> FollowUpFacts:
    lead = uow.leads.get(conversation.lead_id)
    contact = uow.contacts.get(conversation.contact_id)
    company = uow.companies.get(contact.company_id) if contact is not None and contact.company_id else None
    entries: list[DoNotContactEntry] = []
    if contact is not None:
        entries += uow.dnc.list_for_value(DNCScope.EMAIL, contact.email)
        for domain in sorted({contact.email.split("@", 1)[1], *([company.domain] if company else [])}):
            entries += uow.dnc.list_for_value(DNCScope.DOMAIN, domain)
    escalations = [e for e in uow.escalations.list_by_lead(conversation.lead_id)
                   if e.status in (EscalationStatus.OPEN, EscalationStatus.ACKNOWLEDGED)]
    # Every message to this contact, across all its leads: the one-outstanding-message rule
    # is about the person, not only this lead.
    lead_ids = {conversation.lead_id, *(found.lead_id for found in uow.leads.list_by_contact(conversation.contact_id))}
    messages = tuple(m for lead_id in sorted(lead_ids) for m in uow.outbound.list_by_lead(lead_id))
    unresolved: set[str] = set()
    conflicts: set[str] = set()
    for message in messages:
        for attempt in uow.dispatch_attempts.list_for_outbound(message.outbound_id):
            if attempt.state in UNRESOLVED_ATTEMPT_STATES:
                unresolved.add(message.outbound_id)
            if attempt.late_acceptance_provider_message_id is not None:
                conflicts.add(message.outbound_id)
        if message.status is OutboundStatus.SENDING:
            unresolved.add(message.outbound_id)
    anchor = uow.outbound.get(conversation.last_outbound_id) if conversation.last_outbound_id else None
    return FollowUpFacts(
        conversation=conversation, lead=lead, contact=contact, company_domain=company.domain if company else None,
        dnc_entries=tuple(entries), open_escalations=len(escalations), lead_messages=messages,
        unresolved_outbound_ids=frozenset(unresolved), conflict_outbound_ids=frozenset(conflicts), anchor=anchor,
        open_job=uow.follow_up_jobs.get_open_for_conversation(conversation.conversation_id),
    )


def effective_max(config: FollowUpConfig) -> int:
    return min(config.max_follow_ups, config.limits.max_follow_ups_per_contact)


def next_due(facts: FollowUpFacts, config: FollowUpConfig) -> datetime:
    last = facts.conversation.last_outbound_at
    if last is None:
        raise ValueError("no accepted outbound message to follow up")
    return last + (config.first_delay if facts.conversation.follow_up_count == 0 else config.interval)


def common_blockers(facts: FollowUpFacts, now: datetime, *, own_outbound_id: str | None = None) -> list[str]:
    """Checks shared by scheduling, execution and follow-up dispatch."""
    conversation, lead = facts.conversation, facts.lead
    codes: list[str] = []
    if conversation.status is S.PAUSED:
        codes.append(FollowUpBlock.CONVERSATION_PAUSED)
    elif conversation.status is S.DO_NOT_CONTACT:
        codes.append(FollowUpBlock.DO_NOT_CONTACT)
    elif conversation.status in TERMINAL_CONVERSATION_STATUSES:
        codes.append(FollowUpBlock.CONVERSATION_CLOSED)
    if not conversation.association_certain:
        codes.append(FollowUpBlock.ASSOCIATION_UNCERTAIN)
    if facts.contact is not None and evaluate_suppression(facts.contact.email, facts.company_domain, facts.dnc_entries, now):
        codes.append(FollowUpBlock.DO_NOT_CONTACT)
    if lead is None or lead.stage is LeadStage.CLOSED:
        codes.append(FollowUpBlock.LEAD_CLOSED)
    elif lead.status is LeadStatus.ON_HOLD:
        codes.append(FollowUpBlock.LEAD_ON_HOLD)
    elif lead.status is LeadStatus.OPERATOR_OWNED:
        codes.append(FollowUpBlock.LEAD_OPERATOR_OWNED)  # the agent only records and notifies
    if facts.open_escalations:
        codes.append(FollowUpBlock.OPERATOR_REVIEW_OPEN)
    others = {m.outbound_id for m in facts.lead_messages} - ({own_outbound_id} if own_outbound_id else set())
    if facts.unresolved_outbound_ids & others:
        codes.append(FollowUpBlock.DISPATCH_UNRESOLVED)
    if facts.conflict_outbound_ids:
        codes.append(FollowUpBlock.ACCEPTANCE_CONFLICT)
    if any(m.status in UNDISPATCHED_STATUSES for m in facts.lead_messages if m.outbound_id in others):
        codes.append(FollowUpBlock.OUTBOUND_PENDING)
    return codes


def anchor_blockers(facts: FollowUpFacts, config: FollowUpConfig) -> list[str]:
    conversation, anchor = facts.conversation, facts.anchor
    codes: list[str] = []
    if anchor is None or anchor.status is not OutboundStatus.SENT or anchor.sent_at is None:
        codes.append(FollowUpBlock.NO_ACCEPTED_OUTBOUND)
    elif conversation.last_inbound_at is not None and conversation.last_inbound_at > anchor.sent_at:
        codes.append(FollowUpBlock.NEWER_INBOUND)
    if conversation.last_inbound_message_id is None:
        codes.append(FollowUpBlock.NO_INBOUND_CONTEXT)
    if conversation.follow_up_count >= effective_max(config):
        codes.append(FollowUpBlock.MAX_FOLLOW_UPS)
    return codes


def schedule_blockers(facts: FollowUpFacts, config: FollowUpConfig, now: datetime) -> list[str]:
    codes = common_blockers(facts, now)
    # FOLLOW_UP_DUE: a job is already open (reported below as FOLLOW_UP_ALREADY_SCHEDULED).
    if facts.conversation.status not in (S.WAITING_FOR_REPLY, S.FOLLOW_UP_DUE, *TERMINAL_CONVERSATION_STATUSES, S.PAUSED):
        codes.append(FollowUpBlock.CONVERSATION_NOT_WAITING)
    codes += anchor_blockers(facts, config)
    if facts.open_job is not None:
        codes.append(FollowUpBlock.FOLLOW_UP_ALREADY_SCHEDULED)
    return list(dict.fromkeys(codes))


def execution_blockers(facts: FollowUpFacts, config: FollowUpConfig, job: FollowUpJob, now: datetime) -> list[str]:
    codes = common_blockers(facts, now)
    if facts.conversation.version != job.basis_conversation_version:
        codes.append(FollowUpBlock.CONVERSATION_CHANGED)
    elif facts.conversation.status is not S.FOLLOW_UP_DUE:
        codes.append(FollowUpBlock.CONVERSATION_NOT_WAITING)
    if facts.conversation.last_outbound_id != job.anchor_outbound_id:
        codes.append(FollowUpBlock.STALE_ANCHOR)
    codes += anchor_blockers(facts, config)
    if job.due_at > now:
        codes.append(FollowUpBlock.NOT_DUE)
    return list(dict.fromkeys(codes))
