"""Stage 8 dispatch of campaign touches, provider uncertainty, and the reply handoff."""

from app.campaign import CampaignBlock, ExecutionOutcome
from app.core.enums import (
    CampaignJobStatus,
    CampaignMemberStatus,
    ConversationStatus,
    FollowUpCancelReason,
    FollowUpStatus,
    LeadIntent,
    OutboundStatus,
)
from app.conversation import conversation_id_for
from app.dispatch import DispatchCode, DispatchOutcome, FakeBehavior, FakeEmailTransport, FakeReconciler, FakeStep
from app.llm import LLMTask
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
    send_touch,
)
from tests.dispatch.builders import dispatcher, request, send
from tests.inbound.builders import NOW, ScriptedTransport, classification, envelope, process

M = CampaignMemberStatus
LATER = NOW + INTERVAL


def sent_first_touch(db: Database) -> tuple[str, str]:
    member_id = ready_campaign(db)
    first = draft_touch(db)
    assert send_touch(db, first.outbound_id or "").outcome is DispatchOutcome.ACCEPTED
    return member_id, first.outbound_id or ""


def reply(db: Database, to_outbound_id: str, pid: str = "p-reply", intent: LeadIntent = LeadIntent.NEGOTIATION, body: str = "Tell me more.") -> str:
    result = process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(intent)),
                     envelope(pid, sender=PROSPECT, body=body, in_reply_to=outbound(db, to_outbound_id).rfc_message_id))
    return result.thread_id


def test_accepted_first_touch_updates_membership_lead_and_plan(db: Database) -> None:
    member_id, first = sent_first_touch(db)
    sent = member(db, member_id)
    assert (sent.status, sent.touch_count, sent.latest_outbound_id) == (M.WAITING, 1, first)
    assert outbound(db, first).status is OutboundStatus.SENT and outbound(db, first).thread_id == sent.thread_id
    with db.transaction() as uow:
        plan = uow.follow_ups.get_open_for_lead(sent.lead_id or "")
        thread = uow.threads.get(sent.thread_id or "")
    assert plan is not None and plan.next_due_at == NOW + INTERVAL and plan.steps_sent == 0
    assert thread is not None and len(thread.message_ids) == 1  # our message, recorded for reply threading


def test_unknown_outcome_blocks_the_next_touch_until_reconciled(db: Database) -> None:
    member_id = ready_campaign(db)
    first = draft_touch(db)
    approve(db, first.outbound_id or "")
    transport = FakeEmailTransport().script(FakeBehavior.ACCEPT_THEN_LOSE_RESPONSE)
    assert send(dispatcher(db, transport), first.outbound_id or "").outcome is DispatchOutcome.UNKNOWN
    assert member(db, member_id).status is M.DISPATCHING
    later = FrozenClock(LATER)
    assert scheduler(db, later).schedule(CAMPAIGN_ID, correlation_id="c").scheduled == ()  # nothing while unresolved
    resolved = dispatcher(db, transport, reconciler=FakeReconciler(transport)).reconcile(request(first.outbound_id or "", "corr-rec"))
    assert resolved.outcome is DispatchOutcome.ACCEPTED and member(db, member_id).status is M.WAITING
    assert len(scheduler(db, later).schedule(CAMPAIGN_ID, correlation_id="c2").scheduled) == 1
    assert len(transport.calls) == 1


def test_retryable_rejection_permits_only_the_policy_defined_retry(db: Database) -> None:
    member_id = ready_campaign(db)
    first = draft_touch(db)
    approve(db, first.outbound_id or "")
    transport = FakeEmailTransport().script(FakeStep(FakeBehavior.REJECT, retryable=True), FakeBehavior.ACCEPT)
    assert send(dispatcher(db, transport), first.outbound_id or "").outcome is DispatchOutcome.NOT_ACCEPTED
    assert member(db, member_id).status is M.APPROVED
    assert send(dispatcher(db, transport), first.outbound_id or "", "corr-retry").outcome is DispatchOutcome.ACCEPTED
    assert (member(db, member_id).status, member(db, member_id).touch_count) == (M.WAITING, 1)  # counted once


def test_permanent_rejection_fails_the_membership(db: Database) -> None:
    member_id = ready_campaign(db)
    first = draft_touch(db)
    approve(db, first.outbound_id or "")
    transport = FakeEmailTransport().script(FakeStep(FakeBehavior.REJECT, retryable=False))
    send(dispatcher(db, transport), first.outbound_id or "")
    failed = member(db, member_id)
    assert failed.status is M.FAILED and failed.terminal_reason == "NOT_ACCEPTED:PROVIDER_REJECTED"
    assert send(dispatcher(db, transport), first.outbound_id or "", "c2").reason_codes == (DispatchCode.RETRY_NOT_PERMITTED,)


def test_late_acceptance_evidence_prevents_a_newer_touch(db: Database) -> None:
    member_id, first = sent_first_touch(db)
    with db.transaction() as uow:
        [attempt] = uow.dispatch_attempts.list_for_outbound(first)
        uow.dispatch_attempts.update(attempt.model_copy(update={
            "state": DispatchAttemptState.NOT_ACCEPTED, "reason_code": "X", "provider_message_id": None,
            "late_acceptance_provider_message_id": "fake-late", "late_acceptance_at": NOW, "version": attempt.version + 1,
        }), attempt.version)
    later = FrozenClock(LATER)
    scheduler(db, later).schedule(CAMPAIGN_ID, correlation_id="c")
    [claim] = claim_all(db, later)
    result = executor(db, later).execute(claim, correlation_id="c")
    assert result.outcome is ExecutionOutcome.BLOCKED and CampaignBlock.ACCEPTANCE_CONFLICT in result.reason_codes
    assert len(campaign_messages(db, member(db, member_id).lead_id or "")) == 1


def test_reply_hands_the_contact_to_the_conversation_workflow(db: Database) -> None:
    member_id, first = sent_first_touch(db)
    later = FrozenClock(LATER)
    scheduler(db, later).schedule(CAMPAIGN_ID, correlation_id="c")  # touch 2 is now open
    thread_id = reply(db, first)
    replied = member(db, member_id)
    assert (replied.status, replied.terminal_reason) == (M.REPLIED, "CUSTOMER_REPLIED") and thread_id == replied.thread_id
    with db.transaction() as uow:
        jobs = uow.campaign_jobs.list_for_member(member_id)
        plans = uow._tx.fetch_all("SELECT data FROM follow_up_plans")  # noqa: SLF001
        conversation = uow.conversations.get(conversation_id_for(thread_id))
    assert jobs[-1].status is CampaignJobStatus.SUPERSEDED
    assert '"CANCELLED"' in plans[0][0] and FollowUpCancelReason.REPLY_RECEIVED.value in plans[0][0]
    assert conversation is not None and conversation.status in (ConversationStatus.ACTIVE, ConversationStatus.OPERATOR_REVIEW)
    assert claim_all(db, later) == () and scheduler(db, later).schedule(CAMPAIGN_ID, correlation_id="c2").scheduled == ()


def test_reply_cancels_a_pending_campaign_draft(db: Database) -> None:
    member_id, first = sent_first_touch(db)
    second = draft_touch(db, at=LATER)
    reply(db, first)
    assert outbound(db, second.outbound_id or "").status is OutboundStatus.CANCELLED
    transport = FakeEmailTransport()
    blocked = send(dispatcher(db, transport, clock=FrozenClock(LATER)), second.outbound_id or "")
    assert blocked.outcome is DispatchOutcome.BLOCKED and transport.calls == []
    assert member(db, member_id).status is M.REPLIED


def test_unsubscribe_reply_suppresses_the_member(db: Database) -> None:
    member_id, first = sent_first_touch(db)
    reply(db, first, intent=LeadIntent.UNSUBSCRIBE, body="Please unsubscribe me.")
    assert member(db, member_id).status is M.SUPPRESSED  # suppression wins over REPLIED


def test_follow_up_touch_uses_the_plan_and_threads_as_a_reply(db: Database) -> None:
    member_id, first = sent_first_touch(db)
    second = draft_touch(db, at=LATER)
    transport = FakeEmailTransport()
    approve(db, second.outbound_id or "", LATER)
    assert send(dispatcher(db, transport, clock=FrozenClock(LATER)), second.outbound_id or "").outcome is DispatchOutcome.ACCEPTED
    [call] = transport.calls
    assert call.in_reply_to == outbound(db, first).rfc_message_id and call.subject.startswith("Re: ")
    lead_id = member(db, member_id).lead_id or ""
    with db.transaction() as uow:
        plan = uow.follow_ups.get_open_for_lead(lead_id)
    assert plan is not None and plan.status is FollowUpStatus.ACTIVE and plan.steps_sent == 1


# ---- Adversarial review regressions -------------------------------------------------------


def test_blocked_touch_is_not_reopened_by_later_scheduler_passes(db: Database) -> None:
    member_id, first = sent_first_touch(db)
    with db.transaction() as uow:
        [attempt] = uow.dispatch_attempts.list_for_outbound(first)
        uow.dispatch_attempts.update(attempt.model_copy(update={
            "state": DispatchAttemptState.NOT_ACCEPTED, "reason_code": "X", "provider_message_id": None,
            "late_acceptance_provider_message_id": "fake-late", "late_acceptance_at": NOW, "version": attempt.version + 1,
        }), attempt.version)
    later = FrozenClock(LATER)
    scheduler(db, later).schedule(CAMPAIGN_ID, correlation_id="c")
    [claim] = claim_all(db, later)
    assert executor(db, later).execute(claim, correlation_id="c").outcome is ExecutionOutcome.BLOCKED
    for tick in range(3):
        assert scheduler(db, FrozenClock(LATER + INTERVAL * (tick + 1))).schedule(CAMPAIGN_ID, correlation_id=f"t{tick}").scheduled == ()
    with db.transaction() as uow:
        assert [j.status for j in uow.campaign_jobs.list_for_member(member_id)] == [CampaignJobStatus.COMPLETED, CampaignJobStatus.BLOCKED]


def test_reply_while_the_first_touch_is_in_flight_is_recorded_honestly(db: Database) -> None:
    member_id = ready_campaign(db)
    first = draft_touch(db)
    approve(db, first.outbound_id or "")

    def customer_writes_meanwhile(_: object) -> None:
        process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.NEGOTIATION)),
                envelope("p-early", sender=PROSPECT, body="Hello, saw your site."))

    transport = FakeEmailTransport().script(FakeStep(FakeBehavior.ACCEPT, before=customer_writes_meanwhile))
    assert send(dispatcher(db, transport), first.outbound_id or "").outcome is DispatchOutcome.ACCEPTED
    handed_off = member(db, member_id)
    # The hand-off was already in flight; it is history (touch recorded), but the contact now
    # belongs to the conversation workflow and the campaign never resumes for them.
    assert (handed_off.status, handed_off.touch_count) == (M.REPLIED, 1)
    with db.transaction() as uow:
        assert uow.follow_ups.get_open_for_lead(handed_off.lead_id or "") is None
    assert scheduler(db, FrozenClock(LATER)).schedule(CAMPAIGN_ID, correlation_id="c").scheduled == ()


def test_duplicate_reply_delivery_changes_nothing(db: Database) -> None:
    member_id, first = sent_first_touch(db)
    reply(db, first, pid="p-dup")
    once = member(db, member_id)
    reply(db, first, pid="p-dup")
    assert member(db, member_id) == once
