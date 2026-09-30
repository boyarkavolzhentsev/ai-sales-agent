"""Sales pipeline (Stage 12) durable models: a lead's qualification and its opportunity.

Unknown stays unknown: a qualification fact exists only when there is evidence for it,
and an opportunity's value or decision date is None unless someone knows it. No
probability is modelled at all (it would only ever be invented).
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Self

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from app.core.enums import (
    ConfidenceBand,
    ConflictResolution,
    ConflictStatus,
    DisqualificationReason,
    FactSource,
    LostReason,
    OpportunityStatus,
    QualificationStatus,
)
from app.core.models.base import CoreModel
from app.core.models.types import EntityId, NonEmptyStr, Version
from app.core.validation import ensure_not_before

# A profile-defined qualification field key, e.g. "need" or "timeframe".
FieldKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
# A normalized, extracted value: short text, never an email body.
FactValue = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


def same_value(a: str, b: str) -> bool:
    """Values are compared case- and whitespace-insensitively."""
    return " ".join(a.casefold().split()) == " ".join(b.casefold().split())


class FactEvidence(CoreModel):
    """Why the system believes a fact: IDs only, never message text."""

    source: FactSource
    message_id: EntityId | None = None
    conversation_id: EntityId | None = None
    operator_command_id: EntityId | None = None
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.source is FactSource.EXTRACTION and self.message_id is None:
            raise ValueError("an extracted fact needs the message it came from")
        if self.source is FactSource.OPERATOR and self.operator_command_id is None:
            raise ValueError("an operator fact needs the command that recorded it")
        return self


class QualificationFact(CoreModel):
    field: FieldKey
    value: FactValue
    confidence: ConfidenceBand
    evidence: Annotated[tuple[FactEvidence, ...], Field(min_length=1)]


class QualificationConflict(CoreModel):
    """A proposed value that disagrees with a known fact. The known fact is kept until an
    operator resolves the conflict; nothing is overwritten silently."""

    conflict_id: EntityId
    field: FieldKey
    current_value: FactValue
    proposed_value: FactValue
    evidence: FactEvidence
    status: ConflictStatus = ConflictStatus.OPEN
    resolution: ConflictResolution | None = None
    resolved_by: NonEmptyStr | None = None
    resolved_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        resolved = self.status is ConflictStatus.RESOLVED
        if resolved != (self.resolution is not None) or resolved != (self.resolved_at is not None):
            raise ValueError("resolution and resolved_at are required for, and only allowed on, RESOLVED")
        return self


class LeadQualification(CoreModel):
    lead_id: EntityId
    profile_id: NonEmptyStr
    status: QualificationStatus
    facts: tuple[QualificationFact, ...] = ()
    conflicts: tuple[QualificationConflict, ...] = ()
    disqualification_reason: DisqualificationReason | None = None
    decided_by: NonEmptyStr | None = None  # operator who approved or disqualified
    decided_at: AwareDatetime | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    version: Version = 1

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.status is QualificationStatus.NOT_STARTED:
            raise ValueError("NOT_STARTED is the absence of a record, never stored")
        if (self.status is QualificationStatus.DISQUALIFIED) != (self.disqualification_reason is not None):
            raise ValueError("a disqualification reason is required for, and only allowed on, DISQUALIFIED")
        decided = self.status in (QualificationStatus.QUALIFIED, QualificationStatus.DISQUALIFIED)
        if decided != (self.decided_by is not None and self.decided_at is not None):
            raise ValueError("decided_by/decided_at are required for, and only allowed on, a decided qualification")
        fields = [fact.field for fact in self.facts]
        if len(fields) != len(set(fields)):
            raise ValueError("one fact per field")
        ids = [c.conflict_id for c in self.conflicts]
        if len(ids) != len(set(ids)):
            raise ValueError("conflict ids must be unique")
        ensure_not_before(self.updated_at, self.created_at, "updated_at", "created_at")
        return self

    def fact(self, field: str) -> QualificationFact | None:
        return next((f for f in self.facts if f.field == field), None)

    @property
    def open_conflicts(self) -> tuple[QualificationConflict, ...]:
        return tuple(c for c in self.conflicts if c.status is ConflictStatus.OPEN)


ACTIVE_OPPORTUNITY_STATUSES = frozenset({OpportunityStatus.OPEN, OpportunityStatus.NEGOTIATING})


class Opportunity(CoreModel):
    """Commercial work on a qualified lead. Created only by an operator."""

    opportunity_id: EntityId
    lead_id: EntityId
    status: OpportunityStatus = OpportunityStatus.OPEN
    amount: Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2)] | None = None
    currency: CurrencyCode | None = None
    scope: ShortText | None = None
    expected_decision_date: date | None = None
    next_step: ShortText | None = None
    owner_operator_id: NonEmptyStr
    lost_reason: LostReason | None = None
    closed_at: AwareDatetime | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    version: Version = 1

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.amount is None) != (self.currency is None):
            raise ValueError("amount and currency are known together or not at all")
        closed = self.status not in ACTIVE_OPPORTUNITY_STATUSES
        if closed != (self.closed_at is not None):
            raise ValueError("closed_at is required for, and only allowed on, a closed opportunity")
        if (self.status is OpportunityStatus.LOST) != (self.lost_reason is not None):
            raise ValueError("lost_reason is required for, and only allowed on, LOST")
        ensure_not_before(self.updated_at, self.created_at, "updated_at", "created_at")
        return self
