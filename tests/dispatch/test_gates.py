"""C. Pre-dispatch gates, re-evaluated at claim time with the injected clock."""

from datetime import timedelta

from app.core.enums import (
    CampaignStatus,
    CloseReason,
    DNCReason,
    DNCScope,
    KnowledgeDomain,
    LeadIntent,
    LeadStage,
    LeadStatus,
    OutboundStatus,
    RefKind,
)
from app.core.models import Campaign, CampaignTargetFilter, DoNotContactEntry, EntityRef
from app.dispatch import DispatchOutcome, FakeEmailTransport
from app.llm import LLMTask
from app.operator import BlockCode
from app.persistence import Database, FrozenClock
from app.policy import KillSwitchState, PolicyReason
from tests.dispatch.builders import approved_reply, dispatcher, one_slot, send, snapshot, state
from tests.inbound.builders import NOW, SENDER, ScriptedTransport, classification, envelope, process
from tests.inbound.test_threads_and_transitions import outbound_history
from tests.operator.builders import AS_ALICE, operator, ownership_command


def blocked_codes(db: Database, outbound_id: str, clock: FrozenClock | None = None, **config: object) -> set[str]:
    transport = FakeEmailTransport()
    before = snapshot(db)
    result = send(dispatcher(db, transport, clock=clock, **config), outbound_id)
    assert result.outcome is DispatchOutcome.BLOCKED, result
    assert transport.calls == [] and snapshot(db) == before
    assert status_is_unchanged(db, outbound_id)
    return set(result.reason_codes)


def status_is_unchanged(db: Database, outbound_id: str) -> bool:
    current = state(db, outbound_id)
    return current.attempts == [] and current.reservations == [] and current.outbound.send_permit_id is None


def set_lead(db: Database, lead_id: str, **changes: object) -> None:
    with db.transaction() as uow:
        lead = uow.leads.get(lead_id)
        assert lead is not None
        uow.leads.update(lead.model_copy(update=changes | {"version": lead.version + 1}), lead.version)


def test_dnc_blocks_dispatch(db: Database) -> None:
    outbound_id = approved_reply(db)
    with db.transaction() as uow:
        uow.dnc.add(DoNotContactEntry(entry_id="dnc-1", scope=DNCScope.EMAIL, value=SENDER, reason=DNCReason.OPERATOR,
                                      source_ref=EntityRef(kind=RefKind.OPERATOR_COMMAND, id="c"), created_by="op", created_at=NOW))
    assert {BlockCode.CONTACT_SUPPRESSED.value, PolicyReason.DNC_EMAIL.value} <= blocked_codes(db, outbound_id)


def test_closed_lead_blocks_dispatch(db: Database) -> None:
    outbound_id = approved_reply(db)
    set_lead(db, state(db, outbound_id).outbound.lead_id, stage=LeadStage.CLOSED, close_reason=CloseReason.LOST)
    assert BlockCode.LEAD_CLOSED.value in blocked_codes(db, outbound_id)


def test_held_lead_blocks_dispatch(db: Database) -> None:
    outbound_id = approved_reply(db)
    set_lead(db, state(db, outbound_id).outbound.lead_id, status=LeadStatus.ON_HOLD)
    assert blocked_codes(db, outbound_id) == {BlockCode.LEAD_ON_HOLD.value}


def test_operator_owned_lead_may_dispatch_its_human_approved_reply(db: Database) -> None:
    outbound_id = approved_reply(db)
    service = operator(db)
    lead = service.get_lead(AS_ALICE, state(db, outbound_id).outbound.lead_id)
    service.take_ownership(AS_ALICE, ownership_command(lead.lead_id, lead.version))
    assert send(dispatcher(db), outbound_id).outcome is DispatchOutcome.ACCEPTED


def test_paused_campaign_blocks_dispatch(db: Database) -> None:
    outbound_history(db)
    outbound_id = approved_reply(db, in_reply_to="<out-1@ourco.example>")
    campaign = Campaign(campaign_id="camp-x", name="Sample", status=CampaignStatus.PAUSED, activated_by="op",
                        target_filter=CampaignTargetFilter(), allowed_knowledge_domains=(KnowledgeDomain.COMPANY,),
                        sending_mailbox="sales@ourco.example", max_follow_ups=1, min_interval_between_follow_ups=timedelta(days=2),
                        created_by="op", created_at=NOW, updated_at=NOW)
    with db.transaction() as uow:
        uow.campaigns.add(campaign)
    set_lead(db, "lead-out", campaign_id="camp-x")
    assert blocked_codes(db, outbound_id) == {BlockCode.CAMPAIGN_INACTIVE.value}


def test_expired_evidence_blocks_dispatch_by_the_injected_clock(db: Database) -> None:
    outbound_id = approved_reply(db)
    later = FrozenClock(NOW.replace(month=8, day=3, hour=10))  # Monday; price list review_by is 2026-08-01
    assert blocked_codes(db, outbound_id, clock=later) == {BlockCode.EVIDENCE_UNUSABLE.value}


def test_newer_customer_message_blocks_dispatch(db: Database) -> None:
    outbound_id = approved_reply(db)
    process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.NEGOTIATION)),
            envelope("p-2", body="Wait, can we negotiate?", in_reply_to="<p-1@prospect.example>"))
    assert BlockCode.NEWER_INBOUND_MESSAGE.value in blocked_codes(db, outbound_id)


def test_outside_sending_window_holds_dispatch(db: Database) -> None:
    outbound_id = approved_reply(db)
    evening = FrozenClock(NOW.replace(hour=17))  # 20:00 in Kyiv
    assert blocked_codes(db, outbound_id, clock=evening) == {PolicyReason.OUTSIDE_SENDING_WINDOW.value}


def test_kill_switch_holds_dispatch(db: Database) -> None:
    outbound_id = approved_reply(db)
    stop = KillSwitchState(enabled=True, reason="incident", changed_at=NOW, changed_by="ops")
    assert blocked_codes(db, outbound_id, kill_switch=stop) == {PolicyReason.KILL_SWITCH.value}


def test_quota_exhaustion_blocks_dispatch(db: Database) -> None:
    first = approved_reply(db, "p-1")
    second = approved_reply(db, "p-2", sender="other@elsewhere.example")
    assert send(dispatcher(db, limits=one_slot()), first).outcome is DispatchOutcome.ACCEPTED
    assert blocked_codes(db, second, limits=one_slot()) == {PolicyReason.GLOBAL_DAILY_LIMIT.value}
    assert state(db, second).outbound.status is OutboundStatus.OPERATOR_APPROVED


def test_blocked_then_allowed_later_dispatches_normally(db: Database) -> None:
    outbound_id = approved_reply(db)
    evening = FrozenClock(NOW.replace(hour=17))
    blocked_codes(db, outbound_id, clock=evening)
    assert send(dispatcher(db), outbound_id, "corr-2").outcome is DispatchOutcome.ACCEPTED
    assert len(state(db, outbound_id).attempts) == 1
