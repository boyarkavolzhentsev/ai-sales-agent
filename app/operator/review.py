"""Draft review context and approval revalidation, computed from current state.

A review draft is persisted by Stage 6 as a DRAFTED REPLY OutboundMessage (the
authoritative, versioned artifact) plus immutable audit facts: DRAFT_CREATED (claim check,
cited evidence IDs), KNOWLEDGE_ASSESSED (query, evidence locations, verdicts) and
PROCESSING_COMPLETED (classification). Those facts are history. Whether a draft is
approvable is always recomputed here from the current outbound, lead, contact, DNC,
campaign, knowledge and thread state at the injected ``now``.

Lead status rule for a human approval:
- AUTOMATED: approvable (the draft was produced for exactly this lead).
- OPERATOR_OWNED: approvable. The owner is a human, and approval is a human decision;
  ownership stops *automation* (Stage 6 revalidation), not human review.
- ON_HOLD: blocked. A hold means an open escalation or pause that has not been dealt
  with; the operator must first take ownership (explicitly) or clear the cause.
- CLOSED: blocked, whatever the reason.
"""

from dataclasses import dataclass
from datetime import datetime

from app.core.enums import (
    CampaignStatus,
    DNCScope,
    DraftPurpose,
    EmailDirection,
    KnowledgeDomain,
    LeadStage,
    LeadStatus,
    OutboundKind,
    OutboundStatus,
    RefKind,
)
from app.core.models import (
    DoNotContactEntry,
    IntentClassification,
    KnowledgeAssessment,
    KnowledgeEvidence,
    KnowledgeQuery,
    Lead,
    OutboundMessage,
)
from app.inbound import stable_id
from app.inbound.records import Events, ref
from app.knowledge.retrieval import select_for_query
from app.llm import SenderIdentity
from app.llm.claim_check import ClaimCheckResult, check_draft_claims, draft_hash
from app.operator.models import BlockCode, EvidenceView, GeneratedClassification, OperatorConfig
from app.persistence import UnitOfWork
from app.policy.suppression import evaluate_suppression

REVIEWABLE_STATUSES = frozenset({OutboundStatus.DRAFTED, OutboundStatus.PENDING_REVIEW})


@dataclass(frozen=True)
class EvidenceLocation:
    evidence_id: str
    chunk_id: str
    source_id: str
    source_version: int
    domain: KnowledgeDomain
    score: float
    rank: int


@dataclass(frozen=True)
class DraftContext:
    """Immutable facts about how a draft was produced.

    INBOUND_REPLY drafts (Stage 6) answer ``message_id`` from knowledge: they need their
    knowledge query and cited evidence. OUTBOUND_FOLLOW_UP drafts (Stage 9) are
    deterministic, cite no evidence and have no query; ``message_id`` is the customer
    message they follow (the last one when the draft was made)."""

    message_id: str
    claim_check: ClaimCheckResult | None
    cited: tuple[EvidenceLocation, ...]
    query: KnowledgeQuery | None
    assessment: KnowledgeAssessment | None
    classification: IntentClassification | None
    purpose: DraftPurpose = DraftPurpose.INBOUND_REPLY

    @property
    def complete(self) -> bool:
        if self.claim_check is None:
            return False
        if self.purpose is DraftPurpose.INBOUND_REPLY:
            return self.query is not None
        return not self.cited


def load_draft_context(uow: UnitOfWork, outbound: OutboundMessage) -> DraftContext | None:
    events = uow.audit.list_for_subject(ref(RefKind.OUTBOUND_MESSAGE, outbound.outbound_id))
    created = next((e for e in events if e.event_type == Events.DRAFT_CREATED), None)
    if created is None or created.after is None:
        return None
    message_id = next((r.id for r in created.subject_refs if r.kind is RefKind.EMAIL_MESSAGE), None)
    if message_id is None:
        return None
    raw_check = created.after.get("claim_check")
    claim_check = ClaimCheckResult.model_validate(raw_check) if isinstance(raw_check, dict) else None
    used = created.after.get("evidence_ids_used")
    used_ids = [str(i) for i in used] if isinstance(used, list) else []
    purpose = DraftPurpose(str(created.after.get("purpose") or DraftPurpose.INBOUND_REPLY.value))
    if purpose is not DraftPurpose.INBOUND_REPLY:
        # No knowledge of its own: the trigger message's knowledge must not be attached.
        if used_ids:
            return None
        return DraftContext(message_id, claim_check, (), None, None, None, purpose)

    knowledge = uow.audit.get(stable_id("ae", message_id, Events.KNOWLEDGE_ASSESSED))
    query: KnowledgeQuery | None = None
    assessment: KnowledgeAssessment | None = None
    locations: dict[str, EvidenceLocation] = {}
    if knowledge is not None and knowledge.after is not None:
        after = knowledge.after
        if isinstance(after.get("query"), dict):
            query = KnowledgeQuery.model_validate(after["query"])
        final = after.get("final_assessment") or after.get("deterministic_assessment")
        if isinstance(final, dict):
            assessment = KnowledgeAssessment.model_validate(final)
        for item in after.get("evidence") or []:
            if isinstance(item, dict):
                location = EvidenceLocation(
                    evidence_id=str(item["evidence_id"]), chunk_id=str(item["chunk_id"]),
                    source_id=str(item["source_id"]), source_version=int(str(item["source_version"])),
                    domain=KnowledgeDomain(str(item["domain"])), score=float(str(item["score"])), rank=int(str(item["rank"])),
                )
                locations[location.evidence_id] = location

    completed = uow.audit.get(stable_id("ae", message_id, Events.PROCESSING_COMPLETED))
    classification = None
    if completed is not None and completed.after is not None and isinstance(completed.after.get("classification"), dict):
        classification = IntentClassification.model_validate(completed.after["classification"])

    # A cited ID without a recorded location is kept visible as missing context.
    cited = tuple(locations[i] for i in used_ids if i in locations)
    if len(cited) != len(used_ids):
        return DraftContext(message_id, claim_check, (), query, assessment, classification)
    return DraftContext(message_id, claim_check, cited, query, assessment, classification)


def generated_classification(record: IntentClassification | None) -> GeneratedClassification | None:
    if record is None:
        return None
    return GeneratedClassification(
        intent=record.primary_intent,
        secondary_intents=record.secondary_intents,
        confidence=record.confidence_band,
        extracted_questions=record.extracted_questions,
        language=record.language,
    )


def evidence_views(uow: UnitOfWork, context: DraftContext, now: datetime) -> tuple[EvidenceView, ...]:
    usable = _usable_keys(uow, context.query, now)
    views = []
    for location in context.cited:
        excerpt = _chunk_text(uow, location)
        views.append(
            EvidenceView(
                evidence_id=location.evidence_id, source_id=location.source_id, source_version=location.source_version,
                chunk_id=location.chunk_id, domain=location.domain, excerpt=excerpt,
                usable_now=excerpt is not None and (location.source_id, location.source_version) in usable,
            )
        )
    return tuple(views)


def suppression_scopes(uow: UnitOfWork, email: str, company_domain: str | None, now: datetime) -> tuple[DNCScope, ...]:
    entries = _dnc_entries(uow, email, company_domain)
    match = evaluate_suppression(email, company_domain, entries, now)
    if match is None:
        return ()
    return tuple(sorted({entry.scope for entry in entries if evaluate_suppression(email, company_domain, (entry,), now)}))


def approval_blockers(
    uow: UnitOfWork, outbound: OutboundMessage, config: OperatorConfig, now: datetime
) -> tuple[BlockCode, ...]:
    """Every reason the draft cannot be approved right now; empty means approvable."""
    blockers: list[BlockCode] = []
    if outbound.kind is not OutboundKind.REPLY or outbound.status not in REVIEWABLE_STATUSES:
        blockers.append(BlockCode.DRAFT_NOT_REVIEWABLE)
    blockers.extend(reply_gate_blockers(uow, outbound, config.sender, now))
    return tuple(dict.fromkeys(blockers))


def reply_gate_blockers(
    uow: UnitOfWork, outbound: OutboundMessage, sender: SenderIdentity, now: datetime
) -> tuple[BlockCode, ...]:
    """The current-state gates shared by human approval (Stage 7) and dispatch (Stage 8):
    content integrity, lead/contact/thread association, lead status, suppression,
    campaign state, evidence usability and deterministic claims at ``now``, and newer
    customer messages. Status rules are the caller's (they differ per operation)."""
    blockers: list[BlockCode] = []
    if draft_hash(outbound.subject, outbound.body_final) != outbound.content_hash:
        blockers.append(BlockCode.DRAFT_INTEGRITY_FAILED)

    lead = uow.leads.get(outbound.lead_id)
    blockers.extend(_lead_blockers(uow, outbound, lead))
    if lead is not None:
        contact = uow.contacts.get(lead.contact_id)
        company = uow.companies.get(lead.company_id) if lead.company_id else None
        if contact is not None and suppression_scopes(uow, contact.email, company.domain if company else None, now):
            blockers.append(BlockCode.CONTACT_SUPPRESSED)
        for campaign_id in dict.fromkeys(c for c in (lead.campaign_id, outbound.campaign_id) if c is not None):
            campaign = uow.campaigns.get(campaign_id)
            if campaign is None or campaign.status is not CampaignStatus.ACTIVE:
                blockers.append(BlockCode.CAMPAIGN_INACTIVE)

    context = load_draft_context(uow, outbound)
    if context is None or not context.complete:
        blockers.append(BlockCode.DRAFT_CONTEXT_MISSING)
    else:
        blockers.extend(_content_blockers(uow, outbound, context, sender, now))
        if _newer_inbound(uow, outbound, context.message_id):
            blockers.append(BlockCode.NEWER_INBOUND_MESSAGE)
    return tuple(dict.fromkeys(blockers))


def _lead_blockers(uow: UnitOfWork, outbound: OutboundMessage, lead: Lead | None) -> list[BlockCode]:
    if lead is None:
        return [BlockCode.LEAD_MISSING]
    blockers = []
    thread = uow.threads.get(outbound.thread_id) if outbound.thread_id else None
    if lead.contact_id != outbound.contact_id or thread is None or thread.lead_id != lead.lead_id:
        blockers.append(BlockCode.LEAD_ASSOCIATION_CHANGED)
    if lead.stage is LeadStage.CLOSED:
        blockers.append(BlockCode.LEAD_CLOSED)
    elif lead.status is LeadStatus.ON_HOLD:
        blockers.append(BlockCode.LEAD_ON_HOLD)
    return blockers


def _content_blockers(
    uow: UnitOfWork, outbound: OutboundMessage, context: DraftContext, sender: SenderIdentity, now: datetime
) -> list[BlockCode]:
    """Evidence must still be usable at ``now`` and the stored text must still pass the
    deterministic claim check against the current text of the cited evidence."""
    query = context.query
    if context.claim_check is None or not context.complete:
        return [BlockCode.DRAFT_CONTEXT_MISSING]
    if not context.claim_check.passed or context.claim_check.draft_hash != outbound.content_hash:
        return [BlockCode.CLAIM_CHECK_FAILED]
    usable = _usable_keys(uow, query, now)
    evidence: list[KnowledgeEvidence] = []
    for location in context.cited:
        if query is None:
            return [BlockCode.DRAFT_CONTEXT_MISSING]
        text = _chunk_text(uow, location)
        source = uow.knowledge_sources.get(location.source_id, location.source_version)
        if text is None or source is None or (location.source_id, location.source_version) not in usable:
            return [BlockCode.EVIDENCE_UNUSABLE]
        evidence.append(
            KnowledgeEvidence(
                evidence_id=location.evidence_id, query_id=query.query_id, chunk_id=location.chunk_id,
                source_id=location.source_id, source_version=location.source_version, domain=location.domain,
                excerpt=text, score=location.score, rank=location.rank, approval_status=source.approval_status,
                external_use=source.external_use, review_by=source.review_by,
            )
        )
    recheck = check_draft_claims(
        outbound.subject, outbound.body_final, evidence,
        trusted_references=(sender.company_name, sender.sender_name),
    )
    return [] if recheck.passed else [BlockCode.CLAIM_CHECK_FAILED]


def _newer_inbound(uow: UnitOfWork, outbound: OutboundMessage, trigger_message_id: str) -> bool:
    """The customer wrote again after the message this draft answers."""
    thread = uow.threads.get(outbound.thread_id) if outbound.thread_id else None
    if thread is None or trigger_message_id not in thread.message_ids:
        return False
    later = thread.message_ids[thread.message_ids.index(trigger_message_id) + 1 :]
    for message_id in later:
        message = uow.messages.get(message_id)
        if message is not None and message.direction is EmailDirection.INBOUND:
            return True
    return False


def _usable_keys(uow: UnitOfWork, query: KnowledgeQuery | None, now: datetime) -> set[tuple[str, int]]:
    if query is None:
        return set()
    return {(s.source_id, s.version) for s in select_for_query(uow, query, now).usable}


def _chunk_text(uow: UnitOfWork, location: EvidenceLocation) -> str | None:
    for chunk in uow.knowledge_index.list_chunks(location.source_id, location.source_version):
        if chunk.chunk_id == location.chunk_id:
            return chunk.text
    return None


def _dnc_entries(uow: UnitOfWork, email: str, company_domain: str | None) -> list[DoNotContactEntry]:
    domains = {email.split("@", 1)[1], *([company_domain] if company_domain else [])}
    entries = list(uow.dnc.list_for_value(DNCScope.EMAIL, email))
    for domain in sorted(domains):
        entries.extend(uow.dnc.list_for_value(DNCScope.DOMAIN, domain))
    return entries
