"""Provider-agnostic LLM boundary. LLM components return validated proposals only; they
never touch persistence, policy, quota, DNC, permits or sending. Only app.core, stdlib and
pydantic are imported here (enforced by tests)."""

from app.llm.claim_check import (
    COMMITMENT_PATTERNS,
    ClaimCheckResult,
    ClaimFinding,
    ClaimType,
    FindingReason,
    check_draft_claims,
)
from app.llm.classifier import (
    ClassificationOutcome,
    ClassifierInput,
    IntentClassificationProposal,
    classify_intent,
    to_intent_classification,
)
from app.llm.composer import (
    CompositionOutcome,
    NextStep,
    ReplyCompositionInput,
    ReplyDraftProposal,
    SenderIdentity,
    compose_reply,
)
from app.llm.errors import (
    LLMContractViolationError,
    LLMError,
    LLMErrorCode,
    LLMNoScriptedResponseError,
    LLMProviderError,
    LLMStructuredOutputError,
    LLMTimeoutError,
)
from app.llm.fake import FakeLLMTransport, FakeResponse
from app.llm.inputs import UntrustedEmail
from app.llm.models import LLMRawOutput, LLMRequest, LLMResultMetadata, LLMTask, SectionKind
from app.llm.prompts import PROMPTS, PromptTemplate
from app.llm.protocols import LLMTransport
from app.llm.sufficiency import (
    KnowledgeSufficiencyOpinion,
    SufficiencyInput,
    SufficiencyOutcome,
    apply_llm_sufficiency_opinion,
    assess_sufficiency,
)
from app.llm.summarizer import SummaryOutcome, ThreadSummary, ThreadSummaryInput, summarize_thread
from app.llm.validation import StructuredLLM

__all__ = [
    "COMMITMENT_PATTERNS",
    "PROMPTS",
    "ClaimCheckResult",
    "ClaimFinding",
    "ClaimType",
    "ClassificationOutcome",
    "ClassifierInput",
    "CompositionOutcome",
    "FakeLLMTransport",
    "FakeResponse",
    "FindingReason",
    "IntentClassificationProposal",
    "KnowledgeSufficiencyOpinion",
    "LLMContractViolationError",
    "LLMError",
    "LLMErrorCode",
    "LLMNoScriptedResponseError",
    "LLMProviderError",
    "LLMRawOutput",
    "LLMRequest",
    "LLMResultMetadata",
    "LLMStructuredOutputError",
    "LLMTask",
    "LLMTimeoutError",
    "LLMTransport",
    "NextStep",
    "PromptTemplate",
    "ReplyCompositionInput",
    "ReplyDraftProposal",
    "SectionKind",
    "SenderIdentity",
    "StructuredLLM",
    "SufficiencyInput",
    "SufficiencyOutcome",
    "SummaryOutcome",
    "ThreadSummary",
    "ThreadSummaryInput",
    "UntrustedEmail",
    "apply_llm_sufficiency_opinion",
    "assess_sufficiency",
    "check_draft_claims",
    "classify_intent",
    "compose_reply",
    "summarize_thread",
    "to_intent_classification",
]
