"""One lead's cross-subsystem facts, read in ONE transaction (a consistent snapshot).

Every fact comes from the owning subsystem's own read functions, so the coordinator never
re-derives a subsystem rule:
- Stage 12 ``pipeline.next_action.gather``/``derive`` (lead, qualification, opportunity,
  latest conversation, campaign membership, escalations, review state);
- Stage 13 ``commercial.state.gather``/``next_action``/``stage`` for the active opportunity;
- Stage 9 ``conversation.policy.load_facts``/``schedule_blockers`` for the latest
  conversation (whether a follow-up may be scheduled now) and its open job;
- Stage 10 ``campaign.scheduler.pending_touch`` (what the campaign would do next);
- Stage 8 attempts and late-acceptance evidence, and Stage 8's own read-only claim gates
  (``dispatch.gates.claim_gate_codes``) for an operator-approved message.
Nothing here writes.
"""

from dataclasses import dataclass
from datetime import datetime

from app.campaign.scheduler import PendingTouch, pending_touch
from app.commercial.config import CommercialProfile
from app.commercial.state import CommercialFacts, CommercialNextAction
from app.commercial.state import gather as gather_commercial
from app.commercial.state import next_action as commercial_next_action
from app.commercial.state import stage as commercial_stage
from app.conversation.policy import FollowUpConfig, FollowUpFacts, load_facts, schedule_blockers
from app.core.enums import CommercialStage, EscalationStatus, OutboundStatus
from app.core.models import TERMINAL_CONVERSATION_STATUSES, Campaign, Escalation, OutboundMessage
from app.dispatch.gates import claim_gate_codes
from app.dispatch.models import DispatchConfig
from app.persistence import UNRESOLVED_ATTEMPT_STATES, DispatchAttempt, UnitOfWork
from app.pipeline.config import QualificationProfile
from app.pipeline.next_action import NextAction, PipelineFacts
from app.pipeline.next_action import derive as derive_pipeline
from app.pipeline.next_action import gather as gather_pipeline

OPEN_ESCALATIONS = frozenset({EscalationStatus.OPEN, EscalationStatus.ACKNOWLEDGED})
REVIEW_STATUSES = frozenset({OutboundStatus.DRAFTED, OutboundStatus.PENDING_REVIEW})


@dataclass(frozen=True)
class LeadSnapshot:
    pipeline: PipelineFacts
    pipeline_next: NextAction
    commercial: CommercialFacts | None
    commercial_next: CommercialNextAction | None
    commercial_stage: CommercialStage | None
    # Every outbound message to this contact (all of its leads), and their Stage 8 attempts.
    contact_messages: tuple[OutboundMessage, ...]
    attempts: tuple[DispatchAttempt, ...]
    unresolved_outbound_ids: tuple[str, ...]  # SENDING or an unresolved attempt
    conflict_outbound_ids: tuple[str, ...]  # late-acceptance conflict evidence
    escalations: tuple[Escalation, ...]  # open ones, of this lead
    pending_review: tuple[OutboundMessage, ...]  # this lead's drafts awaiting review, oldest first
    approved: tuple[OutboundMessage, ...]  # this lead's operator-approved, undispatched messages, oldest first
    send_policy_reasons: tuple[str, ...]  # Stage 8 claim-gate codes refusing the oldest approved message now
    follow_up: FollowUpFacts | None  # the latest conversation, when not terminal
    follow_up_schedule_blockers: tuple[str, ...]
    campaign: Campaign | None
    pending_touch: PendingTouch | None
    now: datetime


def gather(uow: UnitOfWork, lead_id: str, *, qualification: QualificationProfile, commercial: CommercialProfile,
           follow_up: FollowUpConfig, dispatch: DispatchConfig, now: datetime) -> LeadSnapshot | None:
    lead = uow.leads.get(lead_id)
    if lead is None:
        return None
    pipeline = gather_pipeline(uow, lead, qualification, now)
    commercial_facts = gather_commercial(uow, pipeline.opportunity, now) if pipeline.opportunity is not None else None

    lead_ids = sorted({lead.lead_id, *(other.lead_id for other in uow.leads.list_by_contact(lead.contact_id))})
    messages = tuple(m for found in lead_ids for m in uow.outbound.list_by_lead(found))
    attempts = tuple(a for m in messages for a in uow.dispatch_attempts.list_for_outbound(m.outbound_id))
    unresolved = {a.outbound_id for a in attempts if a.state in UNRESOLVED_ATTEMPT_STATES}
    unresolved |= {m.outbound_id for m in messages if m.status is OutboundStatus.SENDING}
    conflicts = {a.outbound_id for a in attempts if a.late_acceptance_provider_message_id is not None}
    own = sorted((m for m in messages if m.lead_id == lead.lead_id), key=lambda m: (m.created_at, m.outbound_id))
    approved = tuple(m for m in own if m.status is OutboundStatus.OPERATOR_APPROVED)

    conversation = pipeline.conversation
    follow_up_facts = None
    if conversation is not None and conversation.status not in TERMINAL_CONVERSATION_STATUSES:
        follow_up_facts = load_facts(uow, conversation)
    member = pipeline.member
    return LeadSnapshot(
        pipeline=pipeline, pipeline_next=derive_pipeline(pipeline),
        commercial=commercial_facts,
        commercial_next=commercial_next_action(commercial, commercial_facts) if commercial_facts else None,
        commercial_stage=commercial_stage(commercial, commercial_facts) if commercial_facts else None,
        contact_messages=messages, attempts=attempts,
        unresolved_outbound_ids=tuple(sorted(unresolved)), conflict_outbound_ids=tuple(sorted(conflicts)),
        escalations=tuple(sorted((e for e in uow.escalations.list_by_lead(lead.lead_id) if e.status in OPEN_ESCALATIONS),
                                 key=lambda e: (e.created_at, e.escalation_id))),
        pending_review=tuple(m for m in own if m.status in REVIEW_STATUSES),
        approved=approved,
        send_policy_reasons=_send_refusals(uow, approved[0], attempts, dispatch, now) if approved else (),
        follow_up=follow_up_facts,
        follow_up_schedule_blockers=tuple(schedule_blockers(follow_up_facts, follow_up, now)) if follow_up_facts else (),
        campaign=uow.campaigns.get(member.campaign_id) if member is not None else None,
        pending_touch=pending_touch(uow, member, now) if member is not None else None,
        now=now,
    )


def _send_refusals(uow: UnitOfWork, outbound: OutboundMessage, attempts: tuple[DispatchAttempt, ...],
                   config: DispatchConfig, now: datetime) -> tuple[str, ...]:
    """The codes Stage 8's own claim gates would refuse this message with right now
    (approval provenance, binding, Stage 7 gates, Stage 9/10 guards, Stage 3 policy).
    Read-only, the very same function the claim runs; Stage 8 re-evaluates it when it
    actually claims."""
    first_attempt = not any(a.outbound_id == outbound.outbound_id for a in attempts)
    _, codes = claim_gate_codes(uow, outbound, config, first_attempt=first_attempt, now=now)
    return tuple(dict.fromkeys(codes))
