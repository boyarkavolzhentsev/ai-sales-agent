"""Execution of a claimed follow-up job: revalidate, then produce the follow-up draft.

One IMMEDIATE transaction per execution: the claim token check, the execution-time
revalidation against current state, the draft creation and the job completion commit
together or not at all. A crash anywhere before the commit leaves the job CLAIMED; its
lease expires and another worker re-executes it from scratch.

The effect is a reviewable REPLY outbound message (DRAFTED) with the idempotency key
``follow-up:<follow_up_id>`` (unique in SQL), so one logical follow-up can produce at
most one draft, and Stage 8 allows at most one accepted dispatch per message. Nothing is
sent here: V1 follow-ups are operator-gated through Stage 7 approval and Stage 8 dispatch,
which revalidate everything again (newer inbound, suppression, lead, policy, conflicts).
"""

from datetime import datetime

from app.core.enums import ConversationStatus, DraftPurpose, FollowUpJobStatus, OutboundDecision, OutboundKind, OutboundStatus, RefKind
from app.core.models import Conversation, FollowUpJob, OutboundMessage
from app.conversation.audit import append_event, ref
from app.conversation.drafts import check_follow_up, compose_follow_up
from app.conversation.ids import follow_up_key, stable_id
from app.conversation.models import ExecutionOutcome, ExecutionResult, FollowUpClaim
from app.conversation.policy import STALE_CODES, FollowUpBlock, FollowUpConfig, execution_blockers, load_facts
from app.conversation.state import save
from app.llm.claim_check import draft_hash
from app.persistence import Clock, Database, NotFoundError, UnitOfWork
from app.policy.reply import evaluate_reply_policy

# Event type Stage 7 and Stage 8 read to find a draft's context (see app.operator.review).
DRAFT_CREATED = "DRAFT_CREATED"


class FollowUpExecutor:
    def __init__(self, db: Database, clock: Clock, config: FollowUpConfig) -> None:
        self._db = db
        self._clock = clock
        self._config = config

    def execute(self, claim: FollowUpClaim, *, correlation_id: str) -> ExecutionResult:
        now = self._clock.now()
        with self._db.transaction() as uow:
            job = uow.follow_up_jobs.get(claim.follow_up_id)
            if job is None:
                raise NotFoundError(f"follow-up job {claim.follow_up_id} does not exist")
            if job.status is FollowUpJobStatus.COMPLETED:
                return _result(job, ExecutionOutcome.REPLAYED)
            if job.status is not FollowUpJobStatus.CLAIMED or job.claim_token != claim.claim_token:
                return _result(job, ExecutionOutcome.STALE_CLAIM)
            conversation = uow.conversations.get(job.conversation_id)
            if conversation is None:
                raise NotFoundError(f"conversation {job.conversation_id} does not exist")

            facts = load_facts(uow, conversation)
            codes = execution_blockers(facts, self._config, job, now)
            if codes:
                return self._end(uow, job, conversation, codes, correlation_id, now)

            thread = uow.threads.get(conversation.thread_id)
            if thread is None or facts.contact is None:
                return self._end(uow, job, conversation, [FollowUpBlock.NO_INBOUND_CONTEXT], correlation_id, now)
            policy = evaluate_reply_policy(
                uow, contact=facts.contact, company_domain=facts.company_domain, mailbox=thread.mailbox,
                limits=self._config.limits, window=self._config.window, kill_switch=self._config.kill_switch, now=now,
            )
            if policy.decision is OutboundDecision.HOLD:
                return self._defer(uow, job, conversation, [r.value for r in policy.reasons], correlation_id, now)
            if policy.decision is not OutboundDecision.SEND:
                return self._end(uow, job, conversation, [r.value for r in policy.reasons], correlation_id, now)

            subject, body = compose_follow_up(thread, self._config.sender)
            check = check_follow_up(subject, body, self._config.sender)
            if not check.passed:
                return self._end(uow, job, conversation, [FollowUpBlock.CLAIM_CHECK_FAILED], correlation_id, now)
            outbound = self._draft(uow, job, conversation, subject, body, now)
            append_event(uow, key=(job.follow_up_id,), event_type=DRAFT_CREATED,
                         subjects=(ref(RefKind.EMAIL_MESSAGE, conversation.last_inbound_message_id or ""),
                                   ref(RefKind.OUTBOUND_MESSAGE, outbound.outbound_id), ref(RefKind.MESSAGE_DRAFT, outbound.draft_id),
                                   ref(RefKind.EMAIL_THREAD, conversation.thread_id), ref(RefKind.LEAD, conversation.lead_id),
                                   ref(RefKind.FOLLOW_UP_JOB, job.follow_up_id)),
                         after={"purpose": DraftPurpose.OUTBOUND_FOLLOW_UP.value, "draft_id": outbound.draft_id,
                                "outbound_id": outbound.outbound_id, "follow_up_id": job.follow_up_id,
                                "sequence_no": job.sequence_no, "evidence_ids_used": [],
                                "claim_check": check.model_dump(mode="json")},
                         correlation_id=correlation_id, now=now)
            completed = self._update(uow, job, now, status=FollowUpJobStatus.COMPLETED, outbound_id=outbound.outbound_id)
            save(uow, conversation, status=ConversationStatus.OPERATOR_REVIEW, correlation_id=correlation_id, now=now)
            append_event(uow, key=(job.follow_up_id, str(completed.version)), event_type="FOLLOW_UP_COMPLETED",
                         subjects=(ref(RefKind.FOLLOW_UP_JOB, job.follow_up_id), ref(RefKind.CONVERSATION, conversation.conversation_id),
                                   ref(RefKind.OUTBOUND_MESSAGE, outbound.outbound_id)),
                         after={"outbound_id": outbound.outbound_id, "claim_no": job.claim_count}, correlation_id=correlation_id, now=now)
            return _result(completed, ExecutionOutcome.DRAFT_CREATED)

    def _draft(self, uow: UnitOfWork, job: FollowUpJob, conversation: Conversation, subject: str, body: str, now: datetime) -> OutboundMessage:
        key = follow_up_key(job.follow_up_id)
        existing = uow.outbound.get_by_idempotency_key(key)
        if existing is not None:
            return existing
        outbound = OutboundMessage(
            outbound_id=stable_id("ob", "follow-up", job.follow_up_id), idempotency_key=key, kind=OutboundKind.REPLY,
            lead_id=conversation.lead_id, contact_id=conversation.contact_id, thread_id=conversation.thread_id,
            sequence_no=job.sequence_no, draft_id=stable_id("dr", "follow-up", job.follow_up_id), subject=subject,
            body_final=body, content_hash=draft_hash(subject, body), status=OutboundStatus.DRAFTED, created_at=now,
        )
        uow.outbound.add(outbound)
        return outbound

    def _end(
        self, uow: UnitOfWork, job: FollowUpJob, conversation: Conversation, codes: list[str], correlation_id: str, now: datetime
    ) -> ExecutionResult:
        """BLOCKED, or SUPERSEDED when newer activity made the follow-up stale."""
        stale = bool(STALE_CODES.intersection(codes))
        status = FollowUpJobStatus.SUPERSEDED if stale else FollowUpJobStatus.BLOCKED
        ended = self._update(uow, job, now, status=status, block_codes=() if stale else tuple(dict.fromkeys(codes)),
                             reason=codes[0])
        if conversation.status is ConversationStatus.FOLLOW_UP_DUE:
            save(uow, conversation, status=ConversationStatus.WAITING_FOR_REPLY, correlation_id=correlation_id, now=now)
        append_event(uow, key=(job.follow_up_id, str(ended.version)), event_type=f"FOLLOW_UP_{status.value}",
                     subjects=(ref(RefKind.FOLLOW_UP_JOB, job.follow_up_id), ref(RefKind.CONVERSATION, conversation.conversation_id)),
                     after={"reason_codes": list(dict.fromkeys(codes))}, correlation_id=correlation_id, now=now)
        outcome = ExecutionOutcome.SUPERSEDED if stale else ExecutionOutcome.BLOCKED
        return _result(ended, outcome, codes)

    def _defer(
        self, uow: UnitOfWork, job: FollowUpJob, conversation: Conversation, codes: list[str], correlation_id: str, now: datetime
    ) -> ExecutionResult:
        due_at = now + self._config.defer_delay
        conversation = save(uow, conversation, next_follow_up_at=due_at, correlation_id=correlation_id, now=now)
        deferred = self._update(uow, job, now, status=FollowUpJobStatus.SCHEDULED, due_at=due_at,
                                basis_conversation_version=conversation.version)
        append_event(uow, key=(job.follow_up_id, str(deferred.version)), event_type="FOLLOW_UP_DEFERRED",
                     subjects=(ref(RefKind.FOLLOW_UP_JOB, job.follow_up_id), ref(RefKind.CONVERSATION, conversation.conversation_id)),
                     after={"reason_codes": codes, "due_at": due_at.isoformat()}, correlation_id=correlation_id, now=now)
        return _result(deferred, ExecutionOutcome.DEFERRED, codes)

    @staticmethod
    def _update(uow: UnitOfWork, job: FollowUpJob, now: datetime, **changes: object) -> FollowUpJob:
        updated = FollowUpJob.model_validate(
            job.model_dump()
            | {"claim_token": None, "claimed_by": None, "lease_expires_at": None}
            | changes
            | {"updated_at": max(now, job.updated_at), "version": job.version + 1}
        )
        uow.follow_up_jobs.update(updated, job.version)
        return updated


def _result(job: FollowUpJob, outcome: ExecutionOutcome, codes: list[str] | tuple[str, ...] = ()) -> ExecutionResult:
    return ExecutionResult(
        follow_up_id=job.follow_up_id, outcome=outcome, job_status=job.status, outbound_id=job.outbound_id,
        due_at=job.due_at, reason_codes=tuple(dict.fromkeys(codes)),
    )
