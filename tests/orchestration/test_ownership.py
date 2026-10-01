"""Exactly one execution owner per lead, with the documented precedence, across the
Stage 6-13 state combinations."""

from datetime import timedelta
from pathlib import Path

from app.core.enums import CampaignMemberStatus, ConversationStatus, ObjectionCategory, TermType
from app.dispatch import FakeBehavior, FakeEmailTransport
from app.orchestration import ExecutionAction as A
from app.orchestration import ExecutionBlocker as B
from app.orchestration import ExecutionOwner as O
from app.orchestration import ExecutionSubsystem as S
from app.orchestration import SalesExecutionPlan
from app.persistence import Database
from tests.commercial.builders import LATER, approve, asks, presented, ready_draft, text
from tests.commercial.builders import suppress as add_dnc
from tests.inbound.builders import SENDER
from tests.operator.builders import make_escalation
from app.operator import ApproveProposal, MarkProposalPresented
from tests.orchestration.builders import approve_qualification as w_approve_qualification
from tests.orchestration.builders import create_opportunity as w_create_opportunity
from tests.orchestration.builders import prepare_proposal as w_prepare_proposal
from tests.orchestration.builders import revision_command as w_revision_command
from tests.orchestration.builders import (
    approve_pending,
    customer_replies,
    enrolled,
    orchestrator,
    quiet_message,
    reject_pending,
    suppress,
    world,
)
from tests.pipeline.builders import (
    approve_qualification,
    create_opportunity,
    lead,
    mark_lost,
    opportunity_lead,
    qualified_lead,
    qualifying_lead,
)


def plan_of(db: Database, lead_id: str, **kwargs: object) -> SalesExecutionPlan:
    return orchestrator(db, **kwargs).plan(lead_id)  # type: ignore[arg-type]


def assert_single_owner(plan: SalesExecutionPlan) -> None:
    assert isinstance(plan.owner, O)
    assert plan.requires_operator == (plan.owner is O.OPERATOR)
    assert plan.requires_customer == (plan.owner is O.CUSTOMER)
    assert not (plan.executable and (plan.requires_operator or plan.requires_customer))


# ---- Safety first: DNC, terminal, dispatch uncertainty, escalation ---------------------------------


def test_dnc_wins_over_an_active_campaign_conversation_and_proposal(db: Database) -> None:
    lead_id, _ = presented(db)
    reject_pending(db)
    add_dnc(db, SENDER)
    plan = plan_of(db, lead_id)
    assert (plan.owner, plan.action, plan.subsystem) == (O.NONE, A.NO_AUTOMATION, S.NONE)
    assert plan.blockers[0] is B.DNC and not plan.executable
    assert_single_owner(plan)


def test_a_closed_lead_has_no_owner(db: Database) -> None:
    lead_id = qualifying_lead(db)
    reject_pending(db)
    mark_lost(db, lead_id)
    plan = plan_of(db, lead_id)
    assert (plan.owner, plan.action, plan.blockers) == (O.NONE, A.NO_ACTION, (B.LEAD_CLOSED,))
    assert plan.reasons == ("LEAD_CLOSED:LOST",)


def test_unresolved_dispatch_takes_ownership_before_everything_but_dnc(db_path: Path) -> None:
    w = world(db_path, transport=FakeEmailTransport().script(FakeBehavior.ACCEPT_THEN_LOSE_RESPONSE))
    enrolled(w)
    w.execute()
    approve_pending(w)
    sent = w.execute(dispatch=True)
    assert sent.subsystem_outcome == "UNKNOWN"
    plan = w.plan()
    assert (plan.owner, plan.action, plan.subsystem) == (O.DISPATCH_RECOVERY, A.RECONCILE_DISPATCH, S.DISPATCH)
    assert plan.executable and not plan.requires_operator
    assert len(plan.refs.outbound_ids) == 1 and B.UNRESOLVED_DISPATCH in plan.conditions
    suppress(w)
    assert w.plan().action is A.NO_AUTOMATION  # DNC still dominates
    w.app.stop()


def test_an_open_escalation_routes_to_the_operator(db: Database) -> None:
    result = make_escalation(db)
    assert result.lead_id is not None
    plan = plan_of(db, result.lead_id)
    assert (plan.owner, plan.action, plan.subsystem) == (O.OPERATOR, A.ESCALATION_REVIEW, S.ESCALATION)
    assert plan.blockers == (B.ESCALATION_OPEN,) and plan.refs.escalation_ids == (result.escalation_id,)
    assert "RESOLVE_ESCALATION" in plan.operator_commands


# ---- Campaign before the reply, conversation after it ------------------------------------------------


def test_campaign_owns_before_the_reply_and_loses_ownership_for_good_after_it(db_path: Path) -> None:
    w = world(db_path)
    enrolled(w)
    first = w.plan()
    assert (first.owner, first.action, first.executable) == (O.CAMPAIGN, A.PREPARE_CAMPAIGN_TOUCH, True)
    w.execute()
    assert w.plan().action is A.REVIEW_CAMPAIGN_DRAFT
    approve_pending(w)
    assert (w.plan().owner, w.plan().action) == (O.CAMPAIGN, A.SEND_APPROVED_MESSAGE)
    w.execute(dispatch=True)
    waiting = w.plan()
    assert (waiting.owner, waiting.action, waiting.subsystem) == (O.CUSTOMER, A.WAIT_FOR_CUSTOMER, S.CAMPAIGN)
    assert waiting.waiting_until is not None  # the next touch's due time
    customer_replies(w, "p-reply")
    with w.db.transaction() as uow:
        member = uow.campaign_members.get(w.member_id or "")
    assert member is not None and member.status is CampaignMemberStatus.REPLIED
    after = w.plan()
    assert after.owner is not O.CAMPAIGN and after.subsystem is not S.CAMPAIGN
    # Even when the next touch would have been due, the old membership is history only.
    w.advance(timedelta(days=30))
    assert w.plan().owner is not O.CAMPAIGN
    w.app.stop()


def test_reply_draft_then_send_then_customer_wait_or_follow_up(db_path: Path) -> None:
    w = world(db_path)
    enrolled(w)
    w.execute()
    approve_pending(w)
    w.execute(dispatch=True)
    customer_replies(w, "p-reply")
    assert (w.plan().owner, w.plan().action, w.plan().subsystem) == (O.OPERATOR, A.REVIEW_REPLY_DRAFT, S.CONVERSATION)
    approve_pending(w)
    assert (w.plan().owner, w.plan().action) == (O.CONVERSATION, A.SEND_APPROVED_MESSAGE)
    w.execute(dispatch=True)
    # Qualification facts were all extracted: an operator review now supersedes the conversation.
    review = w.plan()
    assert (review.owner, review.action, review.subsystem) == (O.OPERATOR, A.REVIEW_QUALIFICATION, S.PIPELINE)
    assert B.WAITING_FOR_CUSTOMER in review.conditions  # the conversation waits meanwhile
    w.app.stop()


def test_customer_wrote_last_without_a_draft_needs_a_human(db: Database) -> None:
    lead_id = qualifying_lead(db, facts={"need": "Automate invoice matching"})  # required facts still missing
    reject_pending(db)
    plan = plan_of(db, lead_id)
    assert (plan.owner, plan.action, plan.subsystem) == (O.OPERATOR, A.QUALIFY_LEAD, S.PIPELINE)
    assert B.QUALIFICATION_INCOMPLETE in plan.blockers and not plan.executable


def test_waiting_conversation_without_follow_up_room_is_owned_by_the_customer(db_path: Path) -> None:
    w = world(db_path)
    enrolled(w)
    w.execute()
    approve_pending(w)
    w.execute(dispatch=True)
    customer_replies(w, "p-reply")
    approve_pending(w)
    w.execute(dispatch=True)
    w_approve_qualification(w)
    w_create_opportunity(w)
    assert (w.plan().owner, w.plan().action) == (O.OPERATOR, A.PREPARE_PROPOSAL)
    w_prepare_proposal(w)
    w_revision_command(w, ApproveProposal, "cmd-ap")
    w_revision_command(w, MarkProposalPresented, "cmd-pr")
    # Waiting for the proposal decision, the Stage 9 policy still permits a follow-up.
    follow_up = w.plan()
    assert (follow_up.owner, follow_up.action, follow_up.executable) == (O.CONVERSATION, A.PROCESS_FOLLOW_UP, True)
    assert w.execute().subsystem_outcome == "SCHEDULED"
    waiting = w.plan()
    assert (waiting.owner, waiting.action, waiting.subsystem) == (O.CUSTOMER, A.WAIT_FOR_CUSTOMER, S.COMMERCIAL)
    assert waiting.waiting_until is not None  # the scheduled follow-up's due time
    w.app.stop()


# ---- Pipeline and commercial: operator decisions ---------------------------------------------------


def test_qualification_review_and_conflict_route_to_the_operator(db: Database) -> None:
    lead_id = qualifying_lead(db)
    reject_pending(db)
    plan = plan_of(db, lead_id)
    assert (plan.owner, plan.action, plan.subsystem) == (O.OPERATOR, A.REVIEW_QUALIFICATION, S.PIPELINE)
    assert "APPROVE_QUALIFICATION" in plan.operator_commands


def test_a_qualified_lead_needs_an_opportunity_decision(db: Database) -> None:
    lead_id = qualified_lead(db)
    reject_pending(db)
    plan = plan_of(db, lead_id)
    assert (plan.owner, plan.action, plan.blockers) == (O.OPERATOR, A.CREATE_OPPORTUNITY, (B.OPPORTUNITY_REQUIRED,))


def test_proposal_preparation_review_and_presentation(db: Database) -> None:
    lead_id = opportunity_lead(db)
    reject_pending(db)
    assert (plan_of(db, lead_id).action, plan_of(db, lead_id).subsystem) == (A.PREPARE_PROPOSAL, S.COMMERCIAL)
    from tests.commercial.builders import create_proposal, set_term, update
    from tests.pipeline.builders import active_opportunity
    opportunity_id = active_opportunity(db, lead_id).opportunity_id
    create_proposal(db, opportunity_id)
    plan = plan_of(db, lead_id)
    assert plan.action is A.PREPARE_PROPOSAL and B.COMMERCIAL_INPUT_MISSING in plan.blockers
    assert any(s.startswith("commercial:") for s in plan.sources) and plan.refs.revision_id is not None
    update(db, opportunity_id)
    set_term(db, opportunity_id, TermType.PAYMENT_TERM, text("NET_30"))
    assert plan_of(db, lead_id).action is A.REVIEW_PROPOSAL
    approve(db, opportunity_id)
    assert plan_of(db, lead_id).action is A.PRESENT_PROPOSAL


def test_term_request_objection_and_acceptance_signal(db: Database) -> None:
    lead_id, opportunity_id = presented(db)
    reject_pending(db)
    quiet_message(db, "p-term", asks((TermType.PAYMENT_TERM, text("NET_60"))), at=LATER)
    plan = plan_of(db, lead_id)
    assert (plan.owner, plan.action) == (O.OPERATOR, A.REVIEW_TERM_REQUEST)
    # An open request on a presented revision also makes the revision stale (Stage 13).
    assert plan.blockers == (B.TERM_REQUEST_OPEN, B.PROPOSAL_REVISION_REQUIRED)
    assert len(plan.refs.request_ids) == 1 and "commercial:UNAPPROVED_TERM_REQUEST" not in plan.sources


def test_open_objection_on_a_presented_proposal(db: Database) -> None:
    lead_id, _ = presented(db)
    reject_pending(db)
    quiet_message(db, "p-obj", asks(objections=((ObjectionCategory.PRICE, "Too expensive"),)), at=LATER)
    plan = plan_of(db, lead_id)
    assert (plan.owner, plan.action) == (O.OPERATOR, A.HANDLE_OBJECTION)
    assert B.OBJECTION_OPEN in plan.blockers and plan.refs.objection_ids


def test_acceptance_signal_needs_an_operator_and_never_closes_the_lead(db: Database) -> None:
    lead_id, _ = presented(db)
    reject_pending(db)
    quiet_message(db, "p-yes", asks(accept=True), at=LATER)
    plan = plan_of(db, lead_id)
    assert (plan.owner, plan.action, plan.blockers) == (O.OPERATOR, A.CONFIRM_ACCEPTANCE, (B.ACCEPTANCE_REQUIRES_OPERATOR,))
    assert lead(db, lead_id).close_reason is None and plan.refs.signal_ids


def test_presented_proposal_without_signals_waits_for_the_customer(db: Database) -> None:
    lead_id, _ = presented(db)
    reject_pending(db)
    plan = plan_of(db, lead_id)
    # The conversation is ACTIVE (the customer wrote last, nothing drafted), so the commercial
    # wait does not yield to a follow-up: the customer owns the decision.
    assert (plan.owner, plan.action, plan.subsystem) == (O.CUSTOMER, A.WAIT_FOR_CUSTOMER, S.COMMERCIAL)


def test_won_and_lost_leads_are_closed(db: Database) -> None:
    lead_id, _ = ready_draft(db)
    reject_pending(db)
    mark_lost(db, lead_id)
    assert plan_of(db, lead_id).action is A.NO_ACTION


def test_owner_precedence_has_exactly_one_owner_in_every_state(db: Database) -> None:
    lead_id = qualifying_lead(db)
    states = [plan_of(db, lead_id)]
    reject_pending(db)
    states.append(plan_of(db, lead_id))
    approve_qualification(db, lead_id)
    states.append(plan_of(db, lead_id))
    create_opportunity(db, lead_id)
    states.append(plan_of(db, lead_id))
    mark_lost(db, lead_id)
    states.append(plan_of(db, lead_id))
    for plan in states:
        assert_single_owner(plan)
    assert [p.action for p in states] == [A.REVIEW_REPLY_DRAFT, A.REVIEW_QUALIFICATION, A.CREATE_OPPORTUNITY,
                                          A.PREPARE_PROPOSAL, A.NO_ACTION]


def test_conversation_status_is_reported_in_the_view(db: Database) -> None:
    lead_id = qualifying_lead(db)
    view = orchestrator(db).view(lead_id)
    assert view.conversation_status is ConversationStatus.ACTIVE and view.pending_review_outbound_ids
    assert view.plan.action is A.REVIEW_REPLY_DRAFT and view.suppressed is False
    assert view.lead_id == lead_id and view.qualification_status.value == "READY_FOR_REVIEW"
