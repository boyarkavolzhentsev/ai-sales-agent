"""Conversation state changes requested by an operator. Authorization, command
idempotency, version checks and auditing of the command itself are done by
``app.operator`` (Stage 7 command machinery); these functions only apply the change,
inside the command's transaction."""

from datetime import datetime

from app.core.enums import ConversationStatus, DNCReason, DNCScope, FollowUpJobStatus, RefKind
from app.core.models import Conversation, DoNotContactEntry, EntityRef
from app.conversation.cancellation import cancel_undispatched
from app.conversation.ids import stable_id
from app.conversation.state import save, stop_follow_ups
from app.persistence import UnitOfWork

S = ConversationStatus


def resting_status(conversation: Conversation) -> ConversationStatus:
    """Where an unpaused conversation without an open follow-up stands."""
    last_out, last_in = conversation.last_outbound_at, conversation.last_inbound_at
    if last_out is not None and (last_in is None or last_out >= last_in):
        return S.WAITING_FOR_REPLY
    return S.ACTIVE


def pause(uow: UnitOfWork, conversation: Conversation, *, correlation_id: str, now: datetime) -> Conversation:
    stop_follow_ups(uow, conversation, FollowUpJobStatus.CANCELLED, "OPERATOR_PAUSED", correlation_id=correlation_id, now=now)
    return save(uow, _fresh(uow, conversation), status=S.PAUSED, correlation_id=correlation_id, now=now)


def resume(uow: UnitOfWork, conversation: Conversation, *, correlation_id: str, now: datetime) -> Conversation:
    return save(uow, conversation, status=resting_status(conversation), correlation_id=correlation_id, now=now)


def follow_up_draft_rejected(uow: UnitOfWork, thread_id: str, *, correlation_id: str, now: datetime) -> Conversation | None:
    """An operator rejected a follow-up draft: the conversation leaves OPERATOR_REVIEW for
    its resting status. The rejected logical follow-up is never re-created (its job is
    COMPLETED); the next follow-up needs a new anchor or an explicit schedule decision."""
    conversation = uow.conversations.get_by_thread(thread_id)
    if conversation is None or conversation.status is not S.OPERATOR_REVIEW:
        return conversation
    return save(uow, conversation, status=resting_status(conversation), correlation_id=correlation_id, now=now)


def cancel_follow_up(uow: UnitOfWork, conversation: Conversation, *, correlation_id: str, now: datetime) -> tuple[Conversation, int]:
    """Returns the conversation and how many jobs and drafts were stopped."""
    jobs, drafts = stop_follow_ups(uow, conversation, FollowUpJobStatus.CANCELLED, "OPERATOR_CANCELLED",
                                   correlation_id=correlation_id, now=now)
    current = _fresh(uow, conversation)
    if current.status in (S.FOLLOW_UP_DUE, S.OPERATOR_REVIEW) and (jobs or drafts):
        current = save(uow, current, status=resting_status(current), correlation_id=correlation_id, now=now)
    return current, len(jobs) + len(drafts)


def close(uow: UnitOfWork, conversation: Conversation, *, correlation_id: str, now: datetime) -> Conversation:
    stop_follow_ups(uow, conversation, FollowUpJobStatus.CANCELLED, "OPERATOR_CLOSED", correlation_id=correlation_id, now=now)
    return save(uow, _fresh(uow, conversation), status=S.CLOSED, correlation_id=correlation_id, now=now)


def mark_do_not_contact(
    uow: UnitOfWork, conversation: Conversation, *, operator_id: str, command_id: str, correlation_id: str, now: datetime
) -> tuple[str | None, list[str]]:
    """Suppress the contact of this conversation (see ``suppress_contact``)."""
    return suppress_contact(uow, conversation.contact_id, operator_id=operator_id, command_id=command_id,
                            correlation_id=correlation_id, now=now)


def suppress_contact(
    uow: UnitOfWork, contact_id: str, *, operator_id: str, command_id: str, correlation_id: str, now: datetime
) -> tuple[str | None, list[str]]:
    """Suppress the contact (never reversed here), end every conversation of the contact,
    and cancel every undispatched message of the contact's leads (releasing quota).
    Returns (new DNC entry id or None if one was already active, cancelled outbound ids)."""
    contact = uow.contacts.get(contact_id)
    entry_id: str | None = None
    if contact is not None and not uow.dnc.list_active(DNCScope.EMAIL, contact.email, now):
        entry = DoNotContactEntry(
            entry_id=stable_id("dnc", "operator", command_id), scope=DNCScope.EMAIL, value=contact.email,
            reason=DNCReason.OPERATOR, source_ref=EntityRef(kind=RefKind.OPERATOR_COMMAND, id=command_id),
            created_by=operator_id, created_at=now,
        )
        uow.dnc.add(entry)
        entry_id = entry.entry_id
    cancelled_ids: list[str] = []
    for other in uow.conversations.list_by_contact(contact_id):
        stop_follow_ups(uow, other, FollowUpJobStatus.CANCELLED, "DO_NOT_CONTACT", correlation_id=correlation_id, now=now)
        current = _fresh(uow, other)
        if current.status is not S.DO_NOT_CONTACT:
            save(uow, current, status=S.DO_NOT_CONTACT, correlation_id=correlation_id, now=now)
    for lead in uow.leads.list_by_contact(contact_id):
        cancelled, _ = cancel_undispatched(uow, uow.outbound.list_by_lead(lead.lead_id), now)
        cancelled_ids += [m.outbound_id for m in cancelled]
    return entry_id, cancelled_ids


def _fresh(uow: UnitOfWork, conversation: Conversation) -> Conversation:
    return uow.conversations.get(conversation.conversation_id) or conversation
