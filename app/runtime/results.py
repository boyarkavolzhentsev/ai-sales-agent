"""Typed runtime reports. Counts come from the results the subsystems return during the
call; nothing here is persisted. IDs and codes only: no email bodies, no secrets."""

from datetime import datetime
from enum import StrEnum

from app.core.models.base import CoreModel


class RuntimeState(StrEnum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    READY = "READY"
    FAILED = "FAILED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


class PhaseStatus(StrEnum):
    OK = "OK"
    SKIPPED = "SKIPPED"  # the phase's capability is not configured
    ERROR = "ERROR"  # at least one item or the phase itself failed (see ``errors``)


class ItemError(CoreModel):
    """One isolated failure: what it concerned and its exception type (no message text,
    which may carry data)."""

    subject: str
    error_type: str


class ReconciliationResult(CoreModel):
    status: PhaseStatus
    reason: str | None = None
    processed: int = 0
    accepted: int = 0
    not_accepted: int = 0
    unresolved: int = 0
    errors: tuple[ItemError, ...] = ()


class WorkResult(CoreModel):
    """A campaign or conversation follow-up phase: schedule, claim, execute."""

    status: PhaseStatus
    reason: str | None = None
    scheduled: int = 0
    claimed: int = 0
    drafted: int = 0
    deferred: int = 0
    blocked: int = 0
    superseded: int = 0
    stale: int = 0
    replayed: int = 0
    cancelled: int = 0
    errors: tuple[ItemError, ...] = ()


class DispatchPhaseResult(CoreModel):
    status: PhaseStatus
    reason: str | None = None
    processed: int = 0
    accepted: int = 0
    not_accepted: int = 0
    unknown: int = 0
    blocked: int = 0
    errors: tuple[ItemError, ...] = ()


class RuntimeTickResult(CoreModel):
    """Each phase keeps its own transactions; the tick as a whole is not atomic."""

    correlation_id: str
    started_at: datetime
    reconciliation: ReconciliationResult
    campaign: WorkResult
    follow_up: WorkResult
    dispatch: DispatchPhaseResult | None = None

    @property
    def ok(self) -> bool:
        phases = [self.reconciliation, self.campaign, self.follow_up, *([self.dispatch] if self.dispatch else [])]
        return all(phase.status is not PhaseStatus.ERROR for phase in phases)


class RecoveryReport(CoreModel):
    """What durable work was waiting when the process started. Read-only inspection: the
    existing subsystems resume it themselves (expired leases are claimable again;
    unresolved dispatch attempts go only through Stage 8 reconciliation)."""

    expired_campaign_claims: int
    expired_follow_up_claims: int
    due_campaign_jobs: int
    due_follow_up_jobs: int
    unresolved_dispatch_attempts: int
    # SENDING messages without an unresolved attempt would contradict Stage 8; always 0.
    sending_without_attempt: int


class CapabilityReport(CoreModel):
    dispatch: bool
    reconciliation: bool
    inbound: bool


class StartupReport(CoreModel):
    schema_version: int
    recovery: RecoveryReport
    capabilities: CapabilityReport


class HealthReport(CoreModel):
    state: RuntimeState
    alive: bool
    database_ok: bool
    schema_version: int | None
    latest_schema_version: int
    ready: bool
    capabilities: CapabilityReport
    problems: tuple[str, ...] = ()
