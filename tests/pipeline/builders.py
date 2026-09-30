"""Builders for Stage 12 pipeline tests: real Stage 6 inbound processing, the pipeline
service with a deterministic fake extractor, and operator commands through Stage 7."""

from datetime import date, datetime
from decimal import Decimal

from app.core.enums import (
    ConfidenceBand,
    ConflictResolution,
    DisqualificationReason,
    LeadStage,
    LostReason,
)
from app.core.models import Lead, LeadQualification, Opportunity
from app.inbound import InboundResult
from app.operator import (
    ApproveQualification,
    CommandResult,
    CreateOpportunity,
    DisqualifyLead,
    MarkLeadLost,
    MarkLeadWon,
    OperatorService,
    RecordQualificationFact,
    ReopenLead,
    ResolveQualificationConflict,
    StartNegotiation,
)
from app.persistence import Database, FrozenClock
from app.pipeline import FactProposal, PipelineConfig, PipelineService, QualificationExtraction
from app.pipeline.fake import FakeQualificationExtractor
from tests.inbound.builders import NOW, envelope, happy_transport, process
from tests.operator.builders import AS_ALICE, operator

REQUIRED = {"need": "Automate invoice matching", "product_interest": "Basic plan", "timeframe": "Q3 2026",
            "decision_role": "Head of finance, decides"}


def extraction(confidence: ConfidenceBand = ConfidenceBand.HIGH, **facts: str) -> QualificationExtraction:
    return QualificationExtraction(proposals=tuple(FactProposal(field=k, value=v, confidence=confidence) for k, v in facts.items()))


def pipeline(db: Database, extractor: FakeQualificationExtractor | None = None, *, at: datetime = NOW,
             config: PipelineConfig | None = None) -> PipelineService:
    return PipelineService(db, FrozenClock(at), config or PipelineConfig(), extractor=extractor)


def inbound(db: Database, provider_message_id: str = "p-1", *, facts: dict[str, str] | None = None,
            extractor: FakeQualificationExtractor | None = None, **envelope_overrides: object) -> InboundResult:
    """A genuine customer message through Stage 6, then the pipeline hook (as the runtime does)."""
    result = process(db, happy_transport(), envelope(provider_message_id, **envelope_overrides))
    fake = extractor or FakeQualificationExtractor(default=extraction(**(facts or {})))
    pipeline(db, fake).record_inbound(result, correlation_id=f"corr-{provider_message_id}")
    return result


def lead(db: Database, lead_id: str) -> Lead:
    with db.transaction() as uow:
        found = uow.leads.get(lead_id)
    assert found is not None
    return found


def qualification(db: Database, lead_id: str) -> LeadQualification | None:
    with db.transaction() as uow:
        return uow.qualifications.get(lead_id)


def opportunity(db: Database, opportunity_id: str) -> Opportunity:
    with db.transaction() as uow:
        found = uow.opportunities.get(opportunity_id)
    assert found is not None
    return found


def ops(db: Database) -> OperatorService:
    return operator(db, FrozenClock(NOW))


# ---- Operator commands built from the current state (what an operator would see) -------------


def approve_qualification(db: Database, lead_id: str, command_id: str = "cmd-approve-q") -> CommandResult:
    q = qualification(db, lead_id)
    assert q is not None
    return ops(db).approve_qualification(AS_ALICE, ApproveQualification(
        command_id=command_id, correlation_id="c", lead_id=lead_id, expected_lead_version=lead(db, lead_id).version,
        expected_qualification_version=q.version))


def record_fact(db: Database, lead_id: str, field: str, value: str, command_id: str = "cmd-fact") -> CommandResult:
    q = qualification(db, lead_id)
    return ops(db).record_qualification_fact(AS_ALICE, RecordQualificationFact(
        command_id=command_id, correlation_id="c", lead_id=lead_id, field=field, value=value,
        expected_qualification_version=q.version if q else None))


def resolve(db: Database, lead_id: str, conflict_id: str, resolution: ConflictResolution,
            command_id: str = "cmd-resolve") -> CommandResult:
    q = qualification(db, lead_id)
    assert q is not None
    return ops(db).resolve_qualification_conflict(AS_ALICE, ResolveQualificationConflict(
        command_id=command_id, correlation_id="c", lead_id=lead_id, conflict_id=conflict_id, resolution=resolution,
        expected_qualification_version=q.version))


def disqualify(db: Database, lead_id: str, reason: DisqualificationReason = DisqualificationReason.NO_PRODUCT_FIT,
               command_id: str = "cmd-disqualify") -> CommandResult:
    return ops(db).disqualify_lead(AS_ALICE, DisqualifyLead(
        command_id=command_id, correlation_id="c", lead_id=lead_id, expected_lead_version=lead(db, lead_id).version,
        reason=reason))


def create_opportunity(db: Database, lead_id: str, command_id: str = "cmd-opportunity", **fields: object) -> CommandResult:
    return ops(db).create_opportunity(AS_ALICE, CreateOpportunity.model_validate(
        {"command_id": command_id, "correlation_id": "c", "lead_id": lead_id,
         "expected_lead_version": lead(db, lead_id).version} | fields))


def active_opportunity(db: Database, lead_id: str) -> Opportunity:
    with db.transaction() as uow:
        found = uow.opportunities.get_active_for_lead(lead_id)
    assert found is not None
    return found


def start_negotiation(db: Database, lead_id: str, command_id: str = "cmd-negotiate") -> CommandResult:
    opp = active_opportunity(db, lead_id)
    return ops(db).start_negotiation(AS_ALICE, StartNegotiation(
        command_id=command_id, correlation_id="c", lead_id=lead_id, expected_lead_version=lead(db, lead_id).version,
        opportunity_id=opp.opportunity_id, expected_opportunity_version=opp.version))


def mark_won(db: Database, lead_id: str, command_id: str = "cmd-won") -> CommandResult:
    opp = active_opportunity(db, lead_id)
    return ops(db).mark_lead_won(AS_ALICE, MarkLeadWon(
        command_id=command_id, correlation_id="c", lead_id=lead_id, expected_lead_version=lead(db, lead_id).version,
        opportunity_id=opp.opportunity_id, expected_opportunity_version=opp.version))


def mark_lost(db: Database, lead_id: str, reason: LostReason = LostReason.CHOSE_COMPETITOR,
              command_id: str = "cmd-lost") -> CommandResult:
    return ops(db).mark_lead_lost(AS_ALICE, MarkLeadLost(
        command_id=command_id, correlation_id="c", lead_id=lead_id, expected_lead_version=lead(db, lead_id).version,
        reason=reason))


def reopen(db: Database, lead_id: str, target: LeadStage = LeadStage.ENGAGED, command_id: str = "cmd-reopen") -> CommandResult:
    return ops(db).reopen_lead(AS_ALICE, ReopenLead(
        command_id=command_id, correlation_id="c", lead_id=lead_id, expected_lead_version=lead(db, lead_id).version,
        target_stage=target, note="Customer came back with a new budget."))


# ---- Ready-made pipeline positions --------------------------------------------------------------


def qualifying_lead(db: Database, facts: dict[str, str] | None = None) -> str:
    result = inbound(db, facts=REQUIRED if facts is None else facts)
    assert result.lead_id is not None
    return result.lead_id


def qualified_lead(db: Database) -> str:
    lead_id = qualifying_lead(db)
    approve_qualification(db, lead_id)
    return lead_id


def opportunity_lead(db: Database, **fields: object) -> str:
    lead_id = qualified_lead(db)
    create_opportunity(db, lead_id, **fields)
    return lead_id


def known(amount: str, currency: str = "EUR") -> dict[str, object]:
    return {"amount": Decimal(amount), "currency": currency, "expected_decision_date": date(2026, 9, 1)}
