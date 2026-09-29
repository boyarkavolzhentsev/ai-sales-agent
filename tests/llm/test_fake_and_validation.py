import pytest

from app.core.enums import ConfidenceBand, LeadIntent
from app.llm import (
    ClassificationOutcome,
    ClassifierInput,
    FakeResponse,
    LLMNoScriptedResponseError,
    LLMProviderError,
    LLMStructuredOutputError,
    LLMTask,
    LLMTimeoutError,
    LLMTransport,
    classify_intent,
)
from app.llm.fake import FAKE_MODEL, FAKE_PROVIDER, FakeLLMTransport
from app.llm.models import sha256_hex
from tests.llm.builders import T0, email, llm

TASK = LLMTask.INTENT_CLASSIFICATION


def classifier_input(body: str = "What does it cost?") -> ClassifierInput:
    return ClassifierInput(message_id="msg-1", latest_message=email(body), locale="en")


def proposal(intent: LeadIntent = LeadIntent.PRICING_REQUEST, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "intent": intent,
        "confidence": ConfidenceBand.HIGH,
        "detected_language": "en",
        "needs_operator_review": False,
        "rationale_summary": "Asks about price.",
    }
    return base | overrides


def run(fake: FakeLLMTransport, data: ClassifierInput | None = None) -> ClassificationOutcome:
    structured, _ = llm(fake)
    return classify_intent(structured, data or classifier_input(), correlation_id="corr-1")


# ---- Fake adapter --------------------------------------------------------------------------


def test_fake_is_a_transport() -> None:
    assert isinstance(FakeLLMTransport(), LLMTransport)


def test_scripted_response_is_returned_with_metadata() -> None:
    fake = FakeLLMTransport().script(TASK, FakeResponse.of(proposal()))
    outcome = run(fake)
    assert outcome.proposal.intent is LeadIntent.PRICING_REQUEST
    meta = outcome.metadata
    assert (meta.provider_name, meta.model_name, meta.attempt, meta.created_at) == (FAKE_PROVIDER, FAKE_MODEL, 1, T0)
    assert (meta.task, meta.prompt_id, meta.prompt_version) == (TASK, "intent_classifier", "1")
    assert meta.input_hash == fake.requests[0].input_hash
    assert meta.output_hash == sha256_hex(FakeResponse.of(proposal()).text)


def test_requests_are_recorded_and_served_in_order() -> None:
    fake = FakeLLMTransport().script(
        TASK, FakeResponse.of(proposal(LeadIntent.INFO_REQUEST)), FakeResponse.of(proposal(LeadIntent.OBJECTION))
    )
    first = run(fake, classifier_input("first"))
    second = run(fake, classifier_input("second"))
    assert (first.proposal.intent, second.proposal.intent) == (LeadIntent.INFO_REQUEST, LeadIntent.OBJECTION)
    assert len(fake.requests) == 2 and fake.pending(TASK) == 0
    assert '"first"' in fake.requests[0].sections[-1].content


def test_missing_script_is_an_explicit_error() -> None:
    with pytest.raises(LLMNoScriptedResponseError):
        run(FakeLLMTransport())
    fake = FakeLLMTransport().script(LLMTask.REPLY_COMPOSITION, FakeResponse.raw("{}"))
    with pytest.raises(LLMNoScriptedResponseError):
        run(fake)  # scripts are per task


def test_timeout_provider_error_and_crash_are_typed() -> None:
    with pytest.raises(LLMTimeoutError):
        run(FakeLLMTransport().script(TASK, FakeResponse.timeout()))
    with pytest.raises(LLMProviderError):
        run(FakeLLMTransport().script(TASK, FakeResponse.provider_error()))
    with pytest.raises(LLMProviderError) as info:
        run(FakeLLMTransport().script(TASK, FakeResponse.crash()))
    assert not isinstance(info.value, RuntimeError)  # raw exception wrapped at the boundary


# ---- Structured-output validation ----------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse.raw("not json"),
        FakeResponse.raw('{"intent": "PRICING_REQUEST"'),
        FakeResponse.raw("[]"),
        FakeResponse.raw('"PRICING_REQUEST"'),
        FakeResponse.of({k: v for k, v in proposal().items() if k != "confidence"}),  # missing field
        FakeResponse.of(proposal(send_now=True)),  # extra field
        FakeResponse.of(proposal(intent="BUY_NOW")),  # unknown enum
        FakeResponse.of(proposal(confidence=0.97)),  # numbers are not confidence bands
        FakeResponse.of(proposal(detected_language="English")),
        FakeResponse.of(proposal(rationale_summary="")),
        FakeResponse.of(proposal(secondary_intents=["PRICING_REQUEST"])),
    ],
)
def test_invalid_output_is_rejected_not_repaired(response: FakeResponse) -> None:
    with pytest.raises(LLMStructuredOutputError):
        run(FakeLLMTransport().script(TASK, response))


def test_input_hash_is_deterministic_and_ignores_correlation_id() -> None:
    fake = FakeLLMTransport().script(TASK, FakeResponse.of(proposal()), FakeResponse.of(proposal()), FakeResponse.of(proposal()))
    structured, _ = llm(fake)
    classify_intent(structured, classifier_input("same"), correlation_id="corr-a")
    classify_intent(structured, classifier_input("same"), correlation_id="corr-b")
    classify_intent(structured, classifier_input("different"), correlation_id="corr-a")
    hashes = [r.input_hash for r in fake.requests]
    assert hashes[0] == hashes[1] != hashes[2]
