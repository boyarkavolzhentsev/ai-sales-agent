"""Builders for Stage 9 tests: real Stage 6 inbound, Stage 7 approval, Stage 8 dispatch with
the fake transport, FrozenClock and a file-backed temporary SQLite database."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.enums import LeadIntent
from app.core.models import Conversation, FollowUpJob, OutboundMessage
from app.conversation import (
    FollowUpClaim,
    FollowUpConfig,
    FollowUpExecutor,
    FollowUpScheduler,
    conversation_id_for,
)
from app.dispatch import DispatchResult, FakeEmailTransport
from app.llm import LLMTask, SenderIdentity
from app.persistence import Database, FrozenClock
from app.policy import KillSwitchState, LimitPolicy
from tests.dispatch.builders import dispatcher, send
from tests.inbound.builders import NOW, ScriptedTransport, classification, envelope, happy_transport, process
from tests.operator.builders import AS_ALICE, approve_command, operator
from tests.policy import builders as policy

SENT_AT = NOW  # the first reply is accepted at NOW (Monday 15:00 in Kyiv)
FIRST_DUE = NOW + timedelta(days=3)  # Thursday
SECOND_DUE_AFTER = timedelta(days=4)


def config(**overrides: object) -> FollowUpConfig:
    base: dict[str, object] = {
        "sender": SenderIdentity(sender_name="Alex Seller", company_name="Samplewidget Co"),
        "limits": policy.limits(sends=10, new_contacts=10),
        "window": policy.window(),
        "kill_switch": KillSwitchState(enabled=False, changed_at=NOW, changed_by="ops"),
    }
    return FollowUpConfig.model_validate(base | overrides)


def one_slot() -> LimitPolicy:
    return policy.limits(sends=1, new_contacts=10)


def scheduler(db: Database, clock: FrozenClock | None = None, **overrides: object) -> FollowUpScheduler:
    return FollowUpScheduler(db, clock or FrozenClock(NOW), config(**overrides))


def executor(db: Database, clock: FrozenClock | None = None, **overrides: object) -> FollowUpExecutor:
    return FollowUpExecutor(db, clock or FrozenClock(NOW), config(**overrides))


@dataclass(frozen=True)
class Replied:
    conversation_id: str
    thread_id: str
    lead_id: str
    reply_outbound_id: str


def replied_conversation(db: Database, provider_message_id: str = "p-1", **envelope_overrides: object) -> Replied:
    """Inbound question -> grounded draft -> operator approval -> accepted dispatch."""
    result = process(db, happy_transport(), envelope(provider_message_id, **envelope_overrides))
    assert result.outbound_id is not None and result.lead_id is not None
    approve(db, result.outbound_id, FrozenClock(NOW))
    assert send(dispatcher(db), result.outbound_id).outcome.value == "ACCEPTED"
    return Replied(conversation_id_for(result.thread_id), result.thread_id, result.lead_id, result.outbound_id)


def approve(db: Database, outbound_id: str, clock: FrozenClock, command_id: str | None = None) -> None:
    service = operator(db, clock)
    service.approve_draft(AS_ALICE, approve_command(service.get_draft(AS_ALICE, outbound_id), command_id or f"cmd-approve-{outbound_id}"))


def customer_writes(db: Database, provider_message_id: str, *, in_reply_to: str | None = "<p-1@prospect.example>",
                    received_at: datetime = NOW, intent: LeadIntent = LeadIntent.NEGOTIATION, **overrides: object) -> None:
    process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(intent)),
            envelope(provider_message_id, body="Thanks, one more thing.", in_reply_to=in_reply_to, received_at=received_at, **overrides))


def claim_one(db: Database, clock: FrozenClock, worker: str = "worker-a") -> FollowUpClaim:
    [claim] = scheduler(db, clock).claim_due(worker, correlation_id=f"corr-claim-{worker}")
    return claim


def follow_up_to_draft(db: Database, conversation_id: str, due: datetime = FIRST_DUE) -> tuple[FollowUpJob, str]:
    """Schedule, claim at the due time and execute: returns the job and its draft's outbound id."""
    clock = FrozenClock(due)
    schedule = scheduler(db, clock).schedule(conversation_id, correlation_id="corr-schedule")
    assert schedule.outcome.value == "SCHEDULED", schedule
    result = executor(db, clock).execute(claim_one(db, clock), correlation_id="corr-exec")
    assert result.outcome.value == "DRAFT_CREATED", result
    assert result.outbound_id is not None
    return job(db, result.follow_up_id), result.outbound_id


def dispatch_follow_up(db: Database, outbound_id: str, at: datetime, transport: FakeEmailTransport | None = None) -> DispatchResult:
    clock = FrozenClock(at)
    approve(db, outbound_id, clock)
    return send(dispatcher(db, transport, clock=clock), outbound_id, f"corr-dispatch-{outbound_id}")


def conversation(db: Database, conversation_id: str) -> Conversation:
    with db.transaction() as uow:
        found = uow.conversations.get(conversation_id)
    assert found is not None
    return found


def job(db: Database, follow_up_id: str) -> FollowUpJob:
    with db.transaction() as uow:
        found = uow.follow_up_jobs.get(follow_up_id)
    assert found is not None
    return found


def jobs(db: Database, conversation_id: str) -> list[FollowUpJob]:
    with db.transaction() as uow:
        return uow.follow_up_jobs.list_for_conversation(conversation_id)


def follow_up_drafts(db: Database, lead_id: str) -> list[OutboundMessage]:
    with db.transaction() as uow:
        return [m for m in uow.outbound.list_by_lead(lead_id) if m.idempotency_key.startswith("follow-up:")]
