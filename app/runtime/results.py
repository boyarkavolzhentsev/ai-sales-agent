"""Typed runtime reports. Counts come from the results the subsystems return during the
call; nothing here is persisted. IDs and codes only: no email bodies, no secrets."""

from datetime import datetime
from enum import StrEnum

from app.core.models.base import CoreModel
from app.integrations import IntegrationStatus
from app.knowledge import IndexReport


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
    email_sync: bool = False
    operator_channel: bool = False
    qualification_extraction: bool = False
    commercial_extraction: bool = False
    sales_advice: bool = False
    semantic_retrieval: bool = False  # an embeddings provider is configured (Stage 19)


class KnowledgeIndexResult(CoreModel):
    """One ``knowledge-index`` run: ingestion of the configured knowledge directory, then
    the embedding index. Counts and codes only: no knowledge text, no vectors."""

    status: PhaseStatus
    reason: str | None = None
    # Ingestion of KNOWLEDGE_DIR (when configured): source versions ingested / already present.
    sources_ingested: int = 0
    sources_unchanged: int = 0
    ingestion_error: str | None = None  # an exception type: the directory was rejected as a whole
    # The embedding index (when an embeddings provider is configured).
    embeddings: IndexReport | None = None


class ServiceTickResult(CoreModel):
    """One ``service-tick``: the existing one-shot passes in a fixed order, each with its own
    transactions and outcome. A phase that raised is listed in ``errors`` (its exception type
    only) and the cycle continues; ``None`` means the phase did not run (shutting down)."""

    correlation_id: str
    email_sync: dict[str, object] | None = None
    ai_recovery: dict[str, object] | None = None
    tick: RuntimeTickResult | None = None
    operator_sync: dict[str, object] | None = None
    errors: tuple[ItemError, ...] = ()

    @property
    def ok(self) -> bool:
        statuses = [phase.get("status") for phase in (self.email_sync, self.ai_recovery, self.operator_sync) if phase]
        return not self.errors and (self.tick is None or self.tick.ok) and not any(
            s in ("ERROR", "RECOVERY_REQUIRED") for s in statuses)


class StartupReport(CoreModel):
    schema_version: int
    recovery: RecoveryReport
    capabilities: CapabilityReport
    integrations: IntegrationStatus  # configuration health only; sanitized


class HealthReport(CoreModel):
    state: RuntimeState
    alive: bool
    database_ok: bool
    schema_version: int | None
    latest_schema_version: int
    ready: bool
    capabilities: CapabilityReport
    # Provider configuration health (no connectivity check); None until startup evaluated it.
    integrations: IntegrationStatus | None = None
    problems: tuple[str, ...] = ()
