"""Startup recovery inspection (read-only).

Durable work left by a crashed or stopped process is already recoverable by the owning
subsystem, so startup only inspects and reports it:
- campaign and follow-up jobs whose lease expired are claimable again by the next tick
  (a fresh claim token; the dead worker's token can no longer act);
- due SCHEDULED jobs are claimed by the next tick;
- unresolved dispatch attempts (CLAIMED/UNKNOWN) are resolved only by Stage 8
  reconciliation with positive evidence; startup never resends, never guesses acceptance,
  and never marks anything failed.
Startup therefore creates no business work and changes no durable state.
"""

from datetime import datetime

from app.core.enums import CampaignJobStatus, FollowUpJobStatus, OutboundStatus
from app.persistence import UnitOfWork
from app.runtime.results import RecoveryReport

_SCAN_LIMIT = 100_000


def inspect_recovery(uow: UnitOfWork, now: datetime) -> RecoveryReport:
    campaign_jobs = uow.campaign_jobs.list_claimable(now, _SCAN_LIMIT)
    follow_up_jobs = uow.follow_up_jobs.list_claimable(now, _SCAN_LIMIT)
    unresolved = uow.dispatch_attempts.list_unresolved()
    in_flight = {attempt.outbound_id for attempt in unresolved}
    sending = uow.outbound.list_by_status(OutboundStatus.SENDING)
    return RecoveryReport(
        expired_campaign_claims=sum(1 for j in campaign_jobs if j.status is CampaignJobStatus.CLAIMED),
        expired_follow_up_claims=sum(1 for j in follow_up_jobs if j.status is FollowUpJobStatus.CLAIMED),
        due_campaign_jobs=sum(1 for j in campaign_jobs if j.status is CampaignJobStatus.SCHEDULED),
        due_follow_up_jobs=sum(1 for j in follow_up_jobs if j.status is FollowUpJobStatus.SCHEDULED),
        unresolved_dispatch_attempts=len(unresolved),
        sending_without_attempt=sum(1 for m in sending if m.outbound_id not in in_flight),
    )
