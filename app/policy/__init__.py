"""Deterministic sending policy: suppression, sending windows, limits, quota and
outbound/follow-up eligibility. The LLM never participates in these decisions."""

from app.persistence.records import QuotaReservation
from app.policy.errors import DuplicateQuotaReservationError, PolicyError, QuotaExceededError
from app.policy.follow_up import evaluate_follow_up_policy
from app.policy.limits import (
    CampaignLimits,
    GlobalDailyLimits,
    LimitPolicy,
    MailboxLimits,
    ScopedDailyLimits,
)
from app.policy.models import (
    REASON_DECISIONS,
    RESERVED_REASONS,
    KillSwitchState,
    PolicyCheck,
    PolicyDecisionResult,
    PolicyReason,
)
from app.policy.outbound import PolicyContext, evaluate_outbound_policy
from app.policy.quota import (
    COUNTED_STATUSES,
    QuotaSnapshot,
    ScopeCounts,
    build_quota_snapshot,
    evaluate_quota,
)
from app.policy.reservation import reserve_quota
from app.policy.suppression import SuppressionMatch, evaluate_suppression
from app.policy.windows import SendingWindow, Weekday, is_within_sending_window

__all__ = [
    "COUNTED_STATUSES",
    "REASON_DECISIONS",
    "RESERVED_REASONS",
    "CampaignLimits",
    "DuplicateQuotaReservationError",
    "GlobalDailyLimits",
    "KillSwitchState",
    "LimitPolicy",
    "MailboxLimits",
    "PolicyCheck",
    "PolicyContext",
    "PolicyDecisionResult",
    "PolicyError",
    "PolicyReason",
    "QuotaExceededError",
    "QuotaReservation",
    "QuotaSnapshot",
    "ScopeCounts",
    "ScopedDailyLimits",
    "SendingWindow",
    "SuppressionMatch",
    "Weekday",
    "build_quota_snapshot",
    "evaluate_follow_up_policy",
    "evaluate_outbound_policy",
    "evaluate_quota",
    "evaluate_suppression",
    "is_within_sending_window",
    "reserve_quota",
]
