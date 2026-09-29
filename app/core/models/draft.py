from typing import Annotated, Self

from pydantic import AfterValidator, AwareDatetime, model_validator

from app.core.enums import ClaimCheckStatus, DraftPurpose, DraftReviewStatus
from app.core.models.base import CoreModel
from app.core.models.types import EntityId, NonEmptyStr, Sha256Hex, UniqueNonEmptyStrs
from app.core.validation import unique_items

_THREADED_PURPOSES = frozenset({DraftPurpose.INBOUND_REPLY, DraftPurpose.OUTBOUND_FOLLOW_UP})


class EvidenceCitation(CoreModel):
    """Binds a claim in a draft to the evidence that supports it."""

    evidence_id: EntityId
    claim: NonEmptyStr


class MessageDraft(CoreModel):
    """Generated email text. Generated content is immutable; review fields record the outcome."""

    draft_id: EntityId
    purpose: DraftPurpose
    lead_id: EntityId
    thread_id: EntityId | None = None
    subject: NonEmptyStr
    body_generated: NonEmptyStr
    # Generated body plus the deterministic footer appended outside the LLM.
    body_final: NonEmptyStr
    evidence_citations: Annotated[tuple[EvidenceCitation, ...], AfterValidator(unique_items)] = ()
    claim_check_status: ClaimCheckStatus
    claim_check_findings: UniqueNonEmptyStrs = ()
    model: NonEmptyStr
    prompt_version: NonEmptyStr
    input_hash: Sha256Hex
    review_status: DraftReviewStatus = DraftReviewStatus.PENDING
    reviewed_by: NonEmptyStr | None = None
    edited_body: NonEmptyStr | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.purpose in _THREADED_PURPOSES and self.thread_id is None:
            raise ValueError(f"{self.purpose} draft requires thread_id")
        has_findings = bool(self.claim_check_findings)
        if self.claim_check_status is ClaimCheckStatus.PASS and has_findings:
            raise ValueError("a PASS claim check must not have findings")
        if self.claim_check_status is not ClaimCheckStatus.PASS and not has_findings:
            raise ValueError(f"a {self.claim_check_status} claim check requires findings")
        reviewed = self.review_status is not DraftReviewStatus.PENDING
        if reviewed != (self.reviewed_by is not None):
            raise ValueError("reviewed_by is required for, and only allowed on, a reviewed draft")
        if self.edited_body is not None and self.review_status is not DraftReviewStatus.APPROVED:
            raise ValueError("edited_body is only allowed on an APPROVED draft")
        return self
