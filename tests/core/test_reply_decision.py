import itertools

import pytest

from app.core.decisions import resolve_initial_reply_decision
from app.core.enums import KnowledgeDecision as K
from app.core.enums import ReplyDecision as R


@pytest.mark.parametrize(
    ("knowledge", "expected"),
    [
        (K.SUFFICIENT, R.DRAFT_FOR_REVIEW),
        (K.PARTIAL, R.DRAFT_FOR_REVIEW),
        (K.INSUFFICIENT, R.ESCALATE),
        (K.CONFLICTING, R.ESCALATE),
        (K.STALE, R.ESCALATE),
        (K.NOT_APPROVED, R.ESCALATE),
    ],
)
def test_knowledge_mapping(knowledge: K, expected: R) -> None:
    assert resolve_initial_reply_decision(knowledge) is expected


@pytest.mark.parametrize("knowledge", list(K))
def test_prefilter_no_action_wins(knowledge: K) -> None:
    assert resolve_initial_reply_decision(knowledge, prefilter_no_action=True) is R.NO_ACTION
    assert (
        resolve_initial_reply_decision(knowledge, hard_escalation=True, prefilter_no_action=True)
        is R.NO_ACTION
    )


@pytest.mark.parametrize("knowledge", list(K))
def test_hard_escalation_overrides_knowledge(knowledge: K) -> None:
    assert resolve_initial_reply_decision(knowledge, hard_escalation=True) is R.ESCALATE


@pytest.mark.parametrize(
    ("knowledge", "hard_escalation", "prefilter_no_action"),
    list(itertools.product(K, [False, True], [False, True])),
)
def test_auto_reply_is_unreachable(
    knowledge: K, hard_escalation: bool, prefilter_no_action: bool
) -> None:
    decision = resolve_initial_reply_decision(
        knowledge, hard_escalation=hard_escalation, prefilter_no_action=prefilter_no_action
    )
    assert decision is not R.AUTO_REPLY


def test_flags_are_keyword_only() -> None:
    with pytest.raises(TypeError):
        resolve_initial_reply_decision(K.SUFFICIENT, True)  # type: ignore[misc]
