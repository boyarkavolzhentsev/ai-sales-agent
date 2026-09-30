"""Commercial decisioning (Stage 13) durable models.

No fabrication: every commercial value carries its provenance (``ValueSource``) and comes
from an operator command, an operator-approved customer request, an explicitly configured
profile default, or an approved internal knowledge fact. A customer's request is stored
as a request (``TermRequest``), never as an approved value. Unknown stays None.

Money is Decimal only (never float), one currency per proposal; tax is never computed.
"""

from decimal import Decimal
from typing import Annotated, Self

from pydantic import AwareDatetime, Field, PositiveInt, StringConstraints, model_validator

from app.core.enums import (
    ObjectionCategory,
    ObjectionStatus,
    RevisionStatus,
    SignalKind,
    SignalStatus,
    TermRequestStatus,
    TermSource,
    TermType,
    ValueKind,
)
from app.core.models.base import CoreModel
from app.core.models.pipeline import CurrencyCode, ShortText
from app.core.models.types import EntityId, NonEmptyStr, Version
from app.core.validation import ensure_not_before

Amount = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
Percent = Annotated[Decimal, Field(gt=0, lt=100, max_digits=7, decimal_places=4)]
Quantity = Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=4)]
TermKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")]
ItemRef = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")]
UnitName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
MAIN_KEY = "main"


class Money(CoreModel):
    amount: Amount
    currency: CurrencyCode


class CommercialValue(CoreModel):
    """A term value of exactly one kind: text (e.g. "NET_30"), money, or a percentage."""

    kind: ValueKind
    text: ShortText | None = None
    money: Money | None = None
    percent: Percent | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        present = {ValueKind.TEXT: self.text is not None, ValueKind.MONEY: self.money is not None,
                   ValueKind.PERCENT: self.percent is not None}
        if [k for k, v in present.items() if v] != [self.kind]:
            raise ValueError("a commercial value carries exactly the field of its kind")
        return self

    def display(self) -> str:
        if self.money is not None:
            return f"{self.money.amount} {self.money.currency}"
        if self.percent is not None:
            return f"{self.percent}%"
        return self.text or ""

    def same_as(self, other: "CommercialValue") -> bool:
        if self.kind is not other.kind:
            return False
        if self.text is not None and other.text is not None:
            return " ".join(self.text.casefold().split()) == " ".join(other.text.casefold().split())
        return self.money == other.money and self.percent == other.percent


class ValueSource(CoreModel):
    """Provenance of an approved commercial value. IDs only."""

    source: TermSource
    operator_id: NonEmptyStr | None = None
    command_id: EntityId | None = None
    request_id: EntityId | None = None
    knowledge_source_id: NonEmptyStr | None = None
    knowledge_source_version: PositiveInt | None = None
    fact_key: NonEmptyStr | None = None
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def _check(self) -> Self:
        needs_operator = self.source in (TermSource.OPERATOR, TermSource.TERM_REQUEST, TermSource.REVISION_OVERRIDE)
        if needs_operator and (self.operator_id is None or self.command_id is None):
            raise ValueError(f"{self.source} values need the operator and command that approved them")
        if self.source is TermSource.TERM_REQUEST and self.request_id is None:
            raise ValueError("an approved request value needs its request id")
        if self.source is TermSource.KNOWLEDGE and None in (self.knowledge_source_id, self.knowledge_source_version,
                                                             self.fact_key):
            raise ValueError("a knowledge value needs its source, version and fact key")
        return self


class CommercialTerm(CoreModel):
    """An approved, opportunity-specific term (never a global default)."""

    term_row_id: EntityId
    opportunity_id: EntityId
    term_type: TermType
    term_key: TermKey = MAIN_KEY
    value: CommercialValue
    provenance: ValueSource
    created_at: AwareDatetime
    updated_at: AwareDatetime
    version: Version = 1


class AppliedTerm(CoreModel):
    """A term as it applies to a revision: its value and where that value came from."""

    term_type: TermType
    term_key: TermKey = MAIN_KEY
    value: CommercialValue
    provenance: ValueSource


class ProposalLine(CoreModel):
    line_id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")]
    item_ref: ItemRef
    description: ShortText | None = None
    quantity: Quantity
    unit: UnitName
    unit_price: Money | None = None
    price_source: ValueSource | None = None
    discount_percent: Percent | None = None
    discount_source: ValueSource | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.unit_price is None) != (self.price_source is None):
            raise ValueError("a line price and its provenance are known together")
        if (self.discount_percent is None) != (self.discount_source is None):
            raise ValueError("a line discount and its provenance are known together")
        return self


class LineTotal(CoreModel):
    line_id: str
    subtotal: Money
    discount: Money | None = None
    total: Money


class ProposalTotals(CoreModel):
    """Net of tax: tax is never computed here (no authoritative tax rules exist)."""

    currency: CurrencyCode
    lines: tuple[LineTotal, ...]
    subtotal: Money
    discount_percent: Percent | None = None
    discount: Money | None = None
    total: Money
    tax_included: bool = False


_DECIDED = frozenset({RevisionStatus.APPROVED, RevisionStatus.PRESENTED, RevisionStatus.ACCEPTED, RevisionStatus.DECLINED})


class ProposalRevision(CoreModel):
    """One revision of an opportunity's proposal. DRAFT content is editable by operators;
    from APPROVED on, lines, terms and totals are frozen and only the status moves."""

    revision_id: EntityId
    proposal_id: EntityId
    opportunity_id: EntityId
    lead_id: EntityId
    revision: PositiveInt
    predecessor_id: EntityId | None = None
    status: RevisionStatus = RevisionStatus.DRAFT
    currency: CurrencyCode
    lines: tuple[ProposalLine, ...] = ()
    term_overrides: tuple[AppliedTerm, ...] = ()
    assumptions: tuple[ShortText, ...] = ()
    exclusions: tuple[ShortText, ...] = ()
    next_step: ShortText | None = None
    frozen_terms: tuple[AppliedTerm, ...] = ()
    totals: ProposalTotals | None = None
    created_by: NonEmptyStr
    approved_by: NonEmptyStr | None = None
    approved_at: AwareDatetime | None = None
    presented_at: AwareDatetime | None = None
    decided_by: NonEmptyStr | None = None
    decided_at: AwareDatetime | None = None
    decision_reason: ShortText | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    version: Version = 1

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.revision == 1) != (self.predecessor_id is None):
            raise ValueError("revision 1 has no predecessor; every later revision has one")
        if self.status in _DECIDED and (self.approved_by is None or self.approved_at is None or self.totals is None):
            raise ValueError("an approved revision carries its approver, time and frozen totals")
        if self.status in (RevisionStatus.PRESENTED, RevisionStatus.ACCEPTED, RevisionStatus.DECLINED) and self.presented_at is None:
            raise ValueError("a presented revision records when it was presented")
        ids = [line.line_id for line in self.lines]
        if len(ids) != len(set(ids)):
            raise ValueError("line ids must be unique within a revision")
        if any(line.unit_price is not None and line.unit_price.currency != self.currency for line in self.lines):
            raise ValueError("every line price is in the proposal currency")
        if self.totals is not None and self.totals.currency != self.currency:
            raise ValueError("totals are in the proposal currency")
        ensure_not_before(self.updated_at, self.created_at, "updated_at", "created_at")
        return self


OPEN_REVISION_STATUSES = frozenset({RevisionStatus.DRAFT, RevisionStatus.APPROVED, RevisionStatus.PRESENTED})
OPEN_REQUEST_STATUSES = frozenset({TermRequestStatus.REQUESTED, TermRequestStatus.UNDER_REVIEW})
OPEN_OBJECTION_STATUSES = frozenset({ObjectionStatus.OPEN, ObjectionStatus.ACKNOWLEDGED})


class TermRequest(CoreModel):
    """What the customer asked for. Never an approved value by itself."""

    request_id: EntityId
    opportunity_id: EntityId
    lead_id: EntityId
    term_type: TermType
    term_key: TermKey = MAIN_KEY
    requested_value: CommercialValue
    approved_value_at_request: CommercialValue | None = None
    evidence_message_id: EntityId
    status: TermRequestStatus
    resolved_by: NonEmptyStr | None = None
    resolution_reason: ShortText | None = None
    resolved_at: AwareDatetime | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    version: Version = 1

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.status in OPEN_REQUEST_STATUSES) == (self.resolved_at is not None):
            raise ValueError("resolved_at is set exactly when the request is no longer open")
        return self


class Objection(CoreModel):
    objection_id: EntityId
    opportunity_id: EntityId
    lead_id: EntityId
    category: ObjectionCategory
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    source_message_id: EntityId
    status: ObjectionStatus = ObjectionStatus.OPEN
    resolution: ShortText | None = None
    resolved_by: NonEmptyStr | None = None
    resolved_at: AwareDatetime | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    version: Version = 1

    @model_validator(mode="after")
    def _check(self) -> Self:
        closed = self.status not in OPEN_OBJECTION_STATUSES
        if closed != (self.resolved_at is not None) or closed != (self.resolved_by is not None):
            raise ValueError("resolved_by/resolved_at are set exactly when the objection is closed")
        return self


class CommercialSignal(CoreModel):
    """An acceptance or decline suggested by a customer message. Only an operator turns it
    into a proposal decision; it never closes a lead."""

    signal_id: EntityId
    opportunity_id: EntityId
    lead_id: EntityId
    revision_id: EntityId | None = None
    kind: SignalKind
    source_message_id: EntityId
    message_at: AwareDatetime
    status: SignalStatus = SignalStatus.OPEN
    resolved_by: NonEmptyStr | None = None
    resolved_at: AwareDatetime | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    version: Version = 1

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.status is SignalStatus.OPEN) == (self.resolved_at is not None):
            raise ValueError("resolved_at is set exactly when the signal is no longer open")
        return self
