import itertools

import pytest
from pydantic import ValidationError

from app.core.decisions import KNOWLEDGE_DECISION_SEVERITY, knowledge_severity
from app.core.enums import KnowledgeDecision as K
from app.llm import (
    FakeLLMTransport,
    FakeResponse,
    LLMContractViolationError,
    LLMTask,
    SufficiencyInput,
    SufficiencyOutcome,
    apply_llm_sufficiency_opinion,
    assess_sufficiency,
)
from tests.llm.builders import PRICE_EVIDENCE, assessment, llm, query

TASK = LLMTask.KNOWLEDGE_SUFFICIENCY


def opinion(value: K) -> FakeResponse:
    return FakeResponse.of({"opinion": value, "rationale_summary": "Checked the evidence."})


def run(deterministic: K, llm_opinion: K) -> SufficiencyOutcome:
    structured, _ = llm(FakeLLMTransport().script(TASK, opinion(llm_opinion)))
    data = SufficiencyInput(query=query(), assessment=assessment(deterministic), evidence=(PRICE_EVIDENCE,))
    return assess_sufficiency(structured, data, correlation_id="corr-1")


@pytest.mark.parametrize("llm_opinion", [K.SUFFICIENT, K.PARTIAL, K.INSUFFICIENT, K.CONFLICTING, K.STALE, K.NOT_APPROVED])
def test_sufficient_may_be_kept_or_downgraded(llm_opinion: K) -> None:
    outcome = run(K.SUFFICIENT, llm_opinion)
    assert outcome.assessment.decision is llm_opinion
    assert outcome.assessment.llm_sufficiency_opinion is llm_opinion


@pytest.mark.parametrize(
    ("deterministic", "llm_opinion"),
    [(K.PARTIAL, K.SUFFICIENT), (K.STALE, K.SUFFICIENT), (K.INSUFFICIENT, K.PARTIAL)]
    + [(K.NOT_APPROVED, less) for less in KNOWLEDGE_DECISION_SEVERITY[1:]],
)
def test_upward_opinions_are_rejected(deterministic: K, llm_opinion: K) -> None:
    with pytest.raises(LLMContractViolationError, match="upgrade"):
        run(deterministic, llm_opinion)


@pytest.mark.parametrize(("deterministic", "llm_opinion"), list(itertools.product(K, K)))
def test_merge_matrix(deterministic: K, llm_opinion: K) -> None:
    base = assessment(deterministic)
    if knowledge_severity(llm_opinion) < knowledge_severity(deterministic):
        with pytest.raises(LLMContractViolationError):
            apply_llm_sufficiency_opinion(base, llm_opinion)
        return
    merged = apply_llm_sufficiency_opinion(base, llm_opinion)
    assert merged.decision is llm_opinion  # the more severe of the two
    assert merged.llm_sufficiency_opinion is llm_opinion
    assert merged.per_question == base.per_question  # deterministic per-question results kept
    assert set(base.deterministic_flags) <= set(merged.deterministic_flags)
    downgraded = llm_opinion is not deterministic
    assert (f"LLM_DOWNGRADE:{llm_opinion}" in merged.deterministic_flags) is downgraded


def test_agreement_changes_nothing_but_the_recorded_opinion() -> None:
    base = assessment(K.PARTIAL)
    merged = apply_llm_sufficiency_opinion(base, K.PARTIAL)
    assert merged.model_dump() == base.model_dump() | {"llm_sufficiency_opinion": K.PARTIAL}


def test_opinion_can_be_applied_only_once() -> None:
    merged = apply_llm_sufficiency_opinion(assessment(K.SUFFICIENT), K.PARTIAL)
    with pytest.raises(LLMContractViolationError):
        apply_llm_sufficiency_opinion(merged, K.INSUFFICIENT)
    with pytest.raises(ValidationError):
        SufficiencyInput(query=query(), assessment=merged, evidence=(PRICE_EVIDENCE,))


def test_input_must_be_consistent() -> None:
    other = assessment(K.SUFFICIENT).model_copy(update={"query_id": "q-other"})
    with pytest.raises(ValidationError):
        SufficiencyInput(query=query(), assessment=other, evidence=(PRICE_EVIDENCE,))
    with pytest.raises(ValidationError):
        SufficiencyInput(query=query(), assessment=assessment(K.SUFFICIENT), evidence=(PRICE_EVIDENCE, PRICE_EVIDENCE))
