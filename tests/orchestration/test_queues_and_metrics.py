"""Queues and metrics are derived from the canonical plan; batch passes are bounded,
deterministic, fair (oldest first within a priority) and never act twice on a lead."""

from datetime import timedelta
from pathlib import Path

from app.orchestration import ExecutionAction as A
from app.orchestration import ExecutionOutcome as X
from app.orchestration import ExecutionOwner as O
from app.orchestration import ExecutionQueue as Q
from app.orchestration.views import in_queue, order_key
from tests.campaign.builders import CAMPAIGN_ID, add_campaign, add_prospect
from tests.inbound.builders import envelope
from tests.orchestration.builders import World, suppress, world
from tests.runtime.builders import activate_campaign

EMAILS = ("ann@alpha.example", "bob@beta.example", "cid@gamma.example")


def campaign_with(w: World, *emails: str, gap: timedelta = timedelta(0)) -> list[str]:
    """Enroll each prospect (``gap`` apart); returns their lead ids in enrollment order."""
    add_campaign(w.db)
    activate_campaign(w.app)
    leads = []
    for email in emails:
        contact = add_prospect(w.db, email, company_name=None)
        result = w.app.services.campaign_enroller.enroll(CAMPAIGN_ID, contact.contact_id, correlation_id=f"enroll-{len(leads)}")
        with w.db.transaction() as uow:
            member = uow.campaign_members.get(result.member_id or "")
        assert member is not None and member.lead_id is not None
        leads.append(member.lead_id)
        w.advance(gap)
    return leads


def test_queue_membership_is_derived_from_each_plan(db_path: Path) -> None:
    w = world(db_path)
    ann, bob, cid = campaign_with(w, *EMAILS)
    w.lead_id = ann
    w.execute()  # ann: draft awaits review
    suppress(w, EMAILS[2])  # cid: do-not-contact
    plans = {p.lead_id: p for p in w.app.execution_queue(Q.ACTIONABLE)}
    assert set(plans) == {bob} and plans[bob].action is A.PREPARE_CAMPAIGN_TOUCH
    assert [p.lead_id for p in w.app.execution_queue(Q.OPERATOR)] == [ann]
    assert [p.lead_id for p in w.app.execution_queue(Q.BLOCKED)] == [cid]
    assert [p.lead_id for p in w.app.execution_queue(Q.UNOWNED)] == [cid]
    assert [p.lead_id for p in w.app.execution_queue(Q.CAMPAIGN)] == [bob]
    for which in Q:
        for plan in w.app.execution_queue(which):
            assert in_queue(which, plan) and plan == w.app.execution_plan(plan.lead_id).model_copy(
                update={"observed_at": plan.observed_at})
    w.app.stop()


def test_ordering_is_priority_then_oldest_activity_then_id_and_stable(db_path: Path) -> None:
    w = world(db_path)
    leads = campaign_with(w, *EMAILS, gap=timedelta(minutes=5))
    w.app.handle_inbound(envelope("p-in", sender="dora@delta.example"), correlation_id="corr-in")  # a reply draft to review
    actionable = w.app.execution_queue(Q.ACTIONABLE)
    assert [p.lead_id for p in actionable] == leads  # equal priority: the oldest first
    everything = [p for which in (Q.OPERATOR, Q.ACTIONABLE) for p in w.app.execution_queue(which)]
    assert everything[0].action is A.REVIEW_REPLY_DRAFT  # operator queue first in this listing
    combined = sorted(everything, key=order_key)
    assert combined[0].action is A.REVIEW_REPLY_DRAFT and combined[0].priority < actionable[0].priority
    again = w.app.execution_queue(Q.ACTIONABLE)  # deterministic: the same order every time
    assert [p.model_dump(exclude={"observed_at"}) for p in again] == [p.model_dump(exclude={"observed_at"}) for p in actionable]
    w.app.stop()


def test_a_pass_is_bounded_one_action_per_lead_and_fair(db_path: Path) -> None:
    w = world(db_path)
    leads = campaign_with(w, *EMAILS, gap=timedelta(minutes=5))
    first = w.app.execution_pass(limit=2)
    assert (first.considered, first.attempted) == (3, 2)
    assert [r.lead_id for r in first.results] == leads[:2]  # oldest first, each lead once
    assert all(r.outcome is X.EXECUTED and r.subsystem_outcome == "DRAFT_CREATED" for r in first.results)
    second = w.app.execution_pass(limit=2)
    assert (second.considered, second.attempted, [r.lead_id for r in second.results]) == (1, 1, leads[2:])
    third = w.app.execution_pass(limit=2)
    assert (third.considered, third.attempted) == (0, 0)  # every lead now waits for the operator
    assert w.transport.calls == []
    w.app.stop()


def test_a_pass_without_dispatch_leaves_approved_sends_alone(db_path: Path) -> None:
    from tests.orchestration.builders import approve_pending
    w = world(db_path)
    campaign_with(w, EMAILS[0])
    w.app.execution_pass()
    approve_pending(w)
    assert w.app.execution_pass().considered == 0 and w.transport.calls == []
    sent = w.app.execution_pass(dispatch_approved=True)
    assert (sent.attempted, sent.results[0].subsystem_outcome) == (1, "ACCEPTED") and len(w.transport.calls) == 1
    w.app.stop()


def test_metrics_count_owners_actions_and_waits(db_path: Path) -> None:
    w = world(db_path)
    ann, bob, cid = campaign_with(w, *EMAILS)
    w.lead_id = ann
    w.execute()
    suppress(w, EMAILS[2])
    metrics = w.app.execution_metrics()
    assert (metrics.open_leads, metrics.closed_leads) == (3, 0)
    assert metrics.by_owner == {O.CAMPAIGN: 1, O.NONE: 1, O.OPERATOR: 1}
    assert metrics.by_action == {A.NO_AUTOMATION: 1, A.PREPARE_CAMPAIGN_TOUCH: 1, A.REVIEW_CAMPAIGN_DRAFT: 1}
    assert (metrics.actionable, metrics.waiting_operator, metrics.waiting_customer) == (1, 1, 0)
    assert (metrics.blocked, metrics.recovery_required, metrics.no_action) == (1, 0, 1)
    w.app.stop()
