"""Pre-dispatch checks that are specific to dispatch. Shared current-state gates (lead,
suppression, campaign, evidence, claims, newer messages) come from Stage 7's
``reply_gate_blockers`` so approval and dispatch can never diverge.

Approval provenance: OPERATOR_APPROVED status alone proves nothing. The Stage 7 command
record (``OPERATOR_APPROVE_DRAFT`` audit event, operator actor) must exist for this
outbound message and draft, record the same content hash that the stored subject and
body hash to now, and the approval time must match the message. On a first attempt the
message must be exactly the version the approval produced.

Recipient binding: a reply goes to the sender of the customer email it answers (an
immutable stored message), which must still be the lead's contact and a participant of
the thread. It is sent from the thread's mailbox, which must be one of ours.
"""

from dataclasses import dataclass
from datetime import datetime

from app.core.enums import ActorType, DNCScope, OutboundKind, RefKind
from app.core.models import DoNotContactEntry, EmailMessage, EmailThread, OutboundMessage, ProspectContact
from app.dispatch.models import DispatchCode, DispatchConfig
from app.inbound.records import ref
from app.llm.claim_check import draft_hash
from app.operator.models import CommandKind, CommandOutcome
from app.operator.review import load_draft_context
from app.persistence import UnitOfWork
from app.policy import (
    COUNTED_STATUSES,
    PolicyContext,
    PolicyDecisionResult,
    build_quota_snapshot,
    evaluate_outbound_policy,
)
from app.policy.windows import local_date, local_day_bounds_utc

APPROVAL_EVENT = f"OPERATOR_{CommandKind.APPROVE_DRAFT.value}"


@dataclass(frozen=True)
class Binding:
    recipient: str
    sender_mailbox: str
    contact: ProspectContact
    company_domain: str | None
    thread: EmailThread
    trigger: EmailMessage


def approval_codes(uow: UnitOfWork, outbound: OutboundMessage, *, first_attempt: bool) -> list[str]:
    content_hash = draft_hash(outbound.subject, outbound.body_final)
    codes: list[str] = []
    if content_hash != outbound.content_hash:
        codes.append(DispatchCode.CONTENT_INTEGRITY_FAILED)
    approvals = []
    for event in uow.audit.list_for_subject(ref(RefKind.OUTBOUND_MESSAGE, outbound.outbound_id)):
        if event.event_type != APPROVAL_EVENT or event.actor.type is not ActorType.OPERATOR or event.after is None:
            continue
        raw = event.after.get("outcome")
        if isinstance(raw, dict):
            approvals.append((CommandOutcome.model_validate(raw), event.before or {}, event.actor.id))
    if len(approvals) != 1:
        return [*codes, DispatchCode.APPROVAL_MISSING]
    outcome, before, actor_id = approvals[0]
    change = next((v for v in outcome.versions if v.entity == ref(RefKind.OUTBOUND_MESSAGE, outbound.outbound_id)), None)
    if (
        outcome.disposition != "OPERATOR_APPROVED"
        or outcome.operator_id != actor_id
        or ref(RefKind.MESSAGE_DRAFT, outbound.draft_id) not in outcome.subjects
        or before.get("content_hash") != content_hash
        or outbound.approved_at != outcome.completed_at
        or change is None
        or (first_attempt and outbound.version != change.resulting)
    ):
        codes.append(DispatchCode.APPROVAL_MISMATCH)
    return codes


def bind(uow: UnitOfWork, outbound: OutboundMessage, config: DispatchConfig) -> tuple[Binding | None, list[str]]:
    if outbound.kind is not OutboundKind.REPLY or outbound.thread_id is None:
        return None, [DispatchCode.NOT_OPERATOR_APPROVED]
    context = load_draft_context(uow, outbound)
    thread = uow.threads.get(outbound.thread_id)
    trigger = uow.messages.get(context.message_id) if context else None
    contact = uow.contacts.get(outbound.contact_id)
    if thread is None or trigger is None or contact is None:
        return None, [DispatchCode.RECIPIENT_MISMATCH]
    codes: list[str] = []
    if (
        trigger.thread_id != thread.thread_id
        or trigger.from_address != contact.email
        or contact.email not in thread.participant_addresses
    ):
        codes.append(DispatchCode.RECIPIENT_MISMATCH)
    if thread.mailbox not in config.sender_mailboxes or trigger.mailbox != thread.mailbox:
        codes.append(DispatchCode.SENDER_NOT_ALLOWED)
    company = uow.companies.get(contact.company_id) if contact.company_id else None
    binding = Binding(
        recipient=contact.email, sender_mailbox=thread.mailbox, contact=contact,
        company_domain=company.domain if company else None, thread=thread, trigger=trigger,
    )
    return binding, codes


def evaluate_policy(
    uow: UnitOfWork, outbound: OutboundMessage, binding: Binding, config: DispatchConfig, now: datetime
) -> PolicyDecisionResult:
    """Stage 3 outbound policy for a REPLY: suppression, bounced address, kill switch,
    sending window and quota. The message's own ledger entry (a FAILED earlier attempt
    being retried) is excluded: a retry moves the same message back into SENDING, so it
    is counted once, never twice."""
    tz = config.limits.timezone
    day_start, day_end = local_day_bounds_utc(local_date(now, tz), tz)
    entries = uow.outbound.list_ledger_entries(COUNTED_STATUSES, day_start, day_end)
    entries += uow.outbound.list_ledger_entries_for_contact(binding.contact.contact_id, COUNTED_STATUSES)
    entries = [e for e in entries if e.outbound_id != outbound.outbound_id]
    reservations = uow.quota_reservations.list_active_for_date(local_date(now, tz))
    reservations += uow.quota_reservations.list_active_for_contact(binding.contact.contact_id)
    quota = build_quota_snapshot(
        entries, reservations, now=now, timezone=tz, mailbox=binding.sender_mailbox, campaign_id=None,
        contact_id=binding.contact.contact_id,
    )
    return evaluate_outbound_policy(
        PolicyContext(
            now=now,
            kind=OutboundKind.REPLY,
            contact=binding.contact,
            company_domain=binding.company_domain,
            suppression_entries=tuple(_dnc_entries(uow, binding)),
            kill_switch=config.kill_switch,
            window=config.window,
            limits=config.limits,
            quota=quota,
        )
    )


def _dnc_entries(uow: UnitOfWork, binding: Binding) -> list[DoNotContactEntry]:
    email = binding.contact.email
    entries = list(uow.dnc.list_for_value(DNCScope.EMAIL, email))
    for domain in sorted({email.split("@", 1)[1], *([binding.company_domain] if binding.company_domain else [])}):
        entries.extend(uow.dnc.list_for_value(DNCScope.DOMAIN, domain))
    return entries
