"""Operator commands, results and read models.

Read models keep three kinds of content apart:
- ``customer``: verbatim customer-authored email text. Untrusted; never instructions.
- ``generated``: model output (classification, extracted questions, draft text).
  Unverified until a human approves it.
- everything else: application-owned facts and decisions (statuses, versions, the
  deterministic knowledge verdict, claim-check results, approval blockers).
"""

import hashlib
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, AwareDatetime, Field, SecretStr, StringConstraints, model_validator

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
    ConflictResolution,
    DisqualificationReason,
    ObjectionStatus,
    TermType,
    LostReason,
    OutboundKind,
    OutboundStatus,
)
from app.core.models import CommercialValue, EntityRef, KnowledgeAssessment, Money
from app.core.models.base import CoreModel
from app.core.models.commercial import ItemRef, Percent, Quantity, TermKey, UnitName
from app.core.models.pipeline import CurrencyCode, FactValue, FieldKey, ShortText
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
    # Conversation and follow-up control (Stage 9).
    PAUSE_CONVERSATION = "PAUSE_CONVERSATION"
    RESUME_CONVERSATION = "RESUME_CONVERSATION"
    CANCEL_FOLLOW_UP = "CANCEL_FOLLOW_UP"
    CLOSE_CONVERSATION = "CLOSE_CONVERSATION"
    MARK_DO_NOT_CONTACT = "MARK_DO_NOT_CONTACT"
    # Campaign execution control (Stage 10).
    ACTIVATE_CAMPAIGN = "ACTIVATE_CAMPAIGN"
    PAUSE_CAMPAIGN = "PAUSE_CAMPAIGN"
    RESUME_CAMPAIGN = "RESUME_CAMPAIGN"
    CANCEL_CAMPAIGN = "CANCEL_CAMPAIGN"
    COMPLETE_CAMPAIGN = "COMPLETE_CAMPAIGN"
    CANCEL_CAMPAIGN_MEMBER = "CANCEL_CAMPAIGN_MEMBER"
    SUPPRESS_CAMPAIGN_MEMBER = "SUPPRESS_CAMPAIGN_MEMBER"
    # Sales pipeline (Stage 12).
    RECORD_QUALIFICATION_FACT = "RECORD_QUALIFICATION_FACT"
    RESOLVE_QUALIFICATION_CONFLICT = "RESOLVE_QUALIFICATION_CONFLICT"
    APPROVE_QUALIFICATION = "APPROVE_QUALIFICATION"
    DISQUALIFY_LEAD = "DISQUALIFY_LEAD"
    CREATE_OPPORTUNITY = "CREATE_OPPORTUNITY"
    START_NEGOTIATION = "START_NEGOTIATION"
    MARK_LEAD_WON = "MARK_LEAD_WON"
    MARK_LEAD_LOST = "MARK_LEAD_LOST"
    REOPEN_LEAD = "REOPEN_LEAD"
    # Commercial decisioning (Stage 13).
    SET_COMMERCIAL_TERM = "SET_COMMERCIAL_TERM"
    APPROVE_TERM_REQUEST = "APPROVE_TERM_REQUEST"
    REJECT_TERM_REQUEST = "REJECT_TERM_REQUEST"
    CREATE_PROPOSAL = "CREATE_PROPOSAL"
    UPDATE_PROPOSAL = "UPDATE_PROPOSAL"
    APPROVE_PROPOSAL = "APPROVE_PROPOSAL"
    REVISE_PROPOSAL = "REVISE_PROPOSAL"
    WITHDRAW_PROPOSAL = "WITHDRAW_PROPOSAL"
    MARK_PROPOSAL_PRESENTED = "MARK_PROPOSAL_PRESENTED"
    MARK_PROPOSAL_ACCEPTED = "MARK_PROPOSAL_ACCEPTED"
    MARK_PROPOSAL_DECLINED = "MARK_PROPOSAL_DECLINED"
    UPDATE_OBJECTION = "UPDATE_OBJECTION"
    DISMISS_COMMERCIAL_SIGNAL = "DISMISS_COMMERCIAL_SIGNAL"


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
    CONVERSATION_VERSION_CHANGED = "CONVERSATION_VERSION_CHANGED"
    CONVERSATION_ENDED = "CONVERSATION_ENDED"
    CONVERSATION_ALREADY_PAUSED = "CONVERSATION_ALREADY_PAUSED"
    CONVERSATION_NOT_RESUMABLE = "CONVERSATION_NOT_RESUMABLE"
    ESCALATION_OPEN = "ESCALATION_OPEN"
    NO_FOLLOW_UP_TO_CANCEL = "NO_FOLLOW_UP_TO_CANCEL"
    # Campaign execution (Stage 10).
    CAMPAIGN_MEMBER_NOT_READY = "CAMPAIGN_MEMBER_NOT_READY"
    CONVERSATION_ACTIVE = "CONVERSATION_ACTIVE"
    OTHER_OUTBOUND_OUTSTANDING = "OTHER_OUTBOUND_OUTSTANDING"
    CONTACT_ADDRESS_INVALID = "CONTACT_ADDRESS_INVALID"
    CAMPAIGN_VERSION_CHANGED = "CAMPAIGN_VERSION_CHANGED"
    CAMPAIGN_STATE_INVALID = "CAMPAIGN_STATE_INVALID"
    CAMPAIGN_HAS_ACTIVE_MEMBERS = "CAMPAIGN_HAS_ACTIVE_MEMBERS"
    MEMBER_VERSION_CHANGED = "MEMBER_VERSION_CHANGED"
    MEMBER_ENDED = "MEMBER_ENDED"
    # Sales pipeline (Stage 12); identical to app.pipeline.PipelineCode values.
    TRANSITION_NOT_ALLOWED = "TRANSITION_NOT_ALLOWED"
    QUALIFICATION_VERSION_CHANGED = "QUALIFICATION_VERSION_CHANGED"
    QUALIFICATION_NOT_STARTED = "QUALIFICATION_NOT_STARTED"
    QUALIFICATION_NOT_READY = "QUALIFICATION_NOT_READY"
    QUALIFICATION_DECIDED = "QUALIFICATION_DECIDED"
    QUALIFICATION_CONFLICT_OPEN = "QUALIFICATION_CONFLICT_OPEN"
    QUALIFICATION_FIELD_UNKNOWN = "QUALIFICATION_FIELD_UNKNOWN"
    CONFLICT_NOT_OPEN = "CONFLICT_NOT_OPEN"
    OPPORTUNITY_EXISTS = "OPPORTUNITY_EXISTS"
    OPPORTUNITY_REQUIRED = "OPPORTUNITY_REQUIRED"
    OPPORTUNITY_VERSION_CHANGED = "OPPORTUNITY_VERSION_CHANGED"
    OPPORTUNITY_NOT_ACTIVE = "OPPORTUNITY_NOT_ACTIVE"
    OPPORTUNITY_ACTIVE = "OPPORTUNITY_ACTIVE"
    NOT_REOPENABLE = "NOT_REOPENABLE"
    REOPEN_TARGET_NOT_ALLOWED = "REOPEN_TARGET_NOT_ALLOWED"
    # Commercial decisioning (Stage 13); identical to app.commercial codes and blockers.
    OPPORTUNITY_NOT_OPEN = "OPPORTUNITY_NOT_OPEN"
    QUALIFICATION_NOT_APPROVED = "QUALIFICATION_NOT_APPROVED"
    PROPOSAL_EXISTS = "PROPOSAL_EXISTS"
    REVISION_VERSION_CHANGED = "REVISION_VERSION_CHANGED"
    REVISION_NOT_EDITABLE = "REVISION_NOT_EDITABLE"
    REVISION_NOT_CURRENT = "REVISION_NOT_CURRENT"
    REVISION_STATUS_INVALID = "REVISION_STATUS_INVALID"
    PROPOSAL_NOT_READY = "PROPOSAL_NOT_READY"
    CURRENCY_NOT_ALLOWED = "CURRENCY_NOT_ALLOWED"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    TERM_VALUE_INVALID = "TERM_VALUE_INVALID"
    TERM_VERSION_CHANGED = "TERM_VERSION_CHANGED"
    DISCOUNT_NOT_ALLOWED = "DISCOUNT_NOT_ALLOWED"
    DISCOUNT_ABOVE_LIMIT = "DISCOUNT_ABOVE_LIMIT"
    REQUEST_VERSION_CHANGED = "REQUEST_VERSION_CHANGED"
    REQUEST_NOT_OPEN = "REQUEST_NOT_OPEN"
    OBJECTION_VERSION_CHANGED = "OBJECTION_VERSION_CHANGED"
    OBJECTION_NOT_OPEN = "OBJECTION_NOT_OPEN"
    SIGNAL_VERSION_CHANGED = "SIGNAL_VERSION_CHANGED"
    SIGNAL_NOT_OPEN = "SIGNAL_NOT_OPEN"
    DNC = "DNC"
    QUALIFICATION_CONFLICT = "QUALIFICATION_CONFLICT"
    NO_PROPOSAL = "NO_PROPOSAL"
    NO_PROPOSAL_LINES = "NO_PROPOSAL_LINES"
    MISSING_PRICE = "MISSING_PRICE"
    MISSING_CURRENCY = "MISSING_CURRENCY"
    MISSING_REQUIRED_TERM = "MISSING_REQUIRED_TERM"
    UNAPPROVED_TERM_REQUEST = "UNAPPROVED_TERM_REQUEST"
    OPEN_OBJECTION = "OPEN_OBJECTION"
    PROPOSAL_NOT_APPROVED = "PROPOSAL_NOT_APPROVED"
    PROPOSAL_NOT_PRESENTED = "PROPOSAL_NOT_PRESENTED"
    PROPOSAL_REVISION_REQUIRED = "PROPOSAL_REVISION_REQUIRED"
    ACCEPTANCE_SIGNAL = "ACCEPTANCE_SIGNAL"
    DECLINE_SIGNAL = "DECLINE_SIGNAL"


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


class _ConversationCommand(_Command):
    """Applies to one conversation as the operator saw it (``expected_conversation_version``)."""

    conversation_id: EntityId
    expected_conversation_version: Version


class PauseConversation(_ConversationCommand):
    kind: Literal[CommandKind.PAUSE_CONVERSATION] = CommandKind.PAUSE_CONVERSATION


class ResumeConversation(_ConversationCommand):
    kind: Literal[CommandKind.RESUME_CONVERSATION] = CommandKind.RESUME_CONVERSATION


class CancelFollowUp(_ConversationCommand):
    kind: Literal[CommandKind.CANCEL_FOLLOW_UP] = CommandKind.CANCEL_FOLLOW_UP


class CloseConversation(_ConversationCommand):
    kind: Literal[CommandKind.CLOSE_CONVERSATION] = CommandKind.CLOSE_CONVERSATION
    note: OperatorNote | None = None


class MarkDoNotContact(_ConversationCommand):
    kind: Literal[CommandKind.MARK_DO_NOT_CONTACT] = CommandKind.MARK_DO_NOT_CONTACT
    note: OperatorNote | None = None


ConversationCommand = PauseConversation | ResumeConversation | CancelFollowUp | CloseConversation | MarkDoNotContact


class _CampaignCommand(_Command):
    """Applies to one campaign as the operator saw it (``expected_campaign_version``)."""

    campaign_id: EntityId
    expected_campaign_version: Version


class ActivateCampaign(_CampaignCommand):
    kind: Literal[CommandKind.ACTIVATE_CAMPAIGN] = CommandKind.ACTIVATE_CAMPAIGN


class PauseCampaign(_CampaignCommand):
    kind: Literal[CommandKind.PAUSE_CAMPAIGN] = CommandKind.PAUSE_CAMPAIGN


class ResumeCampaign(_CampaignCommand):
    kind: Literal[CommandKind.RESUME_CAMPAIGN] = CommandKind.RESUME_CAMPAIGN


class CancelCampaign(_CampaignCommand):
    kind: Literal[CommandKind.CANCEL_CAMPAIGN] = CommandKind.CANCEL_CAMPAIGN
    note: OperatorNote | None = None


class CompleteCampaign(_CampaignCommand):
    kind: Literal[CommandKind.COMPLETE_CAMPAIGN] = CommandKind.COMPLETE_CAMPAIGN


class _MemberCommand(_Command):
    member_id: EntityId
    expected_member_version: Version
    note: OperatorNote | None = None


class CancelCampaignMember(_MemberCommand):
    kind: Literal[CommandKind.CANCEL_CAMPAIGN_MEMBER] = CommandKind.CANCEL_CAMPAIGN_MEMBER


class SuppressCampaignMember(_MemberCommand):
    kind: Literal[CommandKind.SUPPRESS_CAMPAIGN_MEMBER] = CommandKind.SUPPRESS_CAMPAIGN_MEMBER


# ---- Sales pipeline commands (Stage 12) -------------------------------------------------------
# Every command binds to the versions the operator saw; a newer customer message, fact or
# decision makes it stale. None of them sends, approves a draft or touches suppression.


class _LeadCommand(_Command):
    lead_id: EntityId


class RecordQualificationFact(_LeadCommand):
    """State a fact the operator knows (e.g. from a call). ``expected_qualification_version``
    None means the operator saw no qualification yet."""

    kind: Literal[CommandKind.RECORD_QUALIFICATION_FACT] = CommandKind.RECORD_QUALIFICATION_FACT
    field: FieldKey
    value: FactValue
    expected_qualification_version: Version | None


class ResolveQualificationConflict(_LeadCommand):
    kind: Literal[CommandKind.RESOLVE_QUALIFICATION_CONFLICT] = CommandKind.RESOLVE_QUALIFICATION_CONFLICT
    conflict_id: EntityId
    resolution: ConflictResolution
    expected_qualification_version: Version


class ApproveQualification(_LeadCommand):
    kind: Literal[CommandKind.APPROVE_QUALIFICATION] = CommandKind.APPROVE_QUALIFICATION
    expected_lead_version: Version
    expected_qualification_version: Version
    note: OperatorNote | None = None


class DisqualifyLead(_LeadCommand):
    kind: Literal[CommandKind.DISQUALIFY_LEAD] = CommandKind.DISQUALIFY_LEAD
    expected_lead_version: Version
    reason: DisqualificationReason
    note: OperatorNote | None = None


class CreateOpportunity(_LeadCommand):
    """Unknown commercial data stays None: nothing is estimated for the operator."""

    kind: Literal[CommandKind.CREATE_OPPORTUNITY] = CommandKind.CREATE_OPPORTUNITY
    expected_lead_version: Version
    amount: Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2)] | None = None
    currency: CurrencyCode | None = None
    scope: ShortText | None = None
    expected_decision_date: date | None = None
    next_step: ShortText | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.amount is None) != (self.currency is None):
            raise ValueError("amount and currency are known together or not at all")
        return self


class StartNegotiation(_LeadCommand):
    kind: Literal[CommandKind.START_NEGOTIATION] = CommandKind.START_NEGOTIATION
    expected_lead_version: Version
    opportunity_id: EntityId
    expected_opportunity_version: Version


class MarkLeadWon(_LeadCommand):
    kind: Literal[CommandKind.MARK_LEAD_WON] = CommandKind.MARK_LEAD_WON
    expected_lead_version: Version
    opportunity_id: EntityId
    expected_opportunity_version: Version
    note: OperatorNote | None = None


class MarkLeadLost(_LeadCommand):
    kind: Literal[CommandKind.MARK_LEAD_LOST] = CommandKind.MARK_LEAD_LOST
    expected_lead_version: Version
    reason: LostReason
    note: OperatorNote | None = None


class ReopenLead(_LeadCommand):
    kind: Literal[CommandKind.REOPEN_LEAD] = CommandKind.REOPEN_LEAD
    expected_lead_version: Version
    target_stage: LeadStage
    note: OperatorNote  # a reopen always states why


# ---- Commercial commands (Stage 13) ---------------------------------------------------------
# Every value an operator sets here is the approval: nothing an extractor or advisor
# proposes becomes a term, price, discount or proposal decision without one of these.


class SetCommercialTerm(_Command):
    kind: Literal[CommandKind.SET_COMMERCIAL_TERM] = CommandKind.SET_COMMERCIAL_TERM
    opportunity_id: EntityId
    term_type: TermType
    term_key: TermKey = "main"
    value: CommercialValue
    expected_term_version: Version | None  # None: the operator saw no approved value


class ApproveTermRequest(_Command):
    kind: Literal[CommandKind.APPROVE_TERM_REQUEST] = CommandKind.APPROVE_TERM_REQUEST
    request_id: EntityId
    expected_request_version: Version
    expected_term_version: Version | None  # the approved term the operator saw (None: none)


class RejectTermRequest(_Command):
    kind: Literal[CommandKind.REJECT_TERM_REQUEST] = CommandKind.REJECT_TERM_REQUEST
    request_id: EntityId
    expected_request_version: Version
    reason: OperatorNote


class CreateProposal(_Command):
    kind: Literal[CommandKind.CREATE_PROPOSAL] = CommandKind.CREATE_PROPOSAL
    opportunity_id: EntityId
    expected_opportunity_version: Version
    currency: CurrencyCode


class ProposalLineInput(CoreModel):
    line_id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")]
    item_ref: ItemRef
    quantity: Quantity
    unit: UnitName
    description: ShortText | None = None
    unit_price: Money | None = None  # None: use an approved knowledge price, if one exists
    discount_percent: Percent | None = None


class TermOverrideInput(CoreModel):
    term_type: TermType
    term_key: TermKey = "main"
    value: CommercialValue


class UpdateProposal(_Command):
    """Replaces the DRAFT revision's content; approved revisions are never edited."""

    kind: Literal[CommandKind.UPDATE_PROPOSAL] = CommandKind.UPDATE_PROPOSAL
    revision_id: EntityId
    expected_revision_version: Version
    lines: Annotated[tuple[ProposalLineInput, ...], Field(max_length=100)] = ()
    term_overrides: Annotated[tuple[TermOverrideInput, ...], Field(max_length=30)] = ()
    assumptions: Annotated[tuple[ShortText, ...], Field(max_length=20)] = ()
    exclusions: Annotated[tuple[ShortText, ...], Field(max_length=20)] = ()
    next_step: ShortText | None = None


class _RevisionCommand(_Command):
    revision_id: EntityId
    expected_revision_version: Version


class ApproveProposal(_RevisionCommand):
    kind: Literal[CommandKind.APPROVE_PROPOSAL] = CommandKind.APPROVE_PROPOSAL


class ReviseProposal(_RevisionCommand):
    kind: Literal[CommandKind.REVISE_PROPOSAL] = CommandKind.REVISE_PROPOSAL


class WithdrawProposal(_RevisionCommand):
    kind: Literal[CommandKind.WITHDRAW_PROPOSAL] = CommandKind.WITHDRAW_PROPOSAL
    reason: OperatorNote


class MarkProposalPresented(_RevisionCommand):
    """The operator confirms the approved revision was actually communicated."""

    kind: Literal[CommandKind.MARK_PROPOSAL_PRESENTED] = CommandKind.MARK_PROPOSAL_PRESENTED


class MarkProposalAccepted(_RevisionCommand):
    kind: Literal[CommandKind.MARK_PROPOSAL_ACCEPTED] = CommandKind.MARK_PROPOSAL_ACCEPTED
    note: OperatorNote | None = None


class MarkProposalDeclined(_RevisionCommand):
    kind: Literal[CommandKind.MARK_PROPOSAL_DECLINED] = CommandKind.MARK_PROPOSAL_DECLINED
    reason: OperatorNote | None = None


class UpdateObjection(_Command):
    kind: Literal[CommandKind.UPDATE_OBJECTION] = CommandKind.UPDATE_OBJECTION
    objection_id: EntityId
    expected_objection_version: Version
    status: ObjectionStatus
    resolution: OperatorNote | None = None


class DismissCommercialSignal(_Command):
    kind: Literal[CommandKind.DISMISS_COMMERCIAL_SIGNAL] = CommandKind.DISMISS_COMMERCIAL_SIGNAL
    signal_id: EntityId
    expected_signal_version: Version


CommercialCommand = (
    SetCommercialTerm | ApproveTermRequest | RejectTermRequest | CreateProposal | UpdateProposal | ApproveProposal
    | ReviseProposal | WithdrawProposal | MarkProposalPresented | MarkProposalAccepted | MarkProposalDeclined
    | UpdateObjection | DismissCommercialSignal
)
PipelineCommand = (
    RecordQualificationFact | ResolveQualificationConflict | ApproveQualification | DisqualifyLead | CreateOpportunity
    | StartNegotiation | MarkLeadWon | MarkLeadLost | ReopenLead
)
CampaignCommand = ActivateCampaign | PauseCampaign | ResumeCampaign | CancelCampaign | CompleteCampaign
MemberCommand = CancelCampaignMember | SuppressCampaignMember
OperatorCommand = (
    ApproveDraft | RejectDraft | TakeOwnership | ResolveEscalation | ConversationCommand | CampaignCommand | MemberCommand
    | PipelineCommand | CommercialCommand
)


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
