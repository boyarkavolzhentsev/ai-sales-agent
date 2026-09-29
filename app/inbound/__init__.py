"""Inbound sales-email application workflow (V1: review drafts, escalations, no sending).

Imports app.core, app.persistence, app.knowledge, app.llm and the Stage 3 suppression
evaluator only. It never imports sending, permits, quota reservation, providers or
Telegram (enforced by tests)."""

from app.inbound.errors import IdempotencyCollisionError, InboundProcessingError
from app.inbound.models import (
    ALLOWED_DECISIONS,
    InboundConfig,
    InboundEnvelope,
    InboundResult,
    PrefilterOutcome,
    stable_id,
)
from app.inbound.service import InboundService

__all__ = [
    "ALLOWED_DECISIONS",
    "IdempotencyCollisionError",
    "InboundConfig",
    "InboundEnvelope",
    "InboundProcessingError",
    "InboundResult",
    "InboundService",
    "PrefilterOutcome",
    "stable_id",
]
