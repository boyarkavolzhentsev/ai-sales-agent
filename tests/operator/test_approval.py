"""Approval: exact binding, revalidation against current state, and no sending effects."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.core.enums import (
    ActorType,
    CampaignStatus,
    CloseReason,
    DNCReason,
    DNCScope,
    KnowledgeDomain,
    LeadIntent,
    LeadStage,
    LeadStatus,
    OutboundDecision,
    OutboundStatus,
    RefKind,
)
from app.core.models import Campaign, CampaignTargetFilter, DoNotContactEntry, EntityRef, OutboundMessage
from app.knowledge import ingest_loaded, parse_source_text
from app.llm import LLMTask
from app.operator import BlockCode, CommandRejectedError, OperatorService, StaleCommandError
from app.persistence import Database, FrozenClock
from app.persistence.serialization import dumps_json
from app.policy.errors import PolicyError
from app.policy.reservation import reserve_quota
from tests.inbound.builders import NOW, SENDER, ScriptedTransport, classification, envelope, process
from tests.inbound.test_threads_and_transitions import outbound_history
from tests.knowledge.sources import meta, yaml_doc
from tests.operator.builders import (
    ALICE,
    AS_ALICE,
    approve_command,
    make_draft,
    operator,
    operator_events,
    outbound,
    ownership_command,
    snapshot,
)
from tests.policy import builders as policy


def prepared(db: Database, clock: FrozenClock | None = None, **envelope_overrides: object) -> tuple[OperatorService, str]:
    draft = make_draft(db, **envelope_overrides)
    return operator(db, clock), draft.outbound_id or ""


def approve_now(service: OperatorService, outbound_id: str, command_id: str = "cmd-approve") -> None:
    service.approve_draft(AS_ALICE, approve_command(service.get_draft(AS_ALICE, outbound_id), command_id))


def rejected_codes(service: OperatorService, outbound_id: str) -> set[BlockCode]:
    detail = service.get_draft(AS_ALICE, outbound_id)
    with pytest.raises(CommandRejectedError) as error:
        service.approve_draft(AS_ALICE, approve_command(detail))
    assert set(detail.blockers) == set(error.value.codes)  # the read model predicts the command
    return set(error.value.codes)


# ---- Success ----------------------------------------------------------------------------------


def test_approval_records_a_human_decision_only(db: Database) -> None:
    service, outbound_id = prepared(db)
    detail = service.get_draft(AS_ALICE, outbound_id)
    result = service.approve_draft(AS_ALICE, approve_command(detail))

    approved = outbound(db, outbound_id)
    assert approved.status is OutboundStatus.OPERATOR_APPROVED and approved.decision is OutboundDecision.SEND
    assert approved.approved_at == NOW and approved.version == detail.version + 1
    assert approved.send_permit_id is None and approved.sending_at is None and approved.sent_at is None
    assert (approved.subject, approved.body_final, approved.content_hash) == (
        detail.generated_draft.subject, detail.generated_draft.body, detail.content_hash,
    )
    with db.transaction() as uow:
        assert uow.quota_reservations.get_live_for_outbound(outbound_id) is None
        assert uow.outbound.list_by_status(OutboundStatus.SENDING) == []

    outcome = result.outcome
    assert not result.replayed and outcome.operator_id == ALICE and outcome.disposition == "OPERATOR_APPROVED"
    [change] = outcome.versions
    assert (change.entity, change.expected, change.resulting) == (
        EntityRef(kind=RefKind.OUTBOUND_MESSAGE, id=outbound_id), detail.version, detail.version + 1,
    )
    [event] = operator_events(db, "cmd-approve")
    assert event.actor.type is ActorType.OPERATOR and event.actor.id == ALICE
    assert event.event_type == "OPERATOR_APPROVE_DRAFT" and event.correlation_id == "corr-cmd-approve"
    assert EntityRef(kind=RefKind.MESSAGE_DRAFT, id=detail.draft_id) in event.subject_refs
    audit_text = dumps_json({"before": event.before, "after": event.after})
    assert "100 EUR" not in audit_text and "Basic plan" not in audit_text and "tok-alice" not in audit_text
    # Once approved it is no longer pending review, and it cannot be approved twice.
    assert service.list_pending_drafts(AS_ALICE) == ()
    with pytest.raises(StaleCommandError):
        service.approve_draft(AS_ALICE, approve_command(detail, "cmd-approve-2"))


def test_human_approved_message_cannot_reserve_quota_or_carry_a_permit(db: Database) -> None:
    service, outbound_id = prepared(db)
    approve_now(service, outbound_id)
    approved = outbound(db, outbound_id)
    with db.transaction() as uow, pytest.raises(PolicyError, match="only APPROVED"):
        reserve_quota(uow, policy.limits(sends=5), approved, reservation_id="r-1", now=NOW)
    with pytest.raises(ValidationError, match="must not carry a send_permit_id"):
        OutboundMessage.model_validate(approved.model_dump() | {"send_permit_id": "permit-1"})
    with pytest.raises(ValidationError, match="requires approved_at"):
        OutboundMessage.model_validate(approved.model_dump() | {"approved_at": None})


def test_operator_owned_lead_can_still_be_approved_by_a_human(db: Database) -> None:
    service, outbound_id = prepared(db)
    lead = service.get_draft(AS_ALICE, outbound_id).lead
    assert lead is not None
    service.take_ownership(AS_ALICE, ownership_command(lead.lead_id, lead.version))
    approve_now(service, outbound_id)
    assert outbound(db, outbound_id).status is OutboundStatus.OPERATOR_APPROVED


# ---- Binding to the reviewed artifact ------------------------------------------------------------


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"expected_outbound_version": 2}, BlockCode.DRAFT_VERSION_CHANGED),
        ({"draft_id": "dr_other"}, BlockCode.DRAFT_IDENTITY_MISMATCH),
        ({"content_hash": "0" * 64}, BlockCode.DRAFT_CONTENT_CHANGED),
        ({"expected_lead_version": 1}, BlockCode.LEAD_VERSION_CHANGED),
    ],
)
def test_stale_or_mismatched_commands_are_rejected_without_writes(db: Database, override: dict[str, object], code: BlockCode) -> None:
    service, outbound_id = prepared(db)
    detail = service.get_draft(AS_ALICE, outbound_id)
    assert detail.lead is not None and detail.lead.version != 1  # Stage 6 moved the new lead once
    before = snapshot(db)
    with pytest.raises(StaleCommandError) as error:
        service.approve_draft(AS_ALICE, approve_command(detail, **override))
    assert code in error.value.codes
    assert snapshot(db) == before


def test_content_changed_in_storage_fails_the_integrity_check(db: Database) -> None:
    service, outbound_id = prepared(db)
    current = outbound(db, outbound_id)
    with db.transaction() as uow:
        uow.outbound.update(current.model_copy(update={"body_final": "The Basic plan costs 10 EUR.", "version": 2}), 1)
    assert BlockCode.DRAFT_INTEGRITY_FAILED in rejected_codes(service, outbound_id)


# ---- Revalidation of current state ----------------------------------------------------------------


def add_dnc(db: Database, scope: DNCScope, value: str) -> None:
    with db.transaction() as uow:
        uow.dnc.add(DoNotContactEntry(entry_id=f"dnc-{scope}", scope=scope, value=value, reason=DNCReason.OPERATOR,
                                      source_ref=EntityRef(kind=RefKind.OPERATOR_COMMAND, id="cmd-x"), created_by="op", created_at=NOW))


@pytest.mark.parametrize(("scope", "value"), [(DNCScope.EMAIL, SENDER), (DNCScope.DOMAIN, "prospect.example")])
def test_suppressed_contact_blocks_approval(db: Database, scope: DNCScope, value: str) -> None:
    service, outbound_id = prepared(db)
    add_dnc(db, scope, value)
    assert rejected_codes(service, outbound_id) == {BlockCode.CONTACT_SUPPRESSED}


def set_lead(db: Database, lead_id: str, **changes: object) -> None:
    with db.transaction() as uow:
        lead = uow.leads.get(lead_id)
        assert lead is not None
        uow.leads.update(lead.model_copy(update=changes | {"version": lead.version + 1}), lead.version)


def test_closed_lead_blocks_approval(db: Database) -> None:
    service, outbound_id = prepared(db)
    set_lead(db, outbound(db, outbound_id).lead_id, stage=LeadStage.CLOSED, close_reason=CloseReason.LOST)
    assert BlockCode.LEAD_CLOSED in rejected_codes(service, outbound_id)


def test_held_lead_blocks_approval_until_explicitly_taken_over(db: Database) -> None:
    service, outbound_id = prepared(db)
    lead_id = outbound(db, outbound_id).lead_id
    set_lead(db, lead_id, status=LeadStatus.ON_HOLD)
    assert rejected_codes(service, outbound_id) == {BlockCode.LEAD_ON_HOLD}
    lead = service.get_lead(AS_ALICE, lead_id)
    service.take_ownership(AS_ALICE, ownership_command(lead_id, lead.version))
    approve_now(service, outbound_id)


def test_paused_campaign_blocks_approval(db: Database) -> None:
    outbound_history(db)
    draft = make_draft(db, in_reply_to="<out-1@ourco.example>")
    campaign = Campaign(campaign_id="camp-x", name="Sample", status=CampaignStatus.ACTIVE, activated_by="op",
                        target_filter=CampaignTargetFilter(), allowed_knowledge_domains=(KnowledgeDomain.COMPANY,),
                        sending_mailbox="sales@ourco.example", max_follow_ups=1, min_interval_between_follow_ups=timedelta(days=2),
                        created_by="op", created_at=NOW, updated_at=NOW)
    with db.transaction() as uow:
        uow.campaigns.add(campaign)
    set_lead(db, "lead-out", campaign_id="camp-x")
    service = operator(db)
    assert service.get_draft(AS_ALICE, draft.outbound_id or "").actionable
    with db.transaction() as uow:
        uow.campaigns.update(campaign.model_copy(update={"status": CampaignStatus.PAUSED, "version": 2}), 1)
    assert rejected_codes(service, draft.outbound_id or "") == {BlockCode.CAMPAIGN_INACTIVE}


def test_retired_evidence_blocks_approval(db: Database) -> None:
    service, outbound_id = prepared(db)
    text = yaml_doc(meta(source_id="sample-price-list", domain="PRICING_COMMERCIAL", version=3, approval_status="RETIRED",
                         effective_from="2026-02-01T00:00:00+00:00", review_by="2026-08-01T00:00:00+00:00"),
                    body="## Retired\nThis price list is retired.")
    with db.transaction() as uow:
        ingest_loaded(uow, parse_source_text(text, extension=".yaml", label="pricing/retired.yaml"), now=NOW)
    assert rejected_codes(service, outbound_id) == {BlockCode.EVIDENCE_UNUSABLE}
    assert not any(e.usable_now for e in service.get_draft(AS_ALICE, outbound_id).evidence)


def test_evidence_validity_uses_the_injected_clock(db: Database) -> None:
    clock = FrozenClock(NOW)
    service, outbound_id = prepared(db, clock)
    assert service.get_draft(AS_ALICE, outbound_id).actionable
    clock.set(NOW.replace(month=8, day=2))  # price list review_by is 2026-08-01
    assert rejected_codes(service, outbound_id) == {BlockCode.EVIDENCE_UNUSABLE}


def test_newer_customer_message_makes_the_draft_stale(db: Database) -> None:
    service, outbound_id = prepared(db)
    process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.NEGOTIATION)),
            envelope("p-2", body="Actually, can we negotiate?", in_reply_to="<p-1@prospect.example>"))
    assert BlockCode.NEWER_INBOUND_MESSAGE in rejected_codes(service, outbound_id)
