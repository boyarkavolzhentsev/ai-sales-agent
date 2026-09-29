"""Builders for LLM-boundary tests. All content is fictional."""

from datetime import UTC, datetime

from app.core.enums import (
    DraftPurpose,
    EmailDirection,
    KnowledgeApprovalStatus,
    KnowledgeDecision,
    KnowledgeDomain,
    KnowledgeExternalUse,
    KnowledgePurpose,
    LeadStage,
)
from app.core.models import KnowledgeAssessment, KnowledgeEvidence, KnowledgeQuery, QuestionAssessment
from app.llm import (
    FakeLLMTransport,
    NextStep,
    ReplyCompositionInput,
    SenderIdentity,
    StructuredLLM,
    UntrustedEmail,
)
from app.persistence import FrozenClock

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
QUERY_ID = "q-1"
QUESTION = "What does the Basic plan cost?"


def llm(transport: FakeLLMTransport | None = None) -> tuple[StructuredLLM, FakeLLMTransport]:
    fake = transport or FakeLLMTransport()
    return StructuredLLM(fake, FrozenClock(T0)), fake


def email(body: str, *, subject: str = "Question", direction: EmailDirection = EmailDirection.INBOUND) -> UntrustedEmail:
    return UntrustedEmail(direction=direction, sender="buyer@prospect.example", subject=subject, body=body)


def evidence(evidence_id: str, excerpt: str, *, domain: KnowledgeDomain = KnowledgeDomain.PRICING_COMMERCIAL) -> KnowledgeEvidence:
    return KnowledgeEvidence(
        evidence_id=evidence_id,
        query_id=QUERY_ID,
        chunk_id=f"kc_{evidence_id}",
        source_id="src-sample",
        source_version=1,
        domain=domain,
        excerpt=excerpt,
        score=1.0,
        rank=1,
        approval_status=KnowledgeApprovalStatus.APPROVED,
        external_use=KnowledgeExternalUse.EXTERNAL_OK,
        review_by=datetime(2026, 12, 31, tzinfo=UTC),
    )


def query(*questions: str) -> KnowledgeQuery:
    return KnowledgeQuery(
        query_id=QUERY_ID,
        purpose=KnowledgePurpose.INBOUND_REPLY,
        questions=questions or (QUESTION,),
        allowed_domains=(KnowledgeDomain.PRICING_COMMERCIAL, KnowledgeDomain.FAQ),
        locale="en",
        top_k=5,
        correlation_id="corr-1",
    )


def assessment(decision: KnowledgeDecision, *, question_decision: KnowledgeDecision | None = None) -> KnowledgeAssessment:
    return KnowledgeAssessment(
        query_id=QUERY_ID,
        decision=decision,
        per_question=(
            QuestionAssessment(question=QUESTION, decision=question_decision or decision, evidence_ids=("ev-1",)),
        ),
        deterministic_flags=("DETERMINISTIC_FLAG",),
        reasons=("deterministic reason",),
    )


PRICE_EVIDENCE = evidence("ev-1", "Price list\n\nThe Basic plan costs 100 EUR per month.\nplan.basic.monthly_price = 100 EUR")
FAQ_EVIDENCE = evidence(
    "ev-2",
    "Support\n\nWrite to support@samplewidget.example or see https://samplewidget.example/help. "
    "The launch is on 2026-09-01. Annual billing saves 10%.",
    domain=KnowledgeDomain.FAQ,
)


def composition_input(
    *,
    evidence_items: tuple[KnowledgeEvidence, ...] = (PRICE_EVIDENCE, FAQ_EVIDENCE),
    allowed: tuple[NextStep, ...] = (NextStep.ANSWER_QUESTIONS, NextStep.OFFER_MEETING),
    thread_body: str = "Hi, what does the Basic plan cost?",
) -> ReplyCompositionInput:
    return ReplyCompositionInput(
        purpose=DraftPurpose.INBOUND_REPLY,
        thread=(email(thread_body),),
        lead_stage=LeadStage.ENGAGED,
        contact_name="Sam Buyer",
        assessment=assessment(KnowledgeDecision.SUFFICIENT),
        evidence=evidence_items,
        allowed_next_steps=allowed,
        sender=SenderIdentity(sender_name="Alex Seller", company_name="Samplewidget Co"),
        locale="en",
    )
