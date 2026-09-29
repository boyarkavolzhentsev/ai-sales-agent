"""One campaign worker tick: claim due touches, execute each claim (at-least-once,
idempotent effects; safe to run concurrently and repeatedly)."""

from app.campaign.executor import CampaignExecutor
from app.campaign.models import ExecutionResult
from app.campaign.scheduler import CampaignScheduler


def run_once(scheduler: CampaignScheduler, executor: CampaignExecutor, worker_id: str, *, correlation_id: str, limit: int = 10) -> tuple[ExecutionResult, ...]:
    claims = scheduler.claim_due(worker_id, correlation_id=correlation_id, limit=limit)
    return tuple(executor.execute(claim, correlation_id=correlation_id) for claim in claims)
