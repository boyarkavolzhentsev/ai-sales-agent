"""Campaign scheduling and claiming.

schedule(): one IMMEDIATE transaction per pass. For an ACTIVE campaign it opens the next
logical touch of every membership that is ready for one:
- ENROLLED -> touch 1 (the first touch), due at the membership's next action time;
- WAITING with an ACTIVE campaign FollowUpPlan that has steps left -> the next touch,
  due at the plan's ``next_due_at``;
- WAITING whose plan sent every step and whose final wait is over -> the plan becomes
  EXHAUSTED, the membership COMPLETED and a still-CONTACTED lead CLOSED(NO_RESPONSE).
A touch's identity is "touch N of membership M", so repeated or concurrent passes open it
once; SQL allows one open job per membership. A touch that already produced its draft, or
that was blocked for an operator, is never opened again; a cancelled or superseded one
(which produced no draft) may be reopened, e.g. after a campaign resumes.

claim_due(): leases due jobs and expired leases, like Stage 9 (fresh token per claim).
"""

from datetime import datetime

from app.core.decisions import is_allowed_lead_transition
from app.core.enums import (
    CampaignJobStatus,
    CampaignMemberStatus,
    CampaignStatus,
    CloseReason,
    FollowUpCancelReason,
    FollowUpStatus,
    LeadStage,
    LeadStatus,
    RefKind,
)
from app.core.models import CampaignJob, CampaignMember, FollowUpPlan, Lead
from app.campaign.audit import append_event, ref
from app.campaign.ids import job_id_for, stable_id
from app.campaign.models import CampaignClaim, CampaignExecutionConfig, ScheduleSummary
from app.campaign.policy import CampaignBlock
from app.campaign.state import save_member, stop_member
from app.persistence import Clock, Database, UnitOfWork

M = CampaignMemberStatus


class CampaignScheduler:
    def __init__(self, db: Database, clock: Clock, config: CampaignExecutionConfig) -> None:
        self._db = db
        self._clock = clock
        self._config = config

    def schedule(self, campaign_id: str, *, correlation_id: str) -> ScheduleSummary:
        now = self._clock.now()
        scheduled: list[str] = []
        exhausted: list[str] = []
        with self._db.transaction() as uow:
            campaign = uow.campaigns.get(campaign_id)
            if campaign is None or campaign.status is not CampaignStatus.ACTIVE:
                reason = CampaignBlock.CAMPAIGN_ENDED if campaign is None or campaign.status is CampaignStatus.ENDED else CampaignBlock.CAMPAIGN_NOT_ACTIVE
                return ScheduleSummary(campaign_id=campaign_id, blocked_reason=reason)
            for member in uow.campaign_members.list_by_campaign(campaign_id):
                next_touch = self._next_touch(uow, member, now, correlation_id, exhausted)
                if next_touch is None:
                    continue
                touch_no, due_at = next_touch
                job_id = self._open(uow, member, touch_no, due_at, correlation_id, now)
                if job_id is not None:
                    scheduled.append(job_id)
        return ScheduleSummary(campaign_id=campaign_id, scheduled=tuple(scheduled), exhausted=tuple(exhausted))

    def _next_touch(
        self, uow: UnitOfWork, member: CampaignMember, now: datetime, correlation_id: str, exhausted: list[str]
    ) -> tuple[int, datetime] | None:
        if member.status is M.ENROLLED:
            return 1, member.next_action_at or now
        if member.status is not M.WAITING or member.lead_id is None:
            return None
        plan = uow.follow_ups.get_open_for_lead(member.lead_id)
        if plan is None or plan.campaign_id != member.campaign_id or plan.status is not FollowUpStatus.ACTIVE or plan.next_due_at is None:
            return None
        if plan.steps_sent >= plan.max_steps:
            if plan.next_due_at <= now:
                self._exhaust(uow, member, plan, correlation_id, now)
                exhausted.append(member.member_id)
            return None
        return member.touch_count + 1, plan.next_due_at

    def _open(self, uow: UnitOfWork, member: CampaignMember, touch_no: int, due_at: datetime, correlation_id: str, now: datetime) -> str | None:
        if uow.campaign_jobs.get_open_for_member(member.member_id) is not None:
            return None
        job_id = job_id_for(member.member_id, touch_no)
        existing = uow.campaign_jobs.get(job_id)
        if existing is not None and existing.status in (CampaignJobStatus.COMPLETED, CampaignJobStatus.BLOCKED):
            # COMPLETED: that touch already produced its draft. BLOCKED: it needs an operator
            # (e.g. late-acceptance conflict evidence); reopening it would only loop.
            return None
        member = save_member(uow, member, next_action_at=due_at, correlation_id=correlation_id, now=now)
        if existing is None:
            job = CampaignJob(job_id=job_id, member_id=member.member_id, campaign_id=member.campaign_id, touch_no=touch_no,
                              basis_member_version=member.version, due_at=due_at, created_at=now, updated_at=now)
            uow.campaign_jobs.add(job)
        else:
            job = CampaignJob.model_validate(
                existing.model_dump()
                | {"status": CampaignJobStatus.SCHEDULED, "block_codes": (), "reason": None, "basis_member_version": member.version,
                   "due_at": due_at, "updated_at": max(now, existing.updated_at), "version": existing.version + 1}
            )
            uow.campaign_jobs.update(job, existing.version)
        append_event(uow, key=(job_id, str(job.version)), event_type="CAMPAIGN_JOB_SCHEDULED",
                     subjects=(ref(RefKind.CAMPAIGN_JOB, job_id), ref(RefKind.CAMPAIGN_MEMBER, member.member_id)),
                     after={"touch_no": touch_no, "due_at": due_at.isoformat(), "basis_member_version": member.version},
                     correlation_id=correlation_id, now=now)
        return job_id

    @staticmethod
    def _exhaust(uow: UnitOfWork, member: CampaignMember, plan: FollowUpPlan, correlation_id: str, now: datetime) -> None:
        uow.follow_ups.update(FollowUpPlan.model_validate(
            plan.model_dump() | {"status": FollowUpStatus.EXHAUSTED, "next_due_at": None, "updated_at": max(now, plan.updated_at),
                                 "version": plan.version + 1}
        ), plan.version)
        stop_member(uow, member, M.COMPLETED, "NO_RESPONSE", plan_reason=FollowUpCancelReason.CAMPAIGN_ENDED,
                    correlation_id=correlation_id, now=now)
        lead = uow.leads.get(plan.lead_id)
        if (
            lead is not None and lead.status is LeadStatus.AUTOMATED
            and is_allowed_lead_transition(lead.stage, LeadStage.CLOSED, CloseReason.NO_RESPONSE)
        ):
            uow.leads.update(Lead.model_validate(
                lead.model_dump() | {"stage": LeadStage.CLOSED, "close_reason": CloseReason.NO_RESPONSE,
                                     "updated_at": max(now, lead.updated_at), "version": lead.version + 1}
            ), lead.version)

    def claim_due(self, worker_id: str, *, correlation_id: str, limit: int = 10) -> tuple[CampaignClaim, ...]:
        now = self._clock.now()
        claims: list[CampaignClaim] = []
        with self._db.transaction() as uow:
            for job in uow.campaign_jobs.list_claimable(now, limit):
                recovered = job.status is CampaignJobStatus.CLAIMED
                token = stable_id("cc", job.job_id, str(job.claim_count + 1))
                lease = now + self._config.lease
                claimed = CampaignJob.model_validate(
                    job.model_dump()
                    | {"status": CampaignJobStatus.CLAIMED, "claim_token": token, "claimed_by": worker_id, "lease_expires_at": lease,
                       "claim_count": job.claim_count + 1, "updated_at": max(now, job.updated_at), "version": job.version + 1}
                )
                uow.campaign_jobs.update(claimed, job.version)
                append_event(uow, key=(job.job_id, str(claimed.version)), event_type="CAMPAIGN_JOB_CLAIMED",
                             subjects=(ref(RefKind.CAMPAIGN_JOB, job.job_id), ref(RefKind.CAMPAIGN_MEMBER, job.member_id)),
                             after={"worker_id": worker_id, "claim_no": claimed.claim_count, "recovered": recovered},
                             correlation_id=correlation_id, now=now)
                claims.append(CampaignClaim(job_id=job.job_id, member_id=job.member_id, claim_token=token, claimed_by=worker_id,
                                            lease_expires_at=lease, recovered=recovered))
        return tuple(claims)
