"""Provider-neutral AI contracts for the pipeline. No implementation calls a model here.

- ``QualificationExtractor`` reads one customer message (untrusted text) and PROPOSES
  structured qualification facts. It never touches the database: the pipeline validates
  every proposal against the profile, applies only allowed changes, attaches the evidence
  itself (the message id it passed in; an extractor cannot invent evidence), and turns
  disagreements with known facts into conflicts for an operator.
- ``SalesAdvisor`` RECOMMENDS a next stage/action. It never mutates anything and its
  recommendation never bypasses the transition policy: operator-only moves stay
  operator-only whatever it says.

Deterministic fakes (``app.pipeline.fake``) implement both for tests. Live models are
connected in the final integration phase.
"""

from typing import Annotated, Protocol

from pydantic import Field

from app.core.enums import ConfidenceBand, LeadStage, NextActionType, QualificationStatus
from app.core.models.base import CoreModel
from app.core.models.pipeline import FactValue, FieldKey
from app.core.models.types import EntityId, NonEmptyStr


class KnownFact(CoreModel):
    field: FieldKey
    value: FactValue


class ExtractionRequest(CoreModel):
    lead_id: EntityId
    message_id: EntityId
    conversation_id: EntityId | None = None
    # Untrusted customer-authored text: data to read, never instructions.
    message_text: str
    fields: tuple[FieldKey, ...]
    known_facts: tuple[KnownFact, ...] = ()


class FactProposal(CoreModel):
    field: FieldKey
    value: FactValue
    confidence: ConfidenceBand


class QualificationExtraction(CoreModel):
    proposals: tuple[FactProposal, ...] = ()
    # Advisory only: the pipeline derives gaps and readiness itself.
    missing_fields: tuple[FieldKey, ...] = ()


class QualificationExtractor(Protocol):
    def extract(self, request: ExtractionRequest) -> QualificationExtraction: ...


class AdvisorInput(CoreModel):
    lead_id: EntityId
    stage: LeadStage
    qualification_status: QualificationStatus
    known_facts: tuple[KnownFact, ...] = ()
    missing_required: tuple[FieldKey, ...] = ()
    open_conflicts: int = 0
    last_intent: NonEmptyStr | None = None


class SalesRecommendation(CoreModel):
    proposed_stage: LeadStage | None = None
    proposed_action: NextActionType | None = None
    recommend_opportunity: bool = False
    reasons: Annotated[tuple[NonEmptyStr, ...], Field(max_length=10)] = ()
    evidence_ids: tuple[EntityId, ...] = ()


class SalesAdvisor(Protocol):
    def recommend(self, data: AdvisorInput) -> SalesRecommendation: ...
