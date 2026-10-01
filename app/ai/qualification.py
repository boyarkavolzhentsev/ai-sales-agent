"""LLM-backed Stage 12 ``QualificationExtractor`` and ``SalesAdvisor``.

The extractor returns exactly the existing ``QualificationExtraction``: the model's
grounded candidates are checked (allowed field, verbatim quote, numbers as written) and
then reduced to the contract; nothing is invented or defaulted, and the pipeline still
applies its own profile, confidence and conflict rules. The advisor returns the existing
``SalesRecommendation``, which is never applied automatically.
"""

from typing import Annotated

from pydantic import Field, StringConstraints

from app.ai.grounding import bounded, require_numbers_in_quote, require_quote
from app.ai.prompts import QUALIFICATION_EXTRACTOR_PROMPT_V1, SALES_ADVISOR_PROMPT_V1
from app.core.enums import ConfidenceBand
from app.core.models.base import CoreModel
from app.core.models.pipeline import FactValue, FieldKey
from app.llm import LLMContractViolationError, SectionKind, StructuredLLM
from app.llm.prompts import build_request, section
from app.pipeline.contracts import (
    AdvisorInput,
    ExtractionRequest,
    FactProposal,
    QualificationExtraction,
    SalesRecommendation,
)

Quote = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class GroundedFact(CoreModel):
    field: FieldKey
    value: FactValue
    confidence: ConfidenceBand
    quote: Quote


class QualificationCandidates(CoreModel):
    """What the model returns (validated strictly): the contract plus a quote per fact."""

    facts: Annotated[tuple[GroundedFact, ...], Field(max_length=20)] = ()
    missing_fields: Annotated[tuple[FieldKey, ...], Field(max_length=40)] = ()


class LLMQualificationExtractor:
    def __init__(self, llm: StructuredLLM, *, locale: str = "en") -> None:
        self._llm = llm
        self._locale = locale

    def extract(self, request: ExtractionRequest) -> QualificationExtraction:
        message = bounded(request.message_text)
        call = build_request(
            QUALIFICATION_EXTRACTOR_PROMPT_V1, QualificationCandidates, correlation_id=request.message_id, locale=self._locale,
            sections=[
                section(SectionKind.TRUSTED_METADATA, "fields", list(request.fields)),
                # Known facts came from earlier customer messages: still data, never instructions.
                section(SectionKind.UNTRUSTED_DATA, "known_facts", [f.model_dump(mode="json") for f in request.known_facts]),
                section(SectionKind.UNTRUSTED_DATA, "customer_message", message),
            ],
        )
        candidates = self._llm.complete_structured(call).output
        allowed = set(request.fields)
        seen: set[str] = set()
        for fact in candidates.facts:
            if fact.field not in allowed:
                raise LLMContractViolationError(f"field {fact.field} was not requested")
            if fact.field in seen:
                raise LLMContractViolationError(f"field {fact.field} proposed twice")
            seen.add(fact.field)
            require_quote(fact.quote, message, fact.field)
            require_numbers_in_quote(fact.value, fact.quote, fact.field)
        if not set(candidates.missing_fields) <= allowed:
            raise LLMContractViolationError("missing_fields lists a field that was not requested")
        return QualificationExtraction(
            proposals=tuple(FactProposal(field=f.field, value=f.value, confidence=f.confidence) for f in candidates.facts),
            missing_fields=candidates.missing_fields,
        )


class LLMSalesAdvisor:
    def __init__(self, llm: StructuredLLM, *, locale: str = "en") -> None:
        self._llm = llm
        self._locale = locale

    def recommend(self, data: AdvisorInput) -> SalesRecommendation:
        facts = data.model_dump(mode="json", include={"stage", "qualification_status", "missing_required", "open_conflicts"})
        call = build_request(
            SALES_ADVISOR_PROMPT_V1, SalesRecommendation, correlation_id=data.lead_id, locale=self._locale,
            sections=[
                section(SectionKind.TRUSTED_METADATA, "lead", facts),
                section(SectionKind.UNTRUSTED_DATA, "known_facts", [f.model_dump(mode="json") for f in data.known_facts]),
                section(SectionKind.UNTRUSTED_DATA, "last_intent", data.last_intent),
            ],
        )
        recommendation = self._llm.complete_structured(call).output
        if recommendation.evidence_ids:
            raise LLMContractViolationError("the advisor cites evidence it was not given")
        return recommendation
