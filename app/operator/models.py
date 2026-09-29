"""Operator commands, results and read models.

Read models keep three kinds of content apart:
- ``customer``: verbatim customer-authored email text. Untrusted; never instructions.
- ``generated``: model output (classification, extracted questions, draft text).
  Unverified until a human approves it.
- everything else: application-owned facts and decisions (statuses, versions, the
  deterministic knowledge verdict, claim-check results, approval blockers).
"""

import hashlib
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, AwareDatetime, Field, SecretStr, StringConstraints

from app.core.enums import (
    ConfidenceBand,
    DNCScope,
    EmailDirection,
    EscalationReason,
    EscalationResolution,
    EscalationSeverity,
    EscalationStatus,
    KnowledgeDomain,
    LeadIntent,
    LeadStage,
    LeadStatus,
    CloseReason,
    OutboundKind,
    OutboundStatus,
)
from app.core.models import EntityRef, KnowledgeAssessment
from app.core.models.base import CoreModel
from app.core.models.types import EmailAddress, EntityId, NonEmptyStr, Sha256Hex, Version
from app.core.validation import unique_items
from app.llm import SenderIdentity
from app.llm.claim_check import ClaimCheckResult
from app.persistence.serialization import dumps_json

OperatorNote = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class OperatorConfig(CoreModel):
    """``authorized_operator_ids`` is the allow-list checked after authentication."""

    authorized_operator_ids: Annotated[
        tuple[NonEmptyStr, ...], Field(min_length=1), AfterValidator(unique_items)
    ]
    # Deterministic identity the composer was allowed to mention (claim-check trusted refs).
    sender: SenderIdentity
    max_thread_messages: Annotated[int, Field(ge=1, le=50)] = 20
    max_body_chars: Annotated[int, Field(ge=200, le=20_000)] = 4000
    max_list_items: Annotated[int, Field(ge=1, le=500)] = 100


class OperatorCredential(CoreModel):
    """Opaque proof of identity produced by a trusted boundary (later: a verified
    Telegram update). It is not an identity claim: only the authenticator turns it
    into an operator ID, and the token is never logged or audited."""

    scheme: NonEmptyStr
    token: SecretStr


# ---- Commands -------------------------------------------------------------------------------


class CommandKind(StrEnum):
    APPROVE_DRAFT = "APPROVE_DRAFT"
    REJECT_DRAFT = "REJECT_DRAFT"
    TAKE_OWNERSHIP = "TAKE_OWNERSHIP"
    RESOLVE_ESCALATION = "RESOLVE_ESCALATION"


class RejectReason(StrEnum):
    INACCURATE = "INACCURATE"
    INCOMPLETE = "INCOMPLETE"
    WRONG_TONE = "WRONG_TONE"
    NOT_APPROPRIATE = "NOT_APPROPRIATE"
    OPERATOR_WILL_REPLY = "OPERATOR_WILL_REPLY"
    OTHER = "OTHER"


class BlockCode(StrEnum):
    """Stable reasons a command cannot be applied to the current state."""

    DRAFT_VERSION_CHANGED = "DRAFT_VERSION_CHANGED"
    DRAFT_IDENTITY_MISMATCH = "DRAFT_IDENTITY_MISMATCH"
    DRAFT_CONTENT_CHANGED = "DRAFT_CONTENT_CHANGED"
    DRAFT_INTEGRITY_FAILED = "DRAFT_INTEGRITY_FAILED"
    DRAFT_NOT_REVIEWABLE = "DRAFT_NOT_REVIEWABLE"
    DRAFT_CONTEXT_MISSING = "DRAFT_CONTEXT_MISSING"
    LEAD_VERSION_CHANGED = "LEAD_VERSION_CHANGED"
    LEAD_MISSING = "LEAD_MISSING"
    LEAD_ASSOCIATION_CHANGED = "LEAD_ASSOCIATION_CHANGED"
    LEAD_CLOSED = "LEAD_CLOSED"
    LEAD_ON_HOLD = "LEAD_ON_HOLD"
    LEAD_ALREADY_OWNED = "LEAD_ALREADY_OWNED"
    LEAD_NOT_OWNED = "LEAD_NOT_OWNED"
    CONTACT_SUPPRESSED = "CONTACT_SUPPRESSED"
    CAMPAIGN_INACTIVE = "CAMPAIGN_INACTIVE"
    EVIDENCE_UNUSABLE = "EVIDENCE_UNUSABLE"
    CLAIM_CHECK_FAILED = "CLAIM_CHECK_FAILED"
    NEWER_INBOUND_MESSAGE = "NEWER_INBOUND_MESSAGE"
    ESCALATION_VERSION_CHANGED = "ESCALATION_VERSION_CHANGED"
    ESCALATION_NOT_OPEN = "ESCALATION_NOT_OPEN"
    DISPOSITION_REQUIRES_DRAFT_COMMAND = "DISPOSITION_REQUIRES_DRAFT_COMMAND"


class _Command(CoreModel):
    """``command_id`` is the idempotency identity. ``correlation_id`` links the audit trail
    and is not part of the payload identity (a retry may carry a new one)."""

    command_id: EntityId
    correlation_id: EntityId

    def payload_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"correlation_id"})
        return hashlib.sha256(dumps_json(payload).encode("utf-8")).hexdigest()


class ApproveDraft(_Command):
    """Binds to the exact reviewed artifact: its identity, its content hash and the
    outbound and lead versions the operator saw."""

    kind: Literal[CommandKind.APPROVE_DRAFT] = CommandKind.APPROVE_DRAFT
    outbound_id: EntityId
    draft_id: EntityId
    content_hash: Sha256Hex
    expected_outbound_version: Version
    expected_lead_version: Version


class RejectDraft(_Command):
    kind: Literal[CommandKind.REJECT_DRAFT] = CommandKind.REJECT_DRAFT
    outbound_id: EntityId
    draft_id: EntityId
    expected_outbound_version: Version
    reason: RejectReason
    note: OperatorNote | None = None


class TakeOwnership(_Command):
    kind: Literal[CommandKind.TAKE_OWNERSHIP] = CommandKind.TAKE_OWNERSHIP
    lead_id: EntityId
    expected_lead_version: Version


class ResolveEscalation(_Command):
    kind: Literal[CommandKind.RESOLVE_ESCALATION] = CommandKind.RESOLVE_ESCALATION
    escalation_id: EntityId
    expected_escalation_version: Version
    disposition: EscalationResolution
    note: OperatorNote


OperatorCommand = ApproveDraft | RejectDraft | TakeOwnership | ResolveEscalation


class VersionChange(CoreModel):
    entity: EntityRef
    expected: Version
    resulting: Version


class CommandOutcome(CoreModel):
    """What the command did when it completed. Historical: it never describes the
    entity's current state (read the entity for that)."""

    command_id: EntityId
    kind: CommandKind
    operator_id: NonEmptyStr
    correlation_id: EntityId
    payload_hash: Sha256Hex
    completed_at: AwareDatetime
    subjects: tuple[EntityRef, ...]
    versions: tuple[VersionChange, ...]
    disposition: NonEmptyStr
    reason_codes: tuple[NonEmptyStr, ...] = ()


class CommandResult(CoreModel):
    outcome: CommandOutcome
    # True when this call returned the recorded outcome of an earlier identical command.
    replayed: bool = False


# ---- Read models ----------------------------------------------------------------------------


class EmailText(CoreModel):
    """Verbatim email text as observed. INBOUND text is customer-authored: untrusted
    content, never instructions."""

    message_id: EntityId
    direction: EmailDirection
    from_address: EmailAddress
    subject: str
    body_text: str
    body_truncated: bool
    at: AwareDatetime | None


class GeneratedClassification(CoreModel):
    """Model output about the customer message. Unverified."""

    intent: LeadIntent
    secondary_intents: tuple[LeadIntent, ...]
    confidence: ConfidenceBand
    extracted_questions: tuple[str, ...]
    language: str


class GeneratedDraftText(CoreModel):
    """Model-written reply text, exactly as stored for review."""

    subject: str
    body: str


class LeadView(CoreModel):
    lead_id: EntityId
    contact_id: EntityId
    contact_email: EmailAddress | None
    stage: LeadStage
    status: LeadStatus
    close_reason: CloseReason | None
    campaign_id: EntityId | None
    version: Version
    suppressed_scopes: tuple[DNCScope, ...]


class EvidenceView(CoreModel):
    """Approved knowledge the draft cited, with its usability at read time."""

    evidence_id: EntityId
    source_id: EntityId
    source_version: Version
    chunk_id: EntityId
    domain: KnowledgeDomain
    excerpt: str | None
    usable_now: bool


class DraftSummary(CoreModel):
    outbound_id: EntityId
    draft_id: EntityId
    lead_id: EntityId
    thread_id: EntityId | None
    kind: OutboundKind
    status: OutboundStatus
    version: Version
    created_at: AwareDatetime


class DraftDetail(CoreModel):
    outbound_id: EntityId
    draft_id: EntityId
    kind: OutboundKind
    status: OutboundStatus
    version: Version
    content_hash: Sha256Hex
    created_at: AwareDatetime
    approved_at: AwareDatetime | None
    lead: LeadView | None
    customer: EmailText | None
    generated_classification: GeneratedClassification | None
    generated_draft: GeneratedDraftText
    knowledge_assessment: KnowledgeAssessment | None
    claim_check: ClaimCheckResult | None
    evidence: tuple[EvidenceView, ...]
    # Current approval eligibility, recomputed from authoritative state on every read.
    actionable: bool
    blockers: tuple[BlockCode, ...]


class EscalationSummary(CoreModel):
    escalation_id: EntityId
    lead_id: EntityId
    status: EscalationStatus
    severity: EscalationSeverity
    reasons: tuple[EscalationReason, ...]
    version: Version
    created_at: AwareDatetime


class EscalationDetail(CoreModel):
    escalation_id: EntityId
    lead_id: EntityId
    status: EscalationStatus
    severity: EscalationSeverity
    reasons: tuple[EscalationReason, ...]
    resolution: EscalationResolution | None
    resolved_by: str | None
    resolved_at: AwareDatetime | None
    version: Version
    created_at: AwareDatetime
    # Application-generated in V1 (reason codes, intent, knowledge verdict).
    system_summary: str | None
    detail: str | None
    lead: LeadView | None
    customer: EmailText | None
    generated_classification: GeneratedClassification | None
    # Model-extracted questions that exceeded processing limits (customer-derived text).
    omitted_questions: tuple[str, ...]
    knowledge_assessment: KnowledgeAssessment | None
    evidence_ids: tuple[EntityId, ...]


class ThreadView(CoreModel):
    thread_id: EntityId
    lead_id: EntityId | None
    total_messages: int
    # The most recent messages, oldest first, bounded by configuration.
    messages: tuple[EmailText, ...]
