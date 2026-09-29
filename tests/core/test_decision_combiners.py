import itertools

import pytest

from app.core.decisions import (
    KNOWLEDGE_DECISION_SEVERITY,
    OUTBOUND_DECISION_PRECEDENCE,
    EmptyOutboundDecisionsError,
    combine_knowledge_decisions,
    combine_outbound_decisions,
    knowledge_severity,
)
from app.core.enums import KnowledgeDecision as K
from app.core.enums import OutboundDecision as O

# ---- KnowledgeDecision -------------------------------------------------------


def test_knowledge_severity_order_is_the_approved_one() -> None:
    assert KNOWLEDGE_DECISION_SEVERITY == (
        K.NOT_APPROVED,
        K.CONFLICTING,
        K.STALE,
        K.INSUFFICIENT,
        K.PARTIAL,
        K.SUFFICIENT,
    )
    assert set(KNOWLEDGE_DECISION_SEVERITY) == set(K)
    ranks = [knowledge_severity(decision) for decision in KNOWLEDGE_DECISION_SEVERITY]
    assert ranks == sorted(ranks, reverse=True)


def test_empty_knowledge_decisions_are_insufficient() -> None:
    assert combine_knowledge_decisions([]) is K.INSUFFICIENT


@pytest.mark.parametrize("decision", list(K))
def test_single_knowledge_decision_is_returned(decision: K) -> None:
    assert combine_knowledge_decisions([decision]) is decision


@pytest.mark.parametrize(("a", "b"), list(itertools.product(K, K)))
def test_worst_knowledge_decision_wins_pairwise(a: K, b: K) -> None:
    expected = a if KNOWLEDGE_DECISION_SEVERITY.index(a) <= KNOWLEDGE_DECISION_SEVERITY.index(b) else b
    assert combine_knowledge_decisions([a, b]) is expected
    assert combine_knowledge_decisions([b, a]) is expected


def test_knowledge_examples() -> None:
    assert combine_knowledge_decisions([K.SUFFICIENT, K.SUFFICIENT]) is K.SUFFICIENT
    assert combine_knowledge_decisions([K.SUFFICIENT, K.PARTIAL]) is K.PARTIAL
    assert combine_knowledge_decisions([K.STALE, K.INSUFFICIENT, K.SUFFICIENT]) is K.STALE
    assert combine_knowledge_decisions(iter([K.CONFLICTING, K.NOT_APPROVED])) is K.NOT_APPROVED


# ---- OutboundDecision --------------------------------------------------------


def test_outbound_precedence_is_the_approved_one() -> None:
    assert OUTBOUND_DECISION_PRECEDENCE == (O.SKIP, O.ESCALATE, O.HOLD, O.SEND)
    assert set(OUTBOUND_DECISION_PRECEDENCE) == set(O)


def test_empty_outbound_decisions_raise() -> None:
    # No decisions means no eligibility check ran: a programming error, not "HOLD".
    with pytest.raises(EmptyOutboundDecisionsError):
        combine_outbound_decisions([])
    with pytest.raises(ValueError):
        combine_outbound_decisions(iter(()))


@pytest.mark.parametrize(("a", "b"), list(itertools.product(O, O)))
def test_highest_precedence_outbound_decision_wins_pairwise(a: O, b: O) -> None:
    expected = a if OUTBOUND_DECISION_PRECEDENCE.index(a) <= OUTBOUND_DECISION_PRECEDENCE.index(b) else b
    assert combine_outbound_decisions([a, b]) is expected
    assert combine_outbound_decisions([b, a]) is expected


def test_outbound_examples() -> None:
    assert combine_outbound_decisions([O.SEND]) is O.SEND
    assert combine_outbound_decisions([O.SEND, O.HOLD]) is O.HOLD
    assert combine_outbound_decisions([O.HOLD, O.ESCALATE, O.SEND]) is O.ESCALATE
    assert combine_outbound_decisions(iter([O.SEND, O.SKIP, O.ESCALATE])) is O.SKIP
