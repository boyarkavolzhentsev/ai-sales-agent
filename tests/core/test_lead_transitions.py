import itertools

import pytest

from app.core.decisions import is_allowed_lead_transition
from app.core.decisions.lead_transitions import AUTOMATIC_STAGES
from app.core.enums import CloseReason, LeadStage

S = LeadStage

ALLOWED_FORWARD = {
    (S.NEW, S.CONTACTED),
    (S.NEW, S.ENGAGED),
    (S.CONTACTED, S.ENGAGED),
    (S.ENGAGED, S.INTERESTED),
    (S.ENGAGED, S.MEETING_REQUESTED),
    (S.INTERESTED, S.MEETING_REQUESTED),
    # Stage 12: qualification starts automatically once facts are being collected.
    (S.ENGAGED, S.QUALIFYING),
    (S.INTERESTED, S.QUALIFYING),
    (S.MEETING_REQUESTED, S.QUALIFYING),
}
OPEN_STAGES = [stage for stage in LeadStage if stage is not S.CLOSED]


@pytest.mark.parametrize(("current", "target"), list(itertools.product(LeadStage, LeadStage)))
def test_forward_transition_matrix_without_close_reason(current: LeadStage, target: LeadStage) -> None:
    expected = (current, target) in ALLOWED_FORWARD
    assert is_allowed_lead_transition(current, target) is expected


@pytest.mark.parametrize(
    ("current", "reason"), list(itertools.product(OPEN_STAGES, CloseReason))
)
def test_any_open_stage_can_close_with_a_valid_reason(current: LeadStage, reason: CloseReason) -> None:
    # NO_RESPONSE means follow-ups were exhausted without a reply: only from CONTACTED.
    # NOT_INTERESTED (an automatic, sentiment-based close) never applies to a lead an
    # operator has moved into the commercial stages (Stage 12).
    if reason is CloseReason.NO_RESPONSE:
        expected = current is S.CONTACTED
    elif reason is CloseReason.NOT_INTERESTED:
        expected = current in AUTOMATIC_STAGES
    else:
        expected = True
    assert is_allowed_lead_transition(current, S.CLOSED, reason) is expected


@pytest.mark.parametrize("current", OPEN_STAGES)
def test_closing_without_reason_is_rejected(current: LeadStage) -> None:
    assert not is_allowed_lead_transition(current, S.CLOSED)


@pytest.mark.parametrize(
    ("target", "reason"), list(itertools.product(LeadStage, [None, *CloseReason]))
)
def test_closed_is_terminal(target: LeadStage, reason: CloseReason | None) -> None:
    # Re-opening is not implemented in Stage 1.
    assert not is_allowed_lead_transition(S.CLOSED, target, reason)


@pytest.mark.parametrize(("current", "target"), sorted(ALLOWED_FORWARD))
def test_close_reason_on_non_closing_transition_is_rejected(
    current: LeadStage, target: LeadStage
) -> None:
    assert not is_allowed_lead_transition(current, target, CloseReason.WON)


def test_no_backward_or_same_stage_transitions() -> None:
    assert not is_allowed_lead_transition(S.INTERESTED, S.ENGAGED)
    assert not is_allowed_lead_transition(S.MEETING_REQUESTED, S.INTERESTED)
    assert not is_allowed_lead_transition(S.CONTACTED, S.NEW)
    assert not is_allowed_lead_transition(S.ENGAGED, S.ENGAGED)
