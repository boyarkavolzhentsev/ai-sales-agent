"""Reply composition proposal. Side-effect free: build request, call, validate, bind
evidence, run the deterministic claim check. No persistence, sending or state change.

The output contract holds only draftable content. It has no fields for sender or
recipient addresses, footers, unsubscribe text, headers, Message-ID, send time, campaign,
approval or send decisions; any such field in model output is rejected as unknown.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, Field, StringConstraints, model_validator

from app.core.enums import DraftPurpose, LeadIntent, LeadStage
from app.core.models import KnowledgeAssessment, KnowledgeEvidence
from app.core.models.base import CoreModel
from app.core.models.types import EntityId, LocaleTag, NonEmptyStr
from app.core.validation import unique_items
from app.llm.claim_check import ClaimCheckResult, check_draft_claims
from app.llm.errors import LLMContractViolationError
from app.llm.inputs import UntrustedEmail
from app.llm.models import LLMResultMetadata, SectionKind
from app.llm.prompts import REPLY_COMPOSER_PROMPT_V1, build_request, section
from app.llm.validation import StructuredLLM, require_known_evidence_ids

MAX_THREAD_MESSAGES = 10
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class NextStep(StrEnum):
    """Next steps the application may allow a draft to propose."""

    ANSWER_QUESTIONS = "ANSWER_QUESTIONS"
    ASK_CLARIFYING_QUESTION = "ASK_CLARIFYING_QUESTION"
    OFFER_MEETING = "OFFER_MEETING"
    SHARE_APPROVED_MATERIAL = "SHARE_APPROVED_MATERIAL"
    OPERATOR_FOLLOW_UP = "OPERATOR_FOLLOW_UP"


class SenderIdentity(CoreModel):
    """Deterministic placeholders from configuration, for tone and self-reference only.
    The real signature and footer are appended by the application, not the model."""

    sender_name: NonEmptyStr
    company_name: NonEmptyStr


class ReplyCompositionInput(CoreModel):
    """Only what a reply needs; no database dumps, secrets, quota, DNC or credentials."""

    purpose: DraftPurpose
    thread: Annotated[tuple[UntrustedEmail, ...], Field(min_length=1, max_length=MAX_THREAD_MESSAGES)]
    lead_stage: LeadStage
    lead_intent: LeadIntent | None = None
    contact_name: Annotated[str, StringConstraints(max_length=200)] = ""
    assessment: KnowledgeAssessment
    evidence: tuple[KnowledgeEvidence, ...]
    allowed_next_steps: Annotated[tuple[NextStep, ...], Field(min_length=1), AfterValidator(unique_items)]
    sender: SenderIdentity
    locale: LocaleTag

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        unique_items(tuple(e.evidence_id for e in self.evidence))
        if any(e.query_id != self.assessment.query_id for e in self.evidence):
            raise ValueError("evidence is for a different query than the assessment")
        return self


class ReplyDraftProposal(CoreModel):
    subject: Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"^[^\r\n]*$")]
    body: Annotated[str, StringConstraints(min_length=1, max_length=5000)]
    evidence_ids_used: Annotated[tuple[EntityId, ...], AfterValidator(unique_items)] = ()
    proposed_next_step: NextStep
    uncertain_claims: tuple[ShortText, ...] = ()
    review_notes: tuple[ShortText, ...] = ()


@dataclass(frozen=True)
class CompositionOutcome:
    """A validated draft proposal and its claim check. A draft whose claim check failed is
    still returned so it can be shown to an operator; it must never be sent as-is."""

    proposal: ReplyDraftProposal
    claim_check: ClaimCheckResult
    cited_evidence: tuple[KnowledgeEvidence, ...]
    metadata: LLMResultMetadata


def compose_reply(llm: StructuredLLM, data: ReplyCompositionInput, *, correlation_id: str) -> CompositionOutcome:
    sections = [
        section(
            SectionKind.TRUSTED_METADATA,
            "context",
            {
                "purpose": data.purpose,
                "lead_stage": data.lead_stage,
                "lead_intent": data.lead_intent,
                "allowed_next_steps": list(data.allowed_next_steps),
                "sender": data.sender.model_dump(mode="json"),
                "knowledge_decision": data.assessment.decision,
                "locale": data.locale,
            },
        ),
        section(
            SectionKind.TRUSTED_EVIDENCE,
            "evidence",
            [{"evidence_id": e.evidence_id, "domain": e.domain, "excerpt": e.excerpt} for e in data.evidence],
        ),
        section(SectionKind.UNTRUSTED_DATA, "contact_name", data.contact_name),
        section(SectionKind.UNTRUSTED_DATA, "thread", [m.model_dump(mode="json") for m in data.thread]),
    ]
    call = build_request(
        REPLY_COMPOSER_PROMPT_V1,
        ReplyDraftProposal,
        correlation_id=correlation_id,
        locale=data.locale,
        sections=sections,
    )
    result = llm.complete_structured(call)
    proposal = result.output
    by_id = {e.evidence_id: e for e in data.evidence}
    require_known_evidence_ids(proposal.evidence_ids_used, by_id.keys())
    if proposal.proposed_next_step not in data.allowed_next_steps:
        raise LLMContractViolationError(f"next step {proposal.proposed_next_step} is not allowed here")
    cited = tuple(by_id[evidence_id] for evidence_id in proposal.evidence_ids_used)
    # Claims are checked only against the evidence the draft says it relied on.
    claim_check = check_draft_claims(
        proposal.subject,
        proposal.body,
        cited,
        trusted_references=(data.sender.company_name, data.sender.sender_name),
    )
    return CompositionOutcome(proposal=proposal, claim_check=claim_check, cited_evidence=cited, metadata=result.metadata)
