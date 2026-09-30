"""Shared checks for pipeline commands: lead loading with version, openness, suppression."""

from datetime import datetime

from app.core.enums import DNCScope, LeadStage
from app.core.models import Lead
from app.persistence import UnitOfWork
from app.pipeline.errors import PipelineCode, PipelineError, PipelineNotFoundError
from app.policy.suppression import evaluate_suppression


def load_lead(uow: UnitOfWork, lead_id: str, expected_version: int | None = None) -> Lead:
    lead = uow.leads.get(lead_id)
    if lead is None:
        raise PipelineNotFoundError(f"lead {lead_id} not found")
    if expected_version is not None and lead.version != expected_version:
        raise PipelineError(PipelineCode.LEAD_VERSION_CHANGED)
    return lead


def require_open(lead: Lead) -> None:
    if lead.stage is LeadStage.CLOSED:
        raise PipelineError(PipelineCode.LEAD_CLOSED)


def is_suppressed(uow: UnitOfWork, lead: Lead, now: datetime) -> bool:
    """Contact-level do-not-contact in force now (email, its domain, or the company domain)."""
    contact = uow.contacts.get(lead.contact_id)
    if contact is None:
        return False
    company = uow.companies.get(lead.company_id) if lead.company_id else None
    domain = company.domain if company else None
    entries = [*uow.dnc.list_for_value(DNCScope.EMAIL, contact.email),
               *uow.dnc.list_for_value(DNCScope.DOMAIN, contact.email.split("@", 1)[1])]
    if domain:
        entries += uow.dnc.list_for_value(DNCScope.DOMAIN, domain)
    return evaluate_suppression(contact.email, domain, entries, now) is not None


def require_not_suppressed(uow: UnitOfWork, lead: Lead, now: datetime) -> None:
    """DNC wins: no commercial progression or reopening for a suppressed contact."""
    if is_suppressed(uow, lead, now):
        raise PipelineError(PipelineCode.CONTACT_SUPPRESSED)
