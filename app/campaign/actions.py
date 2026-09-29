"""Campaign- and recipient-level control, applied inside an operator command's transaction
(authorization, idempotency, version checks and the command audit are Stage 7's).

- activate: DRAFT -> ACTIVE (the only way a campaign becomes executable).
- pause: ACTIVE -> PAUSED; open jobs are CANCELLED so no newly claimed job can run (a job
  claimed before the pause is refused by execution-time revalidation); drafts and
  in-flight Stage 8 attempts are left as they are (dispatch re-checks the campaign).
- resume: PAUSED -> ACTIVE; the scheduler reopens work only for memberships still in
  sequence (terminal ones are never resurrected).
- cancel: -> ENDED; every membership still in sequence becomes CANCELLED, its open job,
  undispatched drafts (releasing quota) and FollowUpPlan are cancelled. Accepted sends and
  in-flight hand-offs are history and are not rewritten.
- complete: -> ENDED once no membership is still in sequence.
"""

from datetime import datetime

from app.core.enums import CampaignJobStatus, CampaignMemberStatus, CampaignStatus, FollowUpCancelReason
from app.core.models import Campaign, CampaignMember
from app.campaign.state import IN_SEQUENCE, end_open_job, record_suppressed, stop_member
from app.conversation.actions import suppress_contact
from app.persistence import UnitOfWork

M = CampaignMemberStatus


def _save_campaign(uow: UnitOfWork, campaign: Campaign, now: datetime, **changes: object) -> Campaign:
    updated = Campaign.model_validate(
        campaign.model_dump() | changes | {"updated_at": max(now, campaign.updated_at), "version": campaign.version + 1}
    )
    uow.campaigns.update(updated, campaign.version)
    return updated


def activate(uow: UnitOfWork, campaign: Campaign, *, operator_id: str, now: datetime) -> Campaign:
    return _save_campaign(uow, campaign, now, status=CampaignStatus.ACTIVE, activated_by=operator_id)


def pause(uow: UnitOfWork, campaign: Campaign, *, correlation_id: str, now: datetime) -> Campaign:
    for member in uow.campaign_members.list_by_campaign(campaign.campaign_id):
        end_open_job(uow, member, CampaignJobStatus.CANCELLED, "CAMPAIGN_PAUSED", correlation_id=correlation_id, now=now)
    return _save_campaign(uow, campaign, now, status=CampaignStatus.PAUSED)


def resume(uow: UnitOfWork, campaign: Campaign, *, now: datetime) -> Campaign:
    return _save_campaign(uow, campaign, now, status=CampaignStatus.ACTIVE)


def cancel(uow: UnitOfWork, campaign: Campaign, *, correlation_id: str, now: datetime) -> tuple[Campaign, list[str]]:
    stopped: list[str] = []
    for member in uow.campaign_members.list_by_campaign(campaign.campaign_id):
        if member.status in IN_SEQUENCE:
            stop_member(uow, member, M.CANCELLED, "CAMPAIGN_CANCELLED", plan_reason=FollowUpCancelReason.CAMPAIGN_ENDED,
                        correlation_id=correlation_id, now=now)
            stopped.append(member.member_id)
    return _save_campaign(uow, campaign, now, status=CampaignStatus.ENDED), stopped


def in_sequence(uow: UnitOfWork, campaign_id: str) -> list[CampaignMember]:
    return [m for m in uow.campaign_members.list_by_campaign(campaign_id) if m.status in IN_SEQUENCE]


def complete(uow: UnitOfWork, campaign: Campaign, *, now: datetime) -> Campaign:
    return _save_campaign(uow, campaign, now, status=CampaignStatus.ENDED)


def cancel_member(uow: UnitOfWork, member: CampaignMember, *, correlation_id: str, now: datetime) -> CampaignMember:
    return stop_member(uow, member, M.CANCELLED, "OPERATOR_CANCELLED", plan_reason=FollowUpCancelReason.OPERATOR,
                       correlation_id=correlation_id, now=now)


def suppress_member(
    uow: UnitOfWork, member: CampaignMember, *, operator_id: str, command_id: str, correlation_id: str, now: datetime
) -> tuple[str | None, list[str]]:
    """Contact-level do-not-contact (the same path as Stage 9), then every membership of
    the contact becomes SUPPRESSED."""
    result = suppress_contact(uow, member.contact_id, operator_id=operator_id, command_id=command_id,
                              correlation_id=correlation_id, now=now)
    record_suppressed(uow, member.contact_id, correlation_id=correlation_id, now=now)
    return result
