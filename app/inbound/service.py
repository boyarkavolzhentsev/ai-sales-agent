"""Inbound sales-email workflow: observe -> analyze -> finalize.

PHASE A (one IMMEDIATE transaction): reserve the provider-message idempotency key, detect
replays and Internet-Message-ID duplicates, run the deterministic prefilter, resolve or
create thread/contact/lead, persist the immutable EmailMessage, audit. The prefilter runs
here (it is pure and cheap) so bounces and auto-replies never create contacts or leads.

PHASE B (no write transaction held; LLM calls happen only here): classification, intent
routing, knowledge query, retrieval + deterministic gate (a short read transaction), LLM
sufficiency (keep-or-downgrade), composition, deterministic claim check.

PHASE C (one IMMEDIATE transaction): reserve the finalization key (exactly one finalizer
wins), re-read the lead, apply only table-valid transitions, add DNC, persist the review
draft or the escalation, provenance, audit, and the PROCESSING_COMPLETED record that
replays return.

V1: AUTO_REPLY is disabled. Outcomes are DRAFT_FOR_REVIEW, ESCALATE or NO_ACTION. Nothing
is sent: no SendPermit, no quota reservation, drafts are stored as DRAFTED replies only.
Fail closed: any failure after observation becomes an ESCALATE when that can still be
persisted; otherwise InboundProcessingError.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from pydantic import JsonValue

from app.core.decisions import is_allowed_lead_transition
from app.core.enums import (
    CampaignStatus,
    CloseReason,
    DNCReason,
    DNCScope,
    DraftPurpose,
    EmailDirection,
    EscalationReason,
    EscalationSeverity,
    KnowledgeDecision,
    LeadIntent,
    LeadStage,
    LeadStatus,
    OutboundKind,
    OutboundStatus,
    RefKind,
    ReplyDecision,
)
from app.core.models import (
    DoNotContactEntry,
    EntityRef,
    EmailMessage,
    EmailThread,
    Escalation,
    KnowledgeAssessment,
    KnowledgeQuery,
    Lead,
    OutboundMessage,
)
from app.core.models.types import JsonObject
from app.inbound.decision import (
    KNOWLEDGE_REASONS,
    TARGET_STAGE,
    IntentRoute,
    Route,
    is_valid_proposed_stage,
    route_intent,
    stage_path,
)
from app.inbound.errors import IdempotencyCollisionError, InboundProcessingError
from app.inbound.knowledge_query import build_query
from app.inbound.lead_resolution import resolve_contact, resolve_lead
from app.inbound.models import InboundConfig, InboundEnvelope, InboundResult, PrefilterOutcome, stable_id
from app.inbound.prefilter import prefilter
from app.inbound.records import Events, audit_event, llm_call_summary, provenance_record, ref
from app.inbound.thread_resolution import find_thread
from app.inbound.unsubscribe import is_unsubscribe_request
from app.knowledge import KnowledgeResult, evaluate_knowledge
from app.knowledge.retrieval import select_for_query
from app.llm import (
    ClassificationOutcome,
    ClassifierInput,
    CompositionOutcome,
    LLMContractViolationError,
    LLMError,
    NextStep,
    ReplyCompositionInput,
    StructuredLLM,
    SufficiencyInput,
    SufficiencyOutcome,
    UntrustedEmail,
    classify_intent,
    compose_reply,
    assess_sufficiency,
    to_intent_classification,
)
from app.llm.inputs import MAX_EMAIL_BODY_CHARS
from app.persistence import Clock, Database, DuplicateIdempotencyKeyError, UnitOfWork
from app.conversation import state as conversation_state
from app.conversation.cancellation import cancel_undispatched
from app.policy.suppression import evaluate_suppression

SYSTEM_ACTOR_ID = "system:inbound"
URGENT_REASONS = frozenset({EscalationReason.LEGAL_OR_COMPLAINT, EscalationReason.INJECTION_SUSPECTED})
MEETING_INTENTS = frozenset({LeadIntent.MEETING_REQUEST, LeadIntent.POSITIVE_INTEREST, LeadIntent.PRICING_REQUEST})
# Messages that could still become (or already are) eligible for dispatch.


class _Append(Protocol):
    def __call__(
        self, event_type: str, subjects: tuple[EntityRef, ...], after: JsonObject, before: JsonObject | None = None
    ) -> None: ...


@dataclass(frozen=True)
class Observation:
    message_id: str
    thread_id: str
    contact_id: str | None
    lead_id: str | None
    prefilter: PrefilterOutcome
    ambiguous_thread: bool
    ambiguous_lead: bool
    unverified_reference: bool = False
    final: InboundResult | None = None

    @property
    def attribution_certain(self) -> bool:
        """The sender's lead is known without ambiguity or an unverified thread claim."""
        return not (self.ambiguous_thread or self.ambiguous_lead or self.unverified_reference)

    def facts(self) -> JsonObject:
        return {
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "contact_id": self.contact_id,
            "lead_id": self.lead_id,
            "prefilter": self.prefilter.value,
            "ambiguous_thread": self.ambiguous_thread,
            "ambiguous_lead": self.ambiguous_lead,
            "unverified_reference": self.unverified_reference,
        }


@dataclass(frozen=True)
class Analysis:
    decision: ReplyDecision
    reasons: tuple[EscalationReason, ...] = ()
    classification: ClassificationOutcome | None = None
    route: IntentRoute | None = None
    target_stage: LeadStage | None = None
    query: KnowledgeQuery | None = None
    knowledge: KnowledgeResult | None = None
    assessment: KnowledgeAssessment | None = None
    sufficiency: SufficiencyOutcome | None = None
    composition: CompositionOutcome | None = None
    detail: str | None = None
    add_dnc: bool = False
    close_reason: CloseReason | None = None
    omitted_questions: tuple[str, ...] = ()

    def escalate(self, *reasons: EscalationReason, detail: str | None = None) -> "Analysis":
        return replace(self, decision=ReplyDecision.ESCALATE, reasons=tuple(dict.fromkeys(reasons)), detail=detail)


def normalize_subject(subject: str) -> str:
    text = " ".join(subject.split())
    while True:
        lowered = text.casefold()
        for prefix in ("re:", "fw:", "fwd:", "aw:"):
            if lowered.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        else:
            return text.casefold()


class InboundService:
    def __init__(self, db: Database, llm: StructuredLLM, clock: Clock, config: InboundConfig) -> None:
        self._db = db
        self._llm = llm
        self._clock = clock
        self._config = config

    # ---- entry point ----------------------------------------------------------------------

    def process(self, envelope: InboundEnvelope, *, correlation_id: str) -> InboundResult:
        observation = self._observe(envelope, correlation_id)
        if observation.final is not None:
            return observation.final
        try:
            analysis = self._analyze(envelope, observation, correlation_id)
        except Exception as exc:  # noqa: BLE001 - fail closed after observation
            analysis = Analysis(ReplyDecision.ESCALATE).escalate(
                EscalationReason.INTERNAL_ERROR, detail=f"analysis failed: {type(exc).__name__}"
            )
        try:
            return self._finalize(observation, analysis, correlation_id)
        except Exception as exc:  # noqa: BLE001 - fall back to an escalation-only outcome
            if analysis.reasons == (EscalationReason.INTERNAL_ERROR,) and analysis.classification is None:
                raise InboundProcessingError(f"could not finalize {observation.message_id}") from exc
            fallback = Analysis(
                ReplyDecision.ESCALATE, add_dnc=analysis.add_dnc, close_reason=analysis.close_reason if analysis.add_dnc else None
            ).escalate(EscalationReason.INTERNAL_ERROR, detail=f"finalization failed: {type(exc).__name__}")
            try:
                return self._finalize(observation, fallback, correlation_id)
            except Exception as final_exc:  # noqa: BLE001
                raise InboundProcessingError(f"could not persist an escalation for {observation.message_id}") from final_exc

    # ---- PHASE A: observe -----------------------------------------------------------------

    def _rfc_message_id(self, envelope: InboundEnvelope) -> str:
        # Without an Internet Message-ID the provider identity stands in (never rewritten later).
        return envelope.internet_message_id or f"<{envelope.provider}.{envelope.provider_message_id}@{envelope.mailbox}>"

    def _observe(self, envelope: InboundEnvelope, correlation_id: str) -> Observation:
        message_id = stable_id("em", envelope.provider, envelope.mailbox, envelope.provider_message_id)
        key = f"inbound:{envelope.provider}:{envelope.mailbox}:{envelope.provider_message_id}"
        now = self._clock.now()
        with self._db.transaction() as uow:
            try:
                uow.idempotency.reserve(key, "inbound.observe", now)
            except DuplicateIdempotencyKeyError:
                return self._replay_observation(uow, envelope, message_id, correlation_id)
            existing = uow.messages.get_by_rfc_message_id(self._rfc_message_id(envelope))
            if existing is not None:
                return self._duplicate_observation(uow, envelope, existing, correlation_id)
            return self._record_observation(uow, envelope, message_id, correlation_id, now)

    def _record_observation(
        self, uow: UnitOfWork, envelope: InboundEnvelope, message_id: str, correlation_id: str, now: datetime
    ) -> Observation:
        outcome = prefilter(envelope, self._config.own_addresses)
        match = find_thread(uow, envelope)
        thread = match.thread
        msg_ref = ref(RefKind.EMAIL_MESSAGE, message_id)
        events = []
        contact_id = lead_id = None
        ambiguous_lead = False
        lead_for_thread: Lead | None = None
        if outcome is PrefilterOutcome.NONE:
            contact = resolve_contact(uow, envelope.from_address, message_id=message_id, now=now)
            lead = resolve_lead(uow, contact.contact, thread, message_id=message_id, now=now)
            contact_id, lead_id, ambiguous_lead = contact.contact.contact_id, lead.lead.lead_id, lead.ambiguous
            lead_for_thread = None if lead.ambiguous else lead.lead
            contact_ref = ref(RefKind.PROSPECT_CONTACT, contact_id)
            lead_ref = ref(RefKind.LEAD, lead_id)
            events.append((Events.CONTACT_CREATED if contact.created else Events.CONTACT_RESOLVED, (msg_ref, contact_ref), {"contact_id": contact_id}))
            events.append((Events.LEAD_CREATED if lead.created else Events.LEAD_RESOLVED, (msg_ref, lead_ref, contact_ref), {"lead_id": lead_id, "stage": lead.lead.stage.value, "ambiguous": lead.ambiguous}))

        thread_created = thread is None
        if thread is None:
            thread = EmailThread(
                thread_id=stable_id("th", message_id),
                mailbox=envelope.mailbox,
                participant_addresses=tuple(dict.fromkeys((envelope.from_address, envelope.mailbox))),
                subject_normalized=normalize_subject(envelope.subject),
                lead_id=lead_for_thread.lead_id if lead_for_thread else None,
                message_ids=(message_id,),
                last_inbound_at=envelope.received_at,
            )
            uow.threads.add(thread)
        else:
            last = thread.last_inbound_at
            updated = EmailThread.model_validate(
                thread.model_dump()
                | {
                    "participant_addresses": tuple(dict.fromkeys((*thread.participant_addresses, envelope.from_address))),
                    "message_ids": (*thread.message_ids, message_id),
                    "last_inbound_at": envelope.received_at if last is None else max(last, envelope.received_at),
                    "lead_id": thread.lead_id or (lead_for_thread.lead_id if lead_for_thread else None),
                    "version": thread.version + 1,
                }
            )
            uow.threads.update(updated, thread.version)
            thread = updated

        uow.messages.add(
            EmailMessage(
                message_id=message_id,
                rfc_message_id=self._rfc_message_id(envelope),
                thread_id=thread.thread_id,
                direction=EmailDirection.INBOUND,
                mailbox=envelope.mailbox,
                from_address=envelope.from_address,
                to_addresses=envelope.to_addresses,
                cc_addresses=envelope.cc_addresses,
                subject=envelope.subject,
                body_text=envelope.body_text,
                raw_ref=envelope.raw_ref,
                raw_hash=envelope.raw_hash,
                in_reply_to=envelope.in_reply_to,
                references=envelope.references,
                auto_submitted=envelope.auto_submitted,
                list_unsubscribe=envelope.list_unsubscribe,
                received_at=envelope.received_at,
                sent_at=envelope.sent_at,
                is_bounce=outcome is PrefilterOutcome.BOUNCE,
                is_auto_generated=outcome in (PrefilterOutcome.BOUNCE, PrefilterOutcome.AUTO_SUBMITTED),
            )
        )
        if outcome is PrefilterOutcome.NONE and contact_id is not None and lead_id is not None:
            # Supersedes pending follow-ups before any analysis starts, so a follow-up that is
            # due right now cannot be executed or dispatched against this newer message.
            conversation_state.record_inbound_activity(
                uow, thread_id=thread.thread_id, lead_id=lead_id, contact_id=contact_id, message_id=message_id,
                received_at=envelope.received_at, certain=not (match.ambiguous or ambiguous_lead or match.unverified),
                correlation_id=correlation_id, now=now,
            )
        observation = Observation(
            message_id=message_id,
            thread_id=thread.thread_id,
            contact_id=contact_id,
            lead_id=lead_id,
            prefilter=outcome,
            ambiguous_thread=match.ambiguous,
            ambiguous_lead=ambiguous_lead,
            unverified_reference=match.unverified,
        )
        thread_ref = ref(RefKind.EMAIL_THREAD, thread.thread_id)
        observed: JsonObject = observation.facts() | {
            "provider": envelope.provider,
            "synthesized_rfc_message_id": envelope.internet_message_id is None,
            "has_attachments": envelope.has_attachments,
            "provider_thread_ref": envelope.provider_thread_ref,
        }
        uow.audit.append(audit_event(message_id=message_id, event_type=Events.INBOUND_OBSERVED, subjects=(msg_ref, thread_ref), after=observed, correlation_id=correlation_id, occurred_at=now))
        uow.audit.append(audit_event(message_id=message_id, event_type=Events.THREAD_CREATED if thread_created else Events.THREAD_RESOLVED, subjects=(msg_ref, thread_ref), after={"thread_id": thread.thread_id, "ambiguous": match.ambiguous, "unverified_reference": match.unverified}, correlation_id=correlation_id, occurred_at=now))
        for event_type, subjects, after in events:
            uow.audit.append(audit_event(message_id=message_id, event_type=event_type, subjects=subjects, after=after, correlation_id=correlation_id, occurred_at=now))
        return observation

    def _replay_observation(
        self, uow: UnitOfWork, envelope: InboundEnvelope, message_id: str, correlation_id: str
    ) -> Observation:
        message = uow.messages.get(message_id)
        if message is None:
            # This provider message was first seen as a duplicate of another Message-ID.
            existing = uow.messages.get_by_rfc_message_id(self._rfc_message_id(envelope))
            if existing is None:
                raise InboundProcessingError(f"idempotency key reserved without an observation: {message_id}")
            return self._duplicate_observation(uow, envelope, existing, correlation_id)
        if message.raw_hash != envelope.raw_hash:
            raise IdempotencyCollisionError(f"provider message {envelope.provider_message_id} arrived with different content")
        observation = _load_observation(uow, message_id)
        final = _load_final(uow, message_id)
        if final is not None:
            return replace(observation, final=final.model_copy(update={"replayed": True}))
        return observation

    def _duplicate_observation(
        self, uow: UnitOfWork, envelope: InboundEnvelope, existing: EmailMessage, correlation_id: str
    ) -> Observation:
        if existing.raw_hash != envelope.raw_hash:
            raise IdempotencyCollisionError(f"Message-ID {existing.rfc_message_id} arrived with different content")
        observation = _load_observation(uow, existing.message_id)
        final = _load_final(uow, existing.message_id)
        if final is not None:
            result = final.model_copy(update={"duplicate": True, "replayed": True})
        else:
            result = InboundResult(
                correlation_id=correlation_id,
                message_id=existing.message_id,
                thread_id=existing.thread_id,
                lead_id=observation.lead_id,
                prefilter=PrefilterOutcome.DUPLICATE,
                reply_decision=ReplyDecision.NO_ACTION,
                duplicate=True,
                completed_at=self._clock.now(),
            )
        return replace(observation, final=result)

    # ---- PHASE B: analyze (no write transaction held) ---------------------------------------

    def _analyze(self, envelope: InboundEnvelope, observation: Observation, correlation_id: str) -> Analysis:
        if observation.prefilter is not PrefilterOutcome.NONE:
            return Analysis(ReplyDecision.NO_ACTION)
        if observation.lead_id is None or observation.contact_id is None:
            raise InboundProcessingError("a human message must have a contact and a lead")
        # Deterministic unsubscribe recognition happens before every early exit and before
        # any LLM call. The contact is always suppressed; the lead is closed only when the
        # sender's lead is attributed with certainty.
        unsubscribe = is_unsubscribe_request(envelope.subject, envelope.body_text)
        base = Analysis(ReplyDecision.NO_ACTION)
        if unsubscribe:
            base = self._with_unsubscribe(base, observation)

        if envelope.has_attachments or len(envelope.body_text) > MAX_EMAIL_BODY_CHARS:
            return base.escalate(EscalationReason.UNSUPPORTED_CONTENT)
        if observation.unverified_reference:
            return base.escalate(EscalationReason.UNVERIFIED_THREAD_REFERENCE)
        if observation.ambiguous_thread:
            return base.escalate(EscalationReason.AMBIGUOUS_THREAD)
        if observation.ambiguous_lead:
            return base.escalate(EscalationReason.AMBIGUOUS_LEAD)

        with self._db.transaction() as uow:
            lead = uow.leads.get(observation.lead_id)
            thread_messages = uow.messages.list_by_thread(observation.thread_id)
            sender = envelope.from_address
            dnc = uow.dnc.list_for_value(DNCScope.EMAIL, sender) + uow.dnc.list_for_value(DNCScope.DOMAIN, sender.split("@", 1)[1])
        if lead is None:
            raise InboundProcessingError(f"lead {observation.lead_id} disappeared")
        suppressed = evaluate_suppression(sender, None, dnc, self._clock.now()) is not None

        # Thread history comes only from a thread the sender verifiably participates in.
        context = [m for m in thread_messages if m.message_id != observation.message_id][-self._config.thread_context_messages :]
        latest = UntrustedEmail(direction=EmailDirection.INBOUND, sender=sender, subject=envelope.subject, body=envelope.body_text)
        try:
            classification = classify_intent(
                self._llm,
                ClassifierInput(
                    message_id=observation.message_id,
                    latest_message=latest,
                    thread_context=tuple(_untrusted(m) for m in context) if self._config.thread_context_messages else (),
                    lead_stage=lead.stage,
                    lead_status=lead.status,
                    locale=self._config.default_locale,
                ),
                correlation_id=correlation_id,
            )
        except LLMContractViolationError as exc:
            return base.escalate(EscalationReason.CONTRACT_VIOLATION, detail=f"classifier: {exc}")
        except LLMError as exc:
            return base.escalate(EscalationReason.CLASSIFIER_FAILURE, detail=type(exc).__name__)

        proposal = classification.proposal
        route = route_intent(proposal)
        analysis = replace(base, classification=classification, route=route)
        if proposal.intent is LeadIntent.UNSUBSCRIBE:
            analysis = self._with_unsubscribe(analysis, observation)
            unsubscribe = True
        if suppressed:
            if unsubscribe:
                return analysis
            return analysis.escalate(EscalationReason.SUPPRESSED_SENDER_INBOUND)
        if not is_valid_proposed_stage(lead.stage, proposal.proposed_stage, route):
            return analysis.escalate(
                EscalationReason.INVALID_LEAD_TRANSITION,
                detail=f"classifier proposed {proposal.proposed_stage} from {lead.stage}",
            )
        if route.route is Route.ESCALATE:
            target = None if unsubscribe else TARGET_STAGE.get(proposal.intent)
            return replace(analysis, target_stage=target).escalate(*route.reasons)
        if unsubscribe:
            # An unsubscribe request is never answered, whatever else the message asks.
            return analysis
        analysis = replace(analysis, target_stage=TARGET_STAGE.get(proposal.intent))
        if route.route is Route.NO_ACTION:
            return replace(analysis, close_reason=route.close_reason)

        plan = build_query(
            message_id=observation.message_id,
            intent=proposal.intent,
            questions=proposal.extracted_questions,
            locale=proposal.detected_language,
            top_k=self._config.top_k,
            correlation_id=correlation_id,
            max_questions=self._config.max_questions,
            max_chars=self._config.max_question_chars,
        )
        if plan.omitted:
            omitted = replace(analysis, omitted_questions=plan.omitted)
            return omitted.escalate(
                EscalationReason.UNASSESSED_QUESTIONS, detail=f"{len(plan.omitted)} question(s) exceed processing limits"
            )
        query = plan.query
        if query is None:
            return analysis.escalate(EscalationReason.NO_ANSWERABLE_QUESTIONS)
        with self._db.transaction() as uow:
            knowledge = evaluate_knowledge(uow, query, self._clock.now())
        analysis = replace(analysis, query=query, knowledge=knowledge, assessment=knowledge.assessment)

        if knowledge.assessment.decision is KnowledgeDecision.SUFFICIENT:
            try:
                sufficiency = assess_sufficiency(
                    self._llm,
                    SufficiencyInput(query=query, assessment=knowledge.assessment, evidence=knowledge.evidence),
                    correlation_id=correlation_id,
                )
            except LLMContractViolationError as exc:
                return analysis.escalate(EscalationReason.CONTRACT_VIOLATION, detail=f"sufficiency: {exc}")
            except LLMError as exc:
                return analysis.escalate(EscalationReason.SUFFICIENCY_FAILURE, detail=type(exc).__name__)
            analysis = replace(analysis, sufficiency=sufficiency, assessment=sufficiency.assessment)
        final = analysis.assessment or knowledge.assessment
        if final.decision is not KnowledgeDecision.SUFFICIENT:
            return analysis.escalate(KNOWLEDGE_REASONS[final.decision])

        try:
            composition = compose_reply(
                self._llm,
                ReplyCompositionInput(
                    purpose=DraftPurpose.INBOUND_REPLY,
                    thread=(*(_untrusted(m) for m in context), latest),
                    lead_stage=lead.stage,
                    lead_intent=proposal.intent,
                    assessment=final,
                    evidence=knowledge.evidence,
                    allowed_next_steps=self._next_steps(proposal.intent),
                    sender=self._config.sender,
                    locale=query.locale,
                ),
                correlation_id=correlation_id,
            )
        except LLMContractViolationError as exc:
            return analysis.escalate(EscalationReason.CONTRACT_VIOLATION, detail=f"composer: {exc}")
        except LLMError as exc:
            return analysis.escalate(EscalationReason.COMPOSER_FAILURE, detail=type(exc).__name__)
        analysis = replace(analysis, composition=composition)
        if not composition.claim_check.passed:
            return analysis.escalate(EscalationReason.CLAIM_CHECK_FAILED)
        return replace(analysis, decision=ReplyDecision.DRAFT_FOR_REVIEW)

    @staticmethod
    def _with_unsubscribe(analysis: Analysis, observation: Observation) -> Analysis:
        close = CloseReason.UNSUBSCRIBED if observation.attribution_certain else None
        return replace(analysis, add_dnc=True, close_reason=close, target_stage=None)

    @staticmethod
    def _next_steps(intent: LeadIntent) -> tuple[NextStep, ...]:
        steps = [NextStep.ANSWER_QUESTIONS]
        if intent in MEETING_INTENTS:
            steps.append(NextStep.OFFER_MEETING)
        steps.append(NextStep.OPERATOR_FOLLOW_UP)
        return tuple(steps)

    # ---- PHASE C: finalize ------------------------------------------------------------------

    def _finalize(self, observation: Observation, analysis: Analysis, correlation_id: str) -> InboundResult:
        if analysis.decision not in (ReplyDecision.DRAFT_FOR_REVIEW, ReplyDecision.ESCALATE, ReplyDecision.NO_ACTION):
            raise InboundProcessingError(f"{analysis.decision} is not a V1 inbound outcome")
        now = self._clock.now()
        message_id = observation.message_id
        msg_ref = ref(RefKind.EMAIL_MESSAGE, message_id)
        thread_ref = ref(RefKind.EMAIL_THREAD, observation.thread_id)
        code_version = self._config.code_version

        with self._db.transaction() as uow:
            try:
                uow.idempotency.reserve(f"inbound:final:{message_id}", "inbound.finalize", now)
            except DuplicateIdempotencyKeyError:
                final = _load_final(uow, message_id)
                if final is None:
                    raise InboundProcessingError(f"finalization reserved without a result: {message_id}") from None
                return final.model_copy(update={"replayed": True})

            def append(event_type: str, subjects: tuple[EntityRef, ...], after: JsonObject, before: JsonObject | None = None) -> None:
                uow.audit.append(
                    audit_event(message_id=message_id, event_type=event_type, subjects=(msg_ref, *subjects), after=after, before=before, correlation_id=correlation_id, occurred_at=now)
                )

            lead = uow.leads.get(observation.lead_id) if observation.lead_id else None
            lead_refs = (ref(RefKind.LEAD, lead.lead_id),) if lead else ()

            # The analysis ran on a snapshot without a transaction held. Before a draft becomes
            # reviewable, recheck the authoritative state inside this transaction.
            if analysis.decision is ReplyDecision.DRAFT_FOR_REVIEW:
                problems = self._revalidate(uow, observation, analysis, lead, now)
                if problems:
                    analysis = replace(
                        analysis, decision=ReplyDecision.ESCALATE,
                        reasons=(EscalationReason.STALE_ANALYSIS,), detail="; ".join(problems),
                    )

            record = None
            if analysis.classification is not None:
                record = to_intent_classification(analysis.classification, message_id=message_id, created_at=analysis.classification.metadata.created_at)
                classification_ref = ref(RefKind.INTENT_CLASSIFICATION, stable_id("ic", message_id))
                uow.provenance.append(provenance_record(analysis.classification.metadata, artifact=classification_ref, inputs=(msg_ref,), evidence_ids=(), code_version=code_version))
                proposal = analysis.classification.proposal
                append(Events.CLASSIFICATION_COMPLETED, (classification_ref, *lead_refs), {
                    "intent": proposal.intent.value,
                    "secondary_intents": [i.value for i in proposal.secondary_intents],
                    "confidence": proposal.confidence.value,
                    "risk_flags": [f.value for f in proposal.risk_flags],
                    "needs_operator_review": proposal.needs_operator_review,
                    "proposed_stage": proposal.proposed_stage.value if proposal.proposed_stage else None,
                    "llm": llm_call_summary(analysis.classification.metadata),
                })

            evidence_ids: tuple[str, ...] = ()
            if analysis.knowledge is not None and analysis.query is not None:
                evidence = analysis.knowledge.evidence
                evidence_ids = tuple(e.evidence_id for e in evidence)
                query_ref = ref(RefKind.KNOWLEDGE_QUERY, analysis.query.query_id)
                if analysis.sufficiency is not None:
                    uow.provenance.append(provenance_record(analysis.sufficiency.metadata, artifact=query_ref, inputs=(msg_ref,), evidence_ids=evidence_ids, code_version=code_version))
                append(Events.KNOWLEDGE_ASSESSED, (query_ref, *lead_refs), {
                    "query": analysis.query.model_dump(mode="json"),
                    "evidence": [
                        {"evidence_id": e.evidence_id, "chunk_id": e.chunk_id, "source_id": e.source_id, "source_version": e.source_version, "domain": e.domain.value, "score": e.score, "rank": e.rank}
                        for e in evidence
                    ],
                    "deterministic_assessment": analysis.knowledge.assessment.model_dump(mode="json"),
                    "final_assessment": analysis.assessment.model_dump(mode="json") if analysis.assessment else None,
                    "sufficiency_llm": llm_call_summary(analysis.sufficiency.metadata) if analysis.sufficiency else None,
                })

            newly_closed: Lead | None = None
            if lead is not None and lead.stage is not LeadStage.CLOSED:
                updated_lead = self._apply_lead_changes(uow, lead, analysis, now, append)
                if updated_lead.stage is LeadStage.CLOSED:
                    newly_closed = updated_lead

            # Suppression is contact-level and only ever added, never reversed here.
            if analysis.add_dnc and observation.contact_id is not None:
                contact = uow.contacts.get(observation.contact_id)
                if contact is not None and not uow.dnc.list_active(DNCScope.EMAIL, contact.email, now):
                    entry = DoNotContactEntry(
                        entry_id=stable_id("dnc", message_id), scope=DNCScope.EMAIL, value=contact.email,
                        reason=DNCReason.UNSUBSCRIBE_REQUEST, source_ref=msg_ref, created_by=SYSTEM_ACTOR_ID, created_at=now,
                    )
                    uow.dnc.add(entry)
                    append(Events.DNC_ADDED, (ref(RefKind.DNC_ENTRY, entry.entry_id), *lead_refs), {"entry_id": entry.entry_id, "scope": DNCScope.EMAIL.value, "reason": entry.reason.value})

            # Earlier drafts, reviewed or not, must not stay eligible for a suppressed contact or a closed lead.
            stale_leads: list[str] = []
            if analysis.add_dnc and observation.contact_id is not None:
                stale_leads = [found.lead_id for found in uow.leads.list_by_contact(observation.contact_id)]
            elif newly_closed is not None:
                stale_leads = [newly_closed.lead_id]
            self._cancel_open_drafts(uow, stale_leads, now, append)

            draft_id = outbound_id = escalation_id = None
            composition = analysis.composition
            if analysis.decision is ReplyDecision.DRAFT_FOR_REVIEW:
                if lead is None or composition is None:
                    raise InboundProcessingError("a review draft needs a lead and a composition")
                draft_id, outbound_id = stable_id("dr", message_id), stable_id("ob", message_id)
                uow.outbound.add(
                    OutboundMessage(
                        outbound_id=outbound_id, idempotency_key=f"inbound-reply:{message_id}", kind=OutboundKind.REPLY,
                        lead_id=lead.lead_id, contact_id=lead.contact_id, thread_id=observation.thread_id, sequence_no=0,
                        draft_id=draft_id, subject=composition.proposal.subject, body_final=composition.proposal.body,
                        content_hash=composition.claim_check.draft_hash, status=OutboundStatus.DRAFTED, created_at=now,
                    )
                )
                draft_ref = ref(RefKind.MESSAGE_DRAFT, draft_id)
                used = tuple(e.evidence_id for e in composition.cited_evidence)
                uow.provenance.append(provenance_record(composition.metadata, artifact=draft_ref, inputs=(msg_ref,), evidence_ids=used, code_version=code_version))
                append(Events.DRAFT_CREATED, (draft_ref, ref(RefKind.OUTBOUND_MESSAGE, outbound_id), thread_ref, *lead_refs), {
                    "draft_id": draft_id,
                    "outbound_id": outbound_id,
                    "evidence_ids_used": list(used),
                    "proposed_next_step": composition.proposal.proposed_next_step.value,
                    "claim_check": composition.claim_check.model_dump(mode="json"),
                    "llm": llm_call_summary(composition.metadata),
                })
            elif analysis.decision is ReplyDecision.ESCALATE:
                if lead is None:
                    raise InboundProcessingError("an escalation needs a lead")
                escalation_id = stable_id("es", message_id)
                intent = analysis.classification.proposal.intent.value if analysis.classification else "unclassified"
                knowledge_decision = analysis.assessment.decision.value if analysis.assessment else "n/a"
                summary = f"{', '.join(r.value for r in analysis.reasons)}; intent={intent}; knowledge={knowledge_decision}"
                uow.escalations.add(
                    Escalation(
                        escalation_id=escalation_id, lead_id=lead.lead_id, trigger_ref=msg_ref, reasons=analysis.reasons,
                        severity=EscalationSeverity.URGENT if URGENT_REASONS & set(analysis.reasons) else EscalationSeverity.NORMAL,
                        summary=summary, created_at=now,
                    )
                )
                if composition is not None:
                    draft_ref = ref(RefKind.MESSAGE_DRAFT, stable_id("dr", message_id))
                    uow.provenance.append(provenance_record(composition.metadata, artifact=draft_ref, inputs=(msg_ref,), evidence_ids=tuple(e.evidence_id for e in composition.cited_evidence), code_version=code_version))
                append(Events.ESCALATION_CREATED, (ref(RefKind.ESCALATION, escalation_id), thread_ref, *lead_refs), {
                    "escalation_id": escalation_id,
                    "lead_id": lead.lead_id,
                    "thread_id": observation.thread_id,
                    "message_id": message_id,
                    "reasons": [r.value for r in analysis.reasons],
                    "summary": summary,
                    "detail": analysis.detail,
                    "classification": record.model_dump(mode="json") if record else None,
                    "knowledge_assessment": analysis.assessment.model_dump(mode="json") if analysis.assessment else None,
                    "evidence_ids": list(evidence_ids),
                    "omitted_questions": list(analysis.omitted_questions),
                    "claim_findings": [f.model_dump(mode="json") for f in composition.claim_check.unsupported] if composition else [],
                    "correlation_id": correlation_id,
                    "created_at": now.isoformat(),
                })

            conversation_state.record_inbound_outcome(
                uow, thread_id=observation.thread_id, contact_id=observation.contact_id,
                lead=uow.leads.get(observation.lead_id) if observation.lead_id else None, dnc_added=analysis.add_dnc,
                escalated=analysis.decision is ReplyDecision.ESCALATE, correlation_id=correlation_id, now=now,
            )
            result = InboundResult(
                correlation_id=correlation_id,
                message_id=message_id,
                thread_id=observation.thread_id,
                lead_id=observation.lead_id,
                prefilter=observation.prefilter,
                classification=record,
                reply_decision=analysis.decision,
                draft_id=draft_id,
                outbound_id=outbound_id,
                escalation_id=escalation_id,
                escalation_reasons=analysis.reasons if analysis.decision is ReplyDecision.ESCALATE else (),
                knowledge_assessment=analysis.assessment,
                evidence_ids=evidence_ids,
                claim_check_status=composition.claim_check.status if composition else None,
                completed_at=now,
            )
            append(Events.PROCESSING_COMPLETED, (thread_ref, *lead_refs), result.model_dump(mode="json"))
        return result

    def _revalidate(
        self, uow: UnitOfWork, observation: Observation, analysis: Analysis, lead: Lead | None, now: datetime
    ) -> list[str]:
        """Reasons a draft analysed on an earlier snapshot may no longer become reviewable."""
        if lead is None:
            return ["lead no longer exists"]
        problems: list[str] = []
        if lead.stage is LeadStage.CLOSED:
            problems.append(f"lead closed ({lead.close_reason})")
        if lead.status is not LeadStatus.AUTOMATED:
            problems.append(f"lead status is {lead.status}")
        if observation.contact_id is not None and lead.contact_id != observation.contact_id and not self._thread_owns(uow, observation, lead):
            problems.append("lead no longer belongs to this conversation")
        contact = uow.contacts.get(observation.contact_id or lead.contact_id)
        if contact is not None:
            entries = uow.dnc.list_for_value(DNCScope.EMAIL, contact.email) + uow.dnc.list_for_value(
                DNCScope.DOMAIN, contact.email.split("@", 1)[1]
            )
            if evaluate_suppression(contact.email, None, entries, now) is not None:
                problems.append("contact is suppressed")
        if lead.campaign_id is not None:
            campaign = uow.campaigns.get(lead.campaign_id)
            if campaign is None or campaign.status is not CampaignStatus.ACTIVE:
                problems.append(f"campaign {lead.campaign_id} is {campaign.status if campaign else 'missing'}")
        if analysis.query is not None and analysis.composition is not None:
            usable = {(s.source_id, s.version) for s in select_for_query(uow, analysis.query, now).usable}
            for evidence in analysis.composition.cited_evidence:
                if (evidence.source_id, evidence.source_version) not in usable:
                    problems.append(f"evidence {evidence.evidence_id} is no longer usable")
        return problems

    @staticmethod
    def _thread_owns(uow: UnitOfWork, observation: Observation, lead: Lead) -> bool:
        thread = uow.threads.get(observation.thread_id)
        return thread is not None and thread.lead_id == lead.lead_id

    @staticmethod
    def _cancel_open_drafts(uow: UnitOfWork, lead_ids: list[str], now: datetime, append: _Append) -> None:
        """Cancel every not-yet-dispatched message (reviewable, held or approved, with or
        without a permit) and, in the same transaction, release any ACTIVE quota
        reservation it held, so the slot is not counted for the rest of its policy date.
        Dispatched history (SENDING and later) and CONSUMED reservations are never touched."""
        cancelled: list[OutboundMessage] = []
        released: list[str] = []
        for lead_id in lead_ids:
            done, freed = cancel_undispatched(uow, uow.outbound.list_by_lead(lead_id), now)
            cancelled += done
            released += freed
        if cancelled:
            refs = tuple(ref(RefKind.OUTBOUND_MESSAGE, message.outbound_id) for message in cancelled)
            append(Events.DRAFTS_CANCELLED, refs, {
                "cancelled": [{"outbound_id": m.outbound_id, "previous_status": m.status.value} for m in cancelled],
                "released_reservation_ids": list[JsonValue](released),
            })

    def _apply_lead_changes(self, uow: UnitOfWork, lead: Lead, analysis: Analysis, now: datetime, append: _Append) -> Lead:
        """Closing for UNSUBSCRIBE applies to any open lead. Everything else (other closures,
        stage advances, holding) is automation and applies only to an AUTOMATED lead, so a
        stale result never advances a lead an operator holds or owns."""
        stage, close_reason, status = lead.stage, lead.close_reason, lead.status
        automated = lead.status is LeadStatus.AUTOMATED
        target_close = analysis.close_reason
        if target_close is not None and (automated or target_close is CloseReason.UNSUBSCRIBED) and is_allowed_lead_transition(stage, LeadStage.CLOSED, target_close):
            stage, close_reason = LeadStage.CLOSED, target_close
        elif automated:
            path = stage_path(stage, analysis.target_stage)
            if path:
                stage = path[-1]
        holds = analysis.decision is ReplyDecision.ESCALATE and EscalationReason.AMBIGUOUS_LEAD not in analysis.reasons
        if holds and stage is not LeadStage.CLOSED and status is LeadStatus.AUTOMATED:
            status = LeadStatus.ON_HOLD
        if (stage, close_reason, status) == (lead.stage, lead.close_reason, lead.status):
            return lead
        updated = Lead.model_validate(
            lead.model_dump()
            | {"stage": stage, "close_reason": close_reason, "status": status, "updated_at": max(now, lead.updated_at), "version": lead.version + 1}
        )
        uow.leads.update(updated, lead.version)
        before: dict[str, JsonValue] = {"stage": lead.stage.value, "status": lead.status.value, "close_reason": lead.close_reason.value if lead.close_reason else None}
        after: dict[str, JsonValue] = {"stage": stage.value, "status": status.value, "close_reason": close_reason.value if close_reason else None}
        append(Events.LEAD_STAGE_CHANGED, (ref(RefKind.LEAD, lead.lead_id),), after, before)
        return updated


def _untrusted(message: EmailMessage) -> UntrustedEmail:
    return UntrustedEmail(direction=message.direction, sender=message.from_address, subject=message.subject, body=message.body_text[:MAX_EMAIL_BODY_CHARS])


def _events_for(uow: UnitOfWork, message_id: str, event_type: str) -> list[JsonObject]:
    return [
        event.after
        for event in uow.audit.list_for_subject(ref(RefKind.EMAIL_MESSAGE, message_id))
        if event.event_type == event_type and event.after is not None
    ]


def _load_final(uow: UnitOfWork, message_id: str) -> InboundResult | None:
    found = _events_for(uow, message_id, Events.PROCESSING_COMPLETED)
    return InboundResult.model_validate(found[0]) if found else None


def _load_observation(uow: UnitOfWork, message_id: str) -> Observation:
    found = _events_for(uow, message_id, Events.INBOUND_OBSERVED)
    if not found:
        raise InboundProcessingError(f"message {message_id} has no observation record")
    facts = found[0]
    return Observation(
        message_id=str(facts["message_id"]),
        thread_id=str(facts["thread_id"]),
        contact_id=facts["contact_id"] if isinstance(facts["contact_id"], str) else None,
        lead_id=facts["lead_id"] if isinstance(facts["lead_id"], str) else None,
        prefilter=PrefilterOutcome(str(facts["prefilter"])),
        ambiguous_thread=bool(facts["ambiguous_thread"]),
        ambiguous_lead=bool(facts["ambiguous_lead"]),
        unverified_reference=bool(facts.get("unverified_reference", False)),
    )
