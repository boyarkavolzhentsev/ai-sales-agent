"""The canonical sales pipeline and its actor-aware transition policy.

Stages (``LeadStage``) and who may move a lead:

  automatic band   NEW -> CONTACTED -> ENGAGED -> INTERESTED -> MEETING_REQUESTED
                   (Stage 8/10 accepted first touch; Stage 6 genuine replies)
                   ENGAGED | INTERESTED | MEETING_REQUESTED -> QUALIFYING
                   (Stage 12: qualification facts started to arrive)
  operator band    QUALIFYING -> QUALIFIED -> OPPORTUNITY -> NEGOTIATION
  terminal         CLOSED with a CloseReason. WON (from OPPORTUNITY/NEGOTIATION), LOST
                   (any open stage) and DISQUALIFIED are operator decisions only; the
                   automatic closes (NOT_INTERESTED from the automatic band, UNSUBSCRIBED,
                   NO_RESPONSE) stay with Stage 6/10.
  reopen           CLOSED (LOST, DISQUALIFIED, NOT_INTERESTED, NO_RESPONSE) -> ENGAGED or
                   QUALIFYING, operator only. WON, UNSUBSCRIBED, INVALID_CONTACT and
                   DUPLICATE are never reopened.

The automatic rules are exactly the edges of ``is_allowed_lead_transition`` (the core
table Stage 6/8/10 apply); Stage 12's own changes go through ``apply_transition`` below,
which checks the rule, the actor and the source stage, writes the lead with its version
check and records one PIPELINE_TRANSITION audit event. No AI output or classifier can
reach an operator rule: every operator rule requires an operator actor.

Three dimensions stay separate: this pipeline (where the prospect is commercially),
ConversationStatus (what happens in one thread) and CampaignMemberStatus (whether
pre-reply outreach may continue). Nothing here writes the other two.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from pydantic import JsonValue

from app.core.decisions.lead_transitions import AUTOMATIC_STAGES
from app.core.enums import ActorType, CloseReason, LeadStage, LeadStatus, PipelineTrigger, RefKind
from app.core.models import Actor, Lead
from app.persistence import UnitOfWork
from app.pipeline.audit import record_event, ref
from app.pipeline.errors import PipelineCode, PipelineError

S = LeadStage
T = PipelineTrigger
OPEN_STAGES = frozenset(stage for stage in LeadStage if stage is not S.CLOSED)
OPERATOR_STAGES = frozenset({S.QUALIFIED, S.OPPORTUNITY, S.NEGOTIATION})
REOPENABLE_REASONS = frozenset({CloseReason.LOST, CloseReason.DISQUALIFIED, CloseReason.NOT_INTERESTED,
                                CloseReason.NO_RESPONSE})


@dataclass(frozen=True)
class TransitionRule:
    trigger: PipelineTrigger
    sources: frozenset[LeadStage]
    targets: frozenset[LeadStage]
    operator_only: bool
    close_reasons: frozenset[CloseReason] = frozenset()

    @property
    def terminal(self) -> bool:
        return S.CLOSED in self.targets

    @property
    def reopen(self) -> bool:
        return self.sources == frozenset({S.CLOSED})


RULES: Mapping[PipelineTrigger, TransitionRule] = {
    T.FIRST_TOUCH_ACCEPTED: TransitionRule(T.FIRST_TOUCH_ACCEPTED, frozenset({S.NEW}), frozenset({S.CONTACTED}), False),
    T.CUSTOMER_REPLIED: TransitionRule(T.CUSTOMER_REPLIED, frozenset({S.NEW, S.CONTACTED}), frozenset({S.ENGAGED}), False),
    T.QUALIFICATION_STARTED: TransitionRule(
        T.QUALIFICATION_STARTED, frozenset({S.ENGAGED, S.INTERESTED, S.MEETING_REQUESTED}), frozenset({S.QUALIFYING}), False),
    T.QUALIFICATION_APPROVED: TransitionRule(T.QUALIFICATION_APPROVED, frozenset({S.QUALIFYING}), frozenset({S.QUALIFIED}), True),
    T.OPPORTUNITY_CREATED: TransitionRule(T.OPPORTUNITY_CREATED, frozenset({S.QUALIFIED}), frozenset({S.OPPORTUNITY}), True),
    T.NEGOTIATION_STARTED: TransitionRule(T.NEGOTIATION_STARTED, frozenset({S.OPPORTUNITY}), frozenset({S.NEGOTIATION}), True),
    T.OPERATOR_MARKED_WON: TransitionRule(
        T.OPERATOR_MARKED_WON, frozenset({S.OPPORTUNITY, S.NEGOTIATION}), frozenset({S.CLOSED}), True,
        frozenset({CloseReason.WON})),
    T.OPERATOR_MARKED_LOST: TransitionRule(
        T.OPERATOR_MARKED_LOST, OPEN_STAGES, frozenset({S.CLOSED}), True, frozenset({CloseReason.LOST})),
    T.DISQUALIFIED: TransitionRule(
        T.DISQUALIFIED, frozenset({S.ENGAGED, S.INTERESTED, S.MEETING_REQUESTED, S.QUALIFYING, S.QUALIFIED}),
        frozenset({S.CLOSED}), True,
        frozenset({CloseReason.DISQUALIFIED, CloseReason.NOT_INTERESTED, CloseReason.DUPLICATE})),
    T.OPERATOR_REOPENED: TransitionRule(
        T.OPERATOR_REOPENED, frozenset({S.CLOSED}), frozenset({S.ENGAGED, S.QUALIFYING}), True),
}

PIPELINE_TRANSITION = "PIPELINE_TRANSITION"


def check_transition(lead: Lead, trigger: PipelineTrigger, target: LeadStage, *, actor: Actor,
                     close_reason: CloseReason | None = None) -> TransitionRule:
    """The rule allowing this move, or PipelineError. Pure: reads nothing else."""
    rule = RULES[trigger]
    if rule.operator_only and actor.type is not ActorType.OPERATOR:
        raise PipelineError(PipelineCode.TRANSITION_NOT_ALLOWED)
    if lead.stage is S.CLOSED and not rule.reopen:
        raise PipelineError(PipelineCode.LEAD_CLOSED)
    if rule.reopen and lead.close_reason not in REOPENABLE_REASONS:
        raise PipelineError(PipelineCode.NOT_REOPENABLE)
    if lead.stage not in rule.sources or target not in rule.targets:
        raise PipelineError(PipelineCode.TRANSITION_NOT_ALLOWED)
    if rule.terminal != (close_reason is not None) or (close_reason is not None and close_reason not in rule.close_reasons):
        raise PipelineError(PipelineCode.TRANSITION_NOT_ALLOWED)
    return rule


def apply_transition(
    uow: UnitOfWork, lead: Lead, trigger: PipelineTrigger, target: LeadStage, *, actor: Actor, correlation_id: str,
    now: datetime, close_reason: CloseReason | None = None, status: LeadStatus | None = None,
    reason: str | None = None, command_id: str | None = None,
) -> Lead:
    """The only way Stage 12 changes a lead's stage. Versioned write + one audit event."""
    check_transition(lead, trigger, target, actor=actor, close_reason=close_reason)
    changes: dict[str, object] = {"stage": target, "close_reason": close_reason,
                                  "updated_at": max(now, lead.updated_at), "version": lead.version + 1}
    if status is not None:
        changes["status"] = status
    updated = Lead.model_validate(lead.model_dump() | changes)
    uow.leads.update(updated, lead.version)
    before: dict[str, JsonValue] = {"stage": lead.stage.value, "status": lead.status.value,
                                    "close_reason": lead.close_reason.value if lead.close_reason else None,
                                    "version": lead.version}
    after: dict[str, JsonValue] = {"stage": target.value, "status": updated.status.value,
                                   "close_reason": close_reason.value if close_reason else None, "version": updated.version,
                                   "trigger": trigger.value, "reason": reason, "command_id": command_id}
    record_event(uow, key=(lead.lead_id, str(updated.version)), event_type=PIPELINE_TRANSITION,
                 subjects=(ref(RefKind.LEAD, lead.lead_id),), before=before, after=after, actor=actor,
                 correlation_id=correlation_id, now=now)
    return updated


def is_automatic_stage(stage: LeadStage) -> bool:
    return stage in AUTOMATIC_STAGES
