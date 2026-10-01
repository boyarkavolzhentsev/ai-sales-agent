"""The application runtime: explicit lifecycle around the composed services.

States: CREATED -> STARTING -> READY -> STOPPING -> STOPPED; any startup failure -> FAILED
(never READY). Nothing happens at construction; ``start()`` validates the provider
configuration (an INVALID provider fails; PRODUCTION mode also needs every required
provider implemented, which none is yet, so it fails closed), connects,
migrates to the latest schema (idempotent; never recreates or clears data), builds the
services, inspects recovery and only then becomes READY. Ticks and the inbound entry
point run only while READY, one at a time (no reentrancy); after ``stop()`` begins no new
work starts. ``stop()`` is idempotent and only releases the database connection:
committed work stays committed and nothing is "undone" at a provider.

Synchronous by design: SQLite transactions and version checks are the concurrency
boundary; an external scheduler may run one-shot ticks.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.inbound import InboundEnvelope, InboundResult
from app.orchestration import (
    ExecutionMetrics,
    ExecutionPassResult,
    ExecutionQueue,
    ExecutionResult,
    SalesExecutionPlan,
    SalesExecutionView,
)
from app.persistence import MEMORY, Clock, Database, PersistenceError, SchemaVersionError, SystemClock
from app.persistence.migrations import latest_version
from app.runtime import workers
from app.runtime.config import RuntimeConfig
from app.integrations import IntegrationStatus, evaluate
from app.runtime.config import RuntimeMode
from app.runtime.container import Adapters, Capabilities, Services, build_services, configured_adapters
from app.runtime.errors import CapabilityUnavailableError, RuntimeBusyError, RuntimeNotReadyError, StartupError
from app.runtime.recovery import inspect_recovery
from app.runtime.results import (
    CapabilityReport,
    DispatchPhaseResult,
    HealthReport,
    PhaseStatus,
    ReconciliationResult,
    RuntimeState,
    RuntimeTickResult,
    StartupReport,
    WorkResult,
)

S = RuntimeState
SHUTTING_DOWN = "SHUTTING_DOWN"


class SalesAgentRuntime:
    def __init__(self, config: RuntimeConfig, *, adapters: Adapters | None = None, clock: Clock | None = None) -> None:
        self._config = config
        # Injected adapters (programmatic composition, tests) take precedence over the
        # configured providers; neither path performs I/O here.
        self._adapters = adapters or configured_adapters(config)
        self._clock = clock or SystemClock()
        self._capabilities = Capabilities.of(self._adapters)
        self._db = Database(config.database_path)
        self._services: Services | None = None
        self._state = S.CREATED
        self._startup: StartupReport | None = None
        self._busy = False
        self._integrations: IntegrationStatus | None = None

    @property
    def state(self) -> RuntimeState:
        return self._state

    @property
    def services(self) -> Services:
        """The composed services (only while READY), e.g. for an operator adapter."""
        return self._ready_services()

    # ---- Lifecycle --------------------------------------------------------------------------

    def start(self) -> StartupReport:
        if self._state is S.READY and self._startup is not None:
            return self._startup  # idempotent
        if self._state is not S.CREATED:
            raise RuntimeNotReadyError(f"cannot start from {self._state}")
        self._state = S.STARTING
        try:
            self._check_integrations()
            self._check_database_path()
            self._db.connect()
            version = self._db.initialize_schema(self._clock)
            if version != latest_version():
                raise StartupError("SCHEMA_NOT_CURRENT", f"schema {version}, expected {latest_version()}")
            services = build_services(self._db, self._clock, self._config, self._adapters)
            with self._db.transaction() as uow:
                recovery = inspect_recovery(uow, self._clock.now())
        except StartupError:
            self._fail()
            raise
        except SchemaVersionError as exc:  # written by newer code: never migrate down or touch it
            self._fail()
            raise StartupError("SCHEMA_NEWER_THAN_SUPPORTED", type(exc).__name__) from exc
        except PersistenceError as exc:
            self._fail()
            raise StartupError("DATABASE_UNAVAILABLE", type(exc).__name__) from exc
        except Exception as exc:
            self._fail()
            raise StartupError("STARTUP_FAILED", type(exc).__name__) from exc
        self._services = services
        assert self._integrations is not None
        self._startup = StartupReport(schema_version=version, recovery=recovery, capabilities=self._capability_report(),
                                      integrations=self._integrations)
        self._state = S.READY
        return self._startup

    def stop(self) -> None:
        """Idempotent. New work is refused at once; if a tick is running it finishes on its
        own transactions and the connection is released when it returns."""
        if self._state in (S.STOPPED, S.STOPPING):
            return
        self._state = S.STOPPING
        if not self._busy:
            self._finish_stop()

    def _finish_stop(self) -> None:
        self._services = None
        self._db.close()
        self._state = S.STOPPED

    def health(self) -> HealthReport:
        """Liveness plus readiness. Never contacts an external service."""
        problems: list[str] = []
        version: int | None = None
        database_ok = False
        if self._db.is_open:
            try:
                version = self._db.schema_version()
                database_ok = True
            except PersistenceError as exc:
                problems.append(f"DATABASE_UNAVAILABLE:{type(exc).__name__}")
        elif self._state is S.READY:
            problems.append("DATABASE_CLOSED")
        if version is not None and version != latest_version():
            problems.append("SCHEMA_NOT_CURRENT")
        ready = self._state is S.READY and database_ok and version == latest_version()
        return HealthReport(state=self._state, alive=self._state not in (S.FAILED, S.STOPPED), database_ok=database_ok,
                            schema_version=version, latest_schema_version=latest_version(), ready=ready,
                            capabilities=self._capability_report(), integrations=self._integrations,
                            problems=tuple(problems))

    # ---- Work ---------------------------------------------------------------------------------

    def reconcile(self, *, correlation_id: str | None = None) -> ReconciliationResult:
        with self._work() as services:
            return workers.reconcile_pass(services, correlation_id=correlation_id or self._correlation("reconcile"),
                                          limit=self._config.worker.batch_limit, available=self._capabilities.reconciliation)

    def campaign_tick(self, *, correlation_id: str | None = None) -> WorkResult:
        with self._work() as services:
            return workers.campaign_pass(services, self._db, worker_id=self._config.worker.worker_id,
                                         correlation_id=correlation_id or self._correlation("campaign"),
                                         limit=self._config.worker.batch_limit)

    def follow_up_tick(self, *, correlation_id: str | None = None) -> WorkResult:
        with self._work() as services:
            return workers.follow_up_pass(services, self._db, worker_id=self._config.worker.worker_id,
                                          correlation_id=correlation_id or self._correlation("follow-up"),
                                          limit=self._config.worker.batch_limit)

    def dispatch_tick(self, *, correlation_id: str | None = None) -> DispatchPhaseResult:
        with self._work() as services:
            return workers.dispatch_pass(services, self._db, correlation_id=correlation_id or self._correlation("dispatch"),
                                         limit=self._config.worker.batch_limit, available=self._capabilities.dispatch)

    def tick(self, *, dispatch_approved: bool = False, correlation_id: str | None = None) -> RuntimeTickResult:
        """Reconciliation, then campaign, then follow-up work; approved-message dispatch only
        when explicitly requested. Each phase is independent and keeps its own transactions."""
        with self._work() as services:
            correlation = correlation_id or self._correlation("tick")
            limit, worker_id = self._config.worker.batch_limit, self._config.worker.worker_id
            started_at = self._clock.now()
            # Phases start only while no shutdown was requested; a phase already running finishes.
            reconciliation = (workers.reconcile_pass(services, correlation_id=correlation, limit=limit,
                                                     available=self._capabilities.reconciliation)
                              if self._running() else ReconciliationResult(status=PhaseStatus.SKIPPED, reason=SHUTTING_DOWN))
            campaign = (workers.campaign_pass(services, self._db, worker_id=worker_id, correlation_id=correlation, limit=limit)
                        if self._running() else WorkResult(status=PhaseStatus.SKIPPED, reason=SHUTTING_DOWN))
            follow_up = (workers.follow_up_pass(services, self._db, worker_id=worker_id, correlation_id=correlation, limit=limit)
                         if self._running() else WorkResult(status=PhaseStatus.SKIPPED, reason=SHUTTING_DOWN))
            dispatch = None
            if dispatch_approved:
                dispatch = (workers.dispatch_pass(services, self._db, correlation_id=correlation, limit=limit,
                                                  available=self._capabilities.dispatch)
                            if self._running() else DispatchPhaseResult(status=PhaseStatus.SKIPPED, reason=SHUTTING_DOWN))
            return RuntimeTickResult(correlation_id=correlation, started_at=started_at, reconciliation=reconciliation,
                                     campaign=campaign, follow_up=follow_up, dispatch=dispatch)

    def handle_inbound(self, envelope: InboundEnvelope, *, correlation_id: str) -> InboundResult:
        """One observed inbound email through the Stage 6 service (which owns identity,
        classification, knowledge, suppression, the campaign handoff and conversation state),
        then the Stage 12 pipeline hook (intent on the lead, qualification extraction) and
        the Stage 13 commercial hook (requests, objections, signals) as their own steps. An
        extraction failure never fails this call; a persistence failure propagates and
        replaying the same message is safe (every step is idempotent)."""
        with self._work() as services:
            if services.inbound is None:
                raise CapabilityUnavailableError("inbound processing needs an LLM transport")
            result = services.inbound.process(envelope, correlation_id=correlation_id)
            services.pipeline.record_inbound(result, correlation_id=correlation_id)
            services.commercial.record_inbound(result, correlation_id=correlation_id)
            return result

    # ---- Sales execution coordination (Stage 14) ------------------------------------------------

    def execution_plan(self, lead_id: str) -> SalesExecutionPlan:
        """Read-only: the single next owner/action of one lead, from one consistent snapshot."""
        return self._ready_services().orchestrator.plan(lead_id)

    def execution_view(self, lead_id: str) -> SalesExecutionView:
        return self._ready_services().orchestrator.view(lead_id)

    def execution_queue(self, which: ExecutionQueue, *, limit: int | None = None) -> tuple[SalesExecutionPlan, ...]:
        return self._ready_services().orchestrator.queue(which, limit=limit or self._config.worker.batch_limit)

    def execution_metrics(self) -> ExecutionMetrics:
        return self._ready_services().orchestrator.metrics()

    def execution_once(self, lead_id: str, expected_fingerprint: str, *, dispatch_approved: bool = False,
                       execution_id: str | None = None, correlation_id: str | None = None) -> ExecutionResult:
        """At most one business action for one lead, only if the plan is still current.
        Dispatch of an operator-approved message only when explicitly requested."""
        with self._work() as services:
            return services.orchestrator.execute(lead_id, expected_fingerprint, allow_dispatch=dispatch_approved,
                                                 execution_id=execution_id,
                                                 correlation_id=correlation_id or self._correlation("execute"))

    def execution_pass(self, *, limit: int | None = None, dispatch_approved: bool = False,
                       correlation_id: str | None = None) -> ExecutionPassResult:
        """One bounded pass: at most ``limit`` leads (default: the worker batch limit), at
        most one action each. Runs once and returns; never on startup, never in a loop."""
        with self._work() as services:
            return services.orchestrator.execution_pass(correlation_id=correlation_id or self._correlation("execution-pass"),
                                                        limit=limit or self._config.worker.batch_limit,
                                                        allow_dispatch=dispatch_approved)

    # ---- Internals ------------------------------------------------------------------------------

    @contextmanager
    def _work(self) -> Iterator[Services]:
        services = self._ready_services()
        if self._busy:
            raise RuntimeBusyError("another tick of this runtime is still running")
        self._busy = True
        try:
            yield services
        finally:
            self._busy = False
            if self._state is S.STOPPING:  # stop() was requested while this tick ran
                self._finish_stop()

    def _running(self) -> bool:
        return self._state is S.READY

    def _ready_services(self) -> Services:
        if self._state is not S.READY or self._services is None:
            raise RuntimeNotReadyError(f"the runtime is {self._state}, not READY")
        return self._services

    def _check_integrations(self) -> None:
        """Configuration health only (local checks, no provider contact). Codes and
        variable names go into the error, never a value."""
        status = evaluate(self._config.integrations, self._config.secrets, mailboxes=self._config.mailboxes)
        self._integrations = status
        if not status.valid:
            problems = [problem for provider in status.providers for problem in provider.problems]
            raise StartupError("INTEGRATION_CONFIG_INVALID", "; ".join(problems))
        if self._config.mode is RuntimeMode.PRODUCTION and not status.production_ready:
            raise StartupError("PRODUCTION_NOT_READY", ", ".join(status.production_blockers))

    def _check_database_path(self) -> None:
        if self._config.database_path == MEMORY:
            return
        path = Path(self._config.database_path)
        if path.is_dir():
            raise StartupError("DATABASE_PATH_INVALID", "is a directory")
        if not path.parent.is_dir():
            raise StartupError("DATABASE_PATH_INVALID", "parent directory does not exist")

    def _fail(self) -> None:
        self._services = None
        self._db.close()
        self._state = S.FAILED

    def _correlation(self, what: str) -> str:
        return f"{what}-{_stamp(self._clock.now())}"

    def _capability_report(self) -> CapabilityReport:
        return CapabilityReport(dispatch=self._capabilities.dispatch, reconciliation=self._capabilities.reconciliation,
                                inbound=self._capabilities.inbound)


def _stamp(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%S%fZ")
