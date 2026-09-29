"""Conversation lifecycle. Every function runs inside the caller's transaction, so state
changes are atomic with the inbound, dispatch or operator step that caused them.

Transitions (terminal: CONVERTED, CLOSED, DO_NOT_CONTACT; PAUSED only changes by operator):
- customer message (certain attribution) -> ACTIVE; (uncertain) -> OPERATOR_REVIEW. Open
  follow-up jobs and pending follow-up drafts of every conversation of that contact are
  superseded/cancelled first, so a stale follow-up can never escape.
- inbound outcome: suppression -> DO_NOT_CONTACT (all the contact's conversations); lead
  closed -> CLOSED, or CONVERTED when won (all the lead's conversations); escalation ->
  OPERATOR_REVIEW.
- our message accepted by the provider -> WAITING_FOR_REPLY (ACTIVE if the customer wrote
  after it was approved).
- follow-up scheduled -> FOLLOW_UP_DUE; follow-up draft created -> OPERATOR_REVIEW.
"""

from datetime import datetime

from app.core.enums import CloseReason, ConversationStatus, FollowUpJobStatus, LeadStage, RefKind
from app.core.models import (
    TERMINAL_CONVERSATION_STATUSES,
    Conversation,
    EntityRef,
    FollowUpJob,
    Lead,
    OutboundMessage,
)
from app.conversation.audit import append_event, ref
from app.conversation.cancellation import cancel_undispatched
from app.conversation.ids import FOLLOW_UP_KEY_PREFIX, conversation_id_for
from app.persistence import UnitOfWork

S = ConversationStatus
FROZEN = frozenset({*TERMINAL_CONVERSATION_STATUSES, S.PAUSED})


def is_follow_up(message: OutboundMessage) -> bool:
    return message.idempotency_key.startswith(FOLLOW_UP_KEY_PREFIX)


def create(
    uow: UnitOfWork, *, thread_id: str, lead_id: str, contact_id: str, status: ConversationStatus,
    certain: bool, correlation_id: str, now: datetime, **fields: object,
) -> Conversation:
    conversation = Conversation.model_validate(
        {
            "conversation_id": conversation_id_for(thread_id), "thread_id": thread_id, "lead_id": lead_id,
            "contact_id": contact_id, "status": status, "association_certain": certain, "last_activity_at": now,
            "created_at": now, "updated_at": now,
        }
        | fields
    )
    uow.conversations.add(conversation)
    append_event(uow, key=(conversation.conversation_id,), event_type="CONVERSATION_CREATED", subjects=_subjects(conversation),
                 after={"status": status.value, "association_certain": certain}, correlation_id=correlation_id, now=now)
    return conversation


def save(uow: UnitOfWork, conversation: Conversation, *, correlation_id: str, now: datetime, **changes: object) -> Conversation:
    """Versioned update. Leaving FOLLOW_UP_DUE always clears next_follow_up_at."""
    status = changes.get("status", conversation.status)
    if status is not S.FOLLOW_UP_DUE:
        changes["next_follow_up_at"] = None
    updated = Conversation.model_validate(
        conversation.model_dump() | changes | {"updated_at": max(now, conversation.updated_at), "version": conversation.version + 1}
    )
    uow.conversations.update(updated, conversation.version)
    if updated.status is not conversation.status:
        append_event(uow, key=(conversation.conversation_id, str(updated.version)), event_type="CONVERSATION_STATUS_CHANGED",
                     subjects=_subjects(updated), after={"from": conversation.status.value, "to": updated.status.value,
                                                         "version": updated.version},
                     correlation_id=correlation_id, now=now)
    return updated


def stop_follow_ups(
    uow: UnitOfWork, conversation: Conversation, status: FollowUpJobStatus, reason: str, *, correlation_id: str, now: datetime
) -> tuple[list[FollowUpJob], list[OutboundMessage]]:
    """End the open follow-up job (CANCELLED or SUPERSEDED) and cancel follow-up drafts of
    this conversation that were not dispatched yet (releasing any reservation)."""
    stopped: list[FollowUpJob] = []
    job = uow.follow_up_jobs.get_open_for_conversation(conversation.conversation_id)
    if job is not None:
        ended = FollowUpJob.model_validate(
            job.model_dump()
            | {"status": status, "reason": reason, "claim_token": None, "claimed_by": None, "lease_expires_at": None,
               "updated_at": max(now, job.updated_at), "version": job.version + 1}
        )
        uow.follow_up_jobs.update(ended, job.version)
        append_event(uow, key=(job.follow_up_id, str(ended.version)), event_type=f"FOLLOW_UP_{status.value}",
                     subjects=(ref(RefKind.FOLLOW_UP_JOB, job.follow_up_id), ref(RefKind.CONVERSATION, conversation.conversation_id)),
                     after={"reason": reason, "previous_status": job.status.value}, correlation_id=correlation_id, now=now)
        stopped.append(ended)
    drafts = [
        m for m in uow.outbound.list_by_lead(conversation.lead_id)
        if m.thread_id == conversation.thread_id and is_follow_up(m)
    ]
    cancelled, released = cancel_undispatched(uow, drafts, now)
    if cancelled:
        append_event(uow, key=(conversation.conversation_id, *(m.outbound_id for m in cancelled)),
                     event_type="FOLLOW_UP_DRAFTS_CANCELLED",
                     subjects=(ref(RefKind.CONVERSATION, conversation.conversation_id),
                               *(ref(RefKind.OUTBOUND_MESSAGE, m.outbound_id) for m in cancelled)),
                     after={"reason": reason, "outbound_ids": [m.outbound_id for m in cancelled], "released_reservation_ids": released},
                     correlation_id=correlation_id, now=now)
    return stopped, cancelled


def record_inbound_activity(
    uow: UnitOfWork, *, thread_id: str, lead_id: str, contact_id: str, message_id: str, received_at: datetime,
    certain: bool, correlation_id: str, now: datetime,
) -> Conversation:
    """A human message from the contact. Runs in the Stage 6 observation transaction."""
    for other in uow.conversations.list_by_contact(contact_id):
        stop_follow_ups(uow, other, FollowUpJobStatus.SUPERSEDED, "INBOUND_ACTIVITY", correlation_id=correlation_id, now=now)
        if other.thread_id != thread_id and other.status is S.FOLLOW_UP_DUE:
            save(uow, other, status=S.WAITING_FOR_REPLY, correlation_id=correlation_id, now=now)

    conversation = uow.conversations.get_by_thread(thread_id)
    if conversation is None:
        return create(
            uow, thread_id=thread_id, lead_id=lead_id, contact_id=contact_id,
            status=S.ACTIVE if certain else S.OPERATOR_REVIEW, certain=certain, correlation_id=correlation_id, now=now,
            last_inbound_message_id=message_id, last_inbound_at=received_at, last_activity_at=received_at,
        )
    certain = certain and conversation.association_certain
    newer = conversation.last_inbound_at is None or received_at >= conversation.last_inbound_at
    changes: dict[str, object] = {
        "association_certain": certain,
        "last_activity_at": max(conversation.last_activity_at, received_at),
    }
    if newer:  # an out-of-order older message still counts as activity, not as the latest
        changes |= {"last_inbound_message_id": message_id, "last_inbound_at": received_at}
    if conversation.status not in FROZEN:
        changes["status"] = S.ACTIVE if certain else S.OPERATOR_REVIEW
    return save(uow, conversation, correlation_id=correlation_id, now=now, **changes)


def record_inbound_outcome(
    uow: UnitOfWork, *, thread_id: str, contact_id: str | None, lead: Lead | None, dnc_added: bool, escalated: bool,
    correlation_id: str, now: datetime,
) -> None:
    """The Stage 6 finalization outcome, in the finalization transaction."""
    if dnc_added and contact_id is not None:
        for conversation in uow.conversations.list_by_contact(contact_id):
            _terminate(uow, conversation, S.DO_NOT_CONTACT, "DO_NOT_CONTACT", correlation_id=correlation_id, now=now)
    if lead is not None and lead.stage is LeadStage.CLOSED:
        target = S.CONVERTED if lead.close_reason is CloseReason.WON else S.CLOSED
        for conversation in uow.conversations.list_by_lead(lead.lead_id):
            _terminate(uow, conversation, target, "LEAD_CLOSED", correlation_id=correlation_id, now=now)
    if escalated:
        conversation = uow.conversations.get_by_thread(thread_id)
        if conversation is not None and conversation.status not in FROZEN and conversation.status is not S.OPERATOR_REVIEW:
            save(uow, conversation, status=S.OPERATOR_REVIEW, correlation_id=correlation_id, now=now)


def record_outbound_accepted(uow: UnitOfWork, outbound: OutboundMessage, *, correlation_id: str, now: datetime) -> Conversation | None:
    """The provider accepted one of our messages (Stage 8), in the result transaction."""
    if outbound.thread_id is None:
        return None
    conversation = uow.conversations.get_by_thread(outbound.thread_id)
    if conversation is None:
        conversation = create(uow, thread_id=outbound.thread_id, lead_id=outbound.lead_id, contact_id=outbound.contact_id,
                              status=S.ACTIVE, certain=True, correlation_id=correlation_id, now=now)
    # A new anchor: any follow-up planned against the previous message is stale.
    stop_follow_ups(uow, conversation, FollowUpJobStatus.SUPERSEDED, "NEW_OUTBOUND", correlation_id=correlation_id, now=now)
    conversation = uow.conversations.get(conversation.conversation_id) or conversation
    changes: dict[str, object] = {
        "last_outbound_id": outbound.outbound_id,
        "last_outbound_at": now,
        "last_activity_at": max(conversation.last_activity_at, now),
        "follow_up_count": conversation.follow_up_count + (1 if is_follow_up(outbound) else 0),
    }
    if conversation.status not in FROZEN and conversation.association_certain:
        changes["status"] = S.ACTIVE if _customer_wrote_since_draft(uow, conversation, outbound) else S.WAITING_FOR_REPLY
    return save(uow, conversation, correlation_id=correlation_id, now=now, **changes)


def _customer_wrote_since_draft(uow: UnitOfWork, conversation: Conversation, outbound: OutboundMessage) -> bool:
    """Whether the customer's latest message is newer than the one this draft answered.

    Compares message identities (the draft's trigger, recorded in its DRAFT_CREATED
    event), not timestamps, which can tie. Unknown trigger: assume the customer wrote
    (the conservative choice: no follow-up until we respond again)."""
    if conversation.last_inbound_message_id is None:
        return False
    trigger = None
    for event in uow.audit.list_for_subject(ref(RefKind.OUTBOUND_MESSAGE, outbound.outbound_id)):
        if event.event_type == "DRAFT_CREATED":
            trigger = next((r.id for r in event.subject_refs if r.kind is RefKind.EMAIL_MESSAGE), None)
            break
    return trigger != conversation.last_inbound_message_id


def _terminate(uow: UnitOfWork, conversation: Conversation, status: ConversationStatus, reason: str, *, correlation_id: str, now: datetime) -> None:
    stop_follow_ups(uow, conversation, FollowUpJobStatus.CANCELLED, reason, correlation_id=correlation_id, now=now)
    current = uow.conversations.get(conversation.conversation_id) or conversation
    # DO_NOT_CONTACT is never downgraded; other terminal states are kept once reached.
    if current.status is S.DO_NOT_CONTACT or (current.status in TERMINAL_CONVERSATION_STATUSES and status is not S.DO_NOT_CONTACT):
        return
    save(uow, current, status=status, correlation_id=correlation_id, now=now)


def _subjects(conversation: Conversation) -> tuple[EntityRef, ...]:
    return (
        ref(RefKind.CONVERSATION, conversation.conversation_id),
        ref(RefKind.EMAIL_THREAD, conversation.thread_id),
        ref(RefKind.LEAD, conversation.lead_id),
    )
