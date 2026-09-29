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

from app.core.enums import ActorType, OutboundKind, RefKind
from app.core.models import Campaign, EmailMessage, EmailThread, OutboundMessage, ProspectContact
from app.dispatch.models import DispatchCode, DispatchConfig
from app.inbound.records import ref
from app.llm.claim_check import draft_hash
from app.operator.models import CommandKind, CommandOutcome
from app.operator.review import load_draft_context
from app.persistence import UnitOfWork
from app.policy import PolicyDecisionResult
from app.policy.reply import evaluate_send_policy

APPROVAL_EVENT = f"OPERATOR_{CommandKind.APPROVE_DRAFT.value}"


@dataclass(frozen=True)
class Binding:
    recipient: str
    sender_mailbox: str
    contact: ProspectContact
    company_domain: str | None
    thread: EmailThread
    # The customer message a REPLY answers; None for campaign touches.
    trigger: EmailMessage | None
    # The campaign of a campaign touch (FIRST_TOUCH, FOLLOW_UP); None for replies.
    campaign: Campaign | None = None


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
    if outbound.kind in (OutboundKind.FIRST_TOUCH, OutboundKind.FOLLOW_UP):
        return _bind_campaign_touch(uow, outbound, config)
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


def _bind_campaign_touch(uow: UnitOfWork, outbound: OutboundMessage, config: DispatchConfig) -> tuple[Binding | None, list[str]]:
    """A campaign touch goes to the lead's contact, in the membership's thread (registered at
    draft time), from the campaign's sending mailbox, which must be one of ours."""
    thread = uow.threads.get(outbound.thread_id) if outbound.thread_id else None
    contact = uow.contacts.get(outbound.contact_id)
    lead = uow.leads.get(outbound.lead_id)
    campaign = uow.campaigns.get(outbound.campaign_id) if outbound.campaign_id else None
    if thread is None or contact is None or lead is None or campaign is None:
        return None, [DispatchCode.RECIPIENT_MISMATCH]
    codes: list[str] = []
    if contact.email not in thread.participant_addresses or thread.lead_id != lead.lead_id or lead.contact_id != contact.contact_id:
        codes.append(DispatchCode.RECIPIENT_MISMATCH)
    if thread.mailbox != campaign.sending_mailbox or thread.mailbox not in config.sender_mailboxes:
        codes.append(DispatchCode.SENDER_NOT_ALLOWED)
    company = uow.companies.get(contact.company_id) if contact.company_id else None
    return Binding(recipient=contact.email, sender_mailbox=thread.mailbox, contact=contact,
                   company_domain=company.domain if company else None, thread=thread, trigger=None, campaign=campaign), codes


def evaluate_policy(
    uow: UnitOfWork, outbound: OutboundMessage, binding: Binding, config: DispatchConfig, now: datetime
) -> PolicyDecisionResult:
    """Stage 3 outbound policy for the message's own kind (shared via ``app.policy.reply``):
    replies without a campaign; campaign touches with their campaign's checks and quotas.
    The message's own ledger entry is excluded so a retry is not counted against itself."""
    return evaluate_send_policy(
        uow, kind=outbound.kind, campaign=binding.campaign, contact=binding.contact, company_domain=binding.company_domain,
        mailbox=binding.sender_mailbox, limits=config.limits, window=config.window, kill_switch=config.kill_switch, now=now,
        exclude_outbound_id=outbound.outbound_id,
    )
