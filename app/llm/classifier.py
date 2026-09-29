"""Intent classification proposal. Side-effect free: returns a validated proposal only."""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Self

from pydantic import AfterValidator, Field, StringConstraints, model_validator

from app.core.enums import ConfidenceBand, LeadIntent, LeadStage, LeadStatus, RiskFlag
from app.core.models import IntentClassification
from app.core.models.base import CoreModel
from app.core.models.types import EntityId, LocaleTag, NonEmptyStr
from app.core.validation import unique_items
from app.llm.errors import LLMContractViolationError
from app.llm.inputs import UntrustedEmail
from app.llm.models import LLMResultMetadata, SectionKind
from app.llm.prompts import INTENT_CLASSIFIER_PROMPT_V1, build_request, section
from app.llm.validation import StructuredLLM

# Intents that always need a human, whatever the model's confidence.
REVIEW_REQUIRED_INTENTS: frozenset[LeadIntent] = frozenset(
    {LeadIntent.NEGOTIATION, LeadIntent.LEGAL_OR_COMPLAINT, LeadIntent.UNCLEAR}
)
MAX_THREAD_CONTEXT = 5

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class ClassifierInput(CoreModel):
    """Only what the classifier needs. Email text is untrusted; the rest is trusted state."""

    message_id: EntityId
    latest_message: UntrustedEmail
    thread_context: Annotated[tuple[UntrustedEmail, ...], Field(max_length=MAX_THREAD_CONTEXT)] = ()
    lead_stage: LeadStage | None = None
    lead_status: LeadStatus | None = None
    prefilter_flags: Annotated[tuple[NonEmptyStr, ...], AfterValidator(unique_items)] = ()
    locale: LocaleTag


class IntentClassificationProposal(CoreModel):
    """Categorical, not probabilistic: ``confidence`` is HIGH / MEDIUM / LOW."""

    intent: LeadIntent
    secondary_intents: Annotated[tuple[LeadIntent, ...], AfterValidator(unique_items)] = ()
    confidence: ConfidenceBand
    detected_language: LocaleTag
    extracted_questions: Annotated[tuple[ShortText, ...], AfterValidator(unique_items)] = ()
    risk_flags: Annotated[tuple[RiskFlag, ...], AfterValidator(unique_items)] = ()
    proposed_stage: LeadStage | None = None
    needs_operator_review: bool
    rationale_summary: ShortText
    classification_notes: tuple[ShortText, ...] = ()

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.intent in self.secondary_intents:
            raise ValueError("intent must not be repeated in secondary_intents")
        return self


@dataclass(frozen=True)
class ClassificationOutcome:
    proposal: IntentClassificationProposal
    metadata: LLMResultMetadata


def classify_intent(llm: StructuredLLM, data: ClassifierInput, *, correlation_id: str) -> ClassificationOutcome:
    """Validated proposal, or a typed LLMError. Never persists or changes state."""
    sections = [
        section(
            SectionKind.TRUSTED_METADATA,
            "context",
            {
                "message_id": data.message_id,
                "lead_stage": data.lead_stage,
                "lead_status": data.lead_status,
                "prefilter_flags": list(data.prefilter_flags),
                "locale": data.locale,
            },
        ),
        section(SectionKind.UNTRUSTED_DATA, "thread_context", [m.model_dump(mode="json") for m in data.thread_context]),
        section(SectionKind.UNTRUSTED_DATA, "latest_message", data.latest_message),
    ]
    call = build_request(
        INTENT_CLASSIFIER_PROMPT_V1,
        IntentClassificationProposal,
        correlation_id=correlation_id,
        locale=data.locale,
        sections=sections,
    )
    result = llm.complete_structured(call)
    proposal = result.output
    if not proposal.needs_operator_review and (
        proposal.confidence is ConfidenceBand.LOW or proposal.intent in REVIEW_REQUIRED_INTENTS
    ):
        raise LLMContractViolationError(
            f"{proposal.intent} with {proposal.confidence} confidence must request operator review"
        )
    return ClassificationOutcome(proposal=proposal, metadata=result.metadata)


def to_intent_classification(outcome: ClassificationOutcome, *, message_id: str, created_at: datetime) -> IntentClassification:
    """Map to the Stage 1 IntentClassification record (still a proposal, not state)."""
    proposal = outcome.proposal
    return IntentClassification(
        message_id=message_id,
        primary_intent=proposal.intent,
        secondary_intents=proposal.secondary_intents,
        extracted_questions=proposal.extracted_questions,
        proposed_stage=proposal.proposed_stage,
        confidence_band=proposal.confidence,
        risk_flags=proposal.risk_flags,
        language=proposal.detected_language,
        created_at=created_at,
    )
