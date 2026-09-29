"""Idempotent enrollment of an already-known contact into a campaign. Sends nothing.

The membership identity is derived from (campaign, contact) and is UNIQUE in SQL, so a
repeated or concurrent request finds the existing membership. A contact that must not be
contacted (suppressed, bounced, already in an active lead or conversation, or who
declined before) is recorded as SKIPPED/SUPPRESSED rather than silently dropped, and gets
no lead. Eligibility is re-checked at draft, approval and dispatch time regardless.
"""

from app.core.enums import CampaignMemberStatus, CampaignStatus, LeadOrigin, LeadStage, RefKind
from app.core.models import CampaignMember, Lead
from app.campaign.audit import append_event, ref
from app.campaign.ids import lead_id_for, member_id_for
from app.campaign.models import EnrollmentOutcome, EnrollmentResult
from app.campaign.policy import ENROLLMENT_TERMINAL, CampaignBlock, enrollment_blockers
from app.persistence import Clock, Database

# Enrollment is allowed while the campaign is being prepared or running, never after it ended.
ENROLLABLE = frozenset({CampaignStatus.DRAFT, CampaignStatus.ACTIVE, CampaignStatus.PAUSED})


class CampaignEnroller:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def enroll(self, campaign_id: str, contact_id: str, *, correlation_id: str) -> EnrollmentResult:
        now = self._clock.now()
        member_id = member_id_for(campaign_id, contact_id)
        with self._db.transaction() as uow:
            existing = uow.campaign_members.get(member_id)
            if existing is not None:
                return EnrollmentResult(outcome=EnrollmentOutcome.ALREADY_ENROLLED, member_id=member_id, status=existing.status)
            campaign = uow.campaigns.get(campaign_id)
            if campaign is None or campaign.status not in ENROLLABLE:
                return EnrollmentResult(outcome=EnrollmentOutcome.REJECTED, reason_codes=(CampaignBlock.CAMPAIGN_ENDED,))
            contact = uow.contacts.get(contact_id)
            if contact is None:
                return EnrollmentResult(outcome=EnrollmentOutcome.REJECTED, reason_codes=(CampaignBlock.CONTACT_MISSING,))

            codes = enrollment_blockers(uow, campaign_id, contact_id, now)
            # Codes come in precedence order (suppression first).
            terminal = ENROLLMENT_TERMINAL[codes[0]] if codes else None
            lead_id: str | None = None
            if terminal is None:
                lead = Lead(
                    lead_id=lead_id_for(member_id), contact_id=contact_id, company_id=contact.company_id,
                    origin=LeadOrigin.OUTBOUND, campaign_id=campaign_id, stage=LeadStage.NEW, created_at=now, updated_at=now,
                )
                uow.leads.add(lead)
                lead_id = lead.lead_id
            member = CampaignMember(
                member_id=member_id, campaign_id=campaign_id, contact_id=contact_id, lead_id=lead_id,
                status=terminal or CampaignMemberStatus.ENROLLED, enrolled_at=now, last_activity_at=now,
                next_action_at=None if terminal else max(now, campaign.start_at or now),
                terminal_reason=codes[0] if terminal else None, created_at=now, updated_at=now,
            )
            uow.campaign_members.add(member)  # UNIQUE (campaign, contact); IMMEDIATE transactions serialize
            append_event(uow, key=(member_id,), event_type="CAMPAIGN_MEMBER_ENROLLED",
                         subjects=(ref(RefKind.CAMPAIGN_MEMBER, member_id), ref(RefKind.CAMPAIGN, campaign_id),
                                   ref(RefKind.PROSPECT_CONTACT, contact_id)),
                         after={"status": member.status.value, "reason_codes": codes, "lead_id": lead_id},
                         correlation_id=correlation_id, now=now)
        outcome = EnrollmentOutcome.ENROLLED if terminal is None else EnrollmentOutcome.ENROLLED_INELIGIBLE
        return EnrollmentResult(outcome=outcome, member_id=member_id, status=member.status, reason_codes=tuple(codes))
