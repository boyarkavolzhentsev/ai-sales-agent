"""Read models derived from durable membership and job state on every read."""

from collections import Counter

from app.core.enums import CampaignMemberStatus
from app.core.models import CampaignMember
from app.campaign.models import CampaignStats, MemberView
from app.persistence import UnitOfWork

M = CampaignMemberStatus


def member_view(uow: UnitOfWork, member: CampaignMember) -> MemberView:
    jobs = uow.campaign_jobs.list_for_member(member.member_id)
    latest = jobs[-1] if jobs else None
    return MemberView(
        member_id=member.member_id, contact_id=member.contact_id, lead_id=member.lead_id, status=member.status,
        touch_count=member.touch_count, latest_outbound_id=member.latest_outbound_id, next_action_at=member.next_action_at,
        terminal_reason=member.terminal_reason, latest_touch_no=latest.touch_no if latest else None,
        latest_job_status=latest.status if latest else None, version=member.version,
    )


def campaign_stats(uow: UnitOfWork, campaign_id: str) -> CampaignStats | None:
    campaign = uow.campaigns.get(campaign_id)
    if campaign is None:
        return None
    members = uow.campaign_members.list_by_campaign(campaign_id)
    counts = Counter(m.status for m in members)
    return CampaignStats(
        campaign_id=campaign_id, campaign_status=campaign.status, total_enrolled=len(members),
        by_status={status.value: counts.get(status, 0) for status in M},
        pending=counts[M.ENROLLED], awaiting_review=counts[M.DRAFTED], approved=counts[M.APPROVED],
        dispatching=counts[M.DISPATCHING], waiting=counts[M.WAITING], replied=counts[M.REPLIED],
        converted=counts[M.CONVERTED], completed=counts[M.COMPLETED],
        suppressed_or_skipped=counts[M.SUPPRESSED] + counts[M.SKIPPED], failed_or_cancelled=counts[M.FAILED] + counts[M.CANCELLED],
        touches_accepted=sum(m.touch_count for m in members), open_jobs=len(uow.campaign_jobs.list_open_for_campaign(campaign_id)),
    )
