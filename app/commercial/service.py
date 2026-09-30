"""The commercial application service: the inbound commercial hook and the reads.

Operator commands live in ``app.operator`` (auth, idempotency, audit) and call
``app.commercial.proposals`` / ``terms`` / ``negotiation`` in the command transaction.

Inbound hook (after Stage 6 and the Stage 12 pipeline hook, as its own step):
- a lead that is now CLOSED (e.g. Stage 6 closed it on an unsubscribe) has its commercial
  work reconciled (``lifecycle.close_for_lead``), nothing else;
- for a genuine customer message on an open, unsuppressed lead with an active
  opportunity, the configured extractor (outside any transaction) proposes requests,
  objections, scope changes and signals; they are recorded once per message
  (``commercial:extraction:<message_id>``). An extraction failure never fails inbound
  processing and records nothing; persistence errors propagate (replay is safe).
Nothing here runs on its own.
"""

from enum import StrEnum

from app.commercial.config import CommercialConfig
from app.commercial.contracts import CommercialExtractionRequest, CommercialExtractor, KnownTerm
from app.commercial.draft import ProposalDraft, build_proposal_draft
from app.commercial.errors import CommercialNotFoundError
from app.commercial.guards import OpportunityContext
from app.commercial.lifecycle import close_for_lead
from app.commercial.negotiation import record_extraction
from app.commercial.pricing import KnowledgePriceCatalog, PriceCatalog
from app.commercial.state import ProposalReadiness, gather, readiness
from app.commercial.terms import effective_terms
from app.commercial.views import CommercialMetrics, CommercialQueue, CommercialView, metrics, opportunity_view, queue
from app.core.enums import LeadStage
from app.core.models.base import CoreModel
from app.core.models.types import EntityId
from app.inbound import InboundResult
from app.inbound.models import PrefilterOutcome
from app.persistence import Clock, Database, DuplicateIdempotencyKeyError, UnitOfWork
from app.pipeline.guards import is_suppressed


class CommercialHookStatus(StrEnum):
    APPLIED = "APPLIED"
    REPLAYED = "REPLAYED"
    SKIPPED = "SKIPPED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"


class CommercialHookOutcome(CoreModel):
    status: CommercialHookStatus
    reason: str | None = None
    opportunity_id: EntityId | None = None
    request_ids: tuple[EntityId, ...] = ()
    objection_ids: tuple[EntityId, ...] = ()
    signal_ids: tuple[EntityId, ...] = ()
    ignored: tuple[str, ...] = ()


class CommercialService:
    def __init__(self, db: Database, clock: Clock, config: CommercialConfig, *,
                 extractor: CommercialExtractor | None = None, catalog: PriceCatalog | None = None) -> None:
        self._db = db
        self._clock = clock
        self._config = config
        self._extractor = extractor
        self._catalog = catalog or KnowledgePriceCatalog(config.knowledge_locale)

    @property
    def config(self) -> CommercialConfig:
        return self._config

    @property
    def catalog(self) -> PriceCatalog:
        return self._catalog

    # ---- Inbound hook ------------------------------------------------------------------------

    def record_inbound(self, result: InboundResult, *, correlation_id: str) -> CommercialHookOutcome:
        if result.prefilter is not PrefilterOutcome.NONE or result.lead_id is None:
            return CommercialHookOutcome(status=CommercialHookStatus.SKIPPED, reason="NOT_A_CUSTOMER_MESSAGE")
        now = self._clock.now()
        with self._db.transaction() as uow:
            lead = uow.leads.get(result.lead_id)
            if lead is not None and lead.stage is LeadStage.CLOSED:
                close_for_lead(uow, lead, correlation_id=correlation_id, now=now)
            context = self._context(uow, result.lead_id)
            if isinstance(context, str):
                return CommercialHookOutcome(status=CommercialHookStatus.SKIPPED, reason=context)
            message = uow.messages.get(result.message_id)
            revisions = uow.proposal_revisions.list_for_opportunity(context.opportunity.opportunity_id)
            current = revisions[-1] if revisions else None
            known = effective_terms(self._config.profile, current, uow.commercial_terms.list_for_opportunity(
                context.opportunity.opportunity_id), now)
        opportunity_id = context.opportunity.opportunity_id
        if self._extractor is None or message is None:
            return CommercialHookOutcome(status=CommercialHookStatus.SKIPPED, reason="NO_EXTRACTOR", opportunity_id=opportunity_id)
        request = CommercialExtractionRequest(
            lead_id=context.lead.lead_id, opportunity_id=opportunity_id, message_id=message.message_id,
            message_text=message.body_text, currency=current.currency if current else None,
            known_terms=tuple(KnownTerm(term_type=t.term_type, term_key=t.term_key, value=t.value.display()) for t in known))
        try:
            extraction = self._extractor.extract(request)
        except Exception as exc:  # noqa: BLE001 - optional enrichment: never fails inbound processing
            return CommercialHookOutcome(status=CommercialHookStatus.EXTRACTION_FAILED, reason=type(exc).__name__,
                                         opportunity_id=opportunity_id)
        now = self._clock.now()
        with self._db.transaction() as uow:
            fresh = self._context(uow, result.lead_id)
            if isinstance(fresh, str) or fresh.opportunity.opportunity_id != opportunity_id:
                return CommercialHookOutcome(status=CommercialHookStatus.SKIPPED,
                                             reason=fresh if isinstance(fresh, str) else "OPPORTUNITY_CHANGED")
            try:
                uow.idempotency.reserve(f"commercial:extraction:{message.message_id}", "commercial.extraction", now)
            except DuplicateIdempotencyKeyError:
                return CommercialHookOutcome(status=CommercialHookStatus.REPLAYED, opportunity_id=opportunity_id)
            outcome = record_extraction(uow, self._config.profile, fresh, message, extraction,
                                        correlation_id=correlation_id, now=now)
        return CommercialHookOutcome(
            status=CommercialHookStatus.APPLIED, opportunity_id=opportunity_id, request_ids=tuple(outcome.requests),
            objection_ids=tuple(outcome.objections), signal_ids=tuple(outcome.signals), ignored=tuple(outcome.ignored))

    def _context(self, uow: UnitOfWork, lead_id: str) -> OpportunityContext | str:
        """The active commercial context of a lead, or why there is none."""
        lead = uow.leads.get(lead_id)
        if lead is None:
            return "LEAD_MISSING"
        if lead.stage is LeadStage.CLOSED:
            return "LEAD_CLOSED"
        if is_suppressed(uow, lead, self._clock.now()):
            return "CONTACT_SUPPRESSED"
        opportunity = uow.opportunities.get_active_for_lead(lead_id)
        if opportunity is None:
            return "NO_ACTIVE_OPPORTUNITY"
        return OpportunityContext(opportunity, lead)

    # ---- Reads ---------------------------------------------------------------------------------

    def view(self, opportunity_id: str) -> CommercialView:
        with self._db.transaction() as uow:
            opportunity = uow.opportunities.get(opportunity_id)
            if opportunity is None:
                raise CommercialNotFoundError(f"opportunity {opportunity_id} not found")
            return opportunity_view(uow, self._config.profile, opportunity, self._clock.now())

    def readiness(self, opportunity_id: str) -> ProposalReadiness:
        with self._db.transaction() as uow:
            opportunity = uow.opportunities.get(opportunity_id)
            if opportunity is None:
                raise CommercialNotFoundError(f"opportunity {opportunity_id} not found")
            return readiness(self._config.profile, gather(uow, opportunity, self._clock.now()))

    def draft(self, revision_id: str) -> ProposalDraft:
        with self._db.transaction() as uow:
            revision = uow.proposal_revisions.get(revision_id)
            opportunity = uow.opportunities.get(revision.opportunity_id) if revision else None
            if revision is None or opportunity is None:
                raise CommercialNotFoundError(f"proposal revision {revision_id} not found")
            now = self._clock.now()
            return build_proposal_draft(self._config.profile, gather(uow, opportunity, now), revision, now)

    def queue(self, which: CommercialQueue, *, limit: int | None = None) -> tuple[CommercialView, ...]:
        with self._db.transaction() as uow:
            return queue(uow, self._config.profile, which, self._clock.now(), limit or self._config.queue_limit)

    def metrics(self) -> CommercialMetrics:
        with self._db.transaction() as uow:
            return metrics(uow)
