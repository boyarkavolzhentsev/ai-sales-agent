"""Deterministic campaign outreach text: nothing is ever fabricated.

V1 has no live LLM, and the Stage 5 composer is reply-only (it needs a thread), so
outreach uses fixed wording with explicit slots filled only from persisted data:
- the contact's stored name (greeting) and the company's stored name;
- at most one value statement quoted verbatim from APPROVED, external-use knowledge the
  campaign may use, retrieved deterministically and cited as evidence;
- the application's sender identity.
Nothing else about the prospect (size, pain points, stack, budget, role, relationship)
or about the offer (pricing, results, capabilities) is written unless it is in that
quoted evidence. Without sufficient knowledge the value statement degrades to generic,
claim-free wording. The Stage 5 deterministic claim check then runs on the result.
"""

import re
from dataclasses import dataclass
from datetime import datetime

from app.core.enums import KnowledgeDecision, KnowledgePurpose
from app.core.models import Campaign, KnowledgeEvidence, KnowledgeQuery, ProspectCompany, ProspectContact
from app.campaign.ids import stable_id
from app.knowledge import evaluate_knowledge
from app.llm import SenderIdentity
from app.llm.claim_check import ClaimCheckResult, check_draft_claims
from app.persistence import UnitOfWork

MAX_VALUE_CHARS = 300
OPT_OUT = 'If you would prefer not to hear from us, reply "unsubscribe" and we will not contact you again.'


@dataclass(frozen=True)
class Composition:
    subject: str
    body: str
    query: KnowledgeQuery | None
    evidence: tuple[KnowledgeEvidence, ...]
    personalization: tuple[str, ...]  # which persisted fields were used, e.g. "contact.name"
    claim_check: ClaimCheckResult


def value_statement(uow: UnitOfWork, campaign: Campaign, question: str, now: datetime) -> tuple[KnowledgeQuery, KnowledgeEvidence | None, str | None]:
    """The first sentence of the best approved evidence for the campaign question, or None."""
    query = KnowledgeQuery(
        query_id=stable_id("kq", "campaign", campaign.campaign_id, str(campaign.config_version)),
        purpose=KnowledgePurpose.OUTBOUND_COMPOSE, questions=(question,),
        allowed_domains=campaign.allowed_knowledge_domains, required_domains=(), locale="en", top_k=3,
        correlation_id=stable_id("corr", "campaign", campaign.campaign_id),
    )
    result = evaluate_knowledge(uow, query, now)
    if result.assessment.decision is not KnowledgeDecision.SUFFICIENT or not result.evidence:
        return query, None, None
    best = min(result.evidence, key=lambda e: e.rank)
    # A chunk is "<section path>" + blank line + "<text>": quote the first sentence of the text.
    paragraphs = [p.strip() for p in best.excerpt.split("\n\n") if p.strip()]
    for paragraph in paragraphs[1:] if len(paragraphs) > 1 else paragraphs:
        if not paragraph.startswith("#"):
            sentence = re.split(r"(?<=[.!?])\s", " ".join(paragraph.split()), maxsplit=1)[0][:MAX_VALUE_CHARS]
            return query, best, sentence
    return query, None, None


def compose_first_touch(
    uow: UnitOfWork, campaign: Campaign, contact: ProspectContact, company: ProspectCompany | None,
    sender: SenderIdentity, question: str, now: datetime,
) -> Composition:
    used: list[str] = []
    greeting = "Hello,"
    if contact.name:
        greeting = f"Hello {contact.name},"
        used.append("contact.name")
    at_company = ""
    if company is not None:
        at_company = f" at {company.name}"
        used.append("company.name")
    query, evidence, value = value_statement(uow, campaign, question, now)
    lines = [
        greeting,
        "",
        f"I am {sender.sender_name} from {sender.company_name}. I am reaching out to you{at_company} "
        f"to briefly introduce what we do.",
    ]
    if value is not None:
        lines += ["", value]
    lines += ["", "Would a short conversation be useful?", "", "Best regards,", sender.sender_name, sender.company_name, "", OPT_OUT]
    subject = f"A short introduction from {sender.company_name}"
    body = "\n".join(lines)
    cited = (evidence,) if evidence is not None else ()
    return Composition(subject, body, query if cited else None, cited, tuple(used),
                       _check(subject, body, cited, sender, contact, company))


def compose_follow_up(
    contact: ProspectContact, company: ProspectCompany | None, sender: SenderIdentity, first_subject: str
) -> Composition:
    used: list[str] = []
    greeting = "Hello,"
    if contact.name:
        greeting = f"Hello {contact.name},"
        used.append("contact.name")
    subject = first_subject if first_subject.startswith("Re: ") else f"Re: {first_subject}"
    body = "\n".join([
        greeting, "", "I wanted to follow up on my previous message. If it is of interest, I would be glad to share more.",
        "", "Best regards,", sender.sender_name, sender.company_name, "", OPT_OUT,
    ])
    return Composition(subject, body, None, (), tuple(used), _check(subject, body, (), sender, contact, company))


def _check(
    subject: str, body: str, evidence: tuple[KnowledgeEvidence, ...], sender: SenderIdentity,
    contact: ProspectContact, company: ProspectCompany | None,
) -> ClaimCheckResult:
    # Persisted prospect facts are trusted references: they are stored data, not model output.
    trusted = [sender.company_name, sender.sender_name, *([contact.name] if contact.name else []), *([company.name] if company else [])]
    return check_draft_claims(subject, body, evidence, trusted_references=tuple(trusted))
