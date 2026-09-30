"""Reconciling commercial work with Stage 12 terminal decisions.

When a lead closes (WON, LOST, disqualified, or Stage 6 closing it on an unsubscribe),
every open proposal revision of its opportunities becomes CLOSED (an ACCEPTED revision
stays ACCEPTED), open term requests are CANCELLED and open signals CANCELLED. History is
never rewritten: approved/presented content, objections and decisions stay as recorded.
Reopening a lead resurrects nothing: old revisions stay historical and a new proposal
needs a new opportunity (one proposal per opportunity).
"""

from datetime import datetime

from app.commercial.negotiation import cancel_open_signals
from app.commercial.proposals import close_open_revisions
from app.commercial.terms import cancel_open_requests
from app.core.enums import LeadStage
from app.core.models import Lead
from app.persistence import UnitOfWork


def close_for_lead(uow: UnitOfWork, lead: Lead, *, correlation_id: str, now: datetime) -> int:
    """Idempotent: nothing open remains after the first call. Returns how many items closed."""
    if lead.stage is not LeadStage.CLOSED:
        return 0
    closed = 0
    for opportunity in uow.opportunities.list_by_lead(lead.lead_id):
        # The CLOSED lead is authoritative even if its opportunity was not closed yet (e.g.
        # Stage 6 closed the lead on an unsubscribe before the pipeline hook ran).
        closed += close_open_revisions(uow, opportunity.opportunity_id, correlation_id=correlation_id, now=now)
        closed += cancel_open_requests(uow, opportunity.opportunity_id, correlation_id=correlation_id, now=now)
        closed += cancel_open_signals(uow, opportunity.opportunity_id, correlation_id=correlation_id, now=now)
    return closed
