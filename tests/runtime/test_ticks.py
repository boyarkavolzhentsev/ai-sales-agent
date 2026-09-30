"""Worker passes, the inbound entry point and the coordinator, through the runtime only."""

from datetime import timedelta
from pathlib import Path

import pytest

from app.core.enums import (
    CampaignMemberStatus,
    ConversationStatus,
    DNCReason,
    DNCScope,
    FollowUpJobStatus,
    LeadIntent,
    OutboundStatus,
    RefKind,
)
from app.core.models import DoNotContactEntry, EntityRef
from app.dispatch import FakeBehavior, FakeEmailTransport, FakeReconciler, ReconciliationResult as ProviderLookup
from app.llm import LLMTask
from app.operator import PauseCampaign
from app.persistence import FrozenClock
from app.runtime import Adapters, PhaseStatus, SalesAgentRuntime
from tests.campaign.builders import CAMPAIGN_ID, PROSPECT, add_campaign, add_prospect
from tests.conversation.builders import FIRST_DUE
from tests.inbound.builders import NOW, ScriptedTransport, classification, envelope, happy_transport
from tests.operator.builders import AS_ALICE, FakeAuthenticator, approve_command
from tests.runtime.builders import activate_campaign, app_db, fake_adapters, runtime

LATER = FIRST_DUE + timedelta(minutes=1)


def started(db_path: Path, *, clock: FrozenClock | None = None, adapters: Adapters | None = None) -> SalesAgentRuntime:
    app = runtime(db_path, clock=clock or FrozenClock(NOW), adapters=adapters)
    app.start()
    return app


def with_campaign(app: SalesAgentRuntime) -> str:
    db = app_db(app)
    add_campaign(db)
    contact = add_prospect(db)
    activate_campaign(app)
    result = app.services.campaign_enroller.enroll(CAMPAIGN_ID, contact.contact_id, correlation_id="enroll")
    assert result.member_id is not None
    return result.member_id


def approve_all(app: SalesAgentRuntime) -> list[str]:
    service = app.services.operator
    ids = [d.outbound_id for d in service.list_pending_drafts(AS_ALICE)]
    for outbound_id in ids:
        service.approve_draft(AS_ALICE, approve_command(service.get_draft(AS_ALICE, outbound_id), f"cmd-approve-{outbound_id}"))
    return ids


def member_status(app: SalesAgentRuntime, member_id: str) -> CampaignMemberStatus:
    with app_db(app).transaction() as uow:
        member = uow.campaign_members.get(member_id)
    assert member is not None
    return member.status


def outbound_status(app: SalesAgentRuntime, outbound_id: str) -> OutboundStatus:
    with app_db(app).transaction() as uow:
        message = uow.outbound.get(outbound_id)
    assert message is not None
    return message.status


# ---- Campaign tick -----------------------------------------------------------------------


def test_campaign_tick_drafts_once_and_never_dispatches(db_path: Path) -> None:
    transport = FakeEmailTransport()
    app = started(db_path, adapters=fake_adapters(transport))
    member_id = with_campaign(app)
    first = app.campaign_tick()
    assert (first.status, first.scheduled, first.claimed, first.drafted) == (PhaseStatus.OK, 1, 1, 1)
    again = app.campaign_tick()
    assert (again.scheduled, again.claimed, again.drafted) == (0, 0, 0)
    assert member_status(app, member_id) is CampaignMemberStatus.DRAFTED and transport.calls == []
    [draft] = app.services.operator.list_pending_drafts(AS_ALICE)
    assert draft.status is OutboundStatus.DRAFTED  # never approved by the runtime
    app.stop()


def test_paused_campaign_work_does_not_escape(db_path: Path) -> None:
    app = started(db_path)
    member_id = with_campaign(app)
    with app_db(app).transaction() as uow:
        campaign = uow.campaigns.get(CAMPAIGN_ID)
    assert campaign is not None
    app.services.operator.pause_campaign(AS_ALICE, PauseCampaign(command_id="cmd-pause", correlation_id="c",
                                                                 campaign_id=CAMPAIGN_ID, expected_campaign_version=campaign.version))
    result = app.campaign_tick()
    assert (result.scheduled, result.claimed, result.drafted) == (0, 0, 0)
    assert member_status(app, member_id) is CampaignMemberStatus.ENROLLED
    app.stop()


def test_dnc_wins_over_the_campaign_tick(db_path: Path) -> None:
    app = started(db_path)
    member_id = with_campaign(app)
    with app_db(app).transaction() as uow:
        uow.dnc.add(DoNotContactEntry(entry_id="dnc-1", scope=DNCScope.EMAIL, value=PROSPECT, reason=DNCReason.OPERATOR,
                                      source_ref=EntityRef(kind=RefKind.OPERATOR_COMMAND, id="c"), created_by="op", created_at=NOW))
    result = app.campaign_tick()
    assert (result.drafted, result.blocked) == (0, 1) and member_status(app, member_id) is CampaignMemberStatus.SUPPRESSED
    app.stop()


# ---- Dispatch tick: approved work only -------------------------------------------------------


def test_dispatch_tick_sends_only_operator_approved_messages(db_path: Path) -> None:
    transport = FakeEmailTransport()
    app = started(db_path, adapters=fake_adapters(transport))
    member_id = with_campaign(app)
    app.campaign_tick()
    nothing = app.dispatch_tick()
    assert (nothing.processed, transport.calls) == (0, [])  # a draft is not approved work
    [outbound_id] = approve_all(app)
    sent = app.dispatch_tick()
    assert (sent.processed, sent.accepted) == (1, 1) and outbound_status(app, outbound_id) is OutboundStatus.SENT
    assert member_status(app, member_id) is CampaignMemberStatus.WAITING
    assert app.dispatch_tick().processed == 0 and len(transport.calls) == 1  # a replayed tick sends nothing
    app.stop()


# ---- Reconciliation tick --------------------------------------------------------------------


def test_reconciliation_tick_applies_positive_evidence_once(db_path: Path) -> None:
    transport = FakeEmailTransport().script(FakeBehavior.ACCEPT_THEN_LOSE_RESPONSE)
    app = started(db_path, adapters=fake_adapters(transport))
    member_id = with_campaign(app)
    app.campaign_tick()
    [outbound_id] = approve_all(app)
    assert app.dispatch_tick().unknown == 1 and member_status(app, member_id) is CampaignMemberStatus.DISPATCHING
    assert app.campaign_tick().drafted == 0  # nothing new while the touch is in doubt
    result = app.reconcile()
    assert (result.processed, result.accepted, result.unresolved) == (1, 1, 0)
    assert outbound_status(app, outbound_id) is OutboundStatus.SENT and member_status(app, member_id) is CampaignMemberStatus.WAITING
    assert app.reconcile().processed == 0 and len(transport.calls) == 1  # replay: nothing left, nothing resent
    app.stop()


class BrokenReconciler:
    def lookup(self, request_id: str, rfc_message_id: str) -> ProviderLookup:
        raise ConnectionError("provider lookup down")


@pytest.mark.parametrize("label", ["not-found", "error"])
def test_not_found_stays_unresolved_and_adapter_errors_are_contained(db_path: Path, label: str) -> None:
    transport = FakeEmailTransport().script(FakeBehavior.TIMEOUT)  # nothing reached the provider
    reconciler = FakeReconciler(transport) if label == "not-found" else BrokenReconciler()
    app = started(db_path, adapters=Adapters(email_transport=transport, reconciler=reconciler, authenticator=FakeAuthenticator()))
    with_campaign(app)
    app.campaign_tick()
    [outbound_id] = approve_all(app)
    calls_before = len(transport.calls)
    app.dispatch_tick()
    result = app.reconcile()
    assert (result.status, result.processed, result.unresolved) == (PhaseStatus.OK, 1, 1)
    assert outbound_status(app, outbound_id) is OutboundStatus.SENDING
    assert app.dispatch_tick().processed == 0 and len(transport.calls) == calls_before + 1  # never resent
    app.stop()


# ---- Follow-up tick and inbound entry point --------------------------------------------------


def replied_conversation(app: SalesAgentRuntime) -> str:
    result = app.handle_inbound(envelope("p-1"), correlation_id="corr-in")
    assert result.outbound_id is not None
    approve_all(app)
    assert app.dispatch_tick().accepted == 1
    return result.thread_id


def test_follow_up_tick_drafts_once_when_due(db_path: Path) -> None:
    clock = FrozenClock(NOW)
    app = started(db_path, clock=clock, adapters=fake_adapters(llm=happy_transport()))
    replied_conversation(app)
    early = app.follow_up_tick()
    assert (early.scheduled, early.claimed, early.drafted) == (1, 0, 0)  # scheduled, not yet due
    clock.set(LATER)
    due = app.follow_up_tick()
    assert (due.claimed, due.drafted) == (1, 1)
    assert app.follow_up_tick().drafted == 0
    assert len(app.services.operator.list_pending_drafts(AS_ALICE)) == 1
    app.stop()


def test_reply_before_the_due_follow_up_supersedes_it(db_path: Path) -> None:
    clock = FrozenClock(NOW)
    llm = happy_transport()
    llm.script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.NEGOTIATION))
    app = started(db_path, clock=clock, adapters=fake_adapters(llm=llm))
    thread_id = replied_conversation(app)
    app.follow_up_tick()
    app.handle_inbound(envelope("p-2", body="One more question.", in_reply_to="<p-1@prospect.example>"), correlation_id="corr-2")
    clock.set(LATER)
    assert app.follow_up_tick().drafted == 0
    with app_db(app).transaction() as uow:
        conversation = uow.conversations.get_by_thread(thread_id)
        assert conversation is not None
        jobs = uow.follow_up_jobs.list_for_conversation(conversation.conversation_id)
    assert [j.status for j in jobs] == [FollowUpJobStatus.SUPERSEDED]
    app.stop()


def test_inbound_entry_point_hands_campaign_replies_to_the_conversation(db_path: Path) -> None:
    llm = ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.NEGOTIATION))
    app = started(db_path, adapters=fake_adapters(llm=llm))
    member_id = with_campaign(app)
    app.campaign_tick()
    [first] = approve_all(app)
    app.dispatch_tick()
    with app_db(app).transaction() as uow:
        message = uow.outbound.get(first)
    assert message is not None and message.rfc_message_id is not None
    reply = envelope("p-reply", sender=PROSPECT, body="Tell me more.", in_reply_to=message.rfc_message_id)
    first_result = app.handle_inbound(reply, correlation_id="corr-reply")
    again = app.handle_inbound(reply, correlation_id="corr-reply-again")  # duplicate delivery
    assert again.replayed or again.duplicate
    assert member_status(app, member_id) is CampaignMemberStatus.REPLIED
    with app_db(app).transaction() as uow:
        conversation = uow.conversations.get_by_thread(first_result.thread_id)
    assert conversation is not None and conversation.status in (ConversationStatus.ACTIVE, ConversationStatus.OPERATOR_REVIEW)
    app.stop()


# ---- Coordinator ------------------------------------------------------------------------------


def test_one_failing_phase_does_not_affect_the_others(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FrozenClock(NOW)
    app = started(db_path, clock=clock, adapters=fake_adapters(llm=happy_transport()))
    replied_conversation(app)
    member_id = with_campaign(app)
    clock.set(LATER)

    def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("campaign executor bug")

    monkeypatch.setattr(app.services.campaign_executor, "execute", broken)
    result = app.tick()
    assert not result.ok and result.campaign.status is PhaseStatus.ERROR
    assert [e.error_type for e in result.campaign.errors] == ["RuntimeError"] and result.campaign.claimed == 1
    assert result.follow_up.status is PhaseStatus.OK and result.follow_up.drafted == 1
    assert result.reconciliation.status is PhaseStatus.OK
    assert member_status(app, member_id) is CampaignMemberStatus.ENROLLED  # the failed job rolled back
    monkeypatch.undo()
    clock.set(LATER + timedelta(hours=1))  # the stuck claim's lease expired: recovered by the next tick
    recovered = app.tick()
    assert recovered.ok and recovered.campaign.drafted == 1 and member_status(app, member_id) is CampaignMemberStatus.DRAFTED
    app.stop()


def test_repeated_ticks_are_safe(db_path: Path) -> None:
    app = started(db_path)
    with_campaign(app)
    results = [app.tick() for _ in range(3)]
    assert [r.campaign.drafted for r in results] == [1, 0, 0] and all(r.ok for r in results)
    assert all(r.dispatch is None for r in results)  # dispatch only when explicitly requested
    app.stop()
