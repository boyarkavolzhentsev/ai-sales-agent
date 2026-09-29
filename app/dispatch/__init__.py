"""Offline dispatch of operator-approved inbound replies (V1: REPLY only).

Imports app.core, app.persistence, app.policy (Stage 3 policy, reservation, release),
app.llm (claim-check hashing, sender identity), app.inbound (stable IDs) and
app.operator.review (shared gates) only. No provider SDKs, sockets or HTTP: the transport
is injected, and the only implementation here is the scripted fake (enforced by tests).
"""

from app.dispatch.errors import DispatchError, DispatchNotFoundError, DispatchStateError
from app.dispatch.fake import FakeBehavior, FakeEmailTransport, FakeReconciler, FakeStep
from app.dispatch.models import (
    AttemptView,
    DispatchCode,
    DispatchConfig,
    DispatchOutcome,
    DispatchRequest,
    DispatchResult,
    DispatchStatusView,
)
from app.dispatch.service import DispatchService
from app.dispatch.transport import (
    DispatchReconciler,
    EmailTransport,
    NotSubmittedError,
    ReconciliationFinding,
    ReconciliationResult,
    TransportOutcome,
    TransportRequest,
    TransportResult,
)

__all__ = [
    "AttemptView",
    "DispatchCode",
    "DispatchConfig",
    "DispatchError",
    "DispatchNotFoundError",
    "DispatchOutcome",
    "DispatchReconciler",
    "DispatchRequest",
    "DispatchResult",
    "DispatchService",
    "DispatchStatusView",
    "DispatchStateError",
    "EmailTransport",
    "FakeBehavior",
    "FakeEmailTransport",
    "FakeReconciler",
    "FakeStep",
    "NotSubmittedError",
    "ReconciliationFinding",
    "ReconciliationResult",
    "TransportOutcome",
    "TransportRequest",
    "TransportResult",
]
