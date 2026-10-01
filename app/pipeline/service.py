"""The pipeline application service: the inbound hook, reads, queues, metrics and the
advisory recommendation. Operator commands live in ``app.operator`` (authentication,
idempotency, audit) and call ``app.pipeline.qualification`` / ``lifecycle`` inside the
command transaction.

The inbound hook runs after Stage 6 has committed its outcome, as its own step:
1. a genuine customer message on an open, unsuppressed lead records the classified
   intent on the lead (``last_intent``; the lead version moves, so an operator decision
   taken on an older snapshot becomes stale), but only when it is the conversation's
   latest customer message (a replayed older message never rewinds it);
2. if an extractor is configured, it proposes qualification facts for that message
   (outside any transaction: a future model call), and the proposals are validated and
   applied once per message (``pipeline:extraction:<message_id>``).
Extraction failures never fail inbound processing: the hook reports EXTRACTION_FAILED and
nothing is recorded, so a replay of the same message may try again. Persistence errors
propagate; the Stage 6 outcome is durable and replaying the message is safe.

Nothing here runs on its own: no polling, no background advancement.
"""

from enum import StrEnum

from app.core.enums import LeadStage, QualificationStatus
from app.core.models import Lead
from app.core.models.base import CoreModel
from app.core.models.types import EntityId
from app.inbound import InboundResult
from app.inbound.models import PrefilterOutcome
from app.persistence import Clock, Database, DuplicateIdempotencyKeyError, UnitOfWork
from app.pipeline.config import PipelineConfig
from app.pipeline.contracts import (
    AdvisorInput,
    ExtractionRequest,
    KnownFact,
    QualificationExtractor,
    SalesAdvisor,
    SalesRecommendation,
)
from app.pipeline.errors import PipelineNotFoundError
from app.pipeline.guards import is_suppressed
from app.pipeline.lifecycle import cancel_opportunity_of_closed_lead
from app.pipeline.policy import RULES
from app.pipeline.qualification import QualificationGap, gaps, record_extraction, status_of
from app.pipeline.views import LeadPipelineView, PipelineMetrics, PipelineQueue, lead_view, metrics, queue


class HookStatus(StrEnum):
    APPLIED = "APPLIED"
    REPLAYED = "REPLAYED"
    SKIPPED = "SKIPPED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"


class InboundPipelineOutcome(CoreModel):
    status: HookStatus
    reason: str | None = None
    error_code: str | None = None  # EXTRACTION_FAILED: the extractor's stable code, if it gave one
    lead_id: EntityId | None = None
    facts_added: tuple[str, ...] = ()
    facts_corroborated: tuple[str, ...] = ()
    conflicts_created: tuple[EntityId, ...] = ()
    ignored: tuple[str, ...] = ()


class Recommendation(CoreModel):
    """An advisor's recommendation, annotated by the policy. Never applied automatically."""

    recommendation: SalesRecommendation
    requires_operator: bool
    allowed_now: bool


class PipelineService:
    def __init__(self, db: Database, clock: Clock, config: PipelineConfig, *,
                 extractor: QualificationExtractor | None = None, advisor: SalesAdvisor | None = None) -> None:
        self._db = db
        self._clock = clock
        self._config = config
        self._extractor = extractor
        self._advisor = advisor

    # ---- Inbound hook ----------------------------------------------------------------------

    def record_inbound(self, result: InboundResult, *, correlation_id: str) -> InboundPipelineOutcome:
        if result.prefilter is not PrefilterOutcome.NONE or result.lead_id is None:
            return InboundPipelineOutcome(status=HookStatus.SKIPPED, reason="NOT_A_CUSTOMER_MESSAGE", lead_id=result.lead_id)
        now = self._clock.now()
        with self._db.transaction() as uow:
            lead = uow.leads.get(result.lead_id)
            if lead is not None:  # e.g. Stage 6 closed it on an unsubscribe: no active opportunity may remain
                cancel_opportunity_of_closed_lead(uow, lead, correlation_id=correlation_id, now=now)
            skip = self._skip_reason(uow, lead)
            if skip is not None or lead is None:
                return InboundPipelineOutcome(status=HookStatus.SKIPPED, reason=skip, lead_id=result.lead_id)
            conversation = uow.conversations.get_by_thread(result.thread_id)
            # Only the conversation's latest customer message speaks for the lead now: a replay
            # of an older message (at-least-once delivery) never rewinds the recorded intent.
            latest = conversation is None or conversation.last_inbound_message_id == result.message_id
            intent = result.classification.primary_intent if result.classification else None
            if intent is not None and latest and lead.last_intent is not intent:
                observed = Lead.model_validate(lead.model_dump() | {"last_intent": intent, "updated_at": max(now, lead.updated_at),
                                                                   "version": lead.version + 1})
                uow.leads.update(observed, lead.version)
                lead = observed
            message = uow.messages.get(result.message_id)
            qualification = uow.qualifications.get(lead.lead_id)
            # Already extracted for this message (a redelivery): never ask the extractor again.
            done = uow.idempotency.exists(f"pipeline:extraction:{result.message_id}")
        if done:
            return InboundPipelineOutcome(status=HookStatus.REPLAYED, lead_id=lead.lead_id)
        if self._extractor is None or message is None:
            return InboundPipelineOutcome(status=HookStatus.SKIPPED, reason="NO_EXTRACTOR", lead_id=lead.lead_id)
        request = ExtractionRequest(
            lead_id=lead.lead_id, message_id=message.message_id,
            conversation_id=conversation.conversation_id if conversation else None, message_text=message.body_text,
            fields=tuple(sorted(self._config.profile.keys)),
            known_facts=tuple(KnownFact(field=f.field, value=f.value) for f in qualification.facts) if qualification else (),
        )
        try:
            extraction = self._extractor.extract(request)
        except Exception as exc:  # noqa: BLE001 - optional enrichment: never fails inbound processing
            return InboundPipelineOutcome(status=HookStatus.EXTRACTION_FAILED, reason=type(exc).__name__,
                                          error_code=_error_code(exc), lead_id=lead.lead_id)
        now = self._clock.now()
        with self._db.transaction() as uow:
            lead_now = uow.leads.get(lead.lead_id)
            skip = self._skip_reason(uow, lead_now)
            if skip is not None or lead_now is None:
                return InboundPipelineOutcome(status=HookStatus.SKIPPED, reason=skip, lead_id=lead.lead_id)
            try:
                uow.idempotency.reserve(f"pipeline:extraction:{message.message_id}", "pipeline.extraction", now)
            except DuplicateIdempotencyKeyError:
                return InboundPipelineOutcome(status=HookStatus.REPLAYED, lead_id=lead.lead_id)
            outcome = record_extraction(
                uow, self._config.profile, lead_now, extraction, message_id=message.message_id,
                conversation_id=request.conversation_id, min_confidence=self._config.min_extraction_confidence,
                correlation_id=correlation_id, now=now,
            )
        return InboundPipelineOutcome(
            status=HookStatus.APPLIED, lead_id=lead.lead_id, facts_added=tuple(outcome.added),
            facts_corroborated=tuple(outcome.corroborated), conflicts_created=tuple(outcome.conflicts),
            ignored=tuple(f"{field}:{code}" for field, code in outcome.ignored),
        )

    def _skip_reason(self, uow: UnitOfWork, lead: Lead | None) -> str | None:
        if lead is None:
            return "LEAD_MISSING"
        if lead.stage is LeadStage.CLOSED:
            return "LEAD_CLOSED"
        if is_suppressed(uow, lead, self._clock.now()):
            return "CONTACT_SUPPRESSED"
        return None

    # ---- Reads ---------------------------------------------------------------------------------

    def view(self, lead_id: str) -> LeadPipelineView:
        with self._db.transaction() as uow:
            found = lead_view(uow, lead_id, self._config.profile, self._clock.now())
        if found is None:
            raise PipelineNotFoundError(f"lead {lead_id} not found")
        return found

    def queue(self, which: PipelineQueue, *, limit: int | None = None) -> tuple[LeadPipelineView, ...]:
        with self._db.transaction() as uow:
            return queue(uow, which, self._config.profile, self._clock.now(), limit or self._config.queue_limit)

    def metrics(self) -> PipelineMetrics:
        with self._db.transaction() as uow:
            return metrics(uow, self._config.profile, self._clock.now(), self._config.queue_limit)

    def gaps(self, lead_id: str) -> tuple[QualificationGap, ...]:
        with self._db.transaction() as uow:
            if uow.leads.get(lead_id) is None:
                raise PipelineNotFoundError(f"lead {lead_id} not found")
            return gaps(self._config.profile, uow.qualifications.get(lead_id))

    def recommend(self, lead_id: str) -> Recommendation | None:
        """Ask the advisor (if any). Read-only: the recommendation is returned, never applied;
        ``requires_operator`` says whether its proposed stage is an operator decision and
        ``allowed_now`` whether the policy would allow that move from the current stage."""
        if self._advisor is None:
            return None
        with self._db.transaction() as uow:
            lead = uow.leads.get(lead_id)
            if lead is None:
                raise PipelineNotFoundError(f"lead {lead_id} not found")
            qualification = uow.qualifications.get(lead_id)
        missing = tuple(g.field for g in gaps(self._config.profile, qualification) if g.reason == "MISSING_REQUIRED")
        data = AdvisorInput(
            lead_id=lead_id, stage=lead.stage, qualification_status=status_of(qualification),
            known_facts=tuple(KnownFact(field=f.field, value=f.value) for f in qualification.facts) if qualification else (),
            missing_required=missing, open_conflicts=len(qualification.open_conflicts) if qualification else 0,
            last_intent=lead.last_intent.value if lead.last_intent else None,
        )
        recommendation = self._advisor.recommend(data)
        target = recommendation.proposed_stage
        rules = [rule for rule in RULES.values() if target is not None and target in rule.targets]
        return Recommendation(
            recommendation=recommendation,
            requires_operator=bool(rules) and all(rule.operator_only for rule in rules),
            allowed_now=any(lead.stage in rule.sources for rule in rules),
        )

    @property
    def config(self) -> PipelineConfig:
        return self._config


def qualification_status(uow: UnitOfWork, lead_id: str) -> QualificationStatus:
    return status_of(uow.qualifications.get(lead_id))


def _error_code(exc: BaseException) -> str | None:
    """A stable failure code the extractor attached (e.g. an LLM provider code), if any."""
    code = getattr(exc, "code", None)
    value = getattr(code, "value", code)
    return value if isinstance(value, str) and value else None
