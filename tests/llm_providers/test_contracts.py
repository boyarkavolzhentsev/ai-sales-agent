"""Model output through the real adapters and validators: strict JSON and schema checks
(never repaired), and the Stage 12/13 AI contracts with deterministic grounding. A model
that invents a fact, a number, a price, a currency or a signal is refused as a whole."""

import json

import pytest

from app.ai import LLMCommercialExtractor, LLMQualificationExtractor, LLMSalesAdvisor
from app.commercial.contracts import CommercialExtractionRequest, KnownTerm
from app.core.enums import ConfidenceBand, LeadStage, QualificationStatus, TermType, ValueKind
from app.llm import LLMContractViolationError, LLMErrorCode, LLMError, LLMProviderError, LLMStructuredOutputError
from app.pipeline.contracts import AdvisorInput, ExtractionRequest, KnownFact
from tests.llm_providers.fakes import PROVIDERS, FakeSession, ok, pricing_intent, prompts, refusal, sections, truncated
from tests.llm_providers.builders import structured
from tests.llm_providers.test_adapters import classify

LEAD, MESSAGE, OPP = "ld_" + "1" * 40, "em_" + "2" * 40, "op_" + "3" * 40
VALID = pricing_intent()


# ---- Structured output ---------------------------------------------------------------------------------


def _without(key: str) -> dict:
    return {k: v for k, v in VALID.items() if k != key}


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(("text", "code"), [
    ("{intent: PRICING_REQUEST", LLMErrorCode.SCHEMA_VALIDATION_FAILED),  # invalid JSON
    ("", LLMErrorCode.SCHEMA_VALIDATION_FAILED),  # empty output
    ("[]", LLMErrorCode.SCHEMA_VALIDATION_FAILED),  # not an object
    (f"Sure! Here is the JSON: {json.dumps(VALID)}", LLMErrorCode.SCHEMA_VALIDATION_FAILED),  # prose: never searched
    (json.dumps(VALID | {"mark_lead_won": True}), LLMErrorCode.SCHEMA_VALIDATION_FAILED),  # extra field
    (json.dumps(_without("intent")), LLMErrorCode.SCHEMA_VALIDATION_FAILED),  # missing field: never defaulted
    (json.dumps(VALID | {"intent": "READY_TO_BUY"}), LLMErrorCode.SCHEMA_VALIDATION_FAILED),  # unknown enum: no fuzzy match
    (json.dumps(VALID | {"intent": "pricing_request"}), LLMErrorCode.SCHEMA_VALIDATION_FAILED),  # wrong case is wrong
    (json.dumps(VALID | {"confidence": 0.9}), LLMErrorCode.SCHEMA_VALIDATION_FAILED),  # wrong type
])
def test_invalid_model_output_is_refused_never_repaired(provider: str, text: str, code: LLMErrorCode) -> None:
    with pytest.raises(LLMStructuredOutputError) as error:
        classify(provider, FakeSession(ok(provider, text)))
    assert error.value.code is code


@pytest.mark.parametrize("provider", PROVIDERS)
def test_valid_output_and_an_exact_fence_are_accepted(provider: str) -> None:
    for text in (json.dumps(VALID), f"```json\n{json.dumps(VALID)}\n```"):
        assert classify(provider, FakeSession(ok(provider, text))).proposal.intent.value == "PRICING_REQUEST"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_refusals_and_truncations_are_typed_failures(provider: str) -> None:
    for response, code in ((refusal(provider), LLMErrorCode.CONTENT_BLOCKED), (truncated(provider), LLMErrorCode.OUTPUT_TRUNCATED)):
        with pytest.raises(LLMProviderError) as error:
            classify(provider, FakeSession(response))
        assert error.value.code is code


# ---- Qualification ----------------------------------------------------------------------------------------------

CUSTOMER = ("Hi! We need to automate invoice matching for 40 people and want the Basic plan. "
            "Timeline: Q3 2026. I'm the Head of finance and I decide.")
FIELDS = ("company_size", "decision_role", "need", "product_interest", "timeframe")


def qualification(answer: dict, provider: str = "openai", text: str = CUSTOMER) -> tuple[object, FakeSession]:
    session = FakeSession(ok(provider, json.dumps(answer)))
    request = ExtractionRequest(lead_id=LEAD, message_id=MESSAGE, message_text=text, fields=FIELDS,
                                known_facts=(KnownFact(field="need", value="Something else"),))
    return LLMQualificationExtractor(structured(provider, session)).extract(request), session


def fact(field: str, value: str, quote: str, confidence: str = "HIGH") -> dict:
    return {"field": field, "value": value, "confidence": confidence, "quote": quote}


@pytest.mark.parametrize("provider", PROVIDERS)
def test_grounded_facts_become_exactly_the_stage12_contract(provider: str) -> None:
    result, session = qualification({"facts": [
        fact("need", "Automate invoice matching", "automate invoice matching"),
        fact("company_size", "40 people", "for 40 people", "MEDIUM"),
        fact("timeframe", "Q3 2026", "Timeline: Q3 2026."),
    ], "missing_fields": ["decision_role"]}, provider)
    assert [(p.field, p.value, p.confidence) for p in result.proposals] == [  # type: ignore[attr-defined]
        ("need", "Automate invoice matching", ConfidenceBand.HIGH), ("company_size", "40 people", ConfidenceBand.MEDIUM),
        ("timeframe", "Q3 2026", ConfidenceBand.HIGH)]
    assert result.missing_fields == ("decision_role",)  # type: ignore[attr-defined]
    system, user = prompts(session.posts[0])
    data = sections(user)
    assert data["customer_message"] == CUSTOMER and data["fields"] == list(FIELDS)
    assert "<<<UNTRUSTED_DATA:customer_message>>>" in user and "<<<UNTRUSTED_DATA:known_facts>>>" in user
    assert "unknown stays unknown" in system and "QualificationCandidates" in system


@pytest.mark.parametrize(("answer", "why"), [
    ({"facts": [fact("need", "Automate invoice matching", "we want to automate payroll")]}, "quote not in message"),
    ({"facts": [fact("company_size", "400 people", "for 40 people")]}, "a number the customer did not write"),
    ({"facts": [fact("company_size", "forty", "40 people") | {"value": "50"}]}, "a number the customer did not write"),
    ({"facts": [fact("budget", "10000 EUR", "Basic plan")]}, "a field that was not requested"),
    ({"facts": [fact("need", "A", "automate invoice matching"), fact("need", "B", "Basic plan")]}, "the same field twice"),
    ({"facts": [], "missing_fields": ["budget"]}, "an unrequested missing field"),
])
def test_ungrounded_qualification_is_refused_whole(answer: dict, why: str) -> None:
    with pytest.raises(LLMContractViolationError):
        qualification(answer)


def test_qualification_schema_violations_are_refused() -> None:
    for answer in ({"facts": [{"field": "need", "value": "x", "confidence": "CERTAIN", "quote": "Hi"}]},
                   {"facts": [{"field": "need", "value": "x", "confidence": "HIGH"}]},  # no quote: never defaulted
                   {"facts": [], "qualified": True}):
        with pytest.raises(LLMStructuredOutputError):
            qualification(answer)


# ---- Commercial --------------------------------------------------------------------------------------------------

ASK = "Thanks for the proposal. Can you do €999? We would also need net 60 payment terms and 20% off the setup."


def commercial(answer: dict, text: str = ASK, provider: str = "openai", currency: str | None = "EUR") -> object:
    session = FakeSession(ok(provider, json.dumps(answer)))
    request = CommercialExtractionRequest(lead_id=LEAD, opportunity_id=OPP, message_id=MESSAGE, message_text=text,
                                          currency=currency, known_terms=(KnownTerm(term_type=TermType.PRICE, value="1200 EUR"),))
    return LLMCommercialExtractor(structured(provider, session)).extract(request)


def money(amount: str, currency: str = "EUR") -> dict:
    return {"kind": "MONEY", "money": {"amount": amount, "currency": currency}}


def term(term_type: str, value: dict, quote: str) -> dict:
    return {"term_type": term_type, "value": value, "quote": quote}


@pytest.mark.parametrize("provider", PROVIDERS)
def test_customer_requests_are_extracted_as_requests_only(provider: str) -> None:
    result = commercial({"requested_terms": [
        term("PRICE", money("999"), "Can you do €999?"),
        term("PAYMENT_TERM", {"kind": "TEXT", "text": "NET_60"}, "net 60 payment terms"),
        term("DISCOUNT", {"kind": "PERCENT", "percent": "20"}, "20% off the setup"),
    ]}, provider=provider)
    kinds = [(t.term_type, t.value.kind) for t in result.requested_terms]  # type: ignore[attr-defined]
    assert kinds == [(TermType.PRICE, ValueKind.MONEY), (TermType.PAYMENT_TERM, ValueKind.TEXT), (TermType.DISCOUNT, ValueKind.PERCENT)]
    assert str(result.requested_terms[0].value.money.amount) == "999"  # type: ignore[attr-defined]
    assert result.acceptance_signal is False and result.decline_signal is False  # type: ignore[attr-defined]


@pytest.mark.parametrize(("answer", "text"), [
    ({"requested_terms": [term("PRICE", money("899"), "Can you do €999?")]}, ASK),  # an invented price
    ({"requested_terms": [term("PRICE", money("999", "USD"), "Can you do €999?")]}, ASK),  # an invented currency
    ({"requested_terms": [term("DISCOUNT", {"kind": "PERCENT", "percent": "90"}, "20% off the setup")]}, ASK),
    ({"requested_terms": [term("DISCOUNT", {"kind": "PERCENT", "percent": "20"}, "twenty percent off")]},
     "Could you give us twenty percent off?"),  # words are never turned into numbers
    ({"requested_terms": [term("SLA", {"kind": "TEXT", "text": "99.99% uptime"}, "net 60 payment terms")]}, ASK),
    ({"acceptance_quote": "We accept your proposal."}, ASK),  # an acceptance the customer never wrote
    ({"decline_quote": "We went with another vendor."}, ASK),
    ({"objections": [{"category": "PRICE", "summary": "Too expensive", "quote": "this is too expensive"}]}, ASK),
])
def test_invented_commercial_values_are_refused_whole(answer: dict, text: str) -> None:
    with pytest.raises(LLMContractViolationError):
        commercial(answer, text)


def test_signals_and_objections_need_the_customers_words() -> None:
    result = commercial({"acceptance_quote": "we accept", "objections": [
        {"category": "TIMING", "summary": "Wants to start later", "quote": "start in October"}]},
        text="Great, we accept. We can only start in October though.")
    assert result.acceptance_signal is True and result.objections[0].summary == "Wants to start later"  # type: ignore[attr-defined]


def test_commercial_schema_violations_are_refused() -> None:
    for answer in ({"approved_terms": [term("PRICE", money("999"), "Can you do €999?")]},
                   {"requested_terms": [term("PRICE", {"kind": "MONEY", "money": {"amount": "999"}}, "Can you do €999?")]},
                   {"requested_terms": [term("FREE_STUFF", money("1"), "x")]},
                   {"mark_won": True}):
        with pytest.raises(LLMStructuredOutputError):
            commercial(answer)


# ---- Advisor ---------------------------------------------------------------------------------------------------


def test_the_advisor_recommends_only_and_cites_nothing_it_was_not_given() -> None:
    data = AdvisorInput(lead_id=LEAD, stage=LeadStage.ENGAGED, qualification_status=QualificationStatus.IN_PROGRESS,
                        missing_required=("timeframe",))
    answer = {"proposed_stage": "QUALIFIED", "proposed_action": None, "recommend_opportunity": False,
              "reasons": ["Ask for the timeframe."]}
    session = FakeSession(ok("anthropic", json.dumps(answer)))
    recommendation = LLMSalesAdvisor(structured("anthropic", session)).recommend(data)
    assert recommendation.proposed_stage is LeadStage.QUALIFIED and recommendation.reasons == ("Ask for the timeframe.",)
    with pytest.raises(LLMContractViolationError):
        LLMSalesAdvisor(structured("anthropic", FakeSession(ok("anthropic", json.dumps(answer | {"evidence_ids": [LEAD]}))))) \
            .recommend(data)
    with pytest.raises(LLMError):
        LLMSalesAdvisor(structured("anthropic", FakeSession(ok("anthropic", json.dumps(answer | {"approve": True}))))) \
            .recommend(data)
