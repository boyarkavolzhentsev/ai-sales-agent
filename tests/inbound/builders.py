"""Builders for inbound-flow tests. All data is fictional; the knowledge base is the Stage 4
fixture tree ("Samplewidget Co"), valid at NOW."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.enums import LeadIntent
from app.inbound import InboundConfig, InboundEnvelope, InboundResult, InboundService
from app.llm import (
    FakeLLMTransport,
    FakeResponse,
    LLMRawOutput,
    LLMRequest,
    LLMTask,
    SectionKind,
    SenderIdentity,
    StructuredLLM,
)
from app.llm.fake import FAKE_MODEL, FAKE_PROVIDER
from app.llm.models import canonical_json
from app.persistence import Database, FrozenClock

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
MAILBOX = "sales@ourco.example"
SENDER = "buyer@prospect.example"
PRICE_QUESTION = "What does the Basic plan cost per month?"


def envelope(
    provider_message_id: str = "p-1",
    body: str = "Hi, how much does the Basic plan cost per month?",
    *,
    sender: str = SENDER,
    subject: str = "Pricing question",
    internet_message_id: str | None = "default",
    received_at: datetime = NOW,
    **overrides: object,
) -> InboundEnvelope:
    if internet_message_id == "default":
        internet_message_id = f"<{provider_message_id}@prospect.example>"
    raw = f"{provider_message_id}|{sender}|{subject}|{body}"
    data: dict[str, object] = {
        "provider": "fake",
        "provider_message_id": provider_message_id,
        "internet_message_id": internet_message_id,
        "mailbox": MAILBOX,
        "from_address": sender,
        "to_addresses": (MAILBOX,),
        "subject": subject,
        "body_text": body,
        "received_at": received_at,
        "raw_ref": f"raw/{provider_message_id}.eml",
        "raw_hash": hashlib.sha256(raw.encode()).hexdigest(),
    }
    return InboundEnvelope.model_validate(data | overrides)


def classification(
    intent: LeadIntent,
    *questions: str,
    confidence: str = "HIGH",
    review: bool | None = None,
    **extra: object,
) -> FakeResponse:
    needs_review = review if review is not None else intent in (
        LeadIntent.NEGOTIATION, LeadIntent.LEGAL_OR_COMPLAINT, LeadIntent.UNCLEAR
    ) or confidence == "LOW"
    return FakeResponse.of(
        {
            "intent": intent.value,
            "confidence": confidence,
            "detected_language": "en",
            "extracted_questions": list(questions),
            "needs_operator_review": needs_review,
            "rationale_summary": f"Classified as {intent.value}.",
        }
        | extra
    )


def sufficiency(opinion: str = "SUFFICIENT") -> FakeResponse:
    return FakeResponse.of({"opinion": opinion, "rationale_summary": "Checked."})


@dataclass(frozen=True)
class ComposerScript:
    body: str
    subject: str = "Re: Pricing question"
    next_step: str = "ANSWER_QUESTIONS"
    cite: Callable[[dict[str, str]], bool] = lambda evidence: True
    extra_ids: tuple[str, ...] = ()
    # Runs while the composer "thinks": Phase B holds no transaction, so this simulates a
    # concurrent worker or operator changing state between analysis and finalization.
    before: Callable[[], None] | None = None


class ScriptedTransport(FakeLLMTransport):
    """The Stage 5 fake, plus composer responses that cite the evidence actually supplied
    in the request (evidence IDs are deterministic hashes, so tests do not hard-code them)."""

    def __init__(self) -> None:
        super().__init__()
        self.composer_scripts: list[ComposerScript] = []

    def compose(self, script: ComposerScript) -> "ScriptedTransport":
        self.composer_scripts.append(script)
        return self

    def generate(self, request: LLMRequest) -> LLMRawOutput:
        if request.task is LLMTask.REPLY_COMPOSITION and self.composer_scripts:
            self.requests.append(request)
            script = self.composer_scripts.pop(0)
            if script.before is not None:
                script.before()
            section = next(s for s in request.sections if s.kind is SectionKind.TRUSTED_EVIDENCE)
            evidence = json.loads(section.content)
            ids = [e["evidence_id"] for e in evidence if script.cite(e)] + list(script.extra_ids)
            text = canonical_json(
                {"subject": script.subject, "body": script.body, "evidence_ids_used": ids, "proposed_next_step": script.next_step}
            )
            return LLMRawOutput(text=text, model_name=FAKE_MODEL, provider_name=FAKE_PROVIDER)
        return super().generate(request)

    def calls(self, task: LLMTask) -> int:
        return sum(1 for r in self.requests if r.task is task)


def config() -> InboundConfig:
    return InboundConfig(
        own_addresses=(MAILBOX,),
        sender=SenderIdentity(sender_name="Alex Seller", company_name="Samplewidget Co"),
        code_version="stage6-test",
    )


def service(db: Database, transport: FakeLLMTransport, *, clock: FrozenClock | None = None) -> InboundService:
    clock = clock or FrozenClock(NOW)
    return InboundService(db, StructuredLLM(transport, clock), clock, config())


def happy_transport() -> ScriptedTransport:
    transport = ScriptedTransport()
    transport.script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.PRICING_REQUEST, PRICE_QUESTION))
    transport.script(LLMTask.KNOWLEDGE_SUFFICIENCY, sufficiency())
    transport.compose(
        ComposerScript(body="Hi, the Basic plan costs 100 EUR per month.", cite=lambda e: "100 EUR" in e["excerpt"])
    )
    return transport


def process(db: Database, transport: FakeLLMTransport, env: InboundEnvelope | None = None, *, correlation_id: str = "corr-1") -> InboundResult:
    return service(db, transport).process(env or envelope(), correlation_id=correlation_id)


LATER = NOW + timedelta(hours=1)
