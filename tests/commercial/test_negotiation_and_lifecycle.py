"""Objections, acceptance/decline signals, terminal/reopen effects and the commercial
next action."""

from datetime import timedelta

import pytest

from app.commercial import CommercialHookStatus
from app.core.enums import (
    CommercialAction,
    LeadIntent,
    LeadStage,
    NextActionOwner,
    ObjectionCategory,
    ObjectionStatus,
    RevisionStatus,
    SignalKind,
    SignalStatus,
    TermRequestStatus,
    TermType,
)
from app.core.models import CommercialSignal, Objection
from app.operator import CommandRejectedError, DismissCommercialSignal, MarkProposalAccepted, MarkProposalDeclined
from app.commercial.fake import FakeCommercialExtractor
from app.llm import LLMTask
from app.persistence import Database, FrozenClock
from tests.commercial.builders import (
    LATER,
    approve,
    approve_request,
    asks,
    commercial,
    create_proposal,
    current,
    customer_message,
    opportunity_for,
    ops,
    present,
    presented,
    ready_draft,
    revision_command,
    revisions,
    set_term,
    suppress,
    text,
    update,
    update_objection,
)
from tests.conversation.builders import approve as approve_draft
from tests.dispatch.builders import dispatcher, send
from tests.inbound.builders import SENDER, ScriptedTransport, classification, envelope, happy_transport, process
from tests.operator.builders import AS_ALICE
from tests.pipeline.builders import lead, mark_lost, mark_won, reopen, start_negotiation


def objections(db: Database, opportunity_id: str) -> list[Objection]:
    with db.transaction() as uow:
        return uow.objections.list_for_opportunity(opportunity_id)


def signals(db: Database, opportunity_id: str) -> list[CommercialSignal]:
    with db.transaction() as uow:
        return uow.commercial_signals.list_for_opportunity(opportunity_id)


def action(db: Database, opportunity_id: str) -> tuple[NextActionOwner, CommercialAction]:
    view = commercial(db).view(opportunity_id)
    return view.next_action.owner, view.next_action.action


# ---- Objections -----------------------------------------------------------------------------------


def test_objections_are_recorded_once_per_category_and_message(db: Database) -> None:
    _, opportunity_id = presented(db)
    found = asks(objections=((ObjectionCategory.PRICE, "Budget is tight"), (ObjectionCategory.SECURITY, "Need a DPA")))
    result, _ = customer_message(db, "p-2", found)
    assert commercial(db, FakeCommercialExtractor(default=found)).record_inbound(result, correlation_id="r").status is CommercialHookStatus.REPLAYED
    recorded = objections(db, opportunity_id)
    assert sorted(o.category for o in recorded) == [ObjectionCategory.PRICE, ObjectionCategory.SECURITY]
    assert all(o.status is ObjectionStatus.OPEN and o.source_message_id == result.message_id for o in recorded)
    assert action(db, opportunity_id) == (NextActionOwner.OPERATOR, CommercialAction.HANDLE_OBJECTION)


def test_only_an_operator_resolves_an_objection_a_sent_reply_does_not(db: Database) -> None:
    _, opportunity_id = presented(db)
    result = process(db, happy_transport(), envelope("p-2", in_reply_to="<p-1@prospect.example>"))
    found = asks(objections=((ObjectionCategory.PRICE, "Too expensive"),))
    commercial(db, FakeCommercialExtractor(default=found)).record_inbound(result, correlation_id="c")
    [objection] = objections(db, opportunity_id)
    assert result.outbound_id is not None  # Stage 6 drafted our answer to that message
    approve_draft(db, result.outbound_id, FrozenClock(LATER))
    assert send(dispatcher(db, clock=FrozenClock(LATER)), result.outbound_id).outcome.value == "ACCEPTED"
    assert objections(db, opportunity_id)[0].status is ObjectionStatus.OPEN  # answering is not resolving
    update_objection(db, objection.objection_id, ObjectionStatus.ACKNOWLEDGED, "cmd-ack")
    update_objection(db, objection.objection_id, ObjectionStatus.RESOLVED, "cmd-resolve")
    resolved = objections(db, opportunity_id)[0]
    assert (resolved.status, resolved.resolved_by) == (ObjectionStatus.RESOLVED, "op-alice")
    with pytest.raises(CommandRejectedError):
        update_objection(db, objection.objection_id, ObjectionStatus.RESOLVED, "cmd-resolve-again")


# ---- Signals --------------------------------------------------------------------------------------


def test_a_yes_is_an_acceptance_signal_never_a_won_lead(db: Database) -> None:
    lead_id, opportunity_id = presented(db)
    customer_message(db, "p-2", asks(accept=True))
    [signal] = signals(db, opportunity_id)
    assert (signal.kind, signal.status) == (SignalKind.ACCEPTANCE, SignalStatus.OPEN)
    assert lead(db, lead_id).stage is LeadStage.OPPORTUNITY and current(db, opportunity_id).status is RevisionStatus.PRESENTED
    assert action(db, opportunity_id) == (NextActionOwner.OPERATOR, CommercialAction.CONFIRM_ACCEPTANCE)
    revision_command(db, opportunity_id, MarkProposalAccepted, "cmd-accept")
    assert signals(db, opportunity_id)[0].status is SignalStatus.CONFIRMED
    assert action(db, opportunity_id) == (NextActionOwner.OPERATOR, CommercialAction.COMPLETE_WON)
    assert lead(db, lead_id).stage is LeadStage.OPPORTUNITY  # WON is still the operator's separate decision


def test_a_no_is_a_decline_signal_never_a_lost_lead(db: Database) -> None:
    lead_id, opportunity_id = presented(db)
    customer_message(db, "p-2", asks(decline=True))
    assert action(db, opportunity_id) == (NextActionOwner.OPERATOR, CommercialAction.DECIDE_LOSS)
    assert lead(db, lead_id).stage is LeadStage.OPPORTUNITY
    revision_command(db, opportunity_id, MarkProposalDeclined, "cmd-decline")
    assert current(db, opportunity_id).status is RevisionStatus.DECLINED and lead(db, lead_id).stage is LeadStage.OPPORTUNITY
    mark_lost(db, lead_id)  # a separate operator decision
    assert lead(db, lead_id).close_reason is not None


def test_a_newer_message_supersedes_and_an_older_replay_never_outranks_it(db: Database) -> None:
    _, opportunity_id = presented(db)
    customer_message(db, "p-yes", asks(accept=True), received_at=LATER)
    customer_message(db, "p-no", asks(decline=True), received_at=LATER + timedelta(minutes=5))
    by_kind = {s.kind: s.status for s in signals(db, opportunity_id)}
    assert by_kind == {SignalKind.ACCEPTANCE: SignalStatus.SUPERSEDED, SignalKind.DECLINE: SignalStatus.OPEN}
    customer_message(db, "p-old-yes", asks(accept=True), received_at=LATER + timedelta(minutes=1))  # arrives late
    open_ = [s for s in signals(db, opportunity_id) if s.status is SignalStatus.OPEN]
    assert [s.kind for s in open_] == [SignalKind.DECLINE]


def test_an_operator_may_dismiss_a_signal(db: Database) -> None:
    _, opportunity_id = presented(db)
    customer_message(db, "p-2", asks(accept=True))
    [signal] = signals(db, opportunity_id)
    ops(db).dismiss_commercial_signal(AS_ALICE, DismissCommercialSignal(
        command_id="cmd-dismiss", correlation_id="c", signal_id=signal.signal_id, expected_signal_version=signal.version))
    assert signals(db, opportunity_id)[0].status is SignalStatus.DISMISSED
    assert action(db, opportunity_id) == (NextActionOwner.CUSTOMER, CommercialAction.WAIT_FOR_CUSTOMER_DECISION)


def test_an_extraction_failure_never_fails_inbound(db: Database) -> None:
    _, opportunity_id = presented(db)
    result = process(db, happy_transport(), envelope("p-2", in_reply_to="<p-1@prospect.example>"))
    broken = FakeCommercialExtractor(default=RuntimeError("model down"))
    assert commercial(db, broken).record_inbound(result, correlation_id="c").status is CommercialHookStatus.EXTRACTION_FAILED
    assert signals(db, opportunity_id) == [] and objections(db, opportunity_id) == []


# ---- Terminal and reopen ----------------------------------------------------------------------------


def test_won_closes_open_commercial_work_and_keeps_history(db: Database) -> None:
    lead_id, opportunity_id = presented(db)
    start_negotiation(db, lead_id)
    customer_message(db, "p-2", asks((TermType.PAYMENT_TERM, text("NET_60")), accept=True))
    mark_won(db, lead_id)
    [revision] = revisions(db, opportunity_id)
    assert revision.status is RevisionStatus.CLOSED and revision.totals is not None  # content kept, work closed
    with db.transaction() as uow:
        [request] = uow.term_requests.list_for_opportunity(opportunity_id)
    assert request.status is TermRequestStatus.CANCELLED
    assert [s.status for s in signals(db, opportunity_id)] == [SignalStatus.CANCELLED]
    assert action(db, opportunity_id) == (NextActionOwner.NONE, CommercialAction.CLOSED)


def test_an_accepted_revision_stays_accepted_when_the_lead_is_won(db: Database) -> None:
    lead_id, opportunity_id = presented(db)
    revision_command(db, opportunity_id, MarkProposalAccepted, "cmd-accept")
    mark_won(db, lead_id)
    assert current(db, opportunity_id).status is RevisionStatus.ACCEPTED


def test_lost_closes_the_proposal_and_reopen_resurrects_nothing(db: Database) -> None:
    lead_id, opportunity_id = ready_draft(db)
    approve(db, opportunity_id)
    mark_lost(db, lead_id)
    assert current(db, opportunity_id).status is RevisionStatus.CLOSED
    reopen(db, lead_id)
    assert current(db, opportunity_id).status is RevisionStatus.CLOSED and len(revisions(db, opportunity_id)) == 1
    with pytest.raises(CommandRejectedError) as error:
        create_proposal(db, opportunity_id, command_id="cmd-proposal-again")
    assert [c.value for c in error.value.codes] == ["OPPORTUNITY_NOT_OPEN"]  # a new opportunity is needed


def test_dnc_stops_commercial_progress_but_keeps_history_readable(db: Database) -> None:
    _, opportunity_id = ready_draft(db)
    approve(db, opportunity_id)
    suppress(db, SENDER)
    view = commercial(db).view(opportunity_id)
    assert view.suppressed and (view.next_action.owner, view.next_action.action) == (NextActionOwner.NONE, CommercialAction.NONE)
    assert view.approved_total is not None  # history stays readable
    with pytest.raises(CommandRejectedError) as error:
        present(db, opportunity_id)
    assert [c.value for c in error.value.codes] == ["CONTACT_SUPPRESSED"]


# ---- Next action ----------------------------------------------------------------------------------------


def test_next_action_follows_the_commercial_position(db: Database) -> None:
    _, opportunity_id = opportunity_for(db)
    assert action(db, opportunity_id) == (NextActionOwner.OPERATOR, CommercialAction.PREPARE_PROPOSAL)
    create_proposal(db, opportunity_id)
    assert action(db, opportunity_id) == (NextActionOwner.OPERATOR, CommercialAction.COMPLETE_COMMERCIAL_INPUTS)
    update(db, opportunity_id)
    set_term(db, opportunity_id, TermType.PAYMENT_TERM, text("NET_30"))
    assert action(db, opportunity_id) == (NextActionOwner.OPERATOR, CommercialAction.REVIEW_PROPOSAL)
    approve(db, opportunity_id)
    assert action(db, opportunity_id) == (NextActionOwner.OPERATOR, CommercialAction.PRESENT_PROPOSAL)
    present(db, opportunity_id)
    assert action(db, opportunity_id) == (NextActionOwner.CUSTOMER, CommercialAction.WAIT_FOR_CUSTOMER_DECISION)
    customer_message(db, "p-2", asks((TermType.PAYMENT_TERM, text("NET_60"))))
    assert action(db, opportunity_id) == (NextActionOwner.OPERATOR, CommercialAction.REVIEW_TERM_REQUEST)
    with db.transaction() as uow:
        [request] = uow.term_requests.list_for_opportunity(opportunity_id)
    approve_request(db, request.request_id)
    assert action(db, opportunity_id) == (NextActionOwner.OPERATOR, CommercialAction.REVIEW_REVISION)


# ---- Adversarial review regressions -------------------------------------------------------------


def test_a_lead_closed_by_stage6_has_its_commercial_work_closed_by_the_commercial_hook(db: Database) -> None:
    _, opportunity_id = presented(db)
    customer_message(db, "p-accept", asks(accept=True))
    result = process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.UNSUBSCRIBE)),
                     envelope("p-unsub", body="Please unsubscribe me.", in_reply_to="<p-1@prospect.example>"))
    outcome = commercial(db, FakeCommercialExtractor()).record_inbound(result, correlation_id="c")  # no pipeline hook ran
    assert outcome.reason == "LEAD_CLOSED"
    assert current(db, opportunity_id).status is RevisionStatus.CLOSED
    assert [s.status for s in signals(db, opportunity_id)] == [SignalStatus.CANCELLED]


def test_commercial_audit_never_holds_message_bodies(db: Database) -> None:
    _, opportunity_id = presented(db)
    customer_message(db, "p-2", asks((TermType.PAYMENT_TERM, text("NET_60")), accept=True,
                                     objections=((ObjectionCategory.PRICE, "Price concern"),)))
    with db.transaction() as uow:
        events = [e for t in ("TERM_REQUESTED", "OBJECTION_RECORDED", "COMMERCIAL_SIGNAL_ACCEPTANCE")
                  for e in uow.audit.list_by_event_type(t, 10)]
    rendered = " ".join(e.model_dump_json() for e in events)
    assert len(events) == 3 and "About your proposal." not in rendered and "how much does the Basic plan" not in rendered
