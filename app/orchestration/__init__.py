"""Sales execution coordination (Stage 14): one deterministic coordinator above the Stage
6-13 subsystems. It answers who owns a lead now, which single action is next, what blocks
it, and runs at most one automatic action per call through existing subsystem operations.

It is not a source of business truth (every subsystem stays authoritative for its own
state), stores nothing derived (plans are recomputed from durable state), never acts for
an operator, never loops, and never calls a provider directly. Dependency direction:
runtime -> orchestration -> subsystems; no subsystem imports this package.
"""

from app.orchestration.errors import OrchestrationError, OrchestrationNotFoundError
from app.orchestration.models import (
    AUTOMATIC_ACTIONS,
    OPERATOR_COMMANDS,
    OUTBOUND_ACTIONS,
    EntityRefs,
    ExecutionAction,
    ExecutionBlocker,
    ExecutionCapabilities,
    ExecutionMetrics,
    ExecutionOutcome,
    ExecutionOwner,
    ExecutionPassResult,
    ExecutionQueue,
    ExecutionResult,
    ExecutionSubsystem,
    SalesExecutionPlan,
    SalesExecutionView,
)
from app.orchestration.service import OrchestratorConfig, SalesOrchestrator

__all__ = [
    "AUTOMATIC_ACTIONS",
    "OPERATOR_COMMANDS",
    "OUTBOUND_ACTIONS",
    "EntityRefs",
    "ExecutionAction",
    "ExecutionBlocker",
    "ExecutionCapabilities",
    "ExecutionMetrics",
    "ExecutionOutcome",
    "ExecutionOwner",
    "ExecutionPassResult",
    "ExecutionQueue",
    "ExecutionResult",
    "ExecutionSubsystem",
    "OrchestrationError",
    "OrchestrationNotFoundError",
    "OrchestratorConfig",
    "SalesExecutionPlan",
    "SalesExecutionView",
    "SalesOrchestrator",
]
