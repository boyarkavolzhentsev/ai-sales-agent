"""Operator workflow (V1, offline): review drafts, take ownership, resolve escalations.

Imports app.core, app.persistence, app.knowledge, app.llm (claim check, sender identity),
app.inbound (stable IDs and audit event names) and the Stage 3 suppression evaluator only.
It never sends, creates send permits, reserves quota, calls providers or Telegram, and
never writes to the knowledge base (enforced by tests).
"""

from app.operator.auth import OperatorAuthenticator, authorize
from app.operator.errors import (
    CommandCollisionError,
    CommandRejectedError,
    OperatorError,
    OperatorNotFoundError,
    OperatorUnauthorizedError,
    StaleCommandError,
)
from app.operator.models import (
    ApproveDraft,
    BlockCode,
    CommandKind,
    CommandOutcome,
    CommandResult,
    DraftDetail,
    DraftSummary,
    EmailText,
    EscalationDetail,
    EscalationSummary,
    LeadView,
    OperatorConfig,
    OperatorCredential,
    RejectDraft,
    RejectReason,
    ResolveEscalation,
    TakeOwnership,
    ThreadView,
)
from app.operator.service import OperatorService

__all__ = [
    "ApproveDraft",
    "BlockCode",
    "CommandCollisionError",
    "CommandKind",
    "CommandOutcome",
    "CommandRejectedError",
    "CommandResult",
    "DraftDetail",
    "DraftSummary",
    "EmailText",
    "EscalationDetail",
    "EscalationSummary",
    "LeadView",
    "OperatorAuthenticator",
    "OperatorConfig",
    "OperatorCredential",
    "OperatorError",
    "OperatorNotFoundError",
    "OperatorService",
    "OperatorUnauthorizedError",
    "RejectDraft",
    "RejectReason",
    "ResolveEscalation",
    "StaleCommandError",
    "TakeOwnership",
    "ThreadView",
    "authorize",
]
