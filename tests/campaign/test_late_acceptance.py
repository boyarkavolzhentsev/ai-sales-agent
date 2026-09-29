"""Provider truth is authoritative for campaign state: a touch reported not accepted but
later positively ACCEPTED is reconciled, exactly once, without new messages or touches."""

from app.campaign import CampaignBlock, ExecutionOutcome
from app.campaign import state as campaign_state
from app.core.enums import (
    CampaignJobStatus,
    CampaignMemberStatus,
    DNCScope,
    FollowUpStatus,
    LeadIntent,
    LeadStage,
    OutboundStatus,
    RefKind,
)
from app.core.models import EntityRef
from app.dispatch import DispatchCode, DispatchOutcome, DispatchResult, FakeBehavior, FakeEmailTransport, FakeStep, TransportOutcome, TransportResult
from app.llm import LLMTask
from app.operator import CancelCampaign, CancelCampaignMember, SuppressCampaignMember
from app.persistence import Database, DispatchAttemptState, FrozenClock
from tests.campaign.builders import (
    CAMPAIGN_ID,
    INTERVAL,
    PROSPECT,
    approve,
    campaign_messages,
    claim_all,
    draft_touch,
    executor,
    member,
    outbound,
    ready_campaign,
    scheduler,
)
from tests.dispatch.builders import dispatcher, send, state
from tests.inbound.builders import NOW, ScriptedTransport, classification, envelope, process
from tests.operator.builders import AS_ALICE, operator

M = CampaignMemberStatus
LATER = NOW + INTERVAL


def rejected_first_touch(db: Database, *, retryable: bool = False) -> tuple[str, str, FakeEmailTransport]:
    member_id = ready_campaign(db)
    first = draft_touch(db).outbound_id or ""
    approve(db, first)
    transport = FakeEmailTransport().script(FakeStep(FakeBehavior.REJECT, retryable=retryable))
    assert send(dispatcher(db, transport), first).outcome is DispatchOutcome.NOT_ACCEPTED
    return member_id, first, transport


def late_accept(db: Database, outbound_id: str, attempt_no: int = 1) -> DispatchResult:
    """Positive provider evidence for an earlier attempt, through Stage 8's own result path."""
    attempt = state(db, outbound_id).attempts[attempt_no - 1]
    evidence = TransportResult(outcome=TransportOutcome.ACCEPTED, reason_code="RECONCILED_ACCEPTED", provider_message_id="fake-late")
    return dispatcher(db)._record(attempt, evidence, "corr-late", reconciled=True, transport_called=False)  # noqa: SLF001


def plans(db: Database, lead_id: str) -> list[str]:
    with db.transaction() as uow:
        return [row[0] for row in uow._tx.fetch_all("SELECT data FROM follow_up_plans WHERE lead_id = ?", (lead_id,))]  # noqa: SLF001


def jobs(db: Database, member_id: str) -> list[CampaignJobStatus]:
    with db.transaction() as uow:
        return [j.status for j in uow.campaign_jobs.list_for_member(member_id)]


def events(db: Database, member_id: str) -> list[dict[str, object]]:
    with db.transaction() as uow:
        found = uow.audit.list_for_subject(EntityRef(kind=RefKind.CAMPAIGN_MEMBER, id=member_id))
    return [{"type": e.event_type, **(e.after or {})} for e in found]


def test_failed_member_is_reconciled_to_waiting_exactly_once(db: Database) -> None:
    member_id, first, transport = rejected_first_touch(db)
    failed = member(db, member_id)
    assert (failed.status, failed.touch_count) == (M.FAILED, 0) and failed.terminal_reason == "NOT_ACCEPTED:PROVIDER_REJECTED"

    result = late_accept(db, first)
    assert result.outcome is DispatchOutcome.ACCEPTED and outbound(db, first).status is OutboundStatus.SENT
    fixed = member(db, member_id)
    # A: no longer falsely FAILED.  B: counted exactly once.
    assert (fixed.status, fixed.terminal_reason, fixed.touch_count, fixed.latest_outbound_id) == (M.WAITING, None, 1, first)
    with db.transaction() as uow:
        lead = uow.leads.get(fixed.lead_id or "")
    assert lead is not None and lead.stage is LeadStage.CONTACTED
    [plan] = plans(db, fixed.lead_id or "")
    assert f'"{FollowUpStatus.ACTIVE.value}"' in plan  # the sequence restarts after the touch actually sent
    # History is kept: the failure and the correction are both recorded.
    recorded = events(db, member_id)
    assert any(e.get("to") == "FAILED" for e in recorded)
    assert any(e["type"] == "CAMPAIGN_TOUCH_ACCEPTED" and e["reconciled_from_failed"] is True for e in recorded)
    # H: the read model follows durable state.
    stats = operator(db).get_campaign_stats(AS_ALICE, CAMPAIGN_ID)
    assert (stats.waiting, stats.failed_or_cancelled, stats.touches_accepted) == (1, 0, 1)
    assert len(transport.calls) == 1


def test_replayed_acceptance_evidence_is_idempotent(db: Database) -> None:
    member_id, first, _ = rejected_first_touch(db)
    late_accept(db, first)
    once = member(db, member_id)
    replay = late_accept(db, first)  # C: the same evidence again
    assert replay.replayed and member(db, member_id) == once
    assert len(plans(db, once.lead_id or "")) == 1


def test_no_duplicate_message_job_or_touch_is_created(db: Database) -> None:
    member_id, first, transport = rejected_first_touch(db)
    late_accept(db, first)
    lead_id = member(db, member_id).lead_id or ""
    assert [m.outbound_id for m in campaign_messages(db, lead_id)] == [first]
    assert jobs(db, member_id) == [CampaignJobStatus.COMPLETED] and len(state(db, first).attempts) == 1
    again = send(dispatcher(db, transport), first, "corr-again")
    assert again.replayed and not again.transport_called  # the accepted message is never sent again
    # The only next touch is the legitimate follow-up: opened once, due after the interval.
    assert len(scheduler(db).schedule(CAMPAIGN_ID, correlation_id="now").scheduled) == 1
    assert scheduler(db, FrozenClock(LATER)).schedule(CAMPAIGN_ID, correlation_id="later").scheduled == ()
    assert claim_all(db, FrozenClock(NOW)) == ()  # nothing is due before the interval
    assert jobs(db, member_id) == [CampaignJobStatus.COMPLETED, CampaignJobStatus.SCHEDULED]
    [claim] = claim_all(db, FrozenClock(LATER))
    assert executor(db, FrozenClock(LATER)).execute(claim, correlation_id="c").outcome is ExecutionOutcome.DRAFT_CREATED
    assert len(campaign_messages(db, lead_id)) == 2  # touch 1 once, touch 2 once


def test_late_acceptance_after_the_contact_replied_keeps_replied(db: Database) -> None:
    member_id, first, _ = rejected_first_touch(db)
    process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.NEGOTIATION)),
            envelope("p-r", sender=PROSPECT, body="Hello, is this still relevant?"))
    late_accept(db, first)
    after = member(db, member_id)
    assert (after.status, after.terminal_reason, after.touch_count) == (M.REPLIED, "CUSTOMER_REPLIED", 1)  # E
    assert not any(f'"{FollowUpStatus.ACTIVE.value}"' in p for p in plans(db, after.lead_id or ""))


def test_late_acceptance_after_suppression_keeps_suppressed(db: Database) -> None:
    member_id, first, _ = rejected_first_touch(db)
    operator(db).suppress_campaign_member(AS_ALICE, SuppressCampaignMember(
        command_id="cmd-dnc", correlation_id="c", member_id=member_id, expected_member_version=member(db, member_id).version))
    late_accept(db, first)
    after = member(db, member_id)
    assert (after.status, after.touch_count, after.latest_outbound_id) == (M.SUPPRESSED, 1, first)  # F
    with db.transaction() as uow:
        assert uow.dnc.list_active(DNCScope.EMAIL, PROSPECT, NOW)
    assert plans(db, after.lead_id or "") == [] or not any(f'"{FollowUpStatus.ACTIVE.value}"' in p for p in plans(db, after.lead_id or ""))


def test_late_acceptance_after_campaign_cancellation_does_not_resurrect_automation(db: Database) -> None:
    member_id, first, _ = rejected_first_touch(db)
    with db.transaction() as uow:
        campaign = uow.campaigns.get(CAMPAIGN_ID)
    assert campaign is not None
    operator(db).cancel_campaign(AS_ALICE, CancelCampaign(command_id="cmd-cancel", correlation_id="c", campaign_id=CAMPAIGN_ID,
                                                          expected_campaign_version=campaign.version))
    late_accept(db, first)
    after = member(db, member_id)
    assert (after.status, after.terminal_reason, after.touch_count) == (M.CANCELLED, "CAMPAIGN_ENDED", 1)  # G
    assert not any(f'"{FollowUpStatus.ACTIVE.value}"' in p for p in plans(db, after.lead_id or ""))


def test_acceptance_after_an_in_sequence_cancellation_keeps_it_cancelled(db: Database) -> None:
    member_id = ready_campaign(db)
    first = draft_touch(db).outbound_id or ""
    approve(db, first)
    transport = FakeEmailTransport().script(FakeBehavior.TIMEOUT)
    assert send(dispatcher(db, transport), first).outcome is DispatchOutcome.UNKNOWN
    operator(db).cancel_campaign_member(AS_ALICE, CancelCampaignMember(
        command_id="cmd-cm", correlation_id="c", member_id=member_id, expected_member_version=member(db, member_id).version))
    evidence = TransportResult(outcome=TransportOutcome.ACCEPTED, reason_code="RECONCILED_ACCEPTED", provider_message_id="fake-1")
    dispatcher(db)._record(state(db, first).attempts[0], evidence, "corr-rec", reconciled=True, transport_called=False)  # noqa: SLF001
    after = member(db, member_id)
    assert (after.status, after.terminal_reason, after.touch_count) == (M.CANCELLED, "OPERATOR_CANCELLED", 1)
    assert scheduler(db, FrozenClock(LATER)).schedule(CAMPAIGN_ID, correlation_id="c").scheduled == ()


def test_stage8_duplicate_protection_holds_when_a_newer_attempt_exists(db: Database) -> None:
    member_id, first, _ = rejected_first_touch(db, retryable=True)
    assert member(db, member_id).status is M.APPROVED
    transport = FakeEmailTransport().script(FakeBehavior.TIMEOUT)
    assert send(dispatcher(db, transport), first, "corr-retry").outcome is DispatchOutcome.UNKNOWN  # attempt 2 in doubt
    conflict = late_accept(db, first, attempt_no=1)  # attempt 1 was accepted after all
    assert conflict.reason_codes == (DispatchCode.LATE_RESULT_CONFLICT,)
    after = member(db, member_id)
    assert (after.status, after.touch_count) == (M.WAITING, 1)  # the accepted send is reflected
    assert [a.state for a in state(db, first).attempts] == [DispatchAttemptState.NOT_ACCEPTED, DispatchAttemptState.UNKNOWN]
    # I: no further submission of this message, and no newer touch while evidence conflicts.
    assert send(dispatcher(db, transport), first, "corr-third").transport_called is False
    later = FrozenClock(LATER)
    scheduler(db, later).schedule(CAMPAIGN_ID, correlation_id="c")
    [claim] = claim_all(db, later)
    blocked = executor(db, later).execute(claim, correlation_id="c")
    assert blocked.outcome is ExecutionOutcome.BLOCKED and CampaignBlock.ACCEPTANCE_CONFLICT in blocked.reason_codes
    assert len(campaign_messages(db, after.lead_id or "")) == 1
    # Attempt 2 later resolves ACCEPTED as well: the touch is still counted once.
    late_accept(db, first, attempt_no=2)
    assert member(db, member_id).touch_count == 1


def test_old_touch_evidence_arriving_after_a_newer_touch_is_not_counted_again(db: Database) -> None:
    member_id, first, _ = rejected_first_touch(db, retryable=True)
    retry = FakeEmailTransport().script(FakeBehavior.TIMEOUT)
    send(dispatcher(db, retry), first, "corr-retry")  # attempt 2 in doubt
    late_accept(db, first, attempt_no=1)  # conflict evidence: touch 1 counted once
    late_accept(db, first, attempt_no=2)  # attempt 2 accepted too: the message is SENT
    assert member(db, member_id).touch_count == 1
    # Touch 2 is drafted and accepted normally...
    with db.transaction() as uow:
        [attempt_1, _] = uow.dispatch_attempts.list_for_outbound(first)
        uow.dispatch_attempts.update(attempt_1.model_copy(update={
            "late_acceptance_provider_message_id": None, "late_acceptance_at": None, "version": attempt_1.version + 1,
        }), attempt_1.version)  # an operator cleared the conflict so the sequence may continue
    second = draft_touch(db, at=LATER).outbound_id or ""
    approve(db, second, LATER)
    assert send(dispatcher(db, clock=FrozenClock(LATER)), second).outcome is DispatchOutcome.ACCEPTED
    assert member(db, member_id).touch_count == 2
    # ...and yet another report about touch 1 arrives: it is already recorded.
    with db.transaction() as uow:
        message = uow.outbound.get(first)
        assert message is not None
        campaign_state.record_touch_accepted(uow, message, correlation_id="late-again", now=LATER)
    assert (member(db, member_id).touch_count, member(db, member_id).latest_outbound_id) == (2, second)
