"""Operator application layer: review reads and review commands. Future Telegram handlers
call this; nothing here knows about Telegram, email providers or sending.

Every call first authorizes the credential (``app.operator.auth``), before any database
access. Reads use current authoritative state; history (audit facts) only supplies what a
draft or escalation was based on.

Commands (one IMMEDIATE transaction each):
1. reserve ``operator:command:<command_id>``; if it exists, return the recorded outcome
   when kind, payload and operator match (replay), else CommandCollisionError;
2. check the command's expected versions / identity / content hash (StaleCommandError);
3. re-validate current state (CommandRejectedError with stable codes);
4. apply the transition, then append one audit event that is also the command's
   completion record. Any failure rolls all of it back, including the reservation, so a
   rejected command writes nothing and may be retried after the operator re-reads.

Approval records a human decision only: the outbound message moves to OPERATOR_APPROVED.
It never creates a send permit, reserves quota, or marks a message SENDING/SENT, and it
overrides nothing: DNC, lead closure, holds, campaign state and evidence validity are
all re-checked at the injected ``now``. OPERATOR_APPROVED is not a sendability
guarantee: a future send gate must re-run every sending check before dispatch.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import JsonValue

from app.core.enums import (
    ActorType,
    CampaignMemberStatus,
    CampaignStatus,
    ConversationStatus,
    is_review_mode_supported_v1,
    EscalationResolution,
    EscalationStatus,
    FollowUpCancelReason,
    FollowUpStatus,
    LeadStage,
    LeadStatus,
    OutboundDecision,
    OutboundKind,
    OutboundStatus,
    RefKind,
)
from app.core.models import (
    TERMINAL_CONVERSATION_STATUSES,
    TERMINAL_MEMBER_STATUSES,
    Actor,
    Campaign,
    CampaignMember,
    Conversation,
    AuditEvent,
    EmailMessage,
    EntityRef,
    Escalation,
    FollowUpPlan,
    IntentClassification,
    KnowledgeAssessment,
    Lead,
    OutboundMessage,
)
from app.core.models.types import JsonObject
from app.campaign import actions as campaign_actions
from app.campaign import state as campaign_state
from app.campaign.models import CampaignStats, MemberView
from app.campaign.queries import campaign_stats, member_view
from app.conversation import FOLLOW_UP_KEY_PREFIX
from app.conversation import actions as conversation_actions
from app.conversation.models import ConversationView
from app.conversation.queries import conversation_view
from app.inbound import stable_id
from app.inbound.records import Events, ref
from app.operator.auth import OperatorAuthenticator, authorize
from app.operator.errors import (
    CommandCollisionError,
    CommandRejectedError,
    OperatorError,
    OperatorNotFoundError,
    StaleCommandError,
)
from app.operator.models import (
    ActivateCampaign,
    ApproveDraft,
    CampaignCommand,
    CancelCampaign,
    CancelCampaignMember,
    CompleteCampaign,
    MemberCommand,
    PauseCampaign,
    ResumeCampaign,
    SuppressCampaignMember,
    CancelFollowUp,
    CloseConversation,
    ConversationCommand,
    MarkDoNotContact,
    PauseConversation,
    ResumeConversation,
    BlockCode,
    CommandKind,
    CommandOutcome,
    CommandResult,
    DraftDetail,
    DraftSummary,
    EmailText,
    EscalationDetail,
    EscalationSummary,
    GeneratedDraftText,
    LeadView,
    OperatorCommand,
    OperatorConfig,
    RejectDraft,
    ResolveEscalation,
    TakeOwnership,
    ThreadView,
    VersionChange,
)
from app.operator.review import (
    REVIEWABLE_KINDS,
    REVIEWABLE_STATUSES,
    approval_blockers,
    evidence_views,
    generated_classification,
    load_draft_context,
    suppression_scopes,
)
from app.persistence import Clock, Database, DuplicateIdempotencyKeyError, UnitOfWork
from app.persistence.serialization import dumps_json

OPEN_ESCALATION_STATUSES = (EscalationStatus.OPEN, EscalationStatus.ACKNOWLEDGED)
# Draft outcomes are recorded by the draft commands themselves; an escalation resolution
# claiming them would imply an approval or rejection that never happened.
DRAFT_DISPOSITIONS = frozenset({EscalationResolution.DRAFT_APPROVED, EscalationResolution.DRAFT_REJECTED})
NOTE_KIND = "OPERATOR_REVIEW_CONTEXT"  # review context only; never knowledge-base evidence


@dataclass
class _Applied:
    """What a command handler changed, for the outcome and the audit event."""

    subjects: list[EntityRef] = field(default_factory=list)
    versions: list[VersionChange] = field(default_factory=list)
    before: dict[str, JsonValue] = field(default_factory=dict)
    disposition: str = ""
    reason_codes: tuple[str, ...] = ()
    note: str | None = None

    def changed(self, entity: EntityRef, expected: int, key: str, state: JsonValue, resulting: int | None = None) -> None:
        self.subjects.append(entity)
        self.versions.append(VersionChange(entity=entity, expected=expected, resulting=resulting or expected + 1))
        self.before[key] = state


def command_event_id(command_id: str) -> str:
    return stable_id("ae", "operator-command", command_id)


class OperatorService:
    def __init__(
        self, db: Database, clock: Clock, config: OperatorConfig, authenticator: OperatorAuthenticator
    ) -> None:
        self._db = db
        self._clock = clock
        self._config = config
        self._authenticator = authenticator

    # ---- Reads ----------------------------------------------------------------------------

    def list_pending_drafts(self, credential: object) -> tuple[DraftSummary, ...]:
        """Reply drafts awaiting a human decision, by current status (never by history)."""
        authorize(self._authenticator, self._config, credential)
        with self._db.transaction() as uow:
            messages = [m for status in sorted(REVIEWABLE_STATUSES) for m in uow.outbound.list_by_status(status)]
        pending = sorted((m for m in messages if m.kind in REVIEWABLE_KINDS), key=lambda m: (m.created_at, m.outbound_id))
        return tuple(_draft_summary(m) for m in pending[: self._config.max_list_items])

    def get_draft(self, credential: object, outbound_id: str) -> DraftDetail:
        authorize(self._authenticator, self._config, credential)
        now = self._clock.now()
        with self._db.transaction() as uow:
            outbound = uow.outbound.get(outbound_id)
            if outbound is None or outbound.kind not in REVIEWABLE_KINDS:
                raise OperatorNotFoundError(f"draft {outbound_id} not found")
            context = load_draft_context(uow, outbound)
            trigger = uow.messages.get(context.message_id) if context else None
            blockers = approval_blockers(uow, outbound, self._config, now)
            return DraftDetail(
                outbound_id=outbound.outbound_id,
                draft_id=outbound.draft_id,
                kind=outbound.kind,
                status=outbound.status,
                version=outbound.version,
                content_hash=outbound.content_hash,
                created_at=outbound.created_at,
                approved_at=outbound.approved_at,
                lead=self._lead_view(uow, outbound.lead_id, now),
                customer=self._text(trigger) if trigger else None,
                generated_classification=generated_classification(context.classification if context else None),
                generated_draft=GeneratedDraftText(subject=outbound.subject, body=outbound.body_final),
                knowledge_assessment=context.assessment if context else None,
                claim_check=context.claim_check if context else None,
                evidence=evidence_views(uow, context, now) if context else (),
                actionable=not blockers,
                blockers=blockers,
            )

    def list_open_escalations(self, credential: object) -> tuple[EscalationSummary, ...]:
        authorize(self._authenticator, self._config, credential)
        with self._db.transaction() as uow:
            found = [e for status in OPEN_ESCALATION_STATUSES for e in uow.escalations.list_by_status(status)]
        found.sort(key=lambda e: (e.created_at, e.escalation_id))
        return tuple(
            EscalationSummary(
                escalation_id=e.escalation_id, lead_id=e.lead_id, status=e.status, severity=e.severity,
                reasons=e.reasons, version=e.version, created_at=e.created_at,
            )
            for e in found[: self._config.max_list_items]
        )

    def get_escalation(self, credential: object, escalation_id: str) -> EscalationDetail:
        authorize(self._authenticator, self._config, credential)
        now = self._clock.now()
        with self._db.transaction() as uow:
            escalation = uow.escalations.get(escalation_id)
            if escalation is None:
                raise OperatorNotFoundError(f"escalation {escalation_id} not found")
            created = next(
                (e for e in uow.audit.list_for_subject(ref(RefKind.ESCALATION, escalation_id)) if e.event_type == Events.ESCALATION_CREATED),
                None,
            )
            context: JsonObject = created.after if created is not None and created.after is not None else {}
            trigger = uow.messages.get(escalation.trigger_ref.id) if escalation.trigger_ref.kind is RefKind.EMAIL_MESSAGE else None
            classification = context.get("classification")
            assessment = context.get("knowledge_assessment")
            return EscalationDetail(
                escalation_id=escalation.escalation_id,
                lead_id=escalation.lead_id,
                status=escalation.status,
                severity=escalation.severity,
                reasons=escalation.reasons,
                resolution=escalation.resolution,
                resolved_by=escalation.resolved_by,
                resolved_at=escalation.resolved_at,
                version=escalation.version,
                created_at=escalation.created_at,
                system_summary=escalation.summary,
                detail=_optional_str(context.get("detail")),
                lead=self._lead_view(uow, escalation.lead_id, now),
                customer=self._text(trigger) if trigger else None,
                generated_classification=generated_classification(
                    IntentClassification.model_validate(classification) if isinstance(classification, dict) else None
                ),
                omitted_questions=tuple(str(q) for q in _list(context.get("omitted_questions"))),
                knowledge_assessment=KnowledgeAssessment.model_validate(assessment) if isinstance(assessment, dict) else None,
                evidence_ids=tuple(str(i) for i in _list(context.get("evidence_ids"))),
            )

    def get_thread(self, credential: object, thread_id: str, limit: int | None = None) -> ThreadView:
        authorize(self._authenticator, self._config, credential)
        bound = min(limit or self._config.max_thread_messages, self._config.max_thread_messages)
        with self._db.transaction() as uow:
            thread = uow.threads.get(thread_id)
            if thread is None:
                raise OperatorNotFoundError(f"thread {thread_id} not found")
            recent = [uow.messages.get(message_id) for message_id in thread.message_ids[-bound:]]
        return ThreadView(
            thread_id=thread.thread_id,
            lead_id=thread.lead_id,
            total_messages=len(thread.message_ids),
            messages=tuple(self._text(m) for m in recent if m is not None),
        )

    def get_conversation(self, credential: object, conversation_id: str) -> ConversationView:
        """Conversation status next to the lead's (separate) pipeline stage, with its follow-ups."""
        authorize(self._authenticator, self._config, credential)
        with self._db.transaction() as uow:
            view = conversation_view(uow, conversation_id)
        if view is None:
            raise OperatorNotFoundError(f"conversation {conversation_id} not found")
        return view

    def get_lead(self, credential: object, lead_id: str) -> LeadView:
        authorize(self._authenticator, self._config, credential)
        with self._db.transaction() as uow:
            view = self._lead_view(uow, lead_id, self._clock.now())
        if view is None:
            raise OperatorNotFoundError(f"lead {lead_id} not found")
        return view

    # ---- Commands -------------------------------------------------------------------------

    def approve_draft(self, credential: object, command: ApproveDraft) -> CommandResult:
        return self._execute(credential, command, self._approve)

    def reject_draft(self, credential: object, command: RejectDraft) -> CommandResult:
        return self._execute(credential, command, self._reject)

    def take_ownership(self, credential: object, command: TakeOwnership) -> CommandResult:
        return self._execute(credential, command, self._take_ownership)

    def resolve_escalation(self, credential: object, command: ResolveEscalation) -> CommandResult:
        return self._execute(credential, command, self._resolve)

    def pause_conversation(self, credential: object, command: PauseConversation) -> CommandResult:
        return self._execute(credential, command, self._pause_conversation)

    def resume_conversation(self, credential: object, command: ResumeConversation) -> CommandResult:
        return self._execute(credential, command, self._resume_conversation)

    def cancel_follow_up(self, credential: object, command: CancelFollowUp) -> CommandResult:
        return self._execute(credential, command, self._cancel_follow_up)

    def close_conversation(self, credential: object, command: CloseConversation) -> CommandResult:
        return self._execute(credential, command, self._close_conversation)

    def mark_do_not_contact(self, credential: object, command: MarkDoNotContact) -> CommandResult:
        return self._execute(credential, command, self._mark_do_not_contact)

    def activate_campaign(self, credential: object, command: ActivateCampaign) -> CommandResult:
        return self._execute(credential, command, self._activate_campaign)

    def pause_campaign(self, credential: object, command: PauseCampaign) -> CommandResult:
        return self._execute(credential, command, self._pause_campaign)

    def resume_campaign(self, credential: object, command: ResumeCampaign) -> CommandResult:
        return self._execute(credential, command, self._resume_campaign)

    def cancel_campaign(self, credential: object, command: CancelCampaign) -> CommandResult:
        return self._execute(credential, command, self._cancel_campaign)

    def complete_campaign(self, credential: object, command: CompleteCampaign) -> CommandResult:
        return self._execute(credential, command, self._complete_campaign)

    def cancel_campaign_member(self, credential: object, command: CancelCampaignMember) -> CommandResult:
        return self._execute(credential, command, self._cancel_campaign_member)

    def suppress_campaign_member(self, credential: object, command: SuppressCampaignMember) -> CommandResult:
        return self._execute(credential, command, self._suppress_campaign_member)

    # ---- Campaign reads (Stage 10) -----------------------------------------------------------

    def get_campaign_stats(self, credential: object, campaign_id: str) -> CampaignStats:
        authorize(self._authenticator, self._config, credential)
        with self._db.transaction() as uow:
            stats = campaign_stats(uow, campaign_id)
        if stats is None:
            raise OperatorNotFoundError(f"campaign {campaign_id} not found")
        return stats

    def list_campaign_members(self, credential: object, campaign_id: str) -> tuple[MemberView, ...]:
        authorize(self._authenticator, self._config, credential)
        with self._db.transaction() as uow:
            return tuple(member_view(uow, m) for m in uow.campaign_members.list_by_campaign(campaign_id)[: self._config.max_list_items])

    def get_campaign_member(self, credential: object, member_id: str) -> MemberView:
        authorize(self._authenticator, self._config, credential)
        with self._db.transaction() as uow:
            member = uow.campaign_members.get(member_id)
            if member is None:
                raise OperatorNotFoundError(f"campaign member {member_id} not found")
            return member_view(uow, member)

    def _execute[C: OperatorCommand](
        self,
        credential: object,
        command: C,
        handler: Callable[[UnitOfWork, C, datetime, str], _Applied],
    ) -> CommandResult:
        operator_id = authorize(self._authenticator, self._config, credential)
        now = self._clock.now()
        payload_hash = command.payload_hash()
        with self._db.transaction() as uow:
            try:
                uow.idempotency.reserve(f"operator:command:{command.command_id}", f"operator.{command.kind.lower()}", now)
            except DuplicateIdempotencyKeyError:
                recorded = _recorded_outcome(uow, command.command_id)
                if recorded is None:
                    raise OperatorError(f"command {command.command_id} reserved without an outcome") from None
                if (recorded.kind, recorded.payload_hash, recorded.operator_id) != (command.kind, payload_hash, operator_id):
                    raise CommandCollisionError(
                        f"command {command.command_id} was already used for a different command or operator"
                    ) from None
                return CommandResult(outcome=recorded, replayed=True)

            applied = handler(uow, command, now, operator_id)
            outcome = CommandOutcome(
                command_id=command.command_id,
                kind=command.kind,
                operator_id=operator_id,
                correlation_id=command.correlation_id,
                payload_hash=payload_hash,
                completed_at=now,
                subjects=tuple(dict.fromkeys(applied.subjects)),
                versions=tuple(applied.versions),
                disposition=applied.disposition,
                reason_codes=applied.reason_codes,
            )
            uow.audit.append(_command_event(outcome, applied))
        return CommandResult(outcome=outcome)

    def _approve(self, uow: UnitOfWork, command: ApproveDraft, now: datetime, operator_id: str) -> _Applied:
        outbound = self._reply(uow, command.outbound_id)
        stale: list[BlockCode] = []
        if outbound.draft_id != command.draft_id:
            stale.append(BlockCode.DRAFT_IDENTITY_MISMATCH)
        if outbound.version != command.expected_outbound_version:
            stale.append(BlockCode.DRAFT_VERSION_CHANGED)
        if outbound.content_hash != command.content_hash:
            stale.append(BlockCode.DRAFT_CONTENT_CHANGED)
        lead = uow.leads.get(outbound.lead_id)
        if lead is not None and lead.version != command.expected_lead_version:
            stale.append(BlockCode.LEAD_VERSION_CHANGED)
        blockers = approval_blockers(uow, outbound, self._config, now)
        if stale:
            raise StaleCommandError((*stale, *blockers))
        if blockers:
            raise CommandRejectedError(blockers)

        approved = OutboundMessage.model_validate(
            outbound.model_dump()
            | {
                "status": OutboundStatus.OPERATOR_APPROVED,
                "decision": OutboundDecision.SEND,
                "decision_reasons": ("OPERATOR_APPROVED",),
                "approved_at": now,
                "version": outbound.version + 1,
            }
        )
        uow.outbound.update(approved, outbound.version)
        campaign_state.record_draft_approved(uow, approved, correlation_id=command.correlation_id, now=now)
        applied = _Applied(disposition=OutboundStatus.OPERATOR_APPROVED.value, reason_codes=("OPERATOR_APPROVED",))
        applied.changed(ref(RefKind.OUTBOUND_MESSAGE, outbound.outbound_id), outbound.version, "outbound", _outbound_state(outbound))
        applied.subjects += [ref(RefKind.MESSAGE_DRAFT, outbound.draft_id), ref(RefKind.LEAD, outbound.lead_id)]
        applied.before["lead_version"] = command.expected_lead_version
        applied.before["content_hash"] = outbound.content_hash
        return applied

    def _reject(self, uow: UnitOfWork, command: RejectDraft, now: datetime, operator_id: str) -> _Applied:
        outbound = self._reply(uow, command.outbound_id)
        stale: list[BlockCode] = []
        if outbound.draft_id != command.draft_id:
            stale.append(BlockCode.DRAFT_IDENTITY_MISMATCH)
        if outbound.version != command.expected_outbound_version:
            stale.append(BlockCode.DRAFT_VERSION_CHANGED)
        not_reviewable = outbound.status not in REVIEWABLE_STATUSES
        if stale:
            raise StaleCommandError((*stale, *((BlockCode.DRAFT_NOT_REVIEWABLE,) if not_reviewable else ())))
        if not_reviewable:
            raise CommandRejectedError((BlockCode.DRAFT_NOT_REVIEWABLE,))

        rejected = OutboundMessage.model_validate(
            outbound.model_dump()
            | {
                "status": OutboundStatus.CANCELLED,
                "decision_reasons": ("OPERATOR_REJECTED", command.reason.value),
                "version": outbound.version + 1,
            }
        )
        uow.outbound.update(rejected, outbound.version)
        campaign_state.record_draft_rejected(uow, outbound, correlation_id=command.correlation_id, now=now)
        if outbound.idempotency_key.startswith(FOLLOW_UP_KEY_PREFIX) and outbound.thread_id is not None:
            conversation_actions.follow_up_draft_rejected(uow, outbound.thread_id, correlation_id=command.correlation_id, now=now)
        applied = _Applied(disposition="REJECTED", reason_codes=(command.reason.value,), note=command.note)
        applied.changed(ref(RefKind.OUTBOUND_MESSAGE, outbound.outbound_id), outbound.version, "outbound", _outbound_state(outbound))
        applied.subjects += [ref(RefKind.MESSAGE_DRAFT, outbound.draft_id), ref(RefKind.LEAD, outbound.lead_id)]
        return applied

    def _take_ownership(self, uow: UnitOfWork, command: TakeOwnership, now: datetime, operator_id: str) -> _Applied:
        lead = uow.leads.get(command.lead_id)
        if lead is None:
            raise OperatorNotFoundError(f"lead {command.lead_id} not found")
        if lead.version != command.expected_lead_version:
            raise StaleCommandError((BlockCode.LEAD_VERSION_CHANGED,))
        if lead.stage is LeadStage.CLOSED:
            raise CommandRejectedError((BlockCode.LEAD_CLOSED,))
        if lead.status is LeadStatus.OPERATOR_OWNED:
            raise CommandRejectedError((BlockCode.LEAD_ALREADY_OWNED,))

        owned = Lead.model_validate(
            lead.model_dump() | {"status": LeadStatus.OPERATOR_OWNED, "updated_at": max(now, lead.updated_at), "version": lead.version + 1}
        )
        uow.leads.update(owned, lead.version)
        campaign_state.record_lead_owned(uow, lead.lead_id, correlation_id=command.correlation_id, now=now)
        applied = _Applied(disposition=LeadStatus.OPERATOR_OWNED.value)
        applied.changed(ref(RefKind.LEAD, lead.lead_id), lead.version, "lead", {"stage": lead.stage.value, "status": lead.status.value})

        # Automated follow-ups stop when a human takes over (Stage 0 cancel reason).
        plan = uow.follow_ups.get_open_for_lead(lead.lead_id)
        if plan is not None:
            cancelled = FollowUpPlan.model_validate(
                plan.model_dump()
                | {
                    "status": FollowUpStatus.CANCELLED,
                    "cancel_reason": FollowUpCancelReason.OPERATOR_TOOK_OVER,
                    "next_due_at": None,
                    "updated_at": max(now, plan.updated_at),
                    "version": plan.version + 1,
                }
            )
            uow.follow_ups.update(cancelled, plan.version)
            applied.changed(ref(RefKind.FOLLOW_UP_PLAN, plan.plan_id), plan.version, "follow_up_plan", {"status": plan.status.value})
        return applied

    def _resolve(self, uow: UnitOfWork, command: ResolveEscalation, now: datetime, operator_id: str) -> _Applied:
        escalation = uow.escalations.get(command.escalation_id)
        if escalation is None:
            raise OperatorNotFoundError(f"escalation {command.escalation_id} not found")
        if escalation.version != command.expected_escalation_version:
            raise StaleCommandError((BlockCode.ESCALATION_VERSION_CHANGED,))
        if escalation.status not in OPEN_ESCALATION_STATUSES:
            raise CommandRejectedError((BlockCode.ESCALATION_NOT_OPEN,))
        if command.disposition in DRAFT_DISPOSITIONS:
            raise CommandRejectedError((BlockCode.DISPOSITION_REQUIRES_DRAFT_COMMAND,))
        if command.disposition is EscalationResolution.TAKEN_OVER:
            lead = uow.leads.get(escalation.lead_id)
            if lead is None or lead.status is not LeadStatus.OPERATOR_OWNED:
                raise CommandRejectedError((BlockCode.LEAD_NOT_OWNED,))

        # Only the escalation changes: no draft approval, no lead reopening or return to
        # automation, no DNC removal.
        resolved = Escalation.model_validate(
            escalation.model_dump()
            | {
                "status": EscalationStatus.RESOLVED,
                "resolution": command.disposition,
                "resolved_at": max(now, escalation.created_at),
                "resolved_by": operator_id,
                "version": escalation.version + 1,
            }
        )
        uow.escalations.update(resolved, escalation.version)
        applied = _Applied(disposition=command.disposition.value, note=command.note)
        applied.changed(ref(RefKind.ESCALATION, escalation.escalation_id), escalation.version, "escalation", {"status": escalation.status.value})
        applied.subjects.append(ref(RefKind.LEAD, escalation.lead_id))
        return applied

    # ---- Conversation commands (Stage 9) ----------------------------------------------------

    @staticmethod
    def _conversation(uow: UnitOfWork, command: ConversationCommand) -> Conversation:
        conversation = uow.conversations.get(command.conversation_id)
        if conversation is None:
            raise OperatorNotFoundError(f"conversation {command.conversation_id} not found")
        if conversation.version != command.expected_conversation_version:
            raise StaleCommandError((BlockCode.CONVERSATION_VERSION_CHANGED,))
        return conversation

    @staticmethod
    def _conversation_applied(before: Conversation, after: Conversation, disposition: str, note: str | None = None) -> _Applied:
        applied = _Applied(disposition=disposition, note=note)
        applied.changed(ref(RefKind.CONVERSATION, before.conversation_id), before.version, "conversation",
                        {"status": before.status.value}, resulting=after.version)
        applied.subjects.append(ref(RefKind.LEAD, before.lead_id))
        return applied

    def _pause_conversation(self, uow: UnitOfWork, command: PauseConversation, now: datetime, operator_id: str) -> _Applied:
        conversation = self._conversation(uow, command)
        if conversation.status in TERMINAL_CONVERSATION_STATUSES:
            raise CommandRejectedError((BlockCode.CONVERSATION_ENDED,))
        if conversation.status is ConversationStatus.PAUSED:
            raise CommandRejectedError((BlockCode.CONVERSATION_ALREADY_PAUSED,))
        paused = conversation_actions.pause(uow, conversation, correlation_id=command.correlation_id, now=now)
        return self._conversation_applied(conversation, paused, ConversationStatus.PAUSED.value)

    def _resume_conversation(self, uow: UnitOfWork, command: ResumeConversation, now: datetime, operator_id: str) -> _Applied:
        conversation = self._conversation(uow, command)
        # PAUSED, or OPERATOR_REVIEW once nothing is left to review. Resolving an escalation
        # never resumes automation by itself (Stage 7); this explicit command does.
        if conversation.status not in (ConversationStatus.PAUSED, ConversationStatus.OPERATOR_REVIEW):
            raise CommandRejectedError((BlockCode.CONVERSATION_NOT_RESUMABLE,))
        if any(e.status in OPEN_ESCALATION_STATUSES for e in uow.escalations.list_by_lead(conversation.lead_id)):
            raise CommandRejectedError((BlockCode.ESCALATION_OPEN,))
        resumed = conversation_actions.resume(uow, conversation, correlation_id=command.correlation_id, now=now)
        return self._conversation_applied(conversation, resumed, resumed.status.value)

    def _cancel_follow_up(self, uow: UnitOfWork, command: CancelFollowUp, now: datetime, operator_id: str) -> _Applied:
        conversation = self._conversation(uow, command)
        after, stopped = conversation_actions.cancel_follow_up(uow, conversation, correlation_id=command.correlation_id, now=now)
        if not stopped:
            raise CommandRejectedError((BlockCode.NO_FOLLOW_UP_TO_CANCEL,))
        return self._conversation_applied(conversation, after, "FOLLOW_UP_CANCELLED")

    def _close_conversation(self, uow: UnitOfWork, command: CloseConversation, now: datetime, operator_id: str) -> _Applied:
        conversation = self._conversation(uow, command)
        if conversation.status in TERMINAL_CONVERSATION_STATUSES:
            raise CommandRejectedError((BlockCode.CONVERSATION_ENDED,))
        closed = conversation_actions.close(uow, conversation, correlation_id=command.correlation_id, now=now)
        return self._conversation_applied(conversation, closed, ConversationStatus.CLOSED.value, command.note)

    def _mark_do_not_contact(self, uow: UnitOfWork, command: MarkDoNotContact, now: datetime, operator_id: str) -> _Applied:
        conversation = self._conversation(uow, command)
        if conversation.status is ConversationStatus.DO_NOT_CONTACT:
            raise CommandRejectedError((BlockCode.CONVERSATION_ENDED,))
        entry_id, cancelled = conversation_actions.mark_do_not_contact(
            uow, conversation, operator_id=operator_id, command_id=command.command_id,
            correlation_id=command.correlation_id, now=now,
        )
        campaign_state.record_suppressed(uow, conversation.contact_id, correlation_id=command.correlation_id, now=now)
        after = uow.conversations.get(conversation.conversation_id) or conversation
        applied = self._conversation_applied(conversation, after, ConversationStatus.DO_NOT_CONTACT.value, command.note)
        if entry_id is not None:
            applied.subjects.append(ref(RefKind.DNC_ENTRY, entry_id))
        applied.subjects += [ref(RefKind.OUTBOUND_MESSAGE, outbound_id) for outbound_id in cancelled]
        applied.reason_codes = tuple(f"CANCELLED:{outbound_id}" for outbound_id in cancelled)
        return applied

    # ---- Campaign commands (Stage 10) ------------------------------------------------------

    @staticmethod
    def _campaign_for(uow: UnitOfWork, command: CampaignCommand) -> Campaign:
        campaign = uow.campaigns.get(command.campaign_id)
        if campaign is None:
            raise OperatorNotFoundError(f"campaign {command.campaign_id} not found")
        if campaign.version != command.expected_campaign_version:
            raise StaleCommandError((BlockCode.CAMPAIGN_VERSION_CHANGED,))
        return campaign

    @staticmethod
    def _campaign_applied(before: Campaign, after: Campaign, disposition: str, note: str | None = None) -> _Applied:
        applied = _Applied(disposition=disposition, note=note)
        applied.changed(ref(RefKind.CAMPAIGN, before.campaign_id), before.version, "campaign", {"status": before.status.value},
                        resulting=after.version)
        return applied

    def _activate_campaign(self, uow: UnitOfWork, command: ActivateCampaign, now: datetime, operator_id: str) -> _Applied:
        campaign = self._campaign_for(uow, command)
        if campaign.status is not CampaignStatus.DRAFT or not is_review_mode_supported_v1(campaign.review_mode):
            raise CommandRejectedError((BlockCode.CAMPAIGN_STATE_INVALID,))
        return self._campaign_applied(campaign, campaign_actions.activate(uow, campaign, operator_id=operator_id, now=now), "ACTIVATED")

    def _pause_campaign(self, uow: UnitOfWork, command: PauseCampaign, now: datetime, operator_id: str) -> _Applied:
        campaign = self._campaign_for(uow, command)
        if campaign.status is not CampaignStatus.ACTIVE:
            raise CommandRejectedError((BlockCode.CAMPAIGN_STATE_INVALID,))
        paused = campaign_actions.pause(uow, campaign, correlation_id=command.correlation_id, now=now)
        return self._campaign_applied(campaign, paused, "PAUSED")

    def _resume_campaign(self, uow: UnitOfWork, command: ResumeCampaign, now: datetime, operator_id: str) -> _Applied:
        campaign = self._campaign_for(uow, command)
        if campaign.status is not CampaignStatus.PAUSED:
            raise CommandRejectedError((BlockCode.CAMPAIGN_STATE_INVALID,))
        return self._campaign_applied(campaign, campaign_actions.resume(uow, campaign, now=now), "RESUMED")

    def _cancel_campaign(self, uow: UnitOfWork, command: CancelCampaign, now: datetime, operator_id: str) -> _Applied:
        campaign = self._campaign_for(uow, command)
        if campaign.status is CampaignStatus.ENDED:
            raise CommandRejectedError((BlockCode.CAMPAIGN_STATE_INVALID,))
        ended, stopped = campaign_actions.cancel(uow, campaign, correlation_id=command.correlation_id, now=now)
        applied = self._campaign_applied(campaign, ended, "CANCELLED", command.note)
        applied.subjects += [ref(RefKind.CAMPAIGN_MEMBER, member_id) for member_id in stopped]
        return applied

    def _complete_campaign(self, uow: UnitOfWork, command: CompleteCampaign, now: datetime, operator_id: str) -> _Applied:
        campaign = self._campaign_for(uow, command)
        if campaign.status is CampaignStatus.ENDED:
            raise CommandRejectedError((BlockCode.CAMPAIGN_STATE_INVALID,))
        if campaign_actions.in_sequence(uow, campaign.campaign_id):
            raise CommandRejectedError((BlockCode.CAMPAIGN_HAS_ACTIVE_MEMBERS,))
        return self._campaign_applied(campaign, campaign_actions.complete(uow, campaign, now=now), "COMPLETED")

    @staticmethod
    def _member_for(uow: UnitOfWork, command: MemberCommand) -> CampaignMember:
        member = uow.campaign_members.get(command.member_id)
        if member is None:
            raise OperatorNotFoundError(f"campaign member {command.member_id} not found")
        if member.version != command.expected_member_version:
            raise StaleCommandError((BlockCode.MEMBER_VERSION_CHANGED,))
        return member

    def _cancel_campaign_member(self, uow: UnitOfWork, command: CancelCampaignMember, now: datetime, operator_id: str) -> _Applied:
        member = self._member_for(uow, command)
        if member.status in TERMINAL_MEMBER_STATUSES:
            raise CommandRejectedError((BlockCode.MEMBER_ENDED,))
        after = campaign_actions.cancel_member(uow, member, correlation_id=command.correlation_id, now=now)
        applied = _Applied(disposition="MEMBER_CANCELLED", note=command.note)
        applied.changed(ref(RefKind.CAMPAIGN_MEMBER, member.member_id), member.version, "member", {"status": member.status.value},
                        resulting=after.version)
        return applied

    def _suppress_campaign_member(self, uow: UnitOfWork, command: SuppressCampaignMember, now: datetime, operator_id: str) -> _Applied:
        member = self._member_for(uow, command)
        if member.status is CampaignMemberStatus.SUPPRESSED:
            raise CommandRejectedError((BlockCode.MEMBER_ENDED,))
        entry_id, cancelled = campaign_actions.suppress_member(uow, member, operator_id=operator_id, command_id=command.command_id,
                                                               correlation_id=command.correlation_id, now=now)
        after = uow.campaign_members.get(member.member_id) or member
        applied = _Applied(disposition="MEMBER_SUPPRESSED", note=command.note)
        applied.changed(ref(RefKind.CAMPAIGN_MEMBER, member.member_id), member.version, "member", {"status": member.status.value},
                        resulting=after.version)
        if entry_id is not None:
            applied.subjects.append(ref(RefKind.DNC_ENTRY, entry_id))
        applied.subjects += [ref(RefKind.OUTBOUND_MESSAGE, outbound_id) for outbound_id in cancelled]
        return applied

    # ---- Helpers --------------------------------------------------------------------------

    @staticmethod
    def _reply(uow: UnitOfWork, outbound_id: str) -> OutboundMessage:
        outbound = uow.outbound.get(outbound_id)
        if outbound is None or outbound.kind not in REVIEWABLE_KINDS:
            raise OperatorNotFoundError(f"draft {outbound_id} not found")
        return outbound

    def _lead_view(self, uow: UnitOfWork, lead_id: str, now: datetime) -> LeadView | None:
        lead = uow.leads.get(lead_id)
        if lead is None:
            return None
        contact = uow.contacts.get(lead.contact_id)
        company = uow.companies.get(lead.company_id) if lead.company_id else None
        scopes = suppression_scopes(uow, contact.email, company.domain if company else None, now) if contact else ()
        return LeadView(
            lead_id=lead.lead_id, contact_id=lead.contact_id, contact_email=contact.email if contact else None,
            stage=lead.stage, status=lead.status, close_reason=lead.close_reason, campaign_id=lead.campaign_id,
            version=lead.version, suppressed_scopes=scopes,
        )

    def _text(self, message: EmailMessage) -> EmailText:
        limit = self._config.max_body_chars
        return EmailText(
            message_id=message.message_id,
            direction=message.direction,
            from_address=message.from_address,
            subject=message.subject,
            body_text=message.body_text[:limit],
            body_truncated=len(message.body_text) > limit,
            at=message.received_at or message.sent_at,
        )


def _draft_summary(outbound: OutboundMessage) -> DraftSummary:
    return DraftSummary(
        outbound_id=outbound.outbound_id, draft_id=outbound.draft_id, lead_id=outbound.lead_id,
        thread_id=outbound.thread_id, kind=outbound.kind, status=outbound.status, version=outbound.version,
        created_at=outbound.created_at,
    )


def _outbound_state(outbound: OutboundMessage) -> JsonValue:
    return {"status": outbound.status.value, "draft_id": outbound.draft_id, "content_hash": outbound.content_hash}


def _command_event(outcome: CommandOutcome, applied: _Applied) -> AuditEvent:
    """The command's completion record. Holds IDs, versions, codes and the operator note;
    never credentials or email bodies."""
    before: JsonObject = dict(applied.before)
    after: JsonObject = {
        "outcome": outcome.model_dump(mode="json"),
        "note": applied.note,
        "note_kind": NOTE_KIND if applied.note is not None else None,
    }
    event_type = f"OPERATOR_{outcome.kind.value}"
    payload: dict[str, JsonValue] = {"event_type": event_type, "before": before, "after": after}
    return AuditEvent(
        event_id=command_event_id(outcome.command_id),
        occurred_at=outcome.completed_at,
        actor=Actor(type=ActorType.OPERATOR, id=outcome.operator_id),
        event_type=event_type,
        subject_refs=(ref(RefKind.OPERATOR_COMMAND, outcome.command_id), *outcome.subjects),
        before=before,
        after=after,
        correlation_id=outcome.correlation_id,
        payload_hash=hashlib.sha256(dumps_json(payload).encode("utf-8")).hexdigest(),
    )


def _recorded_outcome(uow: UnitOfWork, command_id: str) -> CommandOutcome | None:
    event = uow.audit.get(command_event_id(command_id))
    if event is None or event.after is None or not isinstance(event.after.get("outcome"), dict):
        return None
    return CommandOutcome.model_validate(event.after["outcome"])


def _optional_str(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


def _list(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []
