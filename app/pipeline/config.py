"""Business-level pipeline configuration: the qualification profile and reopen targets.

The profile is data, not a hardcoded methodology: BANT-, MEDDIC-like or custom fields are
all just field specs. V1 ships a small generic profile. Operator approval of a
qualification is always required in V1.
"""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.core.enums import ConfidenceBand, LeadStage
from app.core.models.base import CoreModel
from app.core.models.pipeline import FactValue, FieldKey
from app.core.models.types import NonEmptyStr


class FieldSpec(CoreModel):
    key: FieldKey
    label: NonEmptyStr
    # Lower is more important; gaps are planned in this order.
    priority: Annotated[int, Field(ge=1, le=100)]
    # False: the agent should not ask this by itself (e.g. budget); an operator should.
    safe_to_ask: bool = True


class QualificationProfile(CoreModel):
    profile_id: NonEmptyStr
    required: Annotated[tuple[FieldSpec, ...], Field(min_length=1)]
    optional: tuple[FieldSpec, ...] = ()
    # Values that are a disqualification SIGNAL for an operator. Never applied
    # automatically: they only surface as a recommendation.
    disqualifying_values: dict[FieldKey, tuple[FactValue, ...]] = {}
    operator_approval_required: Literal[True] = True

    @model_validator(mode="after")
    def _check(self) -> Self:
        keys = [spec.key for spec in (*self.required, *self.optional)]
        if len(keys) != len(set(keys)):
            raise ValueError("qualification field keys must be unique")
        unknown = set(self.disqualifying_values) - set(keys)
        if unknown:
            raise ValueError(f"disqualifying values for unknown fields: {sorted(unknown)}")
        return self

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(spec.key for spec in (*self.required, *self.optional))

    @property
    def required_keys(self) -> frozenset[str]:
        return frozenset(spec.key for spec in self.required)

    def spec(self, key: str) -> FieldSpec | None:
        return next((spec for spec in (*self.required, *self.optional) if spec.key == key), None)


GENERIC_PROFILE = QualificationProfile(
    profile_id="generic-v1",
    required=(
        FieldSpec(key="need", label="Problem or need", priority=1),
        FieldSpec(key="product_interest", label="Product or service of interest", priority=2),
        FieldSpec(key="timeframe", label="Timeframe", priority=3),
        FieldSpec(key="decision_role", label="Role in the decision", priority=4),
    ),
    optional=(
        FieldSpec(key="budget", label="Budget", priority=5, safe_to_ask=False),
        FieldSpec(key="use_case", label="Use case", priority=6),
        FieldSpec(key="company_size", label="Company size or segment", priority=7),
        FieldSpec(key="geography", label="Geography or market", priority=8),
    ),
)

# Stages a closed lead may be reopened to (operator only). Never an operator stage: the
# commercial decisions after qualification must be taken again.
REOPEN_STAGES = frozenset({LeadStage.ENGAGED, LeadStage.QUALIFYING})


class PipelineConfig(CoreModel):
    profile: QualificationProfile = GENERIC_PROFILE
    reopen_targets: Annotated[tuple[LeadStage, ...], Field(min_length=1)] = (LeadStage.ENGAGED, LeadStage.QUALIFYING)
    # Extraction proposals below this confidence are ignored (unknown stays unknown).
    min_extraction_confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    queue_limit: Annotated[int, Field(ge=1, le=5000)] = 500

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not set(self.reopen_targets) <= REOPEN_STAGES:
            raise ValueError(f"reopen targets must be within {sorted(REOPEN_STAGES)}")
        if self.min_extraction_confidence is ConfidenceBand.LOW:
            raise ValueError("LOW-confidence extraction is never accepted as a fact")
        return self
