"""LLM knowledge-sufficiency opinion: keep-or-downgrade only.

Severity (worst first): NOT_APPROVED > CONFLICTING > STALE > INSUFFICIENT > PARTIAL >
SUFFICIENT. An opinion less severe than the deterministic decision is an upgrade and is
rejected (LLMContractViolationError), never clamped or trusted.
"""

from dataclasses import dataclass
from typing import Annotated, Self

from pydantic import AfterValidator, StringConstraints, model_validator

from app.core.decisions import knowledge_severity
from app.core.enums import KnowledgeDecision
from app.core.models import KnowledgeAssessment, KnowledgeEvidence, KnowledgeQuery
from app.core.models.base import CoreModel
from app.core.validation import unique_items
from app.llm.errors import LLMContractViolationError
from app.llm.models import LLMResultMetadata, SectionKind
from app.llm.prompts import KNOWLEDGE_SUFFICIENCY_PROMPT_V1, build_request, section
from app.llm.validation import StructuredLLM

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class SufficiencyInput(CoreModel):
    query: KnowledgeQuery
    assessment: KnowledgeAssessment
    evidence: tuple[KnowledgeEvidence, ...]

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.assessment.query_id != self.query.query_id:
            raise ValueError("assessment is for a different query")
        if any(e.query_id != self.query.query_id for e in self.evidence):
            raise ValueError("evidence is for a different query")
        unique_items(tuple(e.evidence_id for e in self.evidence))
        if self.assessment.llm_sufficiency_opinion is not None:
            raise ValueError("assessment already carries an LLM opinion")
        return self


class KnowledgeSufficiencyOpinion(CoreModel):
    opinion: KnowledgeDecision
    rationale_summary: ShortText
    concerns: Annotated[tuple[ShortText, ...], AfterValidator(unique_items)] = ()


@dataclass(frozen=True)
class SufficiencyOutcome:
    opinion: KnowledgeSufficiencyOpinion
    assessment: KnowledgeAssessment  # the deterministic assessment with the opinion applied
    metadata: LLMResultMetadata


def apply_llm_sufficiency_opinion(
    assessment: KnowledgeAssessment, opinion: KnowledgeDecision
) -> KnowledgeAssessment:
    """Record the opinion; downgrade the decision if the opinion is more severe.

    Per-question results, flags and reasons are kept. An upward opinion raises."""
    if assessment.llm_sufficiency_opinion is not None:
        raise LLMContractViolationError("assessment already carries an LLM opinion")
    if knowledge_severity(opinion) < knowledge_severity(assessment.decision):
        raise LLMContractViolationError(
            f"LLM opinion {opinion} would upgrade deterministic decision {assessment.decision}"
        )
    flags = assessment.deterministic_flags
    reasons = assessment.reasons
    decision = assessment.decision
    if knowledge_severity(opinion) > knowledge_severity(decision):
        decision = opinion
        flags = (*flags, f"LLM_DOWNGRADE:{opinion}")
        reasons = (*reasons, f"LLM review downgraded {assessment.decision} to {opinion}")
    return KnowledgeAssessment.model_validate(
        assessment.model_dump()
        | {"decision": decision, "llm_sufficiency_opinion": opinion, "deterministic_flags": flags, "reasons": reasons}
    )


def assess_sufficiency(llm: StructuredLLM, data: SufficiencyInput, *, correlation_id: str) -> SufficiencyOutcome:
    sections = [
        section(SectionKind.TRUSTED_METADATA, "deterministic_assessment", data.assessment),
        section(SectionKind.TRUSTED_METADATA, "questions", list(data.query.questions)),
        section(
            SectionKind.TRUSTED_EVIDENCE,
            "evidence",
            [{"evidence_id": e.evidence_id, "domain": e.domain, "excerpt": e.excerpt} for e in data.evidence],
        ),
    ]
    call = build_request(
        KNOWLEDGE_SUFFICIENCY_PROMPT_V1,
        KnowledgeSufficiencyOpinion,
        correlation_id=correlation_id,
        locale=data.query.locale,
        sections=sections,
    )
    result = llm.complete_structured(call)
    merged = apply_llm_sufficiency_opinion(data.assessment, result.output.opinion)
    return SufficiencyOutcome(opinion=result.output, assessment=merged, metadata=result.metadata)
