"""Draft workflow: one draft per logical touch, persisted facts only, operator review."""

import pytest

from app.campaign import ExecutionOutcome
from app.core.enums import CampaignJobStatus, CampaignMemberStatus, KnowledgeDomain, OutboundKind, OutboundStatus, RefKind
from app.core.models import EntityRef
from app.knowledge import ingest_loaded, parse_source_text
from app.operator import BlockCode, CommandRejectedError
from app.persistence import Database, FrozenClock
from tests.campaign.builders import (
    CAMPAIGN_ID,
    activate,
    add_campaign,
    campaign_messages,
    claim_all,
    draft_touch,
    enrolled,
    executor,
    member,
    outbound,
    ready_campaign,
    scheduler,
)
from tests.inbound.builders import NOW
from tests.knowledge.sources import meta, yaml_doc
from tests.operator.builders import AS_ALICE, approve_command, operator, reject_command

M = CampaignMemberStatus


def draft_event(db: Database, outbound_id: str) -> dict[str, object]:
    with db.transaction() as uow:
        events = uow.audit.list_for_subject(EntityRef(kind=RefKind.OUTBOUND_MESSAGE, id=outbound_id))
    [event] = [e for e in events if e.event_type == "DRAFT_CREATED"]
    assert event.after is not None
    return dict(event.after)


def test_one_draft_per_touch_and_repeated_runs_create_nothing_new(db: Database) -> None:
    member_id = ready_campaign(db)
    result = draft_touch(db)
    assert member(db, member_id).status is M.DRAFTED
    clock = FrozenClock(NOW)
    assert scheduler(db, clock).schedule(CAMPAIGN_ID, correlation_id="again").scheduled == ()
    assert claim_all(db, clock) == ()
    lead = member(db, member_id).lead_id or ""
    [draft] = campaign_messages(db, lead)
    assert (draft.outbound_id, draft.kind, draft.status, draft.sequence_no) == (result.outbound_id, OutboundKind.FIRST_TOUCH, OutboundStatus.DRAFTED, 0)


def test_personalization_uses_only_persisted_facts(db: Database) -> None:
    add_campaign(db)
    activate(db)
    member_id = enrolled(db)
    contact_id = member(db, member_id).contact_id
    with db.transaction() as uow:
        contact = uow.contacts.get(contact_id)
        assert contact is not None
        # Stored but never used for claims: role, industry, size.
        uow.contacts.update(contact.model_copy(update={"role_title": "Chief Technology Officer", "version": 2}), 1)
        company = uow.companies.get(contact.company_id or "")
        assert company is not None
        uow.companies.update(company.model_copy(update={"industry": "Fintech", "size_band": "50-200", "version": 2}), 1)
    body = outbound(db, draft_touch(db).outbound_id or "").body_final
    assert "Hello Lena," in body and "Acme Prospect Ltd" in body
    assert "The Sample Widget integrates with spreadsheet exports and a REST webhook." in body  # quoted approved evidence
    for fabricated in ("Chief Technology Officer", "Fintech", "50-200", "budget", "pain", "revenue"):
        assert fabricated not in body
    event = draft_event(db, member(db, member_id).latest_outbound_id or outbound_id_of(db, member_id))
    assert event["personalization_fields"] == ["contact.name", "company.name"] and event["evidence_ids_used"]


def outbound_id_of(db: Database, member_id: str) -> str:
    [draft] = campaign_messages(db, member(db, member_id).lead_id or "")
    return draft.outbound_id


def test_missing_facts_and_knowledge_degrade_to_generic_wording(db: Database) -> None:
    add_campaign(db, domains=(KnowledgeDomain.OBJECTIONS,))  # only an unapproved draft here: nothing usable
    activate(db)
    member_id = enrolled(db, "anon@nameless-prospect.example", name=None, company_name=None)
    result = draft_touch(db)
    body = outbound(db, result.outbound_id or "").body_final
    assert body.startswith("Hello,\n") and " at " not in body.split("\n")[2] and "Sample Widget" not in body
    event = draft_event(db, result.outbound_id or "")
    assert event["evidence_ids_used"] == [] and event["personalization_fields"] == [] and event["query"] is None
    assert member(db, member_id).status is M.DRAFTED


def test_campaign_draft_enters_operator_review_with_its_provenance(db: Database) -> None:
    ready_campaign(db)
    result = draft_touch(db)
    service = operator(db)
    assert [d.outbound_id for d in service.list_pending_drafts(AS_ALICE)] == [result.outbound_id]
    detail = service.get_draft(AS_ALICE, result.outbound_id or "")
    assert detail.actionable and detail.customer is None
    assert [e.source_id for e in detail.evidence] == ["sample-product-widget"] and all(e.usable_now for e in detail.evidence)


def test_operator_rejection_ends_the_membership_deterministically(db: Database) -> None:
    member_id = ready_campaign(db)
    result = draft_touch(db)
    service = operator(db)
    service.reject_draft(AS_ALICE, reject_command(service.get_draft(AS_ALICE, result.outbound_id or ""), "cmd-reject"))
    rejected = member(db, member_id)
    assert (rejected.status, rejected.terminal_reason) == (M.CANCELLED, "OPERATOR_REJECTED")
    assert scheduler(db).schedule(CAMPAIGN_ID, correlation_id="again").scheduled == ()
    with db.transaction() as uow:
        assert [j.status for j in uow.campaign_jobs.list_for_member(member_id)] == [CampaignJobStatus.COMPLETED]


def test_evidence_retired_after_drafting_blocks_approval(db: Database) -> None:
    ready_campaign(db)
    result = draft_touch(db)
    text = yaml_doc(meta(source_id="sample-product-widget", domain="PRODUCTS_SERVICES", version=2, approval_status="RETIRED",
                         effective_from="2026-02-01T00:00:00+00:00", review_by="2026-12-31T23:59:59+00:00"),
                    body="## Retired\nThis sheet is retired.")
    with db.transaction() as uow:
        ingest_loaded(uow, parse_source_text(text, extension=".yaml", label="products/retired.yaml"), now=NOW)
    service = operator(db)
    detail = service.get_draft(AS_ALICE, result.outbound_id or "")
    assert BlockCode.EVIDENCE_UNUSABLE in detail.blockers
    with pytest.raises(CommandRejectedError) as error:
        service.approve_draft(AS_ALICE, approve_command(detail, "cmd-approve"))
    assert BlockCode.EVIDENCE_UNUSABLE in error.value.codes


def test_executing_the_same_claim_twice_is_a_replay(db: Database) -> None:
    ready_campaign(db)
    clock = FrozenClock(NOW)
    scheduler(db, clock).schedule(CAMPAIGN_ID, correlation_id="c")
    [claim] = claim_all(db, clock)
    first = executor(db, clock).execute(claim, correlation_id="c1")
    again = executor(db, clock).execute(claim, correlation_id="c2")
    assert (first.outcome, again.outcome) == (ExecutionOutcome.DRAFT_CREATED, ExecutionOutcome.REPLAYED)
    assert again.outbound_id == first.outbound_id
