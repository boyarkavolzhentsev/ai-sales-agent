"""Runtime passes over the existing subsystems. Orchestration only: every decision stays in
Stage 8 (dispatch, reconciliation), Stage 9 (follow-ups) and Stage 10 (campaigns), each
with its own transactions.

Failure isolation: one item's exception (adapter, persistence or unexpected) is recorded
as an ItemError (subject + exception type) and the pass continues with the next item; the
failed item rolled back in its own transaction (a claimed job simply stays claimed until
its lease expires). An exception before any item runs marks the whole phase ERROR. Domain
outcomes (blocked, deferred, stale, ...) are results, not errors. A crash (BaseException)
is never caught.
"""

from collections.abc import Callable, Iterable

from app.campaign import ExecutionOutcome as CampaignOutcome
from app.conversation import ExecutionOutcome as FollowUpOutcome
from app.conversation import ScheduleOutcome
from app.core.enums import CampaignStatus, ConversationStatus, OutboundStatus
from app.dispatch import DispatchOutcome, DispatchRequest
from app.operator.review import REVIEWABLE_KINDS
from app.persistence import Database
from app.runtime.container import Services
from app.runtime.results import DispatchPhaseResult, ItemError, PhaseStatus, ReconciliationResult, WorkResult


def _each[T, R](items: Iterable[T], subject: Callable[[T], str], run: Callable[[T], R], errors: list[ItemError]) -> list[R]:
    results: list[R] = []
    for item in items:
        try:
            results.append(run(item))
        except Exception as exc:  # noqa: BLE001 - isolated and reported, never silent
            errors.append(ItemError(subject=subject(item), error_type=type(exc).__name__))
    return results


def _status(errors: list[ItemError]) -> PhaseStatus:
    return PhaseStatus.ERROR if errors else PhaseStatus.OK


def reconcile_pass(services: Services, *, correlation_id: str, limit: int, available: bool) -> ReconciliationResult:
    """Stage 8 reconciliation of unresolved attempts. NOT_FOUND and inconclusive lookups keep
    an attempt unresolved; nothing is ever resubmitted here."""
    dispatch = services.dispatch
    if not available or dispatch is None:
        return ReconciliationResult(status=PhaseStatus.SKIPPED, reason="RECONCILER_NOT_CONFIGURED")
    try:
        pending = dispatch.list_unresolved()[:limit]
    except Exception as exc:  # noqa: BLE001
        return ReconciliationResult(status=PhaseStatus.ERROR, errors=(ItemError(subject="phase", error_type=type(exc).__name__),))
    errors: list[ItemError] = []
    results = _each(pending, lambda a: a.outbound_id,
                    lambda a: dispatch.reconcile(DispatchRequest(outbound_id=a.outbound_id, correlation_id=correlation_id)), errors)
    outcomes = [r.outcome for r in results]
    return ReconciliationResult(
        status=_status(errors), processed=len(results), accepted=outcomes.count(DispatchOutcome.ACCEPTED),
        not_accepted=outcomes.count(DispatchOutcome.NOT_ACCEPTED), unresolved=outcomes.count(DispatchOutcome.UNKNOWN),
        errors=tuple(errors),
    )


def campaign_pass(services: Services, db: Database, *, worker_id: str, correlation_id: str, limit: int) -> WorkResult:
    """Stage 10: open the next logical touches of ACTIVE campaigns, claim due touches, and
    execute them into reviewable drafts. Never dispatches."""
    try:
        with db.transaction() as uow:
            campaign_ids = [c.campaign_id for c in uow.campaigns.list_by_status(CampaignStatus.ACTIVE)]
    except Exception as exc:  # noqa: BLE001
        return WorkResult(status=PhaseStatus.ERROR, errors=(ItemError(subject="phase", error_type=type(exc).__name__),))
    errors: list[ItemError] = []
    summaries = _each(campaign_ids, lambda c: c,
                      lambda c: services.campaign_scheduler.schedule(c, correlation_id=correlation_id), errors)
    claims = _claims(lambda: services.campaign_scheduler.claim_due(worker_id, correlation_id=correlation_id, limit=limit), errors)
    results = _each(claims, lambda c: c.job_id, lambda c: services.campaign_executor.execute(c, correlation_id=correlation_id), errors)
    outcomes = [r.outcome for r in results]
    return WorkResult(
        status=_status(errors), scheduled=sum(len(s.scheduled) for s in summaries), claimed=len(claims),
        drafted=outcomes.count(CampaignOutcome.DRAFT_CREATED), deferred=outcomes.count(CampaignOutcome.DEFERRED),
        blocked=outcomes.count(CampaignOutcome.BLOCKED), superseded=outcomes.count(CampaignOutcome.SUPERSEDED),
        stale=outcomes.count(CampaignOutcome.STALE_CLAIM), replayed=outcomes.count(CampaignOutcome.REPLAYED),
        cancelled=outcomes.count(CampaignOutcome.CANCELLED), errors=tuple(errors),
    )


def follow_up_pass(services: Services, db: Database, *, worker_id: str, correlation_id: str, limit: int) -> WorkResult:
    """Stage 9: schedule follow-ups for conversations waiting for a reply (the policy decides
    eligibility), claim due jobs, and execute them into reviewable drafts."""
    try:
        with db.transaction() as uow:
            waiting = [c.conversation_id for c in uow.conversations.list_by_status(ConversationStatus.WAITING_FOR_REPLY, limit)]
    except Exception as exc:  # noqa: BLE001
        return WorkResult(status=PhaseStatus.ERROR, errors=(ItemError(subject="phase", error_type=type(exc).__name__),))
    errors: list[ItemError] = []
    schedules = _each(waiting, lambda c: c, lambda c: services.follow_up_scheduler.schedule(c, correlation_id=correlation_id), errors)
    claims = _claims(lambda: services.follow_up_scheduler.claim_due(worker_id, correlation_id=correlation_id, limit=limit), errors)
    results = _each(claims, lambda c: c.follow_up_id,
                    lambda c: services.follow_up_executor.execute(c, correlation_id=correlation_id), errors)
    outcomes = [r.outcome for r in results]
    return WorkResult(
        status=_status(errors), scheduled=sum(1 for s in schedules if s.outcome is ScheduleOutcome.SCHEDULED), claimed=len(claims),
        drafted=outcomes.count(FollowUpOutcome.DRAFT_CREATED), deferred=outcomes.count(FollowUpOutcome.DEFERRED),
        blocked=outcomes.count(FollowUpOutcome.BLOCKED), superseded=outcomes.count(FollowUpOutcome.SUPERSEDED),
        stale=outcomes.count(FollowUpOutcome.STALE_CLAIM), replayed=outcomes.count(FollowUpOutcome.REPLAYED), errors=tuple(errors),
    )


def dispatch_pass(services: Services, db: Database, *, correlation_id: str, limit: int, available: bool) -> DispatchPhaseResult:
    """Stage 8 dispatch of messages an operator already approved (OPERATOR_APPROVED). Nothing
    is approved here, and every Stage 8 gate is re-evaluated per message."""
    dispatch = services.dispatch
    if not available or dispatch is None:
        return DispatchPhaseResult(status=PhaseStatus.SKIPPED, reason="EMAIL_TRANSPORT_NOT_CONFIGURED")
    try:
        with db.transaction() as uow:
            approved = [m.outbound_id for m in uow.outbound.list_by_status(OutboundStatus.OPERATOR_APPROVED)
                        if m.kind in REVIEWABLE_KINDS][:limit]
    except Exception as exc:  # noqa: BLE001
        return DispatchPhaseResult(status=PhaseStatus.ERROR, errors=(ItemError(subject="phase", error_type=type(exc).__name__),))
    errors: list[ItemError] = []
    results = _each(approved, lambda o: o,
                    lambda o: dispatch.dispatch(DispatchRequest(outbound_id=o, correlation_id=correlation_id)), errors)
    outcomes = [r.outcome for r in results]
    return DispatchPhaseResult(
        status=_status(errors), processed=len(results), accepted=outcomes.count(DispatchOutcome.ACCEPTED),
        not_accepted=outcomes.count(DispatchOutcome.NOT_ACCEPTED), unknown=outcomes.count(DispatchOutcome.UNKNOWN),
        blocked=outcomes.count(DispatchOutcome.BLOCKED), errors=tuple(errors),
    )


def _claims[C](claim: Callable[[], tuple[C, ...]], errors: list[ItemError]) -> tuple[C, ...]:
    try:
        return claim()
    except Exception as exc:  # noqa: BLE001
        errors.append(ItemError(subject="claim", error_type=type(exc).__name__))
        return ()
