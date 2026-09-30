"""Lead stage transition table (Stage 0, approved; extended in Stage 12).

This is the AUTOMATIC subset of the sales pipeline: the forward moves automation may make
from provider and customer facts (Stage 6 replies, Stage 8/10 accepted sends, Stage 12
qualification start), plus the structural validity of a close. CLOSED is terminal here.
Operator-only moves (QUALIFIED, OPPORTUNITY, NEGOTIATION, WON, LOST, DISQUALIFIED,
re-opening) are decided by the actor-aware ``app.pipeline.policy``, which only ever
builds on this table; nothing here makes a commercial judgement.
"""

from collections.abc import Mapping

from app.core.enums import CloseReason, LeadStage

_FORWARD_TRANSITIONS: Mapping[LeadStage, frozenset[LeadStage]] = {
    LeadStage.NEW: frozenset({LeadStage.CONTACTED, LeadStage.ENGAGED}),
    LeadStage.CONTACTED: frozenset({LeadStage.ENGAGED}),
    LeadStage.ENGAGED: frozenset({LeadStage.INTERESTED, LeadStage.MEETING_REQUESTED, LeadStage.QUALIFYING}),
    LeadStage.INTERESTED: frozenset({LeadStage.MEETING_REQUESTED, LeadStage.QUALIFYING}),
    LeadStage.MEETING_REQUESTED: frozenset({LeadStage.QUALIFYING}),
    # Qualification collects facts; everything after it is an operator decision.
    LeadStage.QUALIFYING: frozenset(),
    LeadStage.QUALIFIED: frozenset(),
    LeadStage.OPPORTUNITY: frozenset(),
    LeadStage.NEGOTIATION: frozenset(),
    LeadStage.CLOSED: frozenset(),
}

# Stages whose position was set by automation. A lead in a later (operator) stage is
# never closed by an automatic, sentiment-based decision (e.g. NOT_INTERESTED).
AUTOMATIC_STAGES = frozenset({
    LeadStage.NEW, LeadStage.CONTACTED, LeadStage.ENGAGED, LeadStage.INTERESTED, LeadStage.MEETING_REQUESTED,
    LeadStage.QUALIFYING,
})

# NO_RESPONSE means "follow-ups exhausted without a reply", which only applies to a
# lead that was contacted and never replied.
_CLOSE_REASON_ALLOWED_FROM: Mapping[CloseReason, frozenset[LeadStage]] = {
    CloseReason.NO_RESPONSE: frozenset({LeadStage.CONTACTED}),
    CloseReason.NOT_INTERESTED: AUTOMATIC_STAGES,
}


def is_allowed_lead_transition(
    current_stage: LeadStage,
    target_stage: LeadStage,
    close_reason: CloseReason | None = None,
) -> bool:
    """Return True if moving a lead from ``current_stage`` to ``target_stage`` is allowed.

    A transition to CLOSED requires a ``close_reason``; any other transition must not
    carry one. Same-stage "transitions" are not transitions and return False.
    """
    if current_stage is LeadStage.CLOSED:
        return False
    if target_stage is LeadStage.CLOSED:
        if close_reason is None:
            return False
        allowed_from = _CLOSE_REASON_ALLOWED_FROM.get(close_reason)
        return allowed_from is None or current_stage in allowed_from
    if close_reason is not None:
        return False
    return target_stage in _FORWARD_TRANSITIONS[current_stage]
