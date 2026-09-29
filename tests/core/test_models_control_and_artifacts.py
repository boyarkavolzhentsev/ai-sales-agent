from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.core.enums import (
    ActorType,
    ClaimCheckStatus,
    ConfidenceBand,
    DNCReason,
    DNCScope,
    DraftPurpose,
    DraftReviewStatus,
    EscalationReason,
    EscalationResolution,
    EscalationStatus,
    LeadIntent,
    OperatorCommandKind,
    OperatorResponseStatus,
    RefKind,
    RiskFlag,
)
from app.core.models import (
    Actor,
    AuditEvent,
    DoNotContactEntry,
    EntityRef,
    Escalation,
    EvidenceCitation,
    IntentClassification,
    MessageDraft,
    OperatorCommand,
    OperatorResponse,
    ProvenanceRecord,
    SendPermit,
)

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=1)
T5 = T0 + timedelta(minutes=5)
NAIVE = datetime(2026, 1, 1, 12, 0)
HASH = "d" * 64
MSG_REF = EntityRef(kind=RefKind.EMAIL_MESSAGE, id="msg-1")


# ---- DoNotContactEntry ------------------------------------------------------


def dnc_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "entry_id": "dnc-1",
        "scope": DNCScope.EMAIL,
        "value": "Buyer@Prospect.com",
        "reason": DNCReason.UNSUBSCRIBE_REQUEST,
        "source_ref": MSG_REF,
        "created_by": "system:prefilter",
        "created_at": T0,
    }
    return base | overrides


def test_dnc_value_is_normalized_per_scope() -> None:
    assert DoNotContactEntry(**dnc_kwargs()).value == "buyer@prospect.com"
    domain = DoNotContactEntry(**dnc_kwargs(scope=DNCScope.DOMAIN, value="Prospect.COM"))
    assert domain.value == "prospect.com"


@pytest.mark.parametrize(
    ("scope", "value"),
    [
        (DNCScope.EMAIL, ""),
        (DNCScope.EMAIL, "   "),
        (DNCScope.DOMAIN, ""),
        (DNCScope.EMAIL, "prospect.com"),
        (DNCScope.DOMAIN, "buyer@prospect.com"),
    ],
)
def test_dnc_value_must_be_non_empty_and_match_scope(scope: DNCScope, value: str) -> None:
    with pytest.raises(ValidationError):
        DoNotContactEntry(**dnc_kwargs(scope=scope, value=value))


def test_dnc_expiry_must_follow_creation() -> None:
    DoNotContactEntry(**dnc_kwargs(expires_at=T1))
    with pytest.raises(ValidationError, match="expires_at"):
        DoNotContactEntry(**dnc_kwargs(expires_at=T0))
    with pytest.raises(ValidationError):
        DoNotContactEntry(**dnc_kwargs(created_at=NAIVE))


# ---- Escalation -------------------------------------------------------------


def escalation_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "escalation_id": "esc-1",
        "lead_id": "lead-1",
        "trigger_ref": MSG_REF,
        "reasons": (EscalationReason.KNOWLEDGE_INSUFFICIENT,),
        "created_at": T0,
    }
    return base | overrides


def test_open_escalation() -> None:
    assert Escalation(**escalation_kwargs()).status is EscalationStatus.OPEN


def test_escalation_reasons_non_empty_and_unique() -> None:
    with pytest.raises(ValidationError):
        Escalation(**escalation_kwargs(reasons=()))
    with pytest.raises(ValidationError, match="duplicate"):
        Escalation(
            **escalation_kwargs(
                reasons=(EscalationReason.NEGOTIATION, EscalationReason.NEGOTIATION)
            )
        )


def test_escalation_resolution_rules() -> None:
    resolved = {
        "status": EscalationStatus.RESOLVED,
        "resolution": EscalationResolution.OPERATOR_REPLIED,
        "resolved_at": T1,
        "resolved_by": "operator-1",
    }
    Escalation(**escalation_kwargs(**resolved))
    with pytest.raises(ValidationError, match="RESOLVED"):
        Escalation(**escalation_kwargs(**(resolved | {"resolved_by": None})))
    with pytest.raises(ValidationError, match="only allowed"):
        Escalation(**escalation_kwargs(resolution=EscalationResolution.NO_ACTION))
    with pytest.raises(ValidationError, match="resolved_at"):
        Escalation(**escalation_kwargs(**(resolved | {"resolved_at": T0 - timedelta(seconds=1)})))


# ---- Operator ---------------------------------------------------------------


def command_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "command_id": "cmd-1",
        "telegram_update_id": 1001,
        "operator_user_id": 42,
        "chat_id": -100123,
        "name": "stats",
        "args": ("7d",),
        "kind": OperatorCommandKind.READ,
        "received_at": T0,
    }
    return base | overrides


def test_read_command_is_valid_and_needs_no_confirmation() -> None:
    OperatorCommand(**command_kwargs())
    with pytest.raises(ValidationError, match="READ"):
        OperatorCommand(**command_kwargs(confirmation_nonce="n", confirmation_expires_at=T5))


def test_mutating_command_confirmation_rules() -> None:
    mutate = {"name": "pause", "args": ("camp-1",), "kind": OperatorCommandKind.MUTATE}
    OperatorCommand(**command_kwargs(**mutate))
    OperatorCommand(
        **command_kwargs(
            **mutate, confirmation_nonce="n", confirmation_expires_at=T5, confirmed_at=T1
        )
    )
    with pytest.raises(ValidationError, match="together"):
        OperatorCommand(**command_kwargs(**mutate, confirmation_nonce="n"))
    with pytest.raises(ValidationError, match="confirmed_at requires"):
        OperatorCommand(**command_kwargs(**mutate, confirmed_at=T1))
    with pytest.raises(ValidationError, match="confirmation_expires_at"):
        OperatorCommand(
            **command_kwargs(
                **mutate,
                confirmation_nonce="n",
                confirmation_expires_at=T1,
                confirmed_at=T5,
            )
        )


@pytest.mark.parametrize("name", ["/stats", "Stats", "", "st ats"])
def test_command_name_format(name: str) -> None:
    with pytest.raises(ValidationError):
        OperatorCommand(**command_kwargs(name=name))


def test_operator_response() -> None:
    response = OperatorResponse(
        command_id="cmd-1",
        status=OperatorResponseStatus.OK,
        payload={"sent_today": 3, "campaigns": ["camp-1"]},
        rendered_text="Sent today: 3",
        created_at=T0,
    )
    assert response.payload["sent_today"] == 3
    with pytest.raises(ValidationError):
        OperatorResponse(
            command_id="cmd-1", status=OperatorResponseStatus.OK, rendered_text="", created_at=T0
        )


# ---- IntentClassification ---------------------------------------------------


def classification_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "message_id": "msg-1",
        "primary_intent": LeadIntent.PRICING_REQUEST,
        "secondary_intents": (LeadIntent.MEETING_REQUEST,),
        "extracted_questions": ("What does it cost?",),
        "confidence_band": ConfidenceBand.HIGH,
        "risk_flags": (),
        "language": "en",
        "created_at": T0,
    }
    return base | overrides


def test_classification() -> None:
    IntentClassification(**classification_kwargs())
    with pytest.raises(ValidationError, match="primary_intent"):
        IntentClassification(
            **classification_kwargs(secondary_intents=(LeadIntent.PRICING_REQUEST,))
        )
    with pytest.raises(ValidationError, match="duplicate"):
        IntentClassification(**classification_kwargs(risk_flags=(RiskFlag.LEGAL, RiskFlag.LEGAL)))
    with pytest.raises(ValidationError):
        IntentClassification(**classification_kwargs(created_at=NAIVE))


# ---- MessageDraft -----------------------------------------------------------


def draft_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "draft_id": "draft-1",
        "purpose": DraftPurpose.INBOUND_REPLY,
        "lead_id": "lead-1",
        "thread_id": "thr-1",
        "subject": "Re: Question",
        "body_generated": "The basic plan is 100 EUR / month.",
        "body_final": "The basic plan is 100 EUR / month.\n\n-- footer",
        "evidence_citations": (EvidenceCitation(evidence_id="ev-1", claim="100 EUR / month"),),
        "claim_check_status": ClaimCheckStatus.PASS,
        "model": "model-x",
        "prompt_version": "reply-v1",
        "input_hash": HASH,
        "created_at": T0,
    }
    return base | overrides


def test_draft_defaults_to_pending_review() -> None:
    assert MessageDraft(**draft_kwargs()).review_status is DraftReviewStatus.PENDING


def test_draft_claim_check_findings_rules() -> None:
    with pytest.raises(ValidationError, match="PASS"):
        MessageDraft(**draft_kwargs(claim_check_findings=("unsupported number",)))
    with pytest.raises(ValidationError, match="requires findings"):
        MessageDraft(**draft_kwargs(claim_check_status=ClaimCheckStatus.FAIL))
    MessageDraft(
        **draft_kwargs(
            claim_check_status=ClaimCheckStatus.FAIL, claim_check_findings=("unsupported number",)
        )
    )


def test_draft_review_rules() -> None:
    with pytest.raises(ValidationError, match="reviewed_by"):
        MessageDraft(**draft_kwargs(review_status=DraftReviewStatus.APPROVED))
    with pytest.raises(ValidationError, match="reviewed_by"):
        MessageDraft(**draft_kwargs(reviewed_by="operator-1"))
    with pytest.raises(ValidationError, match="edited_body"):
        MessageDraft(
            **draft_kwargs(
                review_status=DraftReviewStatus.REJECTED, reviewed_by="op", edited_body="x"
            )
        )
    MessageDraft(
        **draft_kwargs(review_status=DraftReviewStatus.APPROVED, reviewed_by="op", edited_body="x")
    )


def test_draft_thread_and_citations() -> None:
    with pytest.raises(ValidationError, match="thread_id"):
        MessageDraft(**draft_kwargs(thread_id=None))
    MessageDraft(**draft_kwargs(purpose=DraftPurpose.OUTBOUND_FIRST_TOUCH, thread_id=None))
    citation = EvidenceCitation(evidence_id="ev-1", claim="c")
    with pytest.raises(ValidationError, match="duplicate"):
        MessageDraft(**draft_kwargs(evidence_citations=(citation, citation)))


# ---- SendPermit -------------------------------------------------------------


def permit_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "permit_id": "permit-1",
        "outbound_id": "out-1",
        "content_hash": HASH,
        "checks_passed": ("DNC", "LIMITS", "CLAIM_CHECK"),
        "policy_config_version": "policy-1",
        "issued_at": T0,
        "expires_at": T5,
    }
    return base | overrides


def test_permit_is_immutable() -> None:
    permit = SendPermit(**permit_kwargs())
    with pytest.raises(ValidationError):
        permit.consumed_at = T1  # type: ignore[misc]


def test_permit_window_rules() -> None:
    SendPermit(**permit_kwargs(consumed_at=T1))
    with pytest.raises(ValidationError, match="expires_at"):
        SendPermit(**permit_kwargs(expires_at=T0))
    with pytest.raises(ValidationError, match="consumed_at"):
        SendPermit(**permit_kwargs(consumed_at=T0 - timedelta(seconds=1)))
    with pytest.raises(ValidationError, match="expires_at"):
        SendPermit(**permit_kwargs(consumed_at=T5 + timedelta(seconds=1)))
    with pytest.raises(ValidationError):
        SendPermit(**permit_kwargs(checks_passed=()))
    with pytest.raises(ValidationError, match="duplicate"):
        SendPermit(**permit_kwargs(checks_passed=("DNC", "DNC")))
    with pytest.raises(ValidationError):
        SendPermit(**permit_kwargs(issued_at=NAIVE))


# ---- Provenance / Audit -----------------------------------------------------


def test_provenance_record() -> None:
    record = ProvenanceRecord(
        artifact_ref=EntityRef(kind=RefKind.MESSAGE_DRAFT, id="draft-1"),
        model="model-x",
        prompt_template_id="reply",
        prompt_template_version="1",
        input_refs=(MSG_REF,),
        input_hashes=(HASH,),
        evidence_ids=("ev-1",),
        code_version="29da14e",
        created_at=T0,
    )
    assert record.input_refs == (MSG_REF,)
    with pytest.raises(ValidationError, match="duplicate"):
        ProvenanceRecord(**(record.model_dump() | {"evidence_ids": ("ev-1", "ev-1")}))


def audit_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "event_id": "evt-1",
        "occurred_at": T0,
        "actor": Actor(type=ActorType.SYSTEM, id="lead_manager"),
        "event_type": "LEAD_STAGE_CHANGED",
        "subject_refs": (EntityRef(kind=RefKind.LEAD, id="lead-1"),),
        "before": {"stage": "CONTACTED"},
        "after": {"stage": "ENGAGED"},
        "correlation_id": "corr-1",
        "payload_hash": HASH,
    }
    return base | overrides


def test_audit_event() -> None:
    event = AuditEvent(**audit_kwargs())
    with pytest.raises(ValidationError):
        event.event_type = "OTHER"  # type: ignore[misc]


def test_audit_event_rules() -> None:
    with pytest.raises(ValidationError):
        AuditEvent(**audit_kwargs(subject_refs=()))
    ref = EntityRef(kind=RefKind.LEAD, id="lead-1")
    with pytest.raises(ValidationError, match="duplicate"):
        AuditEvent(**audit_kwargs(subject_refs=(ref, ref)))
    with pytest.raises(ValidationError):
        AuditEvent(**audit_kwargs(event_type="lead stage changed"))
    with pytest.raises(ValidationError):
        AuditEvent(**audit_kwargs(occurred_at=NAIVE))
    with pytest.raises(ValidationError):
        AuditEvent(**audit_kwargs(actor=Actor(type=ActorType.OPERATOR, id=" ")))
