"""Newer customer commercial evidence invalidates stale terminal operator intent.

A new open acceptance/decline signal advances the opportunity version, the token
MarkLeadWon and MarkLeadLost bind to; a decision prepared on an older snapshot is stale
and must be taken again from the fresh state. The customer message used here is one Stage
6 neither answers nor escalates, so the lead version does not move: only the opportunity
token can catch the stale decision."""

from datetime import timedelta

import pytest

from app.commercial import CommercialExtraction, CommercialHookStatus
from app.commercial.fake import FakeCommercialExtractor
from app.core.enums import LeadIntent, LeadStage, LostReason, RevisionStatus, SignalStatus
from app.inbound import InboundResult
from app.llm import LLMTask
from app.operator import BlockCode, MarkLeadLost, MarkLeadWon, StaleCommandError
from app.persistence import Database
from tests.commercial.builders import LATER, asks, commercial, current, presented
from tests.inbound.builders import ScriptedTransport, classification, envelope, process
from tests.operator.builders import AS_ALICE
from tests.pipeline.builders import active_opportunity, lead, lost_command, ops


def message(db: Database, provider_message_id: str, *, at=LATER) -> InboundResult:  # noqa: ANN001
    transport = ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.OUT_OF_OFFICE))
    return process(db, transport, envelope(provider_message_id, body="Re: your proposal", in_reply_to="<p-1@prospect.example>",
                                           received_at=at), correlation_id=f"corr-{provider_message_id}")


def signal(db: Database, result: InboundResult, extraction: CommercialExtraction):  # noqa: ANN201
    return commercial(db, FakeCommercialExtractor(default=extraction)).record_inbound(result, correlation_id="c-hook")


def opportunity_version(db: Database, lead_id: str) -> int:
    return active_opportunity(db, lead_id).version


def closing_events(db: Database) -> tuple[int, int]:
    with db.transaction() as uow:
        stops = uow.audit.list_by_event_type("LEAD_AUTOMATION_STOPPED", 50)
        transitions = uow.audit.list_by_event_type("PIPELINE_TRANSITION", 100)
    return len(stops), sum(1 for t in transitions if (t.after or {}).get("stage") == "CLOSED")


def test_ordering_a_acceptance_first_makes_the_prepared_lost_stale(db: Database) -> None:
    lead_id, opportunity_id = presented(db)
    stale = lost_command(db, lead_id, LostReason.CHOSE_COMPETITOR, "cmd-lost-stale")
    lead_version, seen = lead(db, lead_id).version, opportunity_version(db, lead_id)
    signal(db, message(db, "p-yes"), asks(accept=True))
    assert lead(db, lead_id).version == lead_version  # only the commercial token moved
    assert opportunity_version(db, lead_id) == seen + 1
    with pytest.raises(StaleCommandError) as refused:
        ops(db).mark_lead_lost(AS_ALICE, stale)
    assert BlockCode.OPPORTUNITY_VERSION_CHANGED in refused.value.codes
    assert lead(db, lead_id).stage is not LeadStage.CLOSED  # not closed by the stale command
    with db.transaction() as uow:
        [accepted] = uow.commercial_signals.list_for_opportunity(opportunity_id)
    assert accepted.status is SignalStatus.OPEN and current(db, opportunity_id).status is RevisionStatus.PRESENTED
    # Explicit fresh intent from the current state is still the operator's call.
    fresh = lost_command(db, lead_id, LostReason.CHOSE_COMPETITOR, "cmd-lost-fresh")
    first = ops(db).mark_lead_lost(AS_ALICE, fresh)
    again = ops(db).mark_lead_lost(AS_ALICE, fresh)  # Stage 7 idempotency unchanged
    assert not first.replayed and again.replayed and again.outcome == first.outcome
    final = lead(db, lead_id)
    assert (final.stage, final.close_reason.value if final.close_reason else None) == (LeadStage.CLOSED, "LOST")
    with db.transaction() as uow:
        [cancelled] = uow.commercial_signals.list_for_opportunity(opportunity_id)
        revisions = uow.proposal_revisions.list_for_opportunity(opportunity_id)
    assert cancelled.status is SignalStatus.CANCELLED
    assert [r.status for r in revisions] == [RevisionStatus.CLOSED] and revisions[0].approved_at is not None  # history kept
    assert closing_events(db) == (1, 1)  # terminal cleanup ran exactly once


def test_ordering_b_lost_first_then_the_acceptance_cannot_contradict_it(db: Database) -> None:
    lead_id, opportunity_id = presented(db)
    ops(db).mark_lead_lost(AS_ALICE, lost_command(db, lead_id, LostReason.NO_DECISION))
    with db.transaction() as uow:
        closed_opportunity = uow.opportunities.get(opportunity_id)
    result = message(db, "p-late-yes")
    outcome = signal(db, result, asks(accept=True))
    assert (outcome.status, outcome.reason) == (CommercialHookStatus.SKIPPED, "LEAD_CLOSED")
    with db.transaction() as uow:
        signals = uow.commercial_signals.list_for_opportunity(opportunity_id)
        assert uow.opportunities.get(opportunity_id) == closed_opportunity  # no version churn on a closed deal
    final = lead(db, lead_id)
    assert (final.stage, final.close_reason.value if final.close_reason else None) == (LeadStage.CLOSED, "LOST")
    assert [s for s in signals if s.status is SignalStatus.OPEN] == []
    assert current(db, opportunity_id).status is RevisionStatus.CLOSED and closing_events(db) == (1, 1)


def test_replays_never_advance_the_version_again(db: Database) -> None:
    lead_id, _ = presented(db)
    seen = opportunity_version(db, lead_id)
    result = message(db, "p-yes")
    assert signal(db, result, asks(accept=True)).status is CommercialHookStatus.APPLIED
    assert opportunity_version(db, lead_id) == seen + 1
    assert signal(db, result, asks(accept=True)).status is CommercialHookStatus.REPLAYED  # same message, hook replay
    duplicate = message(db, "p-yes")  # the same email delivered again
    assert duplicate.duplicate or duplicate.replayed
    signal(db, duplicate, asks(accept=True))
    assert opportunity_version(db, lead_id) == seen + 1


def test_an_older_message_replayed_later_is_not_newer_evidence(db: Database) -> None:
    lead_id, opportunity_id = presented(db)
    signal(db, message(db, "p-new", at=LATER + timedelta(hours=1)), asks(accept=True))
    seen = opportunity_version(db, lead_id)
    signal(db, message(db, "p-old", at=LATER), asks(decline=True))  # superseded at birth
    with db.transaction() as uow:
        statuses = sorted(s.status.value for s in uow.commercial_signals.list_for_opportunity(opportunity_id))
    assert statuses == ["OPEN", "SUPERSEDED"] and opportunity_version(db, lead_id) == seen


def test_signals_without_an_operator_decide_nothing(db: Database) -> None:
    lead_id, opportunity_id = presented(db)
    signal(db, message(db, "p-yes"), asks(accept=True))
    signal(db, message(db, "p-no", at=LATER + timedelta(minutes=5)), asks(decline=True))
    assert lead(db, lead_id).stage is not LeadStage.CLOSED  # no automatic WON or LOST
    assert active_opportunity(db, lead_id).status.value in ("OPEN", "NEGOTIATING")
    assert current(db, opportunity_id).status is RevisionStatus.PRESENTED


def test_a_stale_won_is_refused_the_same_way(db: Database) -> None:
    lead_id, _ = presented(db)
    opportunity = active_opportunity(db, lead_id)
    won = MarkLeadWon(command_id="cmd-won", correlation_id="c", lead_id=lead_id, expected_lead_version=lead(db, lead_id).version,
                      opportunity_id=opportunity.opportunity_id, expected_opportunity_version=opportunity.version)
    signal(db, message(db, "p-no"), asks(decline=True))
    with pytest.raises(StaleCommandError):
        ops(db).mark_lead_won(AS_ALICE, won)
    assert lead(db, lead_id).stage is not LeadStage.CLOSED


def test_lost_must_bind_to_the_opportunity_exactly_when_one_is_active(db: Database) -> None:
    lead_id, _ = presented(db)
    blind = MarkLeadLost(command_id="cmd-blind", correlation_id="c", lead_id=lead_id,
                         expected_lead_version=lead(db, lead_id).version, reason=LostReason.NO_BUDGET)
    with pytest.raises(StaleCommandError) as refused:  # no permissive default: the snapshot lacks the deal
        ops(db).mark_lead_lost(AS_ALICE, blind)
    assert BlockCode.OPPORTUNITY_VERSION_CHANGED in refused.value.codes
    assert lead(db, lead_id).stage is not LeadStage.CLOSED


def test_lost_without_an_opportunity_needs_no_opportunity_version(db: Database) -> None:
    from tests.pipeline.builders import qualified_lead
    lead_id = qualified_lead(db)
    phantom = MarkLeadLost(command_id="cmd-phantom", correlation_id="c", lead_id=lead_id,
                           expected_lead_version=lead(db, lead_id).version, expected_opportunity_version=1,
                           reason=LostReason.NO_BUDGET)
    with pytest.raises(StaleCommandError):
        ops(db).mark_lead_lost(AS_ALICE, phantom)
    ops(db).mark_lead_lost(AS_ALICE, lost_command(db, lead_id, LostReason.NO_BUDGET, "cmd-plain"))
    assert lead(db, lead_id).stage is LeadStage.CLOSED
