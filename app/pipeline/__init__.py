"""Sales pipeline and lead lifecycle orchestration (Stage 12, logic only).

Answers: where a lead stands commercially (LeadStage), who owns the next action and why
(derived read model), and which transitions are allowed for whom (``policy``).

- ``policy``: the canonical pipeline and the actor-aware transition rules; every Stage 12
  stage change goes through ``apply_transition`` (audited, versioned).
- ``qualification``: facts with evidence, conflicts (never silent overwrites), readiness,
  gap planning, operator approval.
- ``lifecycle``: opportunity, negotiation, WON/LOST/disqualify (operator only) and their
  automation stop through the existing Stage 9/10 services; controlled reopen.
- ``next_action`` / ``views``: next-action ownership, blockers, queues and metrics.
- ``contracts``: provider-neutral extraction and advisor contracts (no live model).
- ``service``: the inbound hook and the reads.

No AI output mutates the pipeline directly; WON, LOST, QUALIFIED, OPPORTUNITY,
NEGOTIATION, DISQUALIFIED and reopening require an operator.
"""

from app.pipeline.config import GENERIC_PROFILE, FieldSpec, PipelineConfig, QualificationProfile
from app.pipeline.contracts import (
    AdvisorInput,
    ExtractionRequest,
    FactProposal,
    KnownFact,
    QualificationExtraction,
    QualificationExtractor,
    SalesAdvisor,
    SalesRecommendation,
)
from app.pipeline.errors import PipelineCode, PipelineError, PipelineNotFoundError
from app.pipeline.next_action import NextAction
from app.pipeline.qualification import QualificationGap
from app.pipeline.service import HookStatus, InboundPipelineOutcome, PipelineService, Recommendation
from app.pipeline.views import LeadPipelineView, PipelineMetrics, PipelineQueue, TransitionCount

__all__ = [
    "GENERIC_PROFILE",
    "AdvisorInput",
    "ExtractionRequest",
    "FactProposal",
    "FieldSpec",
    "HookStatus",
    "InboundPipelineOutcome",
    "KnownFact",
    "LeadPipelineView",
    "NextAction",
    "PipelineCode",
    "PipelineConfig",
    "PipelineError",
    "PipelineMetrics",
    "PipelineNotFoundError",
    "PipelineQueue",
    "PipelineService",
    "QualificationExtraction",
    "QualificationExtractor",
    "QualificationGap",
    "QualificationProfile",
    "Recommendation",
    "SalesAdvisor",
    "SalesRecommendation",
    "TransitionCount",
]
