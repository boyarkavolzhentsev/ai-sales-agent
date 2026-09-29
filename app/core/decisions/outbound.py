from collections.abc import Iterable

from app.core.enums import OutboundDecision

# Highest precedence first: permanent ineligibility beats needing a human beats
# temporary blocks beats sending.
OUTBOUND_DECISION_PRECEDENCE: tuple[OutboundDecision, ...] = (
    OutboundDecision.SKIP,
    OutboundDecision.ESCALATE,
    OutboundDecision.HOLD,
    OutboundDecision.SEND,
)

_PRECEDENCE_RANK: dict[OutboundDecision, int] = {
    decision: len(OUTBOUND_DECISION_PRECEDENCE) - index
    for index, decision in enumerate(OUTBOUND_DECISION_PRECEDENCE)
}


class EmptyOutboundDecisionsError(ValueError):
    """Raised when outbound decisions are combined without any check having run."""


def combine_outbound_decisions(decisions: Iterable[OutboundDecision]) -> OutboundDecision:
    """Return the highest-precedence decision.

    An empty input raises instead of defaulting. No decisions means no eligibility
    check ran, which is a programming error. Defaulting to HOLD would claim the
    message is eligible but waiting, and it would be retried forever without a check.
    """
    materialized = tuple(decisions)
    if not materialized:
        raise EmptyOutboundDecisionsError("cannot combine an empty set of outbound decisions")
    return max(materialized, key=_PRECEDENCE_RANK.__getitem__)
