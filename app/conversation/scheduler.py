"""Scheduling and claiming of follow-up jobs.

schedule(): one IMMEDIATE transaction. The job identity is the logical follow-up
("number N after outbound X"), so running the scheduler twice, retrying, or racing two
schedulers yields one job: the second finds it (ALREADY_SCHEDULED / ALREADY_EXECUTED), and
SQL allows one open job per conversation. A caller may pass the conversation version its
decision was based on; a newer version returns STALE_SNAPSHOT without writing.

claim_due(): one IMMEDIATE transaction that leases due SCHEDULED jobs and CLAIMED jobs
whose lease expired (a crashed or stalled worker). Each claim gets a fresh token; only
the current token can execute, so a stalled worker that wakes up later cannot act.
Execution is at-least-once; its effect (one draft per job) is idempotent.
"""

from app.core.enums import ConversationStatus, FollowUpJobStatus, RefKind
from app.core.models import FollowUpJob
from app.conversation.audit import append_event, ref
from app.conversation.ids import follow_up_id_for, stable_id
from app.conversation.models import FollowUpClaim, ScheduleOutcome, ScheduleResult
from app.conversation.policy import FollowUpBlock, FollowUpConfig, load_facts, next_due, schedule_blockers
from app.conversation.state import save
from app.persistence import Clock, Database, NotFoundError

REOPENABLE = frozenset({FollowUpJobStatus.CANCELLED, FollowUpJobStatus.BLOCKED, FollowUpJobStatus.SUPERSEDED})


class FollowUpScheduler:
    def __init__(self, db: Database, clock: Clock, config: FollowUpConfig) -> None:
        self._db = db
        self._clock = clock
        self._config = config

    def schedule(self, conversation_id: str, *, correlation_id: str, expected_version: int | None = None) -> ScheduleResult:
        now = self._clock.now()
        with self._db.transaction() as uow:
            conversation = uow.conversations.get(conversation_id)
            if conversation is None:
                raise NotFoundError(f"conversation {conversation_id} does not exist")
            if expected_version is not None and conversation.version != expected_version:
                return ScheduleResult(conversation_id=conversation_id, outcome=ScheduleOutcome.STALE_SNAPSHOT,
                                      reason_codes=(FollowUpBlock.CONVERSATION_CHANGED,), conversation_version=conversation.version)
            facts = load_facts(uow, conversation)
            codes = schedule_blockers(facts, self._config, now)
            if codes == [FollowUpBlock.FOLLOW_UP_ALREADY_SCHEDULED] and facts.open_job is not None:
                return ScheduleResult(conversation_id=conversation_id, outcome=ScheduleOutcome.ALREADY_SCHEDULED,
                                      follow_up_id=facts.open_job.follow_up_id, due_at=facts.open_job.due_at,
                                      conversation_version=conversation.version)
            if codes:
                return ScheduleResult(conversation_id=conversation_id, outcome=ScheduleOutcome.BLOCKED,
                                      reason_codes=tuple(codes), conversation_version=conversation.version)

            if conversation.last_outbound_id is None:  # excluded by the anchor checks above
                raise ValueError(f"conversation {conversation_id} has no outbound message to follow up")
            sequence_no = conversation.follow_up_count + 1
            follow_up_id = follow_up_id_for(conversation_id, conversation.last_outbound_id, sequence_no)
            existing = uow.follow_up_jobs.get(follow_up_id)
            if existing is not None and existing.status is FollowUpJobStatus.COMPLETED:
                return ScheduleResult(conversation_id=conversation_id, outcome=ScheduleOutcome.ALREADY_EXECUTED,
                                      follow_up_id=follow_up_id, conversation_version=conversation.version)

            due_at = next_due(facts, self._config)
            conversation = save(uow, conversation, status=ConversationStatus.FOLLOW_UP_DUE, next_follow_up_at=due_at,
                                correlation_id=correlation_id, now=now)
            if existing is None:
                job = FollowUpJob(
                    follow_up_id=follow_up_id, conversation_id=conversation_id, anchor_outbound_id=conversation.last_outbound_id,
                    sequence_no=sequence_no, basis_conversation_version=conversation.version, due_at=due_at,
                    created_at=now, updated_at=now,
                )
                uow.follow_up_jobs.add(job)
            else:
                # A cancelled, blocked or superseded job never produced a draft, so the same
                # logical follow-up may be opened again once the policy allows it.
                if existing.status not in REOPENABLE:
                    raise ValueError(f"follow-up {follow_up_id} is {existing.status} and cannot be reopened")
                job = FollowUpJob.model_validate(
                    existing.model_dump()
                    | {"status": FollowUpJobStatus.SCHEDULED, "block_codes": (), "basis_conversation_version": conversation.version,
                       "due_at": due_at, "updated_at": max(now, existing.updated_at), "version": existing.version + 1}
                )
                uow.follow_up_jobs.update(job, existing.version)
            append_event(uow, key=(follow_up_id, str(job.version)), event_type="FOLLOW_UP_SCHEDULED",
                         subjects=(ref(RefKind.FOLLOW_UP_JOB, follow_up_id), ref(RefKind.CONVERSATION, conversation_id)),
                         after={"sequence_no": sequence_no, "due_at": due_at.isoformat(), "anchor_outbound_id": job.anchor_outbound_id,
                                "basis_conversation_version": job.basis_conversation_version},
                         correlation_id=correlation_id, now=now)
            return ScheduleResult(conversation_id=conversation_id, outcome=ScheduleOutcome.SCHEDULED, follow_up_id=follow_up_id,
                                  due_at=due_at, conversation_version=conversation.version)

    def claim_due(self, worker_id: str, *, correlation_id: str, limit: int = 10) -> tuple[FollowUpClaim, ...]:
        now = self._clock.now()
        claims: list[FollowUpClaim] = []
        with self._db.transaction() as uow:
            for job in uow.follow_up_jobs.list_claimable(now, limit):
                recovered = job.status is FollowUpJobStatus.CLAIMED
                token = stable_id("fc", job.follow_up_id, str(job.claim_count + 1))
                claimed = FollowUpJob.model_validate(
                    job.model_dump()
                    | {"status": FollowUpJobStatus.CLAIMED, "claim_token": token, "claimed_by": worker_id,
                       "lease_expires_at": now + self._config.lease, "claim_count": job.claim_count + 1,
                       "updated_at": max(now, job.updated_at), "version": job.version + 1}
                )
                uow.follow_up_jobs.update(claimed, job.version)
                append_event(uow, key=(job.follow_up_id, str(claimed.version)), event_type="FOLLOW_UP_CLAIMED",
                             subjects=(ref(RefKind.FOLLOW_UP_JOB, job.follow_up_id), ref(RefKind.CONVERSATION, job.conversation_id)),
                             after={"worker_id": worker_id, "claim_no": claimed.claim_count, "recovered": recovered},
                             correlation_id=correlation_id, now=now)
                claims.append(FollowUpClaim(follow_up_id=job.follow_up_id, conversation_id=job.conversation_id, claim_token=token,
                                            claimed_by=worker_id, lease_expires_at=now + self._config.lease, recovered=recovered))
        return tuple(claims)
