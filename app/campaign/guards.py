"""Campaign checks at approval (Stage 7) and dispatch (Stage 8) time.

A campaign draft is approvable only while its membership is DRAFTED, and dispatchable
only while it is APPROVED, the campaign is ACTIVE, and the contact-level rules still
hold: no suppression or bounce, no active conversation, no other unresolved, pending or
conflicting message to the contact, an open lead that is not held, owned or escalated,
and, for a follow-up touch, an ACTIVE campaign FollowUpPlan."""

from datetime import datetime

from app.core.enums import CampaignMemberStatus, FollowUpStatus, OutboundKind
from app.core.models import OutboundMessage
from app.campaign.policy import CampaignBlock, campaign_blockers, contact_blockers, lead_blockers, load_contact_facts
from app.campaign.state import is_campaign_message, member_for
from app.persistence import UnitOfWork


def campaign_blockers_for(uow: UnitOfWork, outbound: OutboundMessage, now: datetime, *, expected: CampaignMemberStatus) -> list[str]:
    if not is_campaign_message(outbound):
        return []
    member = member_for(uow, outbound)
    if member is None or member.status is not expected:
        return [CampaignBlock.MEMBER_NOT_READY]
    campaign = uow.campaigns.get(member.campaign_id)
    codes = campaign_blockers(campaign.status if campaign else None)
    codes += contact_blockers(load_contact_facts(uow, member.contact_id), now, own_outbound_id=outbound.outbound_id)
    codes += lead_blockers(uow, member)
    if outbound.kind is OutboundKind.FOLLOW_UP:
        plan = uow.follow_ups.get_open_for_lead(member.lead_id or "")
        if plan is None or plan.campaign_id != member.campaign_id or plan.status is not FollowUpStatus.ACTIVE:
            codes.append(CampaignBlock.NO_FOLLOW_UP_PLAN)
    return list(dict.fromkeys(codes))
