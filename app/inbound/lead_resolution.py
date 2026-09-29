"""Deterministic contact and lead resolution for an inbound sender.

Contact: looked up by normalized email; otherwise created (source INBOUND). Its company is
set only if a company with exactly the sender's email domain already exists; otherwise it
stays unresolved (None). Department is GENERAL; the contact type is ROLE_ADDRESS for
well-known role mailboxes (info@, sales@, ...), otherwise NAMED_BUSINESS.

Lead: the joined thread's lead, if any; else the contact's single open (non-CLOSED) lead;
else a new INBOUND lead at ENGAGED. Several open leads are ambiguous: the most recently
updated one is used only as the escalation anchor and nothing about it is changed.
"""

from dataclasses import dataclass
from datetime import datetime

from app.core.enums import ContactDepartment, ContactSource, ContactType, LeadOrigin, LeadStage, LeadStatus
from app.core.models import EmailThread, Lead, ProspectContact
from app.inbound.models import stable_id
from app.persistence import UnitOfWork

ROLE_LOCAL_PARTS = frozenset(
    {
        "info", "sales", "contact", "hello", "support", "office", "admin", "team", "partnerships",
        "partners", "procurement", "purchasing", "bd", "bizdev", "marketing", "billing", "accounts",
        "enquiries", "inquiries", "help", "hr", "jobs", "careers",
    }
)


@dataclass(frozen=True)
class ContactResolution:
    contact: ProspectContact
    created: bool


@dataclass(frozen=True)
class LeadResolution:
    lead: Lead
    created: bool
    ambiguous: bool


def resolve_contact(uow: UnitOfWork, email: str, *, message_id: str, now: datetime) -> ContactResolution:
    existing = uow.contacts.get_by_email(email)
    if existing is not None:
        return ContactResolution(existing, created=False)
    local, domain = email.split("@", 1)
    company = uow.companies.get_by_domain(domain)
    contact = ProspectContact(
        contact_id=stable_id("ct", email),
        company_id=company.company_id if company is not None else None,
        email=email,
        department=ContactDepartment.GENERAL,
        contact_type=ContactType.ROLE_ADDRESS if local in ROLE_LOCAL_PARTS else ContactType.NAMED_BUSINESS,
        source=ContactSource.INBOUND,
        source_ref=message_id,
        collected_at=now,
        created_at=now,
        updated_at=now,
    )
    uow.contacts.add(contact)
    return ContactResolution(contact, created=True)


def resolve_lead(
    uow: UnitOfWork, contact: ProspectContact, thread: EmailThread | None, *, message_id: str, now: datetime
) -> LeadResolution:
    if thread is not None and thread.lead_id is not None:
        lead = uow.leads.get(thread.lead_id)
        if lead is not None:
            return LeadResolution(lead, created=False, ambiguous=False)
    open_leads = [lead for lead in uow.leads.list_by_contact(contact.contact_id) if lead.stage is not LeadStage.CLOSED]
    if len(open_leads) == 1:
        return LeadResolution(open_leads[0], created=False, ambiguous=False)
    if len(open_leads) > 1:
        anchor = max(open_leads, key=lambda lead: (lead.updated_at, lead.lead_id))
        return LeadResolution(anchor, created=False, ambiguous=True)
    lead = Lead(
        lead_id=stable_id("ld", message_id),
        contact_id=contact.contact_id,
        company_id=contact.company_id,
        origin=LeadOrigin.INBOUND,
        stage=LeadStage.ENGAGED,
        status=LeadStatus.AUTOMATED,
        created_at=now,
        updated_at=now,
    )
    uow.leads.add(lead)
    return LeadResolution(lead, created=True, ambiguous=False)
