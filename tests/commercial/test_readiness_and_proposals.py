"""Proposal readiness and the proposal/revision lifecycle."""

from decimal import Decimal

import pytest

from app.commercial import CommercialNotFoundError, CommercialProfile, ProposalReadiness, ReadinessLevel
from app.core.enums import (
    CommercialBlocker,
    NextActionOwner,
    ObjectionCategory,
    OpportunityStatus,
    RevisionStatus,
    TermSource,
    TermType,
)
from app.core.models import Money
from app.operator import (
    CommandCollisionError,
    CommandRejectedError,
    CreateProposal,
    MarkProposalAccepted,
    MarkProposalDeclined,
    OperatorNotFoundError,
    ReviseProposal,
    StaleCommandError,
    UpdateProposal,
    WithdrawProposal,
)
from app.persistence import Database
from app.pipeline.fake import FakeQualificationExtractor
from tests.commercial.builders import (
    BASIC,
    approve,
    asks,
    commercial,
    create_proposal,
    current,
    customer_message,
    line,
    opportunity_for,
    ops,
    present,
    presented,
    ready_draft,
    revise,
    revision_command,
    revisions,
    set_term,
    text,
    update,
)
from tests.inbound.builders import envelope, happy_transport, process
from tests.operator.builders import AS_ALICE
from tests.pipeline.builders import (
    REQUIRED,
    active_opportunity,
    approve_qualification,
    create_opportunity,
    extraction,
    inbound,
    lead,
    mark_lost,
    pipeline,
    qualified_lead,
)
USD_ONLY = CommercialProfile(profile_id="usd", currencies={"USD": 2}, required_terms=(TermType.PAYMENT_TERM,))


def codes(error: CommandRejectedError) -> list[str]:
    return [c.value for c in error.codes]


def level(db: Database, opportunity_id: str, profile: CommercialProfile | None = None) -> ProposalReadiness:
    return commercial(db, profile=profile).readiness(opportunity_id)


# ---- Readiness -----------------------------------------------------------------------------------


def test_an_unknown_opportunity_has_no_readiness(db: Database) -> None:
    with pytest.raises(CommercialNotFoundError):
        commercial(db).readiness("op-missing")


def test_ready_for_draft_before_any_proposal(db: Database) -> None:
    _, opportunity_id = opportunity_for(db)
    ready = level(db, opportunity_id)
    assert (ready.level, ready.blockers, ready.missing) == (ReadinessLevel.READY_FOR_DRAFT, (CommercialBlocker.NO_PROPOSAL,), ("PROPOSAL",))


def test_an_unapproved_or_disputed_qualification_is_not_ready(db: Database) -> None:
    _, opportunity_id = opportunity_for(db)
    result = process(db, happy_transport(), envelope("p-2", in_reply_to="<p-1@prospect.example>"))
    pipeline(db, FakeQualificationExtractor(default=extraction(timeframe="Next year"))).record_inbound(result, correlation_id="c")
    ready = level(db, opportunity_id)
    assert ready.level is ReadinessLevel.NOT_READY and ready.blockers == (CommercialBlocker.QUALIFICATION_CONFLICT,)
    with pytest.raises(CommandRejectedError) as error:
        create_proposal(db, opportunity_id)
    assert codes(error.value) == ["QUALIFICATION_NOT_APPROVED"]


def test_missing_price_and_missing_required_term_are_explicit(db: Database) -> None:
    _, opportunity_id = opportunity_for(db)
    create_proposal(db, opportunity_id)
    update(db, opportunity_id, line("l1", BASIC), line("l2", "addon.unknown_item", "1"))
    ready = level(db, opportunity_id)
    assert ready.level is ReadinessLevel.READY_FOR_DRAFT
    assert set(ready.blockers) == {CommercialBlocker.MISSING_PRICE, CommercialBlocker.MISSING_REQUIRED_TERM}
    assert set(ready.missing) == {"PRICE:l2", "TERM:PAYMENT_TERM"}  # unknown stays unknown: no guessed price
    [priced, unpriced] = current(db, opportunity_id).lines
    assert priced.unit_price is not None and unpriced.unit_price is None and unpriced.price_source is None


def test_a_currency_the_profile_does_not_allow_is_not_ready(db: Database) -> None:
    _, opportunity_id = ready_draft(db)
    ready = level(db, opportunity_id, profile=USD_ONLY)
    assert CommercialBlocker.CURRENCY_NOT_ALLOWED in ready.blockers and ready.level is not ReadinessLevel.READY_FOR_REVIEW


def test_an_open_term_request_blocks_review_but_an_objection_only_warns(db: Database) -> None:
    _, opportunity_id = ready_draft(db)
    customer_message(db, "p-2", asks(objections=((ObjectionCategory.PRICE, "Too expensive for us"),)))
    ready = level(db, opportunity_id)
    assert ready.level is ReadinessLevel.READY_FOR_REVIEW and ready.warnings == (CommercialBlocker.OPEN_OBJECTION,)
    customer_message(db, "p-3", asks((TermType.PAYMENT_TERM, text("NET_60"))))
    ready = level(db, opportunity_id)
    assert ready.level is ReadinessLevel.READY_FOR_DRAFT and CommercialBlocker.UNAPPROVED_TERM_REQUEST in ready.blockers


def test_complete_approved_inputs_are_ready_for_review(db: Database) -> None:
    _, opportunity_id = ready_draft(db)
    assert level(db, opportunity_id).level is ReadinessLevel.READY_FOR_REVIEW


# ---- Proposals and revisions -------------------------------------------------------------------------


def test_a_proposal_needs_an_open_opportunity(db: Database) -> None:
    qualified_lead(db)  # qualified, but no opportunity exists to propose on
    with pytest.raises(OperatorNotFoundError):
        ops(db).create_proposal(AS_ALICE, CreateProposal(command_id="cmd-p", correlation_id="c", opportunity_id="op-none",
                                                         expected_opportunity_version=1, currency="EUR"))
    _, opportunity_id = opportunity_for_other(db)
    mark_lost(db, lead_from(db, opportunity_id))
    with pytest.raises(CommandRejectedError) as error:
        create_proposal(db, opportunity_id)
    assert codes(error.value) == ["LEAD_CLOSED"]


def opportunity_for_other(db: Database) -> tuple[str, str]:
    result = inbound(db, "p-other", facts=REQUIRED, sender="other@prospect-b.example")
    lead_id = result.lead_id or ""
    approve_qualification(db, lead_id, command_id="cmd-aq-other")
    create_opportunity(db, lead_id, command_id="cmd-op-other")
    return lead_id, active_opportunity(db, lead_id).opportunity_id


def lead_from(db: Database, opportunity_id: str) -> str:
    with db.transaction() as uow:
        found = uow.opportunities.get(opportunity_id)
    assert found is not None
    return found.lead_id


def test_one_proposal_per_opportunity(db: Database) -> None:
    _, opportunity_id = opportunity_for(db)
    create_proposal(db, opportunity_id)
    with pytest.raises(CommandRejectedError) as error:
        create_proposal(db, opportunity_id, command_id="cmd-proposal-2")
    assert codes(error.value) == ["PROPOSAL_EXISTS"]


def test_prices_come_only_from_operators_or_approved_knowledge(db: Database) -> None:
    _, opportunity_id = opportunity_for(db)
    create_proposal(db, opportunity_id)
    update(db, opportunity_id, line("l1", BASIC), line("l2", "custom.setup", "1", unit_price="500.00"))
    knowledge, operator = current(db, opportunity_id).lines
    assert knowledge.price_source is not None and knowledge.price_source.source is TermSource.KNOWLEDGE
    assert knowledge.price_source.fact_key == BASIC and knowledge.unit_price == Money(amount=Decimal("100"), currency="EUR")
    assert operator.price_source is not None and operator.price_source.source is TermSource.OPERATOR
    with pytest.raises(CommandRejectedError) as error:
        update(db, opportunity_id, line("l1", "custom.setup", "1", unit_price="10", currency="USD"), command_id="cmd-usd")
    assert codes(error.value) == ["CURRENCY_MISMATCH"]


def test_an_incomplete_draft_cannot_be_approved(db: Database) -> None:
    _, opportunity_id = opportunity_for(db)
    create_proposal(db, opportunity_id)
    update(db, opportunity_id, line("l1", "addon.unknown_item", "1"))
    with pytest.raises(CommandRejectedError) as error:
        approve(db, opportunity_id)
    assert codes(error.value)[0] == "PROPOSAL_NOT_READY" and "MISSING_PRICE" in codes(error.value)


def test_approval_freezes_terms_and_totals_and_the_revision_is_then_immutable(db: Database) -> None:
    _, opportunity_id = ready_draft(db)
    approve(db, opportunity_id)
    approved = current(db, opportunity_id)
    assert approved.status is RevisionStatus.APPROVED and approved.totals is not None
    assert [t.value.text for t in approved.frozen_terms] == ["NET_30"]
    with pytest.raises(CommandRejectedError) as error:
        update(db, opportunity_id, line("l1", BASIC, "99"), command_id="cmd-edit-approved")
    assert codes(error.value) == ["REVISION_NOT_EDITABLE"]
    set_term(db, opportunity_id, TermType.PAYMENT_TERM, text("NET_45"), command_id="cmd-term-2", expected=1)
    assert [t.value.text for t in current(db, opportunity_id).frozen_terms] == ["NET_30"]  # still what was approved


def test_presentation_is_only_an_explicit_operator_confirmation(db: Database) -> None:
    _, opportunity_id = ready_draft(db)
    approve(db, opportunity_id)
    view = commercial(db).view(opportunity_id)
    assert view.revision_status is RevisionStatus.APPROVED and view.next_action.action.value == "PRESENT_PROPOSAL"
    present(db, opportunity_id)
    presented_revision = current(db, opportunity_id)
    assert presented_revision.status is RevisionStatus.PRESENTED and presented_revision.presented_at is not None
    assert commercial(db).view(opportunity_id).next_action.owner is NextActionOwner.CUSTOMER


def test_a_revision_keeps_history_and_supersedes_its_predecessor(db: Database) -> None:
    _, opportunity_id = presented(db)
    first = current(db, opportunity_id)
    revise(db, opportunity_id)
    rev1, rev2 = revisions(db, opportunity_id)
    assert (rev1.status, rev1.frozen_terms, rev1.totals) == (RevisionStatus.SUPERSEDED, first.frozen_terms, first.totals)
    assert rev1.presented_at == first.presented_at  # it was presented; that fact is kept
    assert (rev2.revision, rev2.predecessor_id, rev2.status, rev2.lines) == (2, rev1.revision_id, RevisionStatus.DRAFT, first.lines)


def test_a_stale_or_non_current_revision_cannot_be_revised_twice(db: Database) -> None:
    _, opportunity_id = presented(db)
    stale = current(db, opportunity_id)
    revise(db, opportunity_id)
    with pytest.raises(StaleCommandError):
        ops(db).revise_proposal(AS_ALICE, ReviseProposal(command_id="cmd-revise-again", correlation_id="c",
                                                         revision_id=stale.revision_id, expected_revision_version=stale.version))
    assert [r.revision for r in revisions(db, opportunity_id)] == [1, 2]


@pytest.mark.parametrize(("cls", "status"), [(MarkProposalAccepted, RevisionStatus.ACCEPTED),
                                             (MarkProposalDeclined, RevisionStatus.DECLINED)])
def test_the_customer_decision_is_recorded_by_an_operator_and_never_closes_the_lead(db: Database, cls: type,
                                                                                  status: RevisionStatus) -> None:
    lead_id, opportunity_id = presented(db)
    revision_command(db, opportunity_id, cls, "cmd-decide")
    assert current(db, opportunity_id).status is status
    assert lead(db, lead_id).stage.value == "OPPORTUNITY"  # WON/LOST stay Stage 12 operator decisions
    assert commercial(db).view(opportunity_id).opportunity_status is OpportunityStatus.OPEN


def test_withdraw_and_replay(db: Database) -> None:
    _, opportunity_id = ready_draft(db)
    revision = current(db, opportunity_id)
    command = WithdrawProposal(command_id="cmd-withdraw", correlation_id="c", revision_id=revision.revision_id,
                               expected_revision_version=revision.version, reason="Scope changed.")
    first = ops(db).withdraw_proposal(AS_ALICE, command)
    assert ops(db).withdraw_proposal(AS_ALICE, command).replayed and not first.replayed
    assert current(db, opportunity_id).status is RevisionStatus.WITHDRAWN and len(revisions(db, opportunity_id)) == 1
    with pytest.raises(CommandCollisionError):
        ops(db).withdraw_proposal(AS_ALICE, command.model_copy(update={"reason": "Something else."}))


def test_update_replay_never_double_applies(db: Database) -> None:
    _, opportunity_id = opportunity_for(db)
    create_proposal(db, opportunity_id)
    revision = current(db, opportunity_id)
    command = UpdateProposal(command_id="cmd-upd", correlation_id="c", revision_id=revision.revision_id,
                             expected_revision_version=revision.version, lines=(line("l1", BASIC, discount="10"),))
    ops(db).update_proposal(AS_ALICE, command)
    assert ops(db).update_proposal(AS_ALICE, command).replayed
    assert current(db, opportunity_id).version == revision.version + 1  # applied exactly once
