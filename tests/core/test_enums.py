from enum import StrEnum

import pytest

from app.core import enums
from app.core.enums import (
    SUPPORTED_REVIEW_MODES_V1,
    CampaignReviewMode,
    CloseReason,
    KnowledgeDecision,
    KnowledgeDomain,
    LeadStage,
    LeadStatus,
    OutboundDecision,
    ReplyDecision,
    is_review_mode_supported_v1,
)


def _values(enum_type: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


def test_lead_stage_values() -> None:
    assert _values(LeadStage) == [
        "NEW",
        "CONTACTED",
        "ENGAGED",
        "INTERESTED",
        "MEETING_REQUESTED",
        "CLOSED",
    ]


def test_lead_status_values() -> None:
    assert _values(LeadStatus) == ["AUTOMATED", "ON_HOLD", "OPERATOR_OWNED"]


def test_close_reason_values() -> None:
    assert _values(CloseReason) == [
        "NOT_INTERESTED",
        "NO_RESPONSE",
        "UNSUBSCRIBED",
        "INVALID_CONTACT",
        "DUPLICATE",
        "WON",
        "LOST",
    ]


def test_reply_decision_values() -> None:
    assert _values(ReplyDecision) == ["AUTO_REPLY", "DRAFT_FOR_REVIEW", "ESCALATE", "NO_ACTION"]


def test_outbound_decision_values() -> None:
    assert _values(OutboundDecision) == ["SEND", "HOLD", "ESCALATE", "SKIP"]


def test_knowledge_decision_values() -> None:
    assert set(_values(KnowledgeDecision)) == {
        "SUFFICIENT",
        "PARTIAL",
        "INSUFFICIENT",
        "CONFLICTING",
        "STALE",
        "NOT_APPROVED",
    }


def test_there_are_exactly_15_knowledge_domains() -> None:
    assert len(KnowledgeDomain) == 15


def test_campaign_review_mode_values() -> None:
    assert _values(CampaignReviewMode) == ["EACH", "FIRST_N", "NONE"]


def test_only_each_review_mode_is_supported_in_v1() -> None:
    # FIRST_N and NONE are reserved contract values with no V1 behaviour.
    assert SUPPORTED_REVIEW_MODES_V1 == frozenset({CampaignReviewMode.EACH})
    assert is_review_mode_supported_v1(CampaignReviewMode.EACH)
    assert not is_review_mode_supported_v1(CampaignReviewMode.FIRST_N)
    assert not is_review_mode_supported_v1(CampaignReviewMode.NONE)


@pytest.mark.parametrize(
    "enum_type",
    [getattr(enums, name) for name in enums.__all__ if isinstance(getattr(enums, name), type)],
)
def test_every_enum_is_a_str_enum_whose_values_equal_names(enum_type: type[StrEnum]) -> None:
    assert issubclass(enum_type, StrEnum)
    assert len(enum_type) > 0
    for member in enum_type:
        assert member.value == member.name
