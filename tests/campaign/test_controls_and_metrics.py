"""Campaign and recipient controls, sequence completion, and derived metrics."""

import pytest

from app.campaign import CampaignBlock
from app.core.enums import (
    CampaignJobStatus,
    CampaignMemberStatus,
    CampaignStatus,
    CloseReason,
    DNCScope,
    FollowUpStatus,
    LeadIntent,
    LeadStage,
    OutboundStatus,
)
from app.dispatch import DispatchOutcome, FakeBehavior, FakeEmailTransport, FakeStep
from app.llm import LLMTask
from app.operator import (
    BlockCode,
    CancelCampaign,
    CancelCampaignMember,
    CommandRejectedError,
    CompleteCampaign,
    PauseCampaign,
    ResumeCampaign,
    StaleCommandError,
    SuppressCampaignMember,
)
from app.persistence import Database, FrozenClock
from tests.campaign.builders import (
    CAMPAIGN_ID,
    INTERVAL,
    PROSPECT,
    activate,
    add_campaign,
    approve,
    campaign_messages,
    claim_all,
    draft_touch,
    enrolled,
    executor,
    member,
    outbound,
    ready_campaign,
    scheduler,
    send_touch,
)
from tests.dispatch.builders import dispatcher, send
from tests.inbound.builders import NOW, ScriptedTransport, classification, envelope, process
from tests.operator.builders import AS_ALICE, operator

M = CampaignMemberStatus


def campaign_version(db: Database) -> int:
    with db.transaction() as uow:
        campaign = uow.campaigns.get(CAMPAIGN_ID)
    assert campaign is not None
    return campaign.version


CampaignCommandType = type[PauseCampaign] | type[ResumeCampaign] | type[CancelCampaign] | type[CompleteCampaign]


def command(kind: CampaignCommandType, command_id: str, db: Database) -> PauseCampaign | ResumeCampaign | CancelCampaign | CompleteCampaign:
    return kind(command_id=command_id, correlation_id="c", campaign_id=CAMPAIGN_ID, expected_campaign_version=campaign_version(db))


def test_a_campaign_runs_only_after_explicit_activation(db: Database) -> None:
    add_campaign(db)
    enrolled(db)  # enrollment is allowed while the campaign is being prepared
    assert scheduler(db).schedule(CAMPAIGN_ID, correlation_id="c").blocked_reason == CampaignBlock.CAMPAIGN_NOT_ACTIVE
    assert claim_all(db, FrozenClock(NOW)) == ()


def test_pause_stops_work_and_resume_continues_the_same_logical_touch(db: Database) -> None:
    member_id = ready_campaign(db)
    summary = scheduler(db).schedule(CAMPAIGN_ID, correlation_id="c")
    service = operator(db)
    service.pause_campaign(AS_ALICE, command(PauseCampaign, "cmd-pause", db))
    with db.transaction() as uow:
        [job] = uow.campaign_jobs.list_for_member(member_id)
    assert job.status is CampaignJobStatus.CANCELLED and claim_all(db, FrozenClock(NOW)) == ()
    assert scheduler(db).schedule(CAMPAIGN_ID, correlation_id="c2").blocked_reason == CampaignBlock.CAMPAIGN_NOT_ACTIVE
    service.resume_campaign(AS_ALICE, command(ResumeCampaign, "cmd-resume", db))
    again = scheduler(db).schedule(CAMPAIGN_ID, correlation_id="c3")
    assert again.scheduled == summary.scheduled  # the same job identity, reopened
    [claim] = claim_all(db, FrozenClock(NOW))
    assert executor(db).execute(claim, correlation_id="c").outbound_id is not None


def test_pause_blocks_an_approved_touch_from_dispatch(db: Database) -> None:
    ready_campaign(db)
    first = draft_touch(db)
    approve(db, first.outbound_id or "")
    operator(db).pause_campaign(AS_ALICE, command(PauseCampaign, "cmd-pause", db))
    transport = FakeEmailTransport()
    assert send(dispatcher(db, transport), first.outbound_id or "").outcome is DispatchOutcome.BLOCKED and transport.calls == []


def test_resume_never_resurrects_finished_recipients(db: Database) -> None:
    member_id = ready_campaign(db)
    first = draft_touch(db)
    send_touch(db, first.outbound_id or "")
    process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.NEGOTIATION)),
            envelope("p-r", sender=PROSPECT, body="Tell me more.", in_reply_to=outbound(db, first.outbound_id or "").rfc_message_id))
    service = operator(db)
    service.pause_campaign(AS_ALICE, command(PauseCampaign, "cmd-pause", db))
    service.resume_campaign(AS_ALICE, command(ResumeCampaign, "cmd-resume", db))
    assert scheduler(db, FrozenClock(NOW + INTERVAL)).schedule(CAMPAIGN_ID, correlation_id="c").scheduled == ()
    assert member(db, member_id).status is M.REPLIED


def test_cancel_stops_unsent_work_but_keeps_sent_history(db: Database) -> None:
    add_campaign(db)
    activate(db)
    sent_member = enrolled(db)
    pending_member = enrolled(db, "max@other-prospect.example", name="Max", company_name=None)
    clock = FrozenClock(NOW)
    scheduler(db, clock).schedule(CAMPAIGN_ID, correlation_id="c")
    claims = claim_all(db, clock)
    drafts = {executor(db, clock).execute(c, correlation_id="c").outbound_id for c in claims}
    sent_draft = campaign_messages(db, member(db, sent_member).lead_id or "")[0].outbound_id
    pending_draft = campaign_messages(db, member(db, pending_member).lead_id or "")[0].outbound_id
    assert {sent_draft, pending_draft} == drafts
    send_touch(db, sent_draft)
    operator(db).cancel_campaign(AS_ALICE, command(CancelCampaign, "cmd-cancel", db))
    assert outbound(db, sent_draft).status is OutboundStatus.SENT  # history is immutable
    assert outbound(db, pending_draft).status is OutboundStatus.CANCELLED
    assert {member(db, sent_member).status, member(db, pending_member).status} == {M.CANCELLED}
    with db.transaction() as uow:
        plans = uow._tx.fetch_all("SELECT data FROM follow_up_plans")  # noqa: SLF001
        campaign = uow.campaigns.get(CAMPAIGN_ID)
    assert campaign is not None and campaign.status is CampaignStatus.ENDED
    assert all('"CANCELLED"' in row[0] and "CAMPAIGN_ENDED" in row[0] for row in plans)


def test_complete_is_refused_while_recipients_are_in_sequence(db: Database) -> None:
    member_id = ready_campaign(db)
    service = operator(db)
    with pytest.raises(CommandRejectedError) as error:
        service.complete_campaign(AS_ALICE, command(CompleteCampaign, "cmd-complete", db))
    assert error.value.codes == (BlockCode.CAMPAIGN_HAS_ACTIVE_MEMBERS,)
    service.cancel_campaign_member(AS_ALICE, CancelCampaignMember(command_id="cmd-cm", correlation_id="c", member_id=member_id,
                                                                  expected_member_version=member(db, member_id).version))
    service.complete_campaign(AS_ALICE, command(CompleteCampaign, "cmd-complete-2", db))


def test_recipient_cancel_and_suppress(db: Database) -> None:
    member_id = ready_campaign(db)
    first = draft_touch(db)
    service = operator(db)
    stale_version = member(db, member_id).version - 1
    with pytest.raises(StaleCommandError):
        service.suppress_campaign_member(AS_ALICE, SuppressCampaignMember(command_id="cmd-s0", correlation_id="c", member_id=member_id,
                                                                          expected_member_version=stale_version))
    service.suppress_campaign_member(AS_ALICE, SuppressCampaignMember(command_id="cmd-s", correlation_id="c", member_id=member_id,
                                                                      expected_member_version=member(db, member_id).version))
    assert member(db, member_id).status is M.SUPPRESSED and outbound(db, first.outbound_id or "").status is OutboundStatus.CANCELLED
    with db.transaction() as uow:
        assert len(uow.dnc.list_active(DNCScope.EMAIL, PROSPECT, NOW)) == 1
    with pytest.raises(CommandRejectedError):
        service.cancel_campaign_member(AS_ALICE, CancelCampaignMember(command_id="cmd-c", correlation_id="c", member_id=member_id,
                                                                      expected_member_version=member(db, member_id).version))


def test_exhausted_sequence_completes_and_closes_the_lead_without_response(db: Database) -> None:
    member_id = ready_campaign(db)
    first = draft_touch(db)
    send_touch(db, first.outbound_id or "")
    second = draft_touch(db, at=NOW + INTERVAL)
    send_touch(db, second.outbound_id or "", at=NOW + INTERVAL)
    summary = scheduler(db, FrozenClock(NOW + 2 * INTERVAL)).schedule(CAMPAIGN_ID, correlation_id="c")
    assert summary.exhausted == (member_id,) and summary.scheduled == ()
    done = member(db, member_id)
    assert (done.status, done.terminal_reason, done.touch_count) == (M.COMPLETED, "NO_RESPONSE", 2)
    with db.transaction() as uow:
        lead = uow.leads.get(done.lead_id or "")
        plans = uow._tx.fetch_all("SELECT data FROM follow_up_plans")  # noqa: SLF001
    assert lead is not None and (lead.stage, lead.close_reason) == (LeadStage.CLOSED, CloseReason.NO_RESPONSE)
    assert FollowUpStatus.EXHAUSTED.value in plans[0][0]


def test_metrics_are_derived_from_durable_state_and_retries_do_not_double_count(db: Database) -> None:
    add_campaign(db)
    activate(db)
    a = enrolled(db)
    b = enrolled(db, "max@other-prospect.example", name="Max", company_name=None)
    enrolled(db, "third@another-prospect.example", company_name=None)
    clock = FrozenClock(NOW)
    scheduler(db, clock).schedule(CAMPAIGN_ID, correlation_id="c")
    results = [executor(db, clock).execute(c, correlation_id="c") for c in claim_all(db, clock)]
    first_a = campaign_messages(db, member(db, a).lead_id or "")[0].outbound_id
    approve(db, first_a)
    transport = FakeEmailTransport().script(FakeStep(FakeBehavior.REJECT, retryable=True), FakeBehavior.ACCEPT)
    send(dispatcher(db, transport), first_a)
    send(dispatcher(db, transport), first_a, "retry")
    stats = operator(db).get_campaign_stats(AS_ALICE, CAMPAIGN_ID)
    assert (stats.total_enrolled, stats.waiting, stats.awaiting_review, stats.touches_accepted) == (3, 1, 2, 1)
    assert stats.by_status[M.WAITING.value] == 1 and len(results) == 3
    views = {v.member_id: v for v in operator(db).list_campaign_members(AS_ALICE, CAMPAIGN_ID)}
    assert views[a].touch_count == 1 and views[b].latest_touch_no == 1 and views[b].status is M.DRAFTED
    process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.NEGOTIATION)),
            envelope("p-r", sender=PROSPECT, body="Tell me more.", in_reply_to=outbound(db, first_a).rfc_message_id))
    after = operator(db).get_campaign_stats(AS_ALICE, CAMPAIGN_ID)
    assert (after.replied, after.waiting, after.touches_accepted) == (1, 0, 1)
