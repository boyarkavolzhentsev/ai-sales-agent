"""Enrollment: one logical membership per (campaign, contact), idempotent, sends nothing."""

from app.campaign import CampaignBlock, EnrollmentOutcome, member_id_for
from app.core.enums import (
    CampaignMemberStatus,
    CampaignStatus,
    CloseReason,
    DNCReason,
    DNCScope,
    EmailValidity,
    LeadOrigin,
    LeadStage,
    RefKind,
)
from app.core.models import DoNotContactEntry, EntityRef, Lead
from app.persistence import Database
from tests.campaign.builders import CAMPAIGN_ID, PROSPECT, add_campaign, add_prospect, enroller, member
from tests.inbound.builders import NOW, envelope, happy_transport, process

M = CampaignMemberStatus


def rows(db: Database, table: str) -> int:
    with db.transaction() as uow:
        return int(uow._tx.fetch_all(f"SELECT COUNT(*) FROM {table}")[0][0])  # noqa: SLF001


def test_enrolling_a_contact_creates_one_membership_and_its_lead(db: Database) -> None:
    add_campaign(db)
    contact = add_prospect(db)
    result = enroller(db).enroll(CAMPAIGN_ID, contact.contact_id, correlation_id="c")
    assert (result.outcome, result.status, result.member_id) == (
        EnrollmentOutcome.ENROLLED, M.ENROLLED, member_id_for(CAMPAIGN_ID, contact.contact_id),
    )
    enrolled = member(db, result.member_id or "")
    with db.transaction() as uow:
        lead = uow.leads.get(enrolled.lead_id or "")
    assert lead is not None and (lead.origin, lead.stage, lead.campaign_id, lead.contact_id) == (
        LeadOrigin.OUTBOUND, LeadStage.NEW, CAMPAIGN_ID, contact.contact_id,
    )
    assert rows(db, "outbound_messages") == rows(db, "campaign_jobs") == 0  # enrollment sends and schedules nothing


def test_repeated_enrollment_is_idempotent(db: Database) -> None:
    add_campaign(db)
    contact = add_prospect(db)
    first = enroller(db).enroll(CAMPAIGN_ID, contact.contact_id, correlation_id="c1")
    again = enroller(db).enroll(CAMPAIGN_ID, contact.contact_id, correlation_id="c2")
    assert again.outcome is EnrollmentOutcome.ALREADY_ENROLLED and again.member_id == first.member_id
    assert rows(db, "campaign_members") == 1 and rows(db, "leads") == 1


def test_the_same_contact_across_campaigns_never_gets_parallel_outreach(db: Database) -> None:
    add_campaign(db, "camp-1")
    add_campaign(db, "camp-2")
    contact = add_prospect(db)
    other = add_prospect(db, "max@other-prospect.example", name="Max", company_name=None)
    assert enroller(db).enroll("camp-1", contact.contact_id, correlation_id="c").outcome is EnrollmentOutcome.ENROLLED
    second = enroller(db).enroll("camp-2", contact.contact_id, correlation_id="c")
    assert (second.outcome, second.status) == (EnrollmentOutcome.ENROLLED_INELIGIBLE, M.SKIPPED)
    assert CampaignBlock.ACTIVE_LEAD_EXISTS in second.reason_codes and member(db, second.member_id or "").lead_id is None
    assert enroller(db).enroll("camp-2", other.contact_id, correlation_id="c").outcome is EnrollmentOutcome.ENROLLED


def test_missing_contact_unknown_or_ended_campaign_fail_safely(db: Database) -> None:
    add_campaign(db)
    add_campaign(db, "camp-ended", status=CampaignStatus.ENDED)
    contact = add_prospect(db)
    assert enroller(db).enroll(CAMPAIGN_ID, "ct-missing", correlation_id="c").outcome is EnrollmentOutcome.REJECTED
    assert enroller(db).enroll("camp-unknown", contact.contact_id, correlation_id="c").outcome is EnrollmentOutcome.REJECTED
    assert enroller(db).enroll("camp-ended", contact.contact_id, correlation_id="c").outcome is EnrollmentOutcome.REJECTED
    assert rows(db, "campaign_members") == 0 and rows(db, "leads") == 0


def test_suppressed_or_bounced_contacts_are_recorded_but_never_given_a_lead(db: Database) -> None:
    add_campaign(db)
    suppressed = add_prospect(db)
    bounced = add_prospect(db, "old@bounced-prospect.example", company_name=None)
    with db.transaction() as uow:
        uow.dnc.add(DoNotContactEntry(entry_id="dnc-1", scope=DNCScope.EMAIL, value=PROSPECT, reason=DNCReason.OPERATOR,
                                      source_ref=EntityRef(kind=RefKind.OPERATOR_COMMAND, id="c"), created_by="op", created_at=NOW))
        stored = uow.contacts.get(bounced.contact_id)
        assert stored is not None
        uow.contacts.update(stored.model_copy(update={"email_validity": EmailValidity.BOUNCED, "version": 2}), 1)
    first = enroller(db).enroll(CAMPAIGN_ID, suppressed.contact_id, correlation_id="c")
    second = enroller(db).enroll(CAMPAIGN_ID, bounced.contact_id, correlation_id="c")
    assert (first.status, second.status) == (M.SUPPRESSED, M.SKIPPED)
    assert rows(db, "leads") == 0


def test_contact_in_an_active_conversation_or_who_declined_before_is_skipped(db: Database) -> None:
    add_campaign(db)
    # The prospect already wrote to us: an inbound lead and a conversation exist.
    process(db, happy_transport(), envelope("p-1", sender=PROSPECT))
    with db.transaction() as uow:
        contact = uow.contacts.get_by_email(PROSPECT)
    assert contact is not None
    result = enroller(db).enroll(CAMPAIGN_ID, contact.contact_id, correlation_id="c")
    assert result.status is M.SKIPPED and CampaignBlock.CONVERSATION_ACTIVE in result.reason_codes

    declined = add_prospect(db, "no@declined-prospect.example", company_name=None)
    with db.transaction() as uow:
        uow.leads.add(Lead(lead_id="ld-old", contact_id=declined.contact_id, origin=LeadOrigin.OUTBOUND, stage=LeadStage.CLOSED,
                           close_reason=CloseReason.NOT_INTERESTED, created_at=NOW, updated_at=NOW))
    skipped = enroller(db).enroll(CAMPAIGN_ID, declined.contact_id, correlation_id="c")
    assert skipped.status is M.SKIPPED and skipped.reason_codes == (CampaignBlock.PREVIOUSLY_DECLINED,)
