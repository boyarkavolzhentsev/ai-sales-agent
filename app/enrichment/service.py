"""Durable AI enrichment of stored inbound messages (Stage 12 qualification extraction and
Stage 13 commercial extraction).

Every customer message with a lead gets one job per configured extraction, identified by
(task, message). The job is the only way the extraction runs:

1. inbound processing creates the jobs (idempotent) and makes one inline attempt each;
2. an attempt first CLAIMS the job (IMMEDIATE transaction, CAS on the version, a lease),
   so concurrent processes never run one job twice at the same time: at most one model
   call is in flight per job;
3. the claim holder runs the existing hook (``PipelineService.record_inbound`` /
   ``CommercialService.record_inbound``), which calls the extractor and applies through the
   existing Stage 12/13 rules and idempotency, and then settles the job (only with its own
   claim token): COMPLETED (applied, already applied, or skipped by the hook),
   RETRY_WAIT (a transient provider failure: bounded backoff), or FAILED_FINAL (anything
   else, or attempts exhausted). A COMPLETED or FAILED_FINAL job never runs again;
4. ``recover`` (the one-shot ``ai-recovery-tick``) claims due jobs and runs them from the
   stored Stage 6 result: no mailbox redelivery, cursor rewind or envelope is needed.

Nothing here drafts, sends, approves or decides: the hooks only record proposals the
existing services validate. Jobs hold ids, codes and times only.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.commercial import CommercialService
from app.core.models.base import CoreModel
from app.inbound import InboundResult
from app.inbound.models import PrefilterOutcome, stable_id
from app.persistence import AIEnrichmentJob, Clock, ConcurrencyError, Database, EnrichmentJobStatus, EnrichmentKind
from app.pipeline import PipelineService

S = EnrichmentJobStatus
K = EnrichmentKind
# Transient provider conditions (stable LLM error codes): worth a later attempt.
RETRYABLE = frozenset({"RATE_LIMITED", "TEMPORARY_PROVIDER_ERROR", "TIMEOUT", "NETWORK_ERROR", "HOOK_ERROR"})


@dataclass(frozen=True)
class EnrichmentConfig:
    max_attempts: int = 5
    # Wait after the 1st, 2nd, ... failed attempt (the last one is reused); ~8.6 h in total.
    backoff: tuple[timedelta, ...] = field(default_factory=lambda: (
        timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2), timedelta(hours=6)))
    lease: timedelta = timedelta(minutes=15)  # longer than any provider call (timeout <= 300 s)


class AIRecoveryResult(CoreModel):
    """One bounded recovery pass: counts and codes only."""

    status: str  # OK, SKIPPED
    reason: str | None = None
    due: int = 0
    completed: int = 0
    retry_scheduled: int = 0
    failed_final: int = 0
    not_claimed: int = 0  # another worker holds it, or it changed meanwhile


class EnrichmentService:
    def __init__(self, db: Database, clock: Clock, *, pipeline: PipelineService, commercial: CommercialService,
                 load_result: Callable[[str], InboundResult | None], kinds: frozenset[EnrichmentKind], worker_id: str,
                 config: EnrichmentConfig | None = None) -> None:
        self._db = db
        self._clock = clock
        self._pipeline = pipeline
        self._commercial = commercial
        self._load = load_result
        self._kinds = kinds
        self._worker = worker_id
        self._config = config or EnrichmentConfig()

    # ---- After Stage 6 ----------------------------------------------------------------------

    def after_inbound(self, result: InboundResult, *, correlation_id: str) -> None:
        """The Stage 12 and Stage 13 hooks for one processed message, in that order. With an
        extractor configured they run as durable jobs; otherwise as before (no model call)."""
        for kind in (K.QUALIFICATION_EXTRACTION, K.COMMERCIAL_EXTRACTION):
            if kind not in self._kinds or result.prefilter is not PrefilterOutcome.NONE or result.lead_id is None:
                self._hook(kind)(result, correlation_id=correlation_id)
                continue
            job_id = self.ensure(kind, result.message_id, result.lead_id)
            self._attempt(job_id, result, correlation_id)

    def ensure(self, kind: EnrichmentKind, message_id: str, lead_id: str) -> str:
        job_id = job_id_for(kind, message_id)
        now = self._clock.now()
        with self._db.transaction() as uow:
            if uow.enrichment_jobs.get(job_id) is None:
                uow.enrichment_jobs.add(AIEnrichmentJob(job_id=job_id, kind=kind, message_id=message_id, lead_id=lead_id,
                                                        due_at=now, created_at=now, updated_at=now))
        return job_id

    # ---- Recovery ---------------------------------------------------------------------------------

    def recover(self, *, limit: int) -> AIRecoveryResult:
        """Run at most ``limit`` due jobs once each, then return (never waits or loops)."""
        with self._db.transaction() as uow:
            due = uow.enrichment_jobs.list_due(self._clock.now(), limit)
        counts = {"completed": 0, "retry_scheduled": 0, "failed_final": 0, "not_claimed": 0}
        for job in due:
            counts[self._attempt(job.job_id, None, None)] += 1
        return AIRecoveryResult(status="OK", due=len(due), **counts)

    def counts(self) -> dict[str, int]:
        with self._db.transaction() as uow:
            return uow.enrichment_jobs.counts()

    # ---- One attempt ------------------------------------------------------------------------------

    def _attempt(self, job_id: str, result: InboundResult | None, correlation_id: str | None) -> str:
        claimed = self._claim(job_id)
        if claimed is None:
            return "not_claimed"
        token, job = claimed
        if result is None:
            result = self._load(job.message_id)  # the stored Stage 6 outcome: no redelivery needed
            if result is None:
                return self._settle(job, token, S.FAILED_FINAL, code="NO_INBOUND_RESULT")
        correlation = correlation_id or stable_id("air", job.job_id, str(job.attempts))
        try:
            outcome = self._hook(job.kind)(result, correlation_id=correlation)
        except Exception as exc:  # noqa: BLE001 - e.g. a database conflict while applying: bounded retry
            del exc
            return self._failed(job, token, "HOOK_ERROR")
        status = outcome.status.value
        if status == "EXTRACTION_FAILED":
            return self._failed(job, token, outcome.error_code or "EXTRACTOR_ERROR")
        detail = status if status != "SKIPPED" else f"SKIPPED:{outcome.reason or 'UNKNOWN'}"
        return self._settle(job, token, S.COMPLETED, outcome=detail)

    def _failed(self, job: AIEnrichmentJob, token: str, code: str) -> str:
        if code not in RETRYABLE or job.attempts >= self._config.max_attempts:
            return self._settle(job, token, S.FAILED_FINAL, code=code)  # no retry storm, nothing invented
        wait = self._config.backoff[min(job.attempts, len(self._config.backoff)) - 1]
        return self._settle(job, token, S.RETRY_WAIT, code=code, due=self._clock.now() + wait)

    def _claim(self, job_id: str) -> tuple[str, AIEnrichmentJob] | None:
        now = self._clock.now()
        try:
            with self._db.transaction() as uow:
                job = uow.enrichment_jobs.get(job_id)
                if job is None or job.status in (S.COMPLETED, S.FAILED_FINAL) or job.due_at > now:
                    return None  # done, waiting for its retry time, or leased by another worker
                if job.attempts >= self._config.max_attempts:  # an abandoned last attempt: never more
                    uow.enrichment_jobs.update(_next(job, now, status=S.FAILED_FINAL, last_error_code="LEASE_EXPIRED",
                                                     finished_at=now), job.version)
                    return None
                token = stable_id("ajc", job.job_id, str(job.attempts + 1))
                claimed = _next(job, now, status=S.CLAIMED, claim_token=token, claimed_by=self._worker,
                                attempts=job.attempts + 1, due_at=now + self._config.lease)
                uow.enrichment_jobs.update(claimed, job.version)
        except ConcurrencyError:
            return None
        return token, claimed

    def _settle(self, job: AIEnrichmentJob, token: str, status: EnrichmentJobStatus, *, outcome: str | None = None,
                code: str | None = None, due: datetime | None = None) -> str:
        now = self._clock.now()
        final = status in (S.COMPLETED, S.FAILED_FINAL)
        with self._db.transaction() as uow:
            current = uow.enrichment_jobs.get(job.job_id)
            if current is None or current.status is not S.CLAIMED or current.claim_token != token:
                return "not_claimed"  # the lease was lost to another worker: it owns the job now
            uow.enrichment_jobs.update(_next(current, now, status=status, claim_token=None, claimed_by=None,
                                             outcome=outcome or current.outcome, last_error_code=code,
                                             due_at=due or now, finished_at=now if final else None), current.version)
        return {S.COMPLETED: "completed", S.RETRY_WAIT: "retry_scheduled", S.FAILED_FINAL: "failed_final"}[status]

    def _hook(self, kind: EnrichmentKind) -> Callable[..., object]:
        return self._pipeline.record_inbound if kind is K.QUALIFICATION_EXTRACTION else self._commercial.record_inbound


def job_id_for(kind: EnrichmentKind, message_id: str) -> str:
    return stable_id("aj", kind.value, message_id)


def _next(job: AIEnrichmentJob, now: datetime, **changes: object) -> AIEnrichmentJob:
    return AIEnrichmentJob.model_validate(job.model_dump() | changes | {
        "updated_at": max(now, job.updated_at), "version": job.version + 1})
