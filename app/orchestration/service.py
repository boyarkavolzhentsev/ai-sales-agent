"""The sales execution coordinator: read-only planning, then an explicit one-step executor.

plan(lead_id)
    One transaction reads a consistent snapshot of every subsystem; the pure planner
    returns exactly one owner and one action with a fingerprint. Nothing is written and
    nothing is persisted (plans are derived, never stored).

execute(lead_id, expected_fingerprint)
    Re-plans from a fresh snapshot. A different fingerprint means durable state changed
    since the caller planned (a reply, DNC, an approval, a reconciliation, a revision...):
    STALE_PLAN, nothing runs. Otherwise AT MOST ONE business action runs, and only an
    automatic one, through the owning subsystem's existing public operation, which
    revalidates everything again in its own transaction (claim tokens, version checks,
    Stage 7 provenance, Stage 8 gates and policy). Operator- and customer-gated actions
    are reported, never performed: the coordinator holds no operator credential.
    Dispatch of an operator-approved message additionally needs ``allow_dispatch``, like
    the runtime's explicit dispatch phase. There is no loop: the call returns after one
    action, with the re-planned fingerprint.

execution_pass(limit)
    Bounded: plans every open lead once, takes at most ``limit`` executable plans in a
    deterministic order and executes each once. No loop, no daemon, no thread.

Concurrency: no lease of its own. The fingerprint check plus the subsystems' own
claims, version checks and SQL uniqueness are the concurrency boundary; a lost race comes
back as STALE_PLAN (or the subsystem's own outcome), never as a duplicate side effect.
Optional ``execution_id``s reuse the existing idempotency table (no schema change): the
same id replays only the same lead/action/fingerprint; any other use is a collision.
"""

from dataclasses import dataclass
from datetime import datetime

from app.campaign import CampaignExecutor, CampaignScheduler
from app.campaign import ExecutionOutcome as CampaignOutcome
from app.commercial.config import CommercialProfile
from app.conversation import FollowUpExecutor, FollowUpScheduler, ScheduleOutcome
from app.conversation import ExecutionOutcome as FollowUpOutcome
from app.conversation.policy import FollowUpConfig
from app.dispatch import DispatchOutcome, DispatchRequest, DispatchService
from app.dispatch.models import DispatchConfig
from app.orchestration.errors import OrchestrationNotFoundError
from app.orchestration.models import (
    AUTOMATIC_ACTIONS,
    ExecutionAction,
    ExecutionBlocker,
    ExecutionCapabilities,
    ExecutionMetrics,
    ExecutionOutcome,
    ExecutionOwner,
    ExecutionPassResult,
    ExecutionQueue,
    ExecutionResult,
    SalesExecutionPlan,
    SalesExecutionView,
)
from app.orchestration.planner import PlannerSettings, plan
from app.orchestration.snapshot import LeadSnapshot, gather
from app.orchestration.views import in_queue, metrics_of, ordered, view_of
from app.persistence import Clock, ConcurrencyError, Database, DuplicateIdempotencyKeyError, UnitOfWork
from app.pipeline.config import QualificationProfile
from app.pipeline.policy import OPEN_STAGES
from app.policy import KillSwitchState

A, X = ExecutionAction, ExecutionOutcome
CAPABILITY_BLOCKERS = frozenset({ExecutionBlocker.PROVIDER_CAPABILITY_MISSING, ExecutionBlocker.LLM_CAPABILITY_MISSING})
EXECUTION_KEY_PREFIX = "orchestration:"
DISPATCH_NOT_REQUESTED = "DISPATCH_NOT_REQUESTED"


@dataclass(frozen=True)
class OrchestratorConfig:
    qualification: QualificationProfile
    commercial: CommercialProfile
    follow_up: FollowUpConfig
    dispatch: DispatchConfig  # Stage 3 policy inputs for the read-only send check
    kill_switch: KillSwitchState
    worker_id: str
    # Upper bound of open leads one queue/metrics/pass call plans (oldest included first
    # only within this bound; the lead table orders by recency).
    scan_limit: int = 100_000


@dataclass(frozen=True)
class _Ran:
    outcome: ExecutionOutcome
    subsystem_outcome: str | None = None
    reason: str | None = None
    codes: tuple[str, ...] = ()


class SalesOrchestrator:
    def __init__(
        self, db: Database, clock: Clock, config: OrchestratorConfig, capabilities: ExecutionCapabilities, *,
        campaign_scheduler: CampaignScheduler, campaign_executor: CampaignExecutor,
        follow_up_scheduler: FollowUpScheduler, follow_up_executor: FollowUpExecutor, dispatch: DispatchService | None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._config = config
        self._settings = PlannerSettings(capabilities=capabilities, kill_switch=config.kill_switch.enabled)
        self._campaign_scheduler = campaign_scheduler
        self._campaign_executor = campaign_executor
        self._follow_up_scheduler = follow_up_scheduler
        self._follow_up_executor = follow_up_executor
        self._dispatch = dispatch

    # ---- Read-only planning ---------------------------------------------------------------------

    def plan(self, lead_id: str) -> SalesExecutionPlan:
        with self._db.transaction() as uow:
            snapshot = self._snapshot(uow, lead_id, self._clock.now())
        return plan(snapshot, self._settings)

    def view(self, lead_id: str) -> SalesExecutionView:
        with self._db.transaction() as uow:
            snapshot = self._snapshot(uow, lead_id, self._clock.now())
        return view_of(snapshot, plan(snapshot, self._settings))

    def queue(self, which: ExecutionQueue, *, limit: int | None = None) -> tuple[SalesExecutionPlan, ...]:
        selected = ordered(p for p in self._open_plans() if in_queue(which, p))
        return tuple(selected[:limit] if limit is not None else selected)

    def metrics(self) -> ExecutionMetrics:
        with self._db.transaction() as uow:
            by_stage = uow.leads.count_by_stage()
            plans = self._plans(uow)
        return metrics_of(plans, by_stage)

    # ---- Execution ------------------------------------------------------------------------------

    def execute(self, lead_id: str, expected_fingerprint: str, *, correlation_id: str, allow_dispatch: bool = False,
                execution_id: str | None = None) -> ExecutionResult:
        """At most one business action. Never raises for a domain or concurrency outcome."""
        current: SalesExecutionPlan | None = None

        def result(outcome: ExecutionOutcome, **fields: object) -> ExecutionResult:
            return ExecutionResult.model_validate({
                "lead_id": lead_id, "planned_action": current.action if current else A.NO_ACTION, "outcome": outcome,
                "state_changed": False, "correlation_id": correlation_id, "execution_id": execution_id,
                "plan_fingerprint": expected_fingerprint, "resulting_fingerprint": current.fingerprint if current else None,
            } | fields)

        try:
            if execution_id is not None:
                replay = self._replay(execution_id, lead_id, expected_fingerprint)
                if replay is not None:
                    return result(replay[0], reason=replay[1], planned_action=replay[2])
            current = self.plan(lead_id)
            if current.fingerprint != expected_fingerprint:
                return result(X.STALE_PLAN, reason="REPLAN_REQUIRED")
            refusal = _refusal(current, allow_dispatch)
            if refusal is not None:
                return result(refusal[0], reason=refusal[1], reason_codes=current.sources)
        except OrchestrationNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001 - nothing ran yet; isolated and reported by type
            return result(X.ERROR, reason=type(exc).__name__)

        # From here a subsystem operation may have committed: the outcome is settled by a
        # fresh re-plan, so a later failure never hides (or invents) a state change.
        try:
            ran = self._run(current, correlation_id)
        except ConcurrencyError:  # another writer committed first: the plan no longer holds
            ran = _Ran(X.STALE_PLAN, reason="CONCURRENT_UPDATE")
        except Exception as exc:  # noqa: BLE001
            ran = _Ran(X.ERROR, reason=type(exc).__name__)
        resulting, changed = self._settle(lead_id, current.fingerprint)
        if ran.outcome is X.EXECUTED and execution_id is not None:
            self._record(execution_id, lead_id, current)
        return result(ran.outcome, state_changed=changed, reason=ran.reason, subsystem_outcome=ran.subsystem_outcome,
                      reason_codes=ran.codes, resulting_fingerprint=resulting)

    def execution_pass(self, *, correlation_id: str, limit: int, allow_dispatch: bool = False) -> ExecutionPassResult:
        """Bounded: each selected lead gets at most one action; no loop until quiet."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        actionable = [p for p in ordered(self._open_plans()) if p.executable
                      and (allow_dispatch or p.action is not A.SEND_APPROVED_MESSAGE)]
        results = tuple(self.execute(p.lead_id, p.fingerprint, correlation_id=correlation_id, allow_dispatch=allow_dispatch)
                        for p in actionable[:limit])
        return ExecutionPassResult(correlation_id=correlation_id, considered=len(actionable), attempted=len(results),
                                   results=results)

    # ---- Internals ------------------------------------------------------------------------------

    def _snapshot(self, uow: UnitOfWork, lead_id: str, now: datetime) -> LeadSnapshot:
        snapshot = gather(uow, lead_id, qualification=self._config.qualification, commercial=self._config.commercial,
                          follow_up=self._config.follow_up, dispatch=self._config.dispatch, now=now)
        if snapshot is None:
            raise OrchestrationNotFoundError(f"lead {lead_id} not found")
        return snapshot

    def _open_plans(self) -> list[SalesExecutionPlan]:
        with self._db.transaction() as uow:  # one consistent snapshot for every plan of the scan
            return self._plans(uow)

    def _plans(self, uow: UnitOfWork) -> list[SalesExecutionPlan]:
        now = self._clock.now()
        leads = uow.leads.list_by_stages(list(OPEN_STAGES), self._config.scan_limit)
        return [plan(self._snapshot(uow, lead.lead_id, now), self._settings) for lead in leads]

    def _run(self, current: SalesExecutionPlan, correlation_id: str) -> _Ran:
        action = current.action
        if action is A.RECONCILE_DISPATCH:
            return self._reconcile(current, correlation_id)
        if action is A.SEND_APPROVED_MESSAGE:
            return self._send(current, correlation_id)
        if action in (A.PREPARE_CAMPAIGN_TOUCH, A.COMPLETE_CAMPAIGN_SEQUENCE):
            return self._campaign(current, correlation_id)
        if action is A.PROCESS_FOLLOW_UP:
            return self._follow_up(current, correlation_id)
        raise AssertionError(f"{action} is not an automatic action")  # guarded by _refusal

    def _reconcile(self, current: SalesExecutionPlan, correlation_id: str) -> _Ran:
        """Stage 8 reconciliation of the oldest unresolved message: never resubmits."""
        assert self._dispatch is not None  # capability checked by the planner
        found = self._dispatch.reconcile(DispatchRequest(outbound_id=current.refs.outbound_ids[0], correlation_id=correlation_id))
        return _Ran(X.EXECUTED, found.outcome.value, codes=found.reason_codes)

    def _send(self, current: SalesExecutionPlan, correlation_id: str) -> _Ran:
        """Stage 8 dispatch of one operator-approved message; Stage 8 revalidates every gate."""
        assert self._dispatch is not None
        sent = self._dispatch.dispatch(DispatchRequest(outbound_id=current.refs.outbound_ids[0], correlation_id=correlation_id))
        if sent.outcome is DispatchOutcome.BLOCKED:
            return _Ran(X.BLOCKED, sent.outcome.value, reason="STAGE8_REFUSED", codes=sent.reason_codes)
        return _Ran(X.EXECUTED, sent.outcome.value, codes=sent.reason_codes)

    def _campaign(self, current: SalesExecutionPlan, correlation_id: str) -> _Ran:
        """One logical touch of one membership: open it (Stage 10 schedule for this member),
        and if it is due, claim exactly that job and execute it into a reviewable draft."""
        member_id, job_id = current.refs.member_id, current.refs.job_id
        assert member_id is not None
        scheduled = False
        if job_id is None:
            summary = self._campaign_scheduler.schedule_member(
                member_id, correlation_id=correlation_id, expected_version=current.expected_versions.get("campaign_member"))
            if summary.blocked_reason == "MEMBER_CHANGED":
                return _Ran(X.STALE_PLAN, reason="CONCURRENT_UPDATE", codes=(summary.blocked_reason,))
            if summary.blocked_reason is not None:
                return _Ran(X.BLOCKED, reason=summary.blocked_reason, codes=(summary.blocked_reason,))
            if summary.exhausted:
                return _Ran(X.EXECUTED, "SEQUENCE_COMPLETED")
            if not summary.scheduled:
                return _Ran(X.SKIPPED, reason="NOTHING_TO_SCHEDULE")
            job_id, scheduled = summary.scheduled[0], True
        claim = self._campaign_scheduler.claim(job_id, self._config.worker_id, correlation_id=correlation_id)
        if claim is None:
            return _Ran(X.EXECUTED, "SCHEDULED") if scheduled else _Ran(X.SKIPPED, reason="CLAIM_UNAVAILABLE")
        done = self._campaign_executor.execute(claim, correlation_id=correlation_id)
        if done.outcome is CampaignOutcome.STALE_CLAIM:
            return _Ran(X.STALE_PLAN, done.outcome.value, reason="CONCURRENT_UPDATE", codes=done.reason_codes)
        return _Ran(X.EXECUTED, done.outcome.value, codes=done.reason_codes)

    def _follow_up(self, current: SalesExecutionPlan, correlation_id: str) -> _Ran:
        """One logical follow-up of one conversation (Stage 9): schedule it if the policy
        permits, and if it is due, claim exactly that job and execute it into a draft."""
        conversation_id, follow_up_id = current.refs.conversation_id, current.refs.follow_up_id
        assert conversation_id is not None
        scheduled = False
        if follow_up_id is None:
            result = self._follow_up_scheduler.schedule(conversation_id, correlation_id=correlation_id,
                                                        expected_version=current.expected_versions.get("conversation"))
            if result.outcome is ScheduleOutcome.STALE_SNAPSHOT:
                return _Ran(X.STALE_PLAN, result.outcome.value, reason="CONCURRENT_UPDATE", codes=result.reason_codes)
            if result.outcome is ScheduleOutcome.BLOCKED:
                return _Ran(X.BLOCKED, result.outcome.value, reason="FOLLOW_UP_POLICY", codes=result.reason_codes)
            if result.outcome is ScheduleOutcome.ALREADY_EXECUTED or result.follow_up_id is None:
                return _Ran(X.SKIPPED, result.outcome.value, reason="ALREADY_EXECUTED")
            follow_up_id, scheduled = result.follow_up_id, result.outcome is ScheduleOutcome.SCHEDULED
        claim = self._follow_up_scheduler.claim(follow_up_id, self._config.worker_id, correlation_id=correlation_id)
        if claim is None:
            return _Ran(X.EXECUTED, "SCHEDULED") if scheduled else _Ran(X.SKIPPED, reason="CLAIM_UNAVAILABLE")
        done = self._follow_up_executor.execute(claim, correlation_id=correlation_id)
        if done.outcome is FollowUpOutcome.STALE_CLAIM:
            return _Ran(X.STALE_PLAN, done.outcome.value, reason="CONCURRENT_UPDATE", codes=done.reason_codes)
        return _Ran(X.EXECUTED, done.outcome.value, codes=done.reason_codes)

    # ---- Execution identity (existing idempotency table; no schema change) -----------------

    @staticmethod
    def _operation(lead_id: str, action: ExecutionAction, fingerprint: str) -> str:
        return f"orchestration.execute:{lead_id}:{action.value}:{fingerprint}"

    def _replay(self, execution_id: str, lead_id: str, fingerprint: str) -> tuple[ExecutionOutcome, str, ExecutionAction] | None:
        with self._db.transaction() as uow:
            record = uow.idempotency.get(EXECUTION_KEY_PREFIX + execution_id)
        if record is None:
            return None
        prefix = f"orchestration.execute:{lead_id}:"
        if record.operation.startswith(prefix) and record.operation.endswith(f":{fingerprint}"):
            action = ExecutionAction(record.operation[len(prefix):].rsplit(":", 1)[0])
            return X.REPLAYED, "EXECUTION_ALREADY_APPLIED", action
        return X.ERROR, "EXECUTION_ID_COLLISION", A.NO_ACTION

    def _settle(self, lead_id: str, before: str) -> tuple[str | None, bool]:
        """The fingerprint after an action and whether durable state changed. If even the
        re-plan fails, the change is unknown: reported as changed (the caller must replan)."""
        try:
            after = self.plan(lead_id).fingerprint
        except Exception:  # noqa: BLE001
            return None, True
        return after, after != before

    def _record(self, execution_id: str, lead_id: str, executed: SalesExecutionPlan) -> None:
        """Best effort: if the record is lost, a replay of the same id finds a stale plan."""
        try:
            with self._db.transaction() as uow:
                uow.idempotency.reserve(EXECUTION_KEY_PREFIX + execution_id,
                                        self._operation(lead_id, executed.action, executed.fingerprint), self._clock.now())
        except DuplicateIdempotencyKeyError:
            pass  # a concurrent call with the same id recorded it first; the action ran once (subsystem guards)
        except Exception:  # noqa: BLE001,S110 - never hides the executed outcome; replays are still stale
            pass


def _refusal(current: SalesExecutionPlan, allow_dispatch: bool) -> tuple[ExecutionOutcome, str] | None:
    """Why this plan is not run by automation, or None when it may run now."""
    blockers = current.blockers
    if current.action in AUTOMATIC_ACTIONS:
        if ExecutionBlocker.KILL_SWITCH_ACTIVE in blockers:
            return X.BLOCKED, ExecutionBlocker.KILL_SWITCH_ACTIVE.value  # explicit, never a generic refusal
        if CAPABILITY_BLOCKERS & set(blockers):
            return X.CAPABILITY_UNAVAILABLE, next(b.value for b in blockers if b in CAPABILITY_BLOCKERS)
        if blockers:
            return X.BLOCKED, blockers[0].value
        if current.action is A.SEND_APPROVED_MESSAGE and not allow_dispatch:
            return X.BLOCKED, DISPATCH_NOT_REQUESTED
        return None
    if current.owner is ExecutionOwner.OPERATOR:
        return X.REQUIRES_OPERATOR, current.action.value
    if current.owner is ExecutionOwner.CUSTOMER:
        return X.REQUIRES_CUSTOMER, current.action.value
    return X.NO_ACTION, current.action.value
