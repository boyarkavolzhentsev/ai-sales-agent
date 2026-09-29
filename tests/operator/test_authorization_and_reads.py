"""Authorization boundary and read models."""

from collections.abc import Callable

import pytest

from app.core.enums import (
    EmailDirection,
    EscalationReason,
    EscalationStatus,
    KnowledgeDecision,
    LeadIntent,
    OutboundStatus,
    ReplyDecision,
)
from app.llm import LLMTask
from app.operator import BlockCode, OperatorService, OperatorUnauthorizedError
from app.persistence import Database
from tests.inbound.builders import SENDER, ScriptedTransport, classification, envelope, process
from tests.operator.builders import (
    ALICE,
    AS_ALICE,
    AS_BOB,
    AS_MALLORY,
    ExplodingAuthenticator,
    approve_command,
    credential,
    make_draft,
    make_escalation,
    operator,
    outbound,
    ownership_command,
    reject_command,
    resolve_command,
    snapshot,
)

# ---- Authorization -------------------------------------------------------------------------------

UNAUTHORIZED = {
    "unknown token": credential("tok-unknown"),
    "authenticated but not allow-listed": AS_MALLORY,
    "wrong scheme": credential("tok-alice", scheme="telegram"),
    "raw operator id instead of credential": ALICE,
    "no credential": None,
}


def reads(service: OperatorService, outbound_id: str, escalation_id: str, lead_id: str, thread_id: str) -> list[Callable[[object], object]]:
    return [
        service.list_pending_drafts,
        lambda c: service.get_draft(c, outbound_id),
        service.list_open_escalations,
        lambda c: service.get_escalation(c, escalation_id),
        lambda c: service.get_thread(c, thread_id),
        lambda c: service.get_lead(c, lead_id),
    ]


@pytest.mark.parametrize("bad", list(UNAUTHORIZED.values()), ids=list(UNAUTHORIZED))
def test_unauthorized_reads_reveal_nothing(db: Database, bad: object) -> None:
    draft = make_draft(db)
    escalation = make_escalation(db)
    service = operator(db)
    for read in reads(service, draft.outbound_id or "", escalation.escalation_id or "", draft.lead_id or "", draft.thread_id):
        with pytest.raises(OperatorUnauthorizedError) as error:
            read(bad)
        assert str(error.value) == "operator not authorized"  # no IDs, no customer data


@pytest.mark.parametrize("bad", list(UNAUTHORIZED.values()), ids=list(UNAUTHORIZED))
def test_unauthorized_commands_change_nothing(db: Database, bad: object) -> None:
    draft = make_draft(db)
    escalation = make_escalation(db)
    service = operator(db)
    detail = service.get_draft(AS_ALICE, draft.outbound_id or "")
    before = snapshot(db)
    commands = [
        lambda: service.approve_draft(bad, approve_command(detail)),
        lambda: service.reject_draft(bad, reject_command(detail)),
        lambda: service.take_ownership(bad, ownership_command(draft.lead_id or "", 1)),
        lambda: service.resolve_escalation(bad, resolve_command(escalation.escalation_id or "")),
    ]
    for command in commands:
        with pytest.raises(OperatorUnauthorizedError):
            command()
    assert snapshot(db) == before


def test_authenticator_failure_fails_closed(db: Database) -> None:
    draft = make_draft(db)
    with pytest.raises(OperatorUnauthorizedError):
        operator(db, authenticator=ExplodingAuthenticator()).get_draft(AS_ALICE, draft.outbound_id or "")


def test_both_authorized_operators_can_read(db: Database) -> None:
    make_draft(db)
    assert len(operator(db).list_pending_drafts(AS_ALICE)) == len(operator(db).list_pending_drafts(AS_BOB)) == 1


def test_credential_token_is_not_exposed_in_repr() -> None:
    assert "tok-alice" not in repr(AS_ALICE) and "tok-alice" not in str(AS_ALICE.model_dump())


# ---- Reads ----------------------------------------------------------------------------------------


def test_draft_detail_separates_customer_generated_and_application_content(db: Database) -> None:
    draft = make_draft(db)
    detail = operator(db).get_draft(AS_ALICE, draft.outbound_id or "")
    assert detail.status is OutboundStatus.DRAFTED and detail.actionable and detail.blockers == ()
    # Customer-authored text, verbatim.
    assert detail.customer is not None and detail.customer.direction is EmailDirection.INBOUND
    assert detail.customer.from_address == SENDER and "Basic plan" in detail.customer.body_text
    # Model output, labelled as such.
    assert detail.generated_classification is not None and detail.generated_classification.intent is LeadIntent.PRICING_REQUEST
    assert detail.generated_classification.extracted_questions == ("What does the Basic plan cost per month?",)
    assert "100 EUR" in detail.generated_draft.body
    # Application-owned decisions.
    assert detail.knowledge_assessment is not None and detail.knowledge_assessment.decision is KnowledgeDecision.SUFFICIENT
    assert detail.claim_check is not None and detail.claim_check.passed
    assert detail.evidence and all(e.usable_now and e.excerpt for e in detail.evidence)
    assert detail.lead is not None and detail.lead.suppressed_scopes == ()


def test_pending_list_excludes_rejected_and_cancelled_drafts(db: Database) -> None:
    first = make_draft(db, "p-1")
    second = make_draft(db, "p-2", sender="other@elsewhere.example")
    service = operator(db)
    assert {d.outbound_id for d in service.list_pending_drafts(AS_ALICE)} == {first.outbound_id, second.outbound_id}

    service.reject_draft(AS_ALICE, reject_command(service.get_draft(AS_ALICE, first.outbound_id or "")))
    process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.UNSUBSCRIBE)),
            envelope("p-3", sender="other@elsewhere.example", body="Please unsubscribe me."))
    assert service.list_pending_drafts(AS_ALICE) == ()


def test_history_never_makes_a_cancelled_draft_actionable(db: Database) -> None:
    draft = make_draft(db)
    assert draft.reply_decision is ReplyDecision.DRAFT_FOR_REVIEW  # the recorded historical result
    process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.UNSUBSCRIBE)),
            envelope("p-unsub", body="Please unsubscribe me."))
    detail = operator(db).get_draft(AS_ALICE, draft.outbound_id or "")
    assert detail.status is OutboundStatus.CANCELLED and not detail.actionable
    assert {BlockCode.DRAFT_NOT_REVIEWABLE, BlockCode.CONTACT_SUPPRESSED, BlockCode.LEAD_CLOSED} <= set(detail.blockers)


def test_escalation_list_and_detail(db: Database) -> None:
    result = make_escalation(db)
    service = operator(db)
    [summary] = service.list_open_escalations(AS_ALICE)
    assert summary.escalation_id == result.escalation_id and summary.status is EscalationStatus.OPEN
    detail = service.get_escalation(AS_ALICE, summary.escalation_id)
    assert EscalationReason.NEGOTIATION in detail.reasons
    assert detail.customer is not None and "30% off" in detail.customer.body_text
    assert detail.generated_classification is not None and detail.generated_classification.intent is LeadIntent.NEGOTIATION
    assert detail.lead is not None and detail.lead.lead_id == result.lead_id


def test_thread_history_is_bounded_and_lead_state_is_current(db: Database) -> None:
    first = make_draft(db, "p-1")
    for index in range(2, 6):
        process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.NEGOTIATION)),
                envelope(f"p-{index}", body=f"Follow-up {index}", in_reply_to="<p-1@prospect.example>"))
    service = operator(db)
    view = service.get_thread(AS_ALICE, first.thread_id, limit=50)
    assert view.total_messages == 5 and len(view.messages) == 3  # capped by configuration
    assert [m.body_text for m in view.messages] == ["Follow-up 3", "Follow-up 4", "Follow-up 5"]
    lead = service.get_lead(AS_ALICE, first.lead_id or "")
    assert lead.version > 1 and lead.contact_email == SENDER


def test_reads_do_not_write(db: Database) -> None:
    draft = make_draft(db)
    escalation = make_escalation(db)
    service = operator(db)
    before = snapshot(db)
    for read in reads(service, draft.outbound_id or "", escalation.escalation_id or "", draft.lead_id or "", draft.thread_id):
        read(AS_ALICE)
    assert snapshot(db) == before


def test_taking_ownership_is_visible_in_reads(db: Database) -> None:
    draft = make_draft(db)
    service = operator(db)
    lead = service.get_lead(AS_ALICE, draft.lead_id or "")
    service.take_ownership(AS_ALICE, ownership_command(lead.lead_id, lead.version))
    assert service.get_lead(AS_ALICE, lead.lead_id).status.value == "OPERATOR_OWNED"
    assert outbound(db, draft.outbound_id or "").status is OutboundStatus.DRAFTED
