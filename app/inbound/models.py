"""Provider-neutral inbound contracts."""

import hashlib
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, AwareDatetime, Field, PositiveInt, StringConstraints, model_validator

from app.core.enums import ClaimCheckStatus, EscalationReason, ReplyDecision
from app.core.models import IntentClassification, KnowledgeAssessment
from app.core.models.base import CoreModel
from app.core.models.types import (
    EmailAddress,
    EntityId,
    LocaleTag,
    NonEmptyStr,
    Sha256Hex,
    UniqueEmailAddresses,
    UniqueEntityIds,
)
from app.core.validation import unique_items
from app.llm import SenderIdentity

# Stage 6 finalizes only these. AUTO_REPLY is disabled in V1 and rejected by InboundResult.
ALLOWED_DECISIONS: frozenset[ReplyDecision] = frozenset(
    {ReplyDecision.DRAFT_FOR_REVIEW, ReplyDecision.ESCALATE, ReplyDecision.NO_ACTION}
)


def stable_id(prefix: str, *parts: str) -> str:
    """Deterministic ID: prefix + first 40 hex chars of SHA-256 over the parts."""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:40]}"


class PrefilterOutcome(StrEnum):
    NONE = "NONE"
    SELF_LOOP = "SELF_LOOP"
    BOUNCE = "BOUNCE"
    AUTO_SUBMITTED = "AUTO_SUBMITTED"
    DUPLICATE = "DUPLICATE"


class InboundEnvelope(CoreModel):
    """One observed inbound email, independent of any provider API.

    Only generic email metadata and the headers the deterministic prefilter needs.
    ``provider_thread_ref`` is recorded but not yet used for threading. Attachments are
    not processed in V1: ``has_attachments`` makes their presence explicit.
    """

    provider: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")]
    provider_message_id: NonEmptyStr
    internet_message_id: NonEmptyStr | None = None
    mailbox: EmailAddress
    from_address: EmailAddress
    to_addresses: UniqueEmailAddresses = ()
    cc_addresses: UniqueEmailAddresses = ()
    subject: str = ""
    body_text: str
    received_at: AwareDatetime
    sent_at: AwareDatetime | None = None
    in_reply_to: NonEmptyStr | None = None
    references: tuple[NonEmptyStr, ...] = ()
    auto_submitted: NonEmptyStr | None = None
    precedence: NonEmptyStr | None = None
    x_autoreply: NonEmptyStr | None = None
    content_type: NonEmptyStr | None = None
    list_unsubscribe: NonEmptyStr | None = None
    provider_thread_ref: NonEmptyStr | None = None
    has_attachments: bool = False
    raw_ref: NonEmptyStr
    raw_hash: Sha256Hex


class InboundConfig(CoreModel):
    own_addresses: Annotated[UniqueEmailAddresses, Field(min_length=1)]
    sender: SenderIdentity
    code_version: NonEmptyStr
    default_locale: LocaleTag = "en"
    top_k: PositiveInt = 5
    max_questions: PositiveInt = 5
    max_question_chars: PositiveInt = 300
    thread_context_messages: Annotated[int, Field(ge=0, le=5)] = 4


class InboundResult(CoreModel):
    """Final outcome of one inbound message. Stored in the PROCESSING_COMPLETED audit event
    and returned unchanged (with ``replayed`` set) on every later replay."""

    correlation_id: EntityId
    message_id: EntityId
    thread_id: EntityId
    lead_id: EntityId | None = None
    prefilter: PrefilterOutcome
    classification: IntentClassification | None = None
    reply_decision: ReplyDecision
    draft_id: EntityId | None = None
    outbound_id: EntityId | None = None
    escalation_id: EntityId | None = None
    escalation_reasons: Annotated[tuple[EscalationReason, ...], AfterValidator(unique_items)] = ()
    knowledge_assessment: KnowledgeAssessment | None = None
    evidence_ids: UniqueEntityIds = ()
    claim_check_status: ClaimCheckStatus | None = None
    duplicate: bool = False
    replayed: bool = False
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        if self.reply_decision not in ALLOWED_DECISIONS:
            raise ValueError(f"{self.reply_decision} is not allowed in the V1 inbound flow")
        draft = self.draft_id is not None or self.outbound_id is not None
        escalated = self.escalation_id is not None or bool(self.escalation_reasons)
        if self.reply_decision is ReplyDecision.DRAFT_FOR_REVIEW:
            if self.draft_id is None or self.outbound_id is None or escalated:
                raise ValueError("DRAFT_FOR_REVIEW requires a draft and no escalation")
            if self.claim_check_status is not ClaimCheckStatus.PASS:
                raise ValueError("a review draft must have passed the claim check")
        elif self.reply_decision is ReplyDecision.ESCALATE:
            if self.escalation_id is None or not self.escalation_reasons or draft:
                raise ValueError("ESCALATE requires an escalation with reasons and no draft")
        elif draft or escalated:
            raise ValueError("NO_ACTION has neither a draft nor an escalation")
        return self
