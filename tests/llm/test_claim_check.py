import pytest

from app.core.enums import ClaimCheckStatus
from app.core.models import KnowledgeEvidence
from app.llm import COMMITMENT_PATTERNS, ClaimCheckResult, ClaimFinding, ClaimType, FindingReason, check_draft_claims
from app.llm.claim_check import draft_hash, normalize_number
from tests.llm.builders import FAQ_EVIDENCE, PRICE_EVIDENCE, evidence

EVIDENCE = (PRICE_EVIDENCE, FAQ_EVIDENCE)


def check(
    body: str, items: tuple[KnowledgeEvidence, ...] = EVIDENCE, *, trusted_references: tuple[str, ...] = ()
) -> ClaimCheckResult:
    return check_draft_claims("Re: Question", body, items, trusted_references=trusted_references)


def only(result: ClaimCheckResult, claim_type: ClaimType) -> list[ClaimFinding]:
    return [f for f in result.findings if f.claim_type is claim_type]


# A-C money
def test_supported_price() -> None:
    result = check("The Basic plan costs 100 EUR per month (that is €100.00).")
    assert result.passed and result.status is ClaimCheckStatus.PASS
    money = only(result, ClaimType.MONEY)
    assert [f.normalized for f in money] == ["EUR 100", "EUR 100"]
    assert all(f.evidence_ids == ("ev-1",) and f.reason is FindingReason.SUPPORTED_BY_EVIDENCE for f in money)


def test_unsupported_price() -> None:
    result = check("The Basic plan costs 90 EUR per month.")
    assert not result.passed and result.status is ClaimCheckStatus.FAIL
    [finding] = result.unsupported
    assert (finding.normalized, finding.reason) == ("EUR 90", FindingReason.NOT_IN_EVIDENCE)


@pytest.mark.parametrize("claim", ["100 USD", "$100", "£100"])
def test_wrong_currency_is_unsupported(claim: str) -> None:
    result = check(f"The Basic plan costs {claim} per month.")
    [finding] = result.unsupported
    assert finding.reason is FindingReason.CURRENCY_MISMATCH


# D-E percentages
def test_supported_percentage_in_either_notation() -> None:
    assert check("Annual billing saves 10%.").passed
    assert check("Annual billing saves 10 percent.").passed


def test_unsupported_percentage() -> None:
    result = check("Annual billing saves 15%.")
    assert [f.normalized for f in result.unsupported] == ["15%"]


# F-G dates
def test_supported_date_in_other_formats() -> None:
    for text in ("The launch is on 2026-09-01.", "The launch is on 1 September 2026.", "The launch is on Sep 1, 2026."):
        assert check(text).passed, text


def test_unsupported_and_ambiguous_dates() -> None:
    assert [f.normalized for f in check("The launch is on 2026-10-01.").unsupported] == ["2026-10-01"]
    assert [f.normalized for f in check("The launch is on 01/09/2026.").unsupported] == ["01/09/2026"]


# H-I URLs
def test_supported_url_ignores_scheme_case_and_trailing_slash() -> None:
    assert check("See https://SampleWidget.example/help/.").passed
    assert check("See http://samplewidget.example/help").passed


def test_invented_url_fails() -> None:
    result = check("Book here: https://samplewidget.example/booking")
    [finding] = result.unsupported
    assert (finding.claim_type, finding.normalized) == (ClaimType.URL, "samplewidget.example/booking")


# J-K emails
def test_supported_email() -> None:
    assert check("Write to Support@SampleWidget.example any time.").passed


def test_invented_email_fails() -> None:
    [finding] = check("Write to sales@samplewidget.example.").unsupported
    assert finding.claim_type is ClaimType.EMAIL


def test_invented_phone_and_bare_number_fail() -> None:
    result = check("Call +380 44 123 4567. Onboarding takes 3 weeks.")
    assert [f.claim_type for f in result.unsupported] == [ClaimType.PHONE, ClaimType.NUMBER]


def test_bare_number_is_supported_by_any_evidence_number() -> None:
    assert check("The Basic plan is 100 per month.").passed


def test_organization_names() -> None:
    assert not check("Acme Retail Ltd uses the Sample Widget.").passed
    retail = evidence("ev-3", "Case study\n\nAcme Retail Ltd uses the Sample Widget.")
    assert check("Acme Retail Ltd uses the Sample Widget.", (*EVIDENCE, retail)).passed
    assert check("Samplewidget Co can help.", trusted_references=("Samplewidget Co",)).passed


def test_organization_names_do_not_span_lines() -> None:
    # Regression: the subject's last word must not merge with a name on the next line.
    retail = evidence("ev-3", "Case study\n\nAcme Retail Ltd uses the Sample Widget.")
    result = check_draft_claims("Customer Question", "Acme Retail Ltd uses it.", (retail,))
    assert [f.normalized for f in only(result, ClaimType.ORGANIZATION)] == ["acme retail ltd"]
    assert result.passed


# L ordering
def test_multiple_findings_have_stable_ordering() -> None:
    body = "We guarantee 15% savings from 2026-10-01 at https://x.example and 90 EUR."
    result = check(body)
    assert [(f.claim_type, f.normalized) for f in result.findings] == [
        (ClaimType.COMMITMENT, "GUARANTEE"),
        (ClaimType.PERCENTAGE, "15%"),
        (ClaimType.DATE, "2026-10-01"),
        (ClaimType.URL, "x.example"),
        (ClaimType.MONEY, "EUR 90"),
    ]
    assert check(body) == result


# M no claims
def test_no_factual_claims_passes_without_evidence() -> None:
    result = check_draft_claims("Thanks", "Thanks for your message! A colleague will follow up shortly.", ())
    assert result.passed and result.findings == () and result.evidence_ids_checked == ()


# N-P commitments
@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("We guarantee a smooth rollout.", "GUARANTEE"),
        ("Results are guaranteed.", "GUARANTEE"),
        ("This is a legally binding offer.", "LEGALLY_BINDING"),
        ("If you are unhappy, we will refund you.", "REFUND_PROMISE"),
        ("I can offer you a special discount.", "DISCOUNT_PROMISE"),
        ("We'll give you a 20% discount.", "DISCOUNT_PROMISE"),
        ("We have a special price for you.", "DISCOUNT_PROMISE"),
        ("We accept your terms.", "CONTRACT_ACCEPTANCE"),
        ("I have booked a call for Tuesday.", "MEETING_CONFIRMED"),
        ("Your meeting is confirmed.", "MEETING_CONFIRMED"),
    ],
)
def test_forbidden_commitments_fail(text: str, code: str) -> None:
    result = check(text)
    assert not result.passed
    assert code in {f.normalized for f in only(result, ClaimType.COMMITMENT)}
    assert all(f.reason is FindingReason.FORBIDDEN_COMMITMENT for f in only(result, ClaimType.COMMITMENT))


def test_harmless_wording_is_not_a_commitment() -> None:
    assert check("Would you like to book a call? A colleague can discuss discounts.").passed
    assert {code for code, _ in COMMITMENT_PATTERNS} == {
        "GUARANTEE", "LEGALLY_BINDING", "REFUND_PROMISE", "DISCOUNT_PROMISE", "CONTRACT_ACCEPTANCE", "MEETING_CONFIRMED",
    }


# Q evidence subset
def test_only_the_supplied_evidence_counts() -> None:
    body = "The Basic plan costs 100 EUR and annual billing saves 10%."
    assert check(body).passed
    result = check(body, (PRICE_EVIDENCE,))
    assert [f.normalized for f in result.unsupported] == ["10%"]
    assert result.evidence_ids_checked == ("ev-1",)


# R hash
def test_draft_hash_is_deterministic() -> None:
    first = check("Hello")
    assert first.draft_hash == check("Hello").draft_hash == draft_hash("Re: Question", "Hello")
    assert check("Hello!").draft_hash != first.draft_hash


def test_subject_is_checked_too() -> None:
    result = check_draft_claims("Special price: 50 EUR", "Hello", EVIDENCE)
    assert [f.normalized for f in result.unsupported] == ["EUR 50"]


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [("100", "100"), ("100.00", "100"), ("1,200.50", "1200.5"), ("1,5", "1,5"), ("1.200,50", "1.200,50"), ("0.0", "0")],
)
def test_number_normalization_is_narrow(raw: str, normalized: str) -> None:
    assert normalize_number(raw) == normalized
