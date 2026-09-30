"""The canonical pipeline and its actor-aware transition policy."""

import itertools
from datetime import timedelta

import pytest

from app.core.decisions import is_allowed_lead_transition
from app.core.enums import ActorType, CloseReason, LeadOrigin, LeadStage, PipelineTrigger
from app.core.models import Actor, Lead
from app.inbound.decision import stage_path
from app.pipeline.errors import PipelineCode, PipelineError
from app.pipeline.policy import OPERATOR_STAGES, REOPENABLE_REASONS, RULES, check_transition
from tests.inbound.builders import NOW

S, T = LeadStage, PipelineTrigger
SYSTEM = Actor(type=ActorType.SYSTEM, id="pipeline_service")
LLM = Actor(type=ActorType.LLM, id="model")
OPERATOR = Actor(type=ActorType.OPERATOR, id="op-alice")


def lead_at(stage: LeadStage, close_reason: CloseReason | None = None) -> Lead:
    return Lead(lead_id="ld-1", contact_id="ct-1", origin=LeadOrigin.INBOUND, stage=stage, close_reason=close_reason,
                created_at=NOW, updated_at=NOW + timedelta(minutes=1))


def allowed(stage: LeadStage, trigger: PipelineTrigger, target: LeadStage, actor: Actor = OPERATOR,
            close_reason: CloseReason | None = None, current_reason: CloseReason | None = None) -> bool:
    try:
        check_transition(lead_at(stage, current_reason), trigger, target, actor=actor, close_reason=close_reason)
    except PipelineError:
        return False
    return True


def test_the_canonical_happy_path_is_allowed_one_step_at_a_time() -> None:
    assert allowed(S.ENGAGED, T.QUALIFICATION_STARTED, S.QUALIFYING, SYSTEM)
    assert allowed(S.QUALIFYING, T.QUALIFICATION_APPROVED, S.QUALIFIED)
    assert allowed(S.QUALIFIED, T.OPPORTUNITY_CREATED, S.OPPORTUNITY)
    assert allowed(S.OPPORTUNITY, T.NEGOTIATION_STARTED, S.NEGOTIATION)
    assert allowed(S.NEGOTIATION, T.OPERATOR_MARKED_WON, S.CLOSED, close_reason=CloseReason.WON)
    assert allowed(S.OPPORTUNITY, T.OPERATOR_MARKED_WON, S.CLOSED, close_reason=CloseReason.WON)


@pytest.mark.parametrize(
    ("stage", "trigger", "target"),
    [(S.ENGAGED, T.QUALIFICATION_APPROVED, S.QUALIFIED), (S.QUALIFYING, T.OPPORTUNITY_CREATED, S.OPPORTUNITY),
     (S.QUALIFIED, T.NEGOTIATION_STARTED, S.NEGOTIATION), (S.NEW, T.QUALIFICATION_STARTED, S.QUALIFYING),
     (S.CONTACTED, T.QUALIFICATION_STARTED, S.QUALIFYING), (S.NEGOTIATION, T.QUALIFICATION_APPROVED, S.QUALIFIED)],
)
def test_stages_are_never_skipped_or_revisited(stage: LeadStage, trigger: PipelineTrigger, target: LeadStage) -> None:
    assert not allowed(stage, trigger, target)


@pytest.mark.parametrize("stage", [S.NEW, S.CONTACTED, S.ENGAGED, S.QUALIFYING, S.QUALIFIED])
def test_won_needs_an_opportunity_stage(stage: LeadStage) -> None:
    assert not allowed(stage, T.OPERATOR_MARKED_WON, S.CLOSED, close_reason=CloseReason.WON)


@pytest.mark.parametrize("trigger", [t for t, rule in RULES.items() if rule.operator_only])
@pytest.mark.parametrize("actor", [SYSTEM, LLM])
def test_operator_rules_refuse_every_non_operator_actor(trigger: PipelineTrigger, actor: Actor) -> None:
    rule = RULES[trigger]
    source = sorted(rule.sources)[0]
    target = sorted(rule.targets)[0]
    reason = sorted(rule.close_reasons)[0] if rule.close_reasons else None
    current = CloseReason.LOST if source is S.CLOSED else None
    assert allowed(source, trigger, target, OPERATOR, reason, current)
    assert not allowed(source, trigger, target, actor, reason, current)


def test_the_close_reason_must_match_the_trigger() -> None:
    assert not allowed(S.NEGOTIATION, T.OPERATOR_MARKED_WON, S.CLOSED, close_reason=CloseReason.LOST)
    assert not allowed(S.QUALIFIED, T.OPERATOR_MARKED_LOST, S.CLOSED, close_reason=CloseReason.WON)
    assert not allowed(S.QUALIFIED, T.OPERATOR_MARKED_LOST, S.CLOSED, close_reason=None)
    assert allowed(S.NEW, T.OPERATOR_MARKED_LOST, S.CLOSED, close_reason=CloseReason.LOST)


@pytest.mark.parametrize("trigger", [t for t in T if t is not T.OPERATOR_REOPENED])
def test_closed_is_terminal_except_for_an_operator_reopen(trigger: PipelineTrigger) -> None:
    for target in LeadStage:
        with pytest.raises(PipelineError) as error:
            check_transition(lead_at(S.CLOSED, CloseReason.LOST), trigger, target, actor=OPERATOR,
                             close_reason=sorted(RULES[trigger].close_reasons)[0] if RULES[trigger].close_reasons else None)
        assert error.value.codes == (PipelineCode.LEAD_CLOSED,)


@pytest.mark.parametrize(("reason", "target"), list(itertools.product(CloseReason, [S.ENGAGED, S.QUALIFYING, S.QUALIFIED])))
def test_only_commercial_closes_reopen_and_only_to_early_stages(reason: CloseReason, target: LeadStage) -> None:
    expected = reason in REOPENABLE_REASONS and target in (S.ENGAGED, S.QUALIFYING)
    assert allowed(S.CLOSED, T.OPERATOR_REOPENED, target, current_reason=reason) is expected
    assert CloseReason.WON not in REOPENABLE_REASONS and CloseReason.UNSUBSCRIBED not in REOPENABLE_REASONS


def test_automatic_rules_are_exactly_edges_of_the_core_automation_table() -> None:
    for rule in RULES.values():
        if rule.operator_only:
            continue
        for source, target in itertools.product(rule.sources, rule.targets):
            assert is_allowed_lead_transition(source, target), (rule.trigger, source, target)


def test_automation_can_never_reach_an_operator_stage() -> None:
    for source, target in itertools.product(LeadStage, OPERATOR_STAGES):
        assert not is_allowed_lead_transition(source, target)
        assert stage_path(source, target) == ()  # Stage 6 intent routing cannot get there either


@pytest.mark.parametrize("stage", sorted(OPERATOR_STAGES))
def test_no_automatic_sentiment_close_of_an_operator_stage(stage: LeadStage) -> None:
    assert not is_allowed_lead_transition(stage, S.CLOSED, CloseReason.NOT_INTERESTED)
    assert is_allowed_lead_transition(stage, S.CLOSED, CloseReason.UNSUBSCRIBED)  # suppression still closes
