"""One scheduler tick: claim due jobs, then execute each claim. Safe to run concurrently
and repeatedly (at-least-once execution with idempotent effects)."""

from app.conversation.executor import FollowUpExecutor
from app.conversation.models import ExecutionResult
from app.conversation.scheduler import FollowUpScheduler


def run_once(scheduler: FollowUpScheduler, executor: FollowUpExecutor, worker_id: str, *, correlation_id: str, limit: int = 10) -> tuple[ExecutionResult, ...]:
    claims = scheduler.claim_due(worker_id, correlation_id=correlation_id, limit=limit)
    return tuple(executor.execute(claim, correlation_id=correlation_id) for claim in claims)
