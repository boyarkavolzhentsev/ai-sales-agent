"""Shared commercial checks: the opportunity must exist, be active, belong to an open lead,
and (for anything that progresses or communicates) the contact must not be suppressed."""

from dataclasses import dataclass
from datetime import datetime

from app.commercial.errors import CommercialCode, CommercialError, CommercialNotFoundError
from app.core.enums import LeadStage
from app.core.models import ACTIVE_OPPORTUNITY_STATUSES, Lead, Opportunity
from app.persistence import UnitOfWork
from app.pipeline.guards import is_suppressed


@dataclass(frozen=True)
class OpportunityContext:
    opportunity: Opportunity
    lead: Lead


def load_opportunity(uow: UnitOfWork, opportunity_id: str) -> OpportunityContext:
    opportunity = uow.opportunities.get(opportunity_id)
    if opportunity is None:
        raise CommercialNotFoundError(f"opportunity {opportunity_id} not found")
    lead = uow.leads.get(opportunity.lead_id)
    if lead is None:
        raise CommercialNotFoundError(f"lead {opportunity.lead_id} not found")
    return OpportunityContext(opportunity, lead)


def require_active(uow: UnitOfWork, context: OpportunityContext, now: datetime, *, allow_suppressed: bool = False) -> None:
    if context.lead.stage is LeadStage.CLOSED:
        raise CommercialError(CommercialCode.LEAD_CLOSED)
    if context.opportunity.status not in ACTIVE_OPPORTUNITY_STATUSES:
        raise CommercialError(CommercialCode.OPPORTUNITY_NOT_OPEN)
    if not allow_suppressed and is_suppressed(uow, context.lead, now):
        raise CommercialError(CommercialCode.CONTACT_SUPPRESSED)
