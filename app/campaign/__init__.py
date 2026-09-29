"""Outbound campaign execution (V1: deterministic, operator-gated, offline).

Enrollment creates one logical membership per (campaign, contact) and, when eligible,
the campaign lead. Each touch is a durable job whose identity is "touch N of membership
M"; executing it produces at most one reviewable FIRST_TOUCH/FOLLOW_UP draft from
persisted facts and approved knowledge only. Stage 7 approval and Stage 8 dispatch send
it; the campaign layer never sends. A reply from the contact hands control to the
Stage 6/9 conversation workflow and stops campaign automation for that contact.

Imports app.core, app.persistence, app.policy, app.knowledge, app.llm (claim check,
sender identity) and app.conversation (shared cancellation, contact suppression) only;
never app.inbound, app.operator or app.dispatch (they depend on it).
"""

from app.campaign.enrollment import CampaignEnroller
from app.campaign.executor import CampaignExecutor
from app.campaign.ids import CAMPAIGN_KEY_PREFIX, job_id_for, member_id_for
from app.campaign.models import (
    CampaignClaim,
    CampaignExecutionConfig,
    CampaignStats,
    EnrollmentOutcome,
    EnrollmentResult,
    ExecutionOutcome,
    ExecutionResult,
    MemberView,
    ScheduleSummary,
)
from app.campaign.policy import CampaignBlock
from app.campaign.scheduler import CampaignScheduler
from app.campaign.worker import run_once

__all__ = [
    "CAMPAIGN_KEY_PREFIX",
    "CampaignBlock",
    "CampaignClaim",
    "CampaignEnroller",
    "CampaignExecutionConfig",
    "CampaignExecutor",
    "CampaignScheduler",
    "CampaignStats",
    "EnrollmentOutcome",
    "EnrollmentResult",
    "ExecutionOutcome",
    "ExecutionResult",
    "MemberView",
    "ScheduleSummary",
    "job_id_for",
    "member_id_for",
    "run_once",
]
