"""Requested vs approved terms, precedence, discount policy, and Decimal money."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.commercial import CommercialHookStatus, CommercialProfile, DiscountPolicy
from app.commercial.config import GENERIC_COMMERCIAL_PROFILE
from app.commercial.fake import FakeCommercialExtractor
from app.commercial.money import line_total, proposal_totals, quantize
from app.commercial.terms import resolve
from app.core.enums import TermRequestStatus, TermSource, TermType
from app.core.models import CommercialTerm, Money, ProposalLine, TermRequest, ValueSource
from app.operator import ApproveProposal, ApproveTermRequest, CommandRejectedError, StaleCommandError, TermOverrideInput
from app.persistence import Database
from tests.commercial.builders import (
    BASIC,
    TEAM,
    approve,
    approve_request,
    asks,
    commercial,
    create_proposal,
    current,
    customer_message,
    line,
    money,
    opportunity_for,
    ops,
    pct,
    presented,
    ready_draft,
    reject_request,
    set_term,
    text,
    update,
)
from tests.commercial.test_readiness_and_proposals import opportunity_for_other
from tests.inbound.builders import NOW
from tests.operator.builders import AS_ALICE

NET = TermType.PAYMENT_TERM


def requests(db: Database, opportunity_id: str) -> list[TermRequest]:
    with db.transaction() as uow:
        return uow.term_requests.list_for_opportunity(opportunity_id)


def term_value(db: Database, opportunity_id: str, term_type: TermType) -> str | None:
    with db.transaction() as uow:
        found = [t for t in uow.commercial_terms.list_for_opportunity(opportunity_id) if t.term_type is term_type]
    return found[0].value.display() if found else None


def codes(error: CommandRejectedError) -> list[str]:
    return [c.value for c in error.codes]


# ---- Requested vs approved ----------------------------------------------------------------------


def test_a_customer_request_is_never_an_approved_term(db: Database) -> None:
    _, opportunity_id = opportunity_for(db)
    create_proposal(db, opportunity_id)
    customer_message(db, "p-2", asks((TermType.DISCOUNT, pct("20"))))
    [request] = requests(db, opportunity_id)
    assert (request.status, request.requested_value.display(), request.approved_value_at_request) == (
        TermRequestStatus.REQUESTED, "20%", None)
    assert term_value(db, opportunity_id, TermType.DISCOUNT) is None  # requested 20% is not approved 20%


def test_a_conflicting_request_is_under_review_and_the_approved_term_stays(db: Database) -> None:
    _, opportunity_id = presented(db)  # approved and presented with NET_30
    customer_message(db, "p-2", asks((NET, text("NET_60"))))
    [request] = requests(db, opportunity_id)
    assert request.status is TermRequestStatus.UNDER_REVIEW and request.approved_value_at_request == text("NET_30")
    assert term_value(db, opportunity_id, NET) == "NET_30"
    assert [t.value.text for t in current(db, opportunity_id).frozen_terms] == ["NET_30"]


def test_the_same_message_is_recorded_once_and_an_agreeing_value_is_not_a_request(db: Database) -> None:
    _, opportunity_id = ready_draft(db)
    extraction = asks((NET, text("NET_60")), (NET, text("net_30")))
    result, _ = customer_message(db, "p-2", extraction)
    replayed = commercial(db, FakeCommercialExtractor(default=extraction)).record_inbound(result, correlation_id="r")
    assert replayed.status is CommercialHookStatus.REPLAYED
    assert [r.requested_value.text for r in requests(db, opportunity_id)] == ["NET_60"]  # NET_30 is already agreed


def test_approving_a_request_sets_the_term_with_its_provenance(db: Database) -> None:
    _, opportunity_id = ready_draft(db)
    customer_message(db, "p-2", asks((NET, text("NET_45"))))
    customer_message(db, "p-3", asks((NET, text("NET_60"))))
    by_value = {r.requested_value.text: r for r in requests(db, opportunity_id)}
    first, second = by_value["NET_45"], by_value["NET_60"]
    approve_request(db, first.request_id)
    assert term_value(db, opportunity_id, NET) == "NET_45"
    with db.transaction() as uow:
        [term] = [t for t in uow.commercial_terms.list_for_opportunity(opportunity_id) if t.term_type is NET]
    assert term.provenance.source is TermSource.TERM_REQUEST and term.provenance.request_id == first.request_id
    statuses = {r.request_id: r.status for r in requests(db, opportunity_id)}
    assert statuses == {first.request_id: TermRequestStatus.APPROVED, second.request_id: TermRequestStatus.SUPERSEDED}


def test_rejecting_a_request_changes_nothing_else(db: Database) -> None:
    _, opportunity_id = ready_draft(db)
    customer_message(db, "p-2", asks((NET, text("NET_90"))))
    [request] = requests(db, opportunity_id)
    reject_request(db, request.request_id)
    assert requests(db, opportunity_id)[0].status is TermRequestStatus.REJECTED
    assert term_value(db, opportunity_id, NET) == "NET_30"


def test_a_stale_term_or_request_command_is_rejected(db: Database) -> None:
    _, opportunity_id = ready_draft(db)  # PAYMENT_TERM version 1 exists
    with pytest.raises(StaleCommandError) as error:
        set_term(db, opportunity_id, NET, text("NET_10"), command_id="cmd-stale", expected=None)
    assert codes(error.value) == ["TERM_VERSION_CHANGED"]
    customer_message(db, "p-2", asks((NET, text("NET_60"))))
    [request] = requests(db, opportunity_id)
    reject_request(db, request.request_id)
    with pytest.raises(CommandRejectedError) as error:
        approve_request(db, request.request_id)
    assert codes(error.value) == ["REQUEST_NOT_OPEN"]


# ---- Precedence ------------------------------------------------------------------------------------


def test_precedence_revision_then_opportunity_then_profile_then_unknown(db: Database) -> None:
    profile = CommercialProfile(profile_id="p", currencies={"EUR": 2}, required_terms=(NET,),
                                term_defaults={NET: text("NET_15"), TermType.VALIDITY_PERIOD: text("30 days")})
    _, opportunity_id = opportunity_for(db)
    create_proposal(db, opportunity_id, profile=profile)
    def view() -> dict[TermType, tuple[str, TermSource]]:
        draft = commercial(db, profile=profile).draft(current(db, opportunity_id).revision_id)
        return {t.term_type: (t.value.display(), t.provenance.source) for t in draft.terms}

    assert view()[NET] == ("NET_15", TermSource.PROFILE_DEFAULT)
    set_term(db, opportunity_id, NET, text("NET_30"), profile=profile)
    assert view()[NET] == ("NET_30", TermSource.OPERATOR)  # customer-specific exception
    update(db, opportunity_id, overrides=(TermOverrideInput(term_type=NET, value=text("NET_20")),), profile=profile)
    assert view()[NET] == ("NET_20", TermSource.REVISION_OVERRIDE)
    assert TermType.SLA not in view()  # unknown stays unknown
    assert profile.term_defaults[NET] == text("NET_15")  # the global profile never changes


def test_a_frozen_revision_means_exactly_what_was_approved() -> None:
    source = ValueSource(source=TermSource.OPERATOR, operator_id="op", command_id="c1", recorded_at=NOW)
    later = CommercialTerm(term_row_id="t", opportunity_id="o", term_type=NET, value=text("NET_60"), provenance=source,
                           created_at=NOW, updated_at=NOW)
    resolved = resolve(GENERIC_COMMERCIAL_PROFILE, None, [later], NET, "main", NOW)
    assert resolved is not None and resolved.value.text == "NET_60"


# ---- Discounts -------------------------------------------------------------------------------------


CAPPED = CommercialProfile(profile_id="capped", currencies={"EUR": 2}, required_terms=(NET,),
                           discount_policy=DiscountPolicy(max_percent=Decimal("15"), forbidden_item_refs=(TEAM,)))


def test_a_configured_discount_policy_is_a_hard_limit(db: Database) -> None:
    _, opportunity_id = opportunity_for(db)
    create_proposal(db, opportunity_id, profile=CAPPED)
    with pytest.raises(CommandRejectedError) as error:
        set_term(db, opportunity_id, TermType.DISCOUNT, pct("20"), profile=CAPPED)
    assert codes(error.value) == ["DISCOUNT_ABOVE_LIMIT"]
    with pytest.raises(CommandRejectedError) as error:
        update(db, opportunity_id, line("l1", TEAM, "1", discount="5"), profile=CAPPED)
    assert codes(error.value) == ["DISCOUNT_NOT_ALLOWED"]
    set_term(db, opportunity_id, TermType.DISCOUNT, pct("10"), command_id="cmd-ok", profile=CAPPED)


def test_without_a_policy_a_discount_is_still_only_an_operator_value(db: Database) -> None:
    _, opportunity_id = opportunity_for(db)
    create_proposal(db, opportunity_id)
    customer_message(db, "p-2", asks((TermType.DISCOUNT, pct("20"))))
    update(db, opportunity_id)
    set_term(db, opportunity_id, NET, text("NET_30"))
    draft = commercial(db).draft(current(db, opportunity_id).revision_id)
    assert all(not c.subject.startswith("TERM:DISCOUNT") for c in draft.allowed_claims)
    assert any(b.reason == "CUSTOMER_REQUEST_NOT_APPROVED" for b in draft.blocked_claims)


# ---- Money -------------------------------------------------------------------------------------------


def priced(quantity: str, price: str, currency: str = "EUR", discount: str | None = None) -> ProposalLine:
    source = ValueSource(source=TermSource.OPERATOR, operator_id="op", command_id="c", recorded_at=NOW)
    return ProposalLine(line_id="l1", item_ref="x", quantity=Decimal(quantity), unit="u",
                        unit_price=Money(amount=Decimal(price), currency=currency), price_source=source,
                        discount_percent=Decimal(discount) if discount else None, discount_source=source if discount else None)


def test_line_and_proposal_arithmetic_is_decimal_and_rounded_per_currency() -> None:
    total = line_total(priced("3", "33.335", discount="10"), "EUR", 2)
    assert total is not None and total.discount is not None
    assert (total.subtotal.amount, total.discount.amount, total.total.amount) == (Decimal("100.01"), Decimal("10.00"), Decimal("90.01"))
    totals = proposal_totals((priced("2", "100"),), "EUR", 2, Decimal("12.5"))
    assert totals is not None and totals.discount is not None
    assert (totals.subtotal.amount, totals.discount.amount, totals.total.amount) == (Decimal("200.00"), Decimal("25.00"), Decimal("175.00"))
    assert totals.tax_included is False and isinstance(totals.total.amount, Decimal)
    assert quantize(Decimal("1.005"), 2) == Decimal("1.01") and quantize(Decimal("7"), 0) == Decimal("7")


def test_no_total_without_every_price_or_across_currencies() -> None:
    unpriced = ProposalLine(line_id="l2", item_ref="y", quantity=Decimal("1"), unit="u")
    assert proposal_totals((priced("1", "10"), unpriced), "EUR", 2, None) is None
    assert proposal_totals((priced("1", "10", "USD"),), "EUR", 2, None) is None
    assert proposal_totals((), "EUR", 2, None) is None


def test_negative_or_float_money_is_refused() -> None:
    with pytest.raises(ValidationError):
        Money(amount=Decimal("-1"), currency="EUR")
    with pytest.raises(ValidationError):
        Money(amount=Decimal("1"), currency="eur")
    assert Money(amount=0.1, currency="EUR").amount == Decimal("0.1")  # coerced exactly, never kept as float


def test_tax_is_never_fabricated(db: Database) -> None:
    _, opportunity_id = ready_draft(db)
    approve(db, opportunity_id)
    revision = current(db, opportunity_id)
    draft = commercial(db).draft(revision.revision_id)
    assert revision.totals is not None and revision.totals.tax_included is False
    assert any(b.subject == "TAX" and b.reason == "TAX_NOT_COMPUTED" for b in draft.blocked_claims)
    assert [c.text for c in draft.allowed_claims if c.subject == "TOTAL"] == ["Total (excluding tax): 1200.00 EUR"]


def test_every_allowed_claim_is_traceable(db: Database) -> None:
    _, opportunity_id = ready_draft(db)
    update(db, opportunity_id, line("l1", BASIC), line("l2", "addon.unknown_item", "1"), command_id="cmd-update-2")
    draft = commercial(db).draft(current(db, opportunity_id).revision_id)
    assert draft.allowed_claims and all(claim.provenance for claim in draft.allowed_claims)
    assert {b.subject for b in draft.blocked_claims} >= {"LINE:l2", "TOTAL", "TAX"}  # unproven: never claimed


def test_metrics_never_sum_different_currencies(db: Database) -> None:
    dual = CommercialProfile(profile_id="dual", currencies={"EUR": 2, "DKK": 2}, required_terms=(NET,))
    _, eur = ready_draft(db)
    approve(db, eur)
    _, dkk = opportunity_for_other(db)
    create_proposal(db, dkk, currency="DKK", command_id="cmd-dkk", profile=dual)
    update(db, dkk, line("l1", "custom.dkk", "2", unit_price="1000", currency="DKK"), command_id="cmd-dkk-u", profile=dual)
    set_term(db, dkk, NET, text("NET_30"), command_id="cmd-dkk-t", profile=dual)
    revision = current(db, dkk)
    ops(db, profile=dual).approve_proposal(AS_ALICE, ApproveProposal(command_id="cmd-dkk-a", correlation_id="c",
                                                                     revision_id=revision.revision_id,
                                                                     expected_revision_version=revision.version))
    values = {(v.currency, v.amount, v.proposals) for v in commercial(db, profile=dual).metrics().proposed_value_by_currency}
    assert values == {("DKK", Decimal("2000.00"), 1), ("EUR", Decimal("1200.00"), 1)}


# ---- Adversarial review regressions -------------------------------------------------------------


def test_prices_and_currency_are_never_approved_as_terms(db: Database) -> None:
    _, opportunity_id = ready_draft(db)
    for term_type, value in ((TermType.PRICE, money("8000")), (TermType.CURRENCY, text("USD"))):
        with pytest.raises(CommandRejectedError) as error:
            set_term(db, opportunity_id, term_type, value, command_id=f"cmd-{term_type.value}")
        assert codes(error.value) == ["TERM_VALUE_INVALID"]
    customer_message(db, "p-2", asks((TermType.PRICE, money("8000"))))
    [request] = requests(db, opportunity_id)
    with pytest.raises(CommandRejectedError) as error:
        approve_request(db, request.request_id)  # a price is agreed on a proposal line, in a revision
    assert codes(error.value) == ["TERM_VALUE_INVALID"]
    draft = commercial(db).draft(current(db, opportunity_id).revision_id)
    assert not any(c.subject.startswith("TERM:PRICE") for c in draft.allowed_claims)


def test_approving_a_request_is_stale_when_the_term_changed_meanwhile(db: Database) -> None:
    _, opportunity_id = ready_draft(db)  # PAYMENT_TERM NET_30, version 1
    customer_message(db, "p-2", asks((NET, text("NET_60"))))
    [request] = requests(db, opportunity_id)
    set_term(db, opportunity_id, NET, text("NET_45"), command_id="cmd-other-operator", expected=1)
    with pytest.raises(StaleCommandError) as error:
        ops(db).approve_term_request(AS_ALICE, ApproveTermRequest(
            command_id="cmd-approve-stale", correlation_id="c", request_id=request.request_id,
            expected_request_version=request.version, expected_term_version=1))
    assert codes(error.value) == ["TERM_VERSION_CHANGED"] and term_value(db, opportunity_id, NET) == "NET_45"
