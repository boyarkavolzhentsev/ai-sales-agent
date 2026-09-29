import pytest

from app.core.enums import ConfidenceBand, EmailDirection, LeadIntent, LeadStage, LeadStatus, RiskFlag
from app.llm import (
    ClassificationOutcome,
    ClassifierInput,
    FakeLLMTransport,
    FakeResponse,
    LLMContractViolationError,
    LLMStructuredOutputError,
    LLMTask,
    LLMTimeoutError,
    SectionKind,
    classify_intent,
    to_intent_classification,
)
from tests.llm.builders import T0, email, llm

TASK = LLMTask.INTENT_CLASSIFICATION


def data(body: str = "Hello") -> ClassifierInput:
    return ClassifierInput(
        message_id="msg-1",
        latest_message=email(body),
        thread_context=(email("Earlier outreach", direction=EmailDirection.OUTBOUND),),
        lead_stage=LeadStage.CONTACTED,
        lead_status=LeadStatus.AUTOMATED,
        prefilter_flags=("NONE",),
        locale="en",
    )


def proposal(intent: LeadIntent, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "intent": intent,
        "confidence": ConfidenceBand.HIGH,
        "detected_language": "en",
        "needs_operator_review": intent in (LeadIntent.NEGOTIATION, LeadIntent.LEGAL_OR_COMPLAINT, LeadIntent.UNCLEAR),
        "rationale_summary": f"Looks like {intent}.",
    }
    return base | overrides


def classify(response: FakeResponse) -> ClassificationOutcome:
    structured, _ = llm(FakeLLMTransport().script(TASK, response))
    return classify_intent(structured, data(), correlation_id="corr-1")


@pytest.mark.parametrize(
    "intent",
    [
        LeadIntent.INFO_REQUEST,
        LeadIntent.PRICING_REQUEST,
        LeadIntent.MEETING_REQUEST,
        LeadIntent.POSITIVE_INTEREST,
        LeadIntent.OBJECTION,
        LeadIntent.NEGOTIATION,
        LeadIntent.NOT_INTERESTED,
        LeadIntent.UNSUBSCRIBE,
        LeadIntent.REFERRAL,
        LeadIntent.LEGAL_OR_COMPLAINT,
        LeadIntent.NON_SALES,
        LeadIntent.UNCLEAR,
    ],
)
def test_representative_intents(intent: LeadIntent) -> None:
    outcome = classify(FakeResponse.of(proposal(intent)))
    assert outcome.proposal.intent is intent


@pytest.mark.parametrize("intent", [LeadIntent.NEGOTIATION, LeadIntent.LEGAL_OR_COMPLAINT, LeadIntent.UNCLEAR])
def test_review_required_intents_must_request_review(intent: LeadIntent) -> None:
    with pytest.raises(LLMContractViolationError, match="operator review"):
        classify(FakeResponse.of(proposal(intent, needs_operator_review=False)))


def test_low_confidence_must_request_review() -> None:
    with pytest.raises(LLMContractViolationError):
        classify(FakeResponse.of(proposal(LeadIntent.INFO_REQUEST, confidence=ConfidenceBand.LOW)))
    outcome = classify(
        FakeResponse.of(proposal(LeadIntent.INFO_REQUEST, confidence=ConfidenceBand.LOW, needs_operator_review=True))
    )
    assert outcome.proposal.needs_operator_review


def test_rich_proposal_maps_to_stage1_record() -> None:
    outcome = classify(
        FakeResponse.of(
            proposal(
                LeadIntent.PRICING_REQUEST,
                secondary_intents=["MEETING_REQUEST"],
                extracted_questions=["What does it cost?"],
                risk_flags=["SENSITIVE"],
                proposed_stage="INTERESTED",
            )
        )
    )
    record = to_intent_classification(outcome, message_id="msg-1", created_at=T0)
    assert (record.primary_intent, record.secondary_intents) == (LeadIntent.PRICING_REQUEST, (LeadIntent.MEETING_REQUEST,))
    assert (record.proposed_stage, record.risk_flags, record.language) == (LeadStage.INTERESTED, (RiskFlag.SENSITIVE,), "en")


def test_fail_closed() -> None:
    with pytest.raises(LLMTimeoutError):
        classify(FakeResponse.timeout())
    with pytest.raises(LLMStructuredOutputError):
        classify(FakeResponse.raw("{}"))


def test_request_separates_trusted_state_from_untrusted_email() -> None:
    structured, fake = llm(FakeLLMTransport().script(TASK, FakeResponse.of(proposal(LeadIntent.INFO_REQUEST))))
    classify_intent(structured, data("What is included?"), correlation_id="corr-1")
    [request] = fake.requests
    kinds = [(s.kind, s.label) for s in request.sections]
    assert kinds == [
        (SectionKind.INSTRUCTIONS, "intent_classifier"),
        (SectionKind.TRUSTED_METADATA, "context"),
        (SectionKind.UNTRUSTED_DATA, "thread_context"),
        (SectionKind.UNTRUSTED_DATA, "latest_message"),
    ]
    assert '"CONTACTED"' in request.sections[1].content
    assert "What is included?" in request.sections[3].content
