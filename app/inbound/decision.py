"""Pure intent handling matrix and lead-stage planning. The Stage 1 transition table
(is_allowed_lead_transition) stays authoritative; nothing here invents a transition."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.core.decisions import is_allowed_lead_transition
from app.core.enums import (
    CloseReason,
    ConfidenceBand,
    EscalationReason,
    KnowledgeDecision,
    LeadIntent,
    LeadStage,
    RiskFlag,
)
from app.inbound.knowledge_query import ANSWERABLE_INTENTS
from app.llm import IntentClassificationProposal


class Route(StrEnum):
    ANSWER = "ANSWER"  # knowledge pipeline, then a review draft or an escalation
    ESCALATE = "ESCALATE"
    NO_ACTION = "NO_ACTION"


@dataclass(frozen=True)
class IntentRoute:
    route: Route
    reasons: tuple[EscalationReason, ...] = ()
    close_reason: CloseReason | None = None
    add_dnc: bool = False


NO_ACTION_INTENTS: Mapping[LeadIntent, IntentRoute] = {
    LeadIntent.UNSUBSCRIBE: IntentRoute(Route.NO_ACTION, close_reason=CloseReason.UNSUBSCRIBED, add_dnc=True),
    LeadIntent.NOT_INTERESTED: IntentRoute(Route.NO_ACTION, close_reason=CloseReason.NOT_INTERESTED),
    LeadIntent.SPAM_OR_IRRELEVANT: IntentRoute(Route.NO_ACTION),
    LeadIntent.OUT_OF_OFFICE: IntentRoute(Route.NO_ACTION),
}
ESCALATE_INTENTS: Mapping[LeadIntent, EscalationReason] = {
    LeadIntent.LEGAL_OR_COMPLAINT: EscalationReason.LEGAL_OR_COMPLAINT,
    LeadIntent.NEGOTIATION: EscalationReason.NEGOTIATION,
    LeadIntent.UNCLEAR: EscalationReason.UNCLEAR_INTENT,
    LeadIntent.NON_SALES: EscalationReason.NON_SALES,
    LeadIntent.REFERRAL: EscalationReason.REFERRAL,
}
RISK_REASONS: Mapping[RiskFlag, EscalationReason] = {
    RiskFlag.LEGAL: EscalationReason.LEGAL_OR_COMPLAINT,
    RiskFlag.COMPLAINT: EscalationReason.LEGAL_OR_COMPLAINT,
    RiskFlag.NEGOTIATION: EscalationReason.NEGOTIATION,
    RiskFlag.INJECTION_SUSPECTED: EscalationReason.INJECTION_SUSPECTED,
    RiskFlag.SENSITIVE: EscalationReason.SENSITIVE_TONE,
}
KNOWLEDGE_REASONS: Mapping[KnowledgeDecision, EscalationReason] = {
    KnowledgeDecision.PARTIAL: EscalationReason.KNOWLEDGE_PARTIAL_REVIEW,
    KnowledgeDecision.INSUFFICIENT: EscalationReason.KNOWLEDGE_INSUFFICIENT,
    KnowledgeDecision.CONFLICTING: EscalationReason.KNOWLEDGE_CONFLICTING,
    KnowledgeDecision.STALE: EscalationReason.KNOWLEDGE_STALE,
    KnowledgeDecision.NOT_APPROVED: EscalationReason.KNOWLEDGE_NOT_APPROVED,
}
# Stage a genuine human reply with this intent may advance the lead to (never backwards).
TARGET_STAGE: Mapping[LeadIntent, LeadStage] = {
    LeadIntent.POSITIVE_INTEREST: LeadStage.INTERESTED,
    LeadIntent.PRICING_REQUEST: LeadStage.INTERESTED,
    LeadIntent.NEGOTIATION: LeadStage.INTERESTED,
    LeadIntent.MEETING_REQUEST: LeadStage.MEETING_REQUESTED,
    LeadIntent.INFO_REQUEST: LeadStage.ENGAGED,
    LeadIntent.OBJECTION: LeadStage.ENGAGED,
    LeadIntent.REFERRAL: LeadStage.ENGAGED,
    LeadIntent.LEGAL_OR_COMPLAINT: LeadStage.ENGAGED,
    LeadIntent.UNCLEAR: LeadStage.ENGAGED,
}


def route_intent(proposal: IntentClassificationProposal) -> IntentRoute:
    """No-action intents first (suppression only ever tightens), then escalations
    (intent, operator review, low confidence, risk flags), then answerable intents."""
    if proposal.intent in NO_ACTION_INTENTS:
        return NO_ACTION_INTENTS[proposal.intent]
    reasons: list[EscalationReason] = []
    if proposal.intent in ESCALATE_INTENTS:
        # These intents always carry needs_operator_review (Stage 5 contract); their own
        # reason already says why, so review/low confidence adds no separate reason.
        reasons.append(ESCALATE_INTENTS[proposal.intent])
    elif proposal.needs_operator_review or proposal.confidence is ConfidenceBand.LOW:
        reasons.append(EscalationReason.LOW_CONFIDENCE)
    reasons.extend(RISK_REASONS[flag] for flag in proposal.risk_flags)
    unique = tuple(dict.fromkeys(reasons))
    if unique:
        return IntentRoute(Route.ESCALATE, reasons=unique)
    if proposal.intent in ANSWERABLE_INTENTS:
        return IntentRoute(Route.ANSWER)
    return IntentRoute(Route.ESCALATE, reasons=(EscalationReason.UNCLEAR_INTENT,))


def stage_path(current: LeadStage, target: LeadStage | None) -> tuple[LeadStage, ...]:
    """Shortest chain of allowed forward transitions from current to target (excluding
    CLOSED), or () if target is None, already reached, or not reachable forwards."""
    if target is None or target is current or target is LeadStage.CLOSED:
        return ()
    frontier: list[tuple[LeadStage, ...]] = [(current,)]
    visited = {current}
    while frontier:
        path = frontier.pop(0)
        for stage in LeadStage:
            if stage in visited or stage is LeadStage.CLOSED or not is_allowed_lead_transition(path[-1], stage):
                continue
            if stage is target:
                return (*path[1:], stage)
            visited.add(stage)
            frontier.append((*path, stage))
    return ()


def is_valid_proposed_stage(current: LeadStage, proposed: LeadStage | None, route: IntentRoute) -> bool:
    """A classifier's proposed stage is acceptable when absent, unchanged, reachable
    forwards, or CLOSED together with a deterministic close reason for this intent."""
    if proposed is None or proposed is current:
        return True
    if proposed is LeadStage.CLOSED:
        return route.close_reason is not None and current is not LeadStage.CLOSED
    return bool(stage_path(current, proposed))
