"""Lead stage transition table (Stage 0, approved).

Automatic transitions only move forward. CLOSED is terminal; re-opening is not
implemented yet. Operator-only closes (WON/LOST) are not actor-checked here; that
belongs to the later actor-aware LeadManager.
"""

from collections.abc import Mapping

from app.core.enums import CloseReason, LeadStage

_FORWARD_TRANSITIONS: Mapping[LeadStage, frozenset[LeadStage]] = {
    LeadStage.NEW: frozenset({LeadStage.CONTACTED, LeadStage.ENGAGED}),
    LeadStage.CONTACTED: frozenset({LeadStage.ENGAGED}),
    LeadStage.ENGAGED: frozenset({LeadStage.INTERESTED, LeadStage.MEETING_REQUESTED}),
    LeadStage.INTERESTED: frozenset({LeadStage.MEETING_REQUESTED}),
    LeadStage.MEETING_REQUESTED: frozenset(),
    LeadStage.CLOSED: frozenset(),
}

# NO_RESPONSE means "follow-ups exhausted without a reply", which only applies to a
# lead that was contacted and never replied.
_CLOSE_REASON_ALLOWED_FROM: Mapping[CloseReason, frozenset[LeadStage]] = {
    CloseReason.NO_RESPONSE: frozenset({LeadStage.CONTACTED}),
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
