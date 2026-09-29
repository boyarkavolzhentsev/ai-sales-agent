from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.core.enums import (
    CampaignReviewMode,
    CampaignStatus,
    FollowUpCancelReason,
    FollowUpStatus,
    KnowledgeDomain,
    OutboundDecision,
    OutboundKind,
    OutboundStatus,
    is_review_mode_supported_v1,
)
from app.core.models import Campaign, CampaignTargetFilter, FollowUpPlan, OutboundMessage

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=1)
T2 = T0 + timedelta(minutes=2)
T3 = T0 + timedelta(minutes=3)
NAIVE = datetime(2026, 1, 1, 12, 0)
HASH = "b" * 64


def campaign_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "campaign_id": "camp-1",
        "name": "Q1 partnerships",
        "target_filter": CampaignTargetFilter(),
        "allowed_knowledge_domains": (KnowledgeDomain.COMPANY, KnowledgeDomain.OUTBOUND_MESSAGING),
        "sending_mailbox": "Outreach@OurCo.com",
        "max_follow_ups": 2,
        "min_interval_between_follow_ups": timedelta(days=3),
        "created_by": "operator-1",
        "created_at": T0,
        "updated_at": T0,
    }
    return base | overrides


def outbound_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "outbound_id": "out-1",
        "idempotency_key": "camp-1:lead-1:0",
        "kind": OutboundKind.FIRST_TOUCH,
        "lead_id": "lead-1",
        "contact_id": "contact-1",
        "campaign_id": "camp-1",
        "sequence_no": 0,
        "draft_id": "draft-1",
        "subject": "Hello",
        "body_final": "Body",
        "content_hash": HASH,
        "created_at": T0,
    }
    return base | overrides


SENT_FIELDS: dict[str, object] = {
    "status": OutboundStatus.SENT,
    "decision": OutboundDecision.SEND,
    "send_permit_id": "permit-1",
    "approved_at": T1,
    "sending_at": T2,
    "sent_at": T3,
}


def plan_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "plan_id": "plan-1",
        "lead_id": "lead-1",
        "campaign_id": "camp-1",
        "anchor_outbound_id": "out-1",
        "max_steps": 2,
        "next_due_at": T1,
        "created_at": T0,
        "updated_at": T0,
    }
    return base | overrides


# ---- Campaign ---------------------------------------------------------------


def test_campaign_defaults_to_draft_and_each_review() -> None:
    campaign = Campaign(**campaign_kwargs())
    assert campaign.status is CampaignStatus.DRAFT
    assert campaign.review_mode is CampaignReviewMode.EACH
    assert campaign.sending_mailbox == "outreach@ourco.com"


@pytest.mark.parametrize("mode", list(CampaignReviewMode))
def test_review_mode_accepts_all_values_but_only_each_is_supported_in_v1(
    mode: CampaignReviewMode,
) -> None:
    # The contract validates every value; V1 behaviour exists only for EACH.
    campaign = Campaign(**campaign_kwargs(review_mode=mode))
    assert is_review_mode_supported_v1(campaign.review_mode) is (mode is CampaignReviewMode.EACH)


def test_max_follow_ups_must_be_non_negative() -> None:
    Campaign(**campaign_kwargs(max_follow_ups=0))
    with pytest.raises(ValidationError):
        Campaign(**campaign_kwargs(max_follow_ups=-1))


@pytest.mark.parametrize("interval", [timedelta(0), timedelta(seconds=-1)])
def test_min_interval_must_be_positive(interval: timedelta) -> None:
    with pytest.raises(ValidationError, match="min_interval"):
        Campaign(**campaign_kwargs(min_interval_between_follow_ups=interval))


def test_campaign_domains_non_empty_and_unique() -> None:
    with pytest.raises(ValidationError):
        Campaign(**campaign_kwargs(allowed_knowledge_domains=()))
    with pytest.raises(ValidationError, match="duplicate"):
        Campaign(
            **campaign_kwargs(
                allowed_knowledge_domains=(KnowledgeDomain.FAQ, KnowledgeDomain.FAQ)
            )
        )


def test_campaign_activation_and_window() -> None:
    with pytest.raises(ValidationError, match="activated_by"):
        Campaign(**campaign_kwargs(status=CampaignStatus.ACTIVE))
    Campaign(**campaign_kwargs(status=CampaignStatus.ACTIVE, activated_by="operator-1"))
    with pytest.raises(ValidationError, match="end_at"):
        Campaign(**campaign_kwargs(start_at=T1, end_at=T1))
    with pytest.raises(ValidationError):
        Campaign(**campaign_kwargs(start_at=NAIVE))


def test_target_filter_rejects_duplicates() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        CampaignTargetFilter(countries=("UA", "UA"))


# ---- OutboundMessage --------------------------------------------------------


def test_drafted_first_touch_is_valid() -> None:
    message = OutboundMessage(**outbound_kwargs())
    assert message.status is OutboundStatus.DRAFTED
    assert message.decision is None


def test_sent_message_is_valid() -> None:
    message = OutboundMessage(**outbound_kwargs(**SENT_FIELDS))
    assert message.sent_at == T3


def test_sequence_no_must_be_non_negative_and_match_kind() -> None:
    with pytest.raises(ValidationError):
        OutboundMessage(**outbound_kwargs(sequence_no=-1))
    with pytest.raises(ValidationError, match="sequence_no 0"):
        OutboundMessage(**outbound_kwargs(sequence_no=1))
    with pytest.raises(ValidationError, match="sequence_no >= 1"):
        OutboundMessage(
            **outbound_kwargs(kind=OutboundKind.FOLLOW_UP, thread_id="thr-1", sequence_no=0)
        )
    OutboundMessage(**outbound_kwargs(kind=OutboundKind.FOLLOW_UP, thread_id="thr-1", sequence_no=1))


def test_kind_requirements() -> None:
    with pytest.raises(ValidationError, match="campaign_id"):
        OutboundMessage(**outbound_kwargs(campaign_id=None))
    with pytest.raises(ValidationError, match="thread_id"):
        OutboundMessage(**outbound_kwargs(kind=OutboundKind.REPLY, campaign_id=None))
    OutboundMessage(**outbound_kwargs(kind=OutboundKind.REPLY, campaign_id=None, thread_id="thr-1"))


def test_sent_at_cannot_precede_created_at_or_sending_at() -> None:
    with pytest.raises(ValidationError, match="sent_at"):
        OutboundMessage(**outbound_kwargs(**(SENT_FIELDS | {"sent_at": T1})))
    with pytest.raises(ValidationError, match="precede created_at"):
        OutboundMessage(
            **outbound_kwargs(
                **(
                    SENT_FIELDS
                    | {
                        "approved_at": T0 - timedelta(minutes=3),
                        "sending_at": T0 - timedelta(minutes=2),
                        "sent_at": T0 - timedelta(minutes=1),
                    }
                )
            )
        )


def test_status_requires_permit_and_timestamps() -> None:
    with pytest.raises(ValidationError, match="send_permit_id"):
        OutboundMessage(**outbound_kwargs(**(SENT_FIELDS | {"send_permit_id": None})))
    with pytest.raises(ValidationError, match="sending_at"):
        OutboundMessage(**outbound_kwargs(**(SENT_FIELDS | {"sending_at": None})))
    with pytest.raises(ValidationError, match="sent_at"):
        OutboundMessage(**outbound_kwargs(**(SENT_FIELDS | {"sent_at": None})))
    with pytest.raises(ValidationError, match="sent_at"):
        OutboundMessage(**outbound_kwargs(sent_at=T1))


def test_status_decision_consistency() -> None:
    with pytest.raises(ValidationError, match="inconsistent"):
        OutboundMessage(**outbound_kwargs(**(SENT_FIELDS | {"decision": OutboundDecision.HOLD})))
    with pytest.raises(ValidationError, match="inconsistent"):
        OutboundMessage(**outbound_kwargs(status=OutboundStatus.SKIPPED))
    OutboundMessage(
        **outbound_kwargs(status=OutboundStatus.SKIPPED, decision=OutboundDecision.SKIP)
    )
    OutboundMessage(
        **outbound_kwargs(
            status=OutboundStatus.PENDING_REVIEW, decision=OutboundDecision.SEND
        )
    )


def test_hold_and_failure_reasons() -> None:
    with pytest.raises(ValidationError, match="hold_reason"):
        OutboundMessage(**outbound_kwargs(status=OutboundStatus.HELD, decision=OutboundDecision.HOLD))
    OutboundMessage(
        **outbound_kwargs(
            status=OutboundStatus.HELD, decision=OutboundDecision.HOLD, hold_reason="daily limit"
        )
    )
    with pytest.raises(ValidationError, match="hold_reason"):
        OutboundMessage(**outbound_kwargs(hold_reason="stray"))
    failed = SENT_FIELDS | {"status": OutboundStatus.FAILED, "sent_at": None}
    with pytest.raises(ValidationError, match="failure_reason"):
        OutboundMessage(**outbound_kwargs(**failed))
    OutboundMessage(**outbound_kwargs(**(failed | {"failure_reason": "smtp 421"})))


def test_decision_reasons_unique() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        OutboundMessage(**outbound_kwargs(decision_reasons=("x", "x")))


# ---- FollowUpPlan -----------------------------------------------------------


def test_active_plan_is_valid() -> None:
    plan = FollowUpPlan(**plan_kwargs())
    assert plan.status is FollowUpStatus.ACTIVE
    assert plan.steps_sent == 0


def test_plan_step_bounds() -> None:
    with pytest.raises(ValidationError):
        FollowUpPlan(**plan_kwargs(max_steps=-1))
    with pytest.raises(ValidationError, match="exceed"):
        FollowUpPlan(**plan_kwargs(steps_sent=3))


def test_plan_status_rules() -> None:
    with pytest.raises(ValidationError, match="next_due_at"):
        FollowUpPlan(**plan_kwargs(next_due_at=None))
    with pytest.raises(ValidationError, match="cancel_reason"):
        FollowUpPlan(**plan_kwargs(status=FollowUpStatus.CANCELLED, next_due_at=None))
    FollowUpPlan(
        **plan_kwargs(
            status=FollowUpStatus.CANCELLED,
            cancel_reason=FollowUpCancelReason.REPLY_RECEIVED,
            next_due_at=None,
        )
    )
    with pytest.raises(ValidationError, match="cancel_reason"):
        FollowUpPlan(**plan_kwargs(cancel_reason=FollowUpCancelReason.OPERATOR))
    with pytest.raises(ValidationError, match="EXHAUSTED"):
        FollowUpPlan(**plan_kwargs(status=FollowUpStatus.EXHAUSTED, next_due_at=None))
    FollowUpPlan(**plan_kwargs(status=FollowUpStatus.EXHAUSTED, steps_sent=2, next_due_at=None))
    with pytest.raises(ValidationError, match="terminal"):
        FollowUpPlan(**plan_kwargs(status=FollowUpStatus.EXHAUSTED, steps_sent=2))


def test_plan_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        FollowUpPlan(**plan_kwargs(next_due_at=NAIVE))
