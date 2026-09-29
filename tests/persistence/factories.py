"""Valid Stage 1 model instances for persistence tests. No secrets, no real addresses."""

from datetime import UTC, datetime, timedelta

from app.core.enums import (
    ActorType,
    ContactDepartment,
    ContactSource,
    ContactType,
    DNCReason,
    DNCScope,
    EmailDirection,
    EscalationReason,
    KnowledgeApprovalStatus,
    KnowledgeDomain,
    KnowledgeExternalUse,
    LeadOrigin,
    LeadStage,
    OperatorCommandKind,
    OperatorResponseStatus,
    OutboundKind,
    RefKind,
)
from app.core.models import (
    Actor,
    AuditEvent,
    Campaign,
    CampaignTargetFilter,
    DoNotContactEntry,
    EmailMessage,
    EmailThread,
    EntityRef,
    Escalation,
    FollowUpPlan,
    KnowledgeSource,
    Lead,
    OperatorCommand,
    OperatorResponse,
    OutboundMessage,
    ProspectCompany,
    ProspectContact,
    ProvenanceRecord,
)

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
HASH = "e" * 64

COMPANY_ID = "company-1"
CONTACT_ID = "contact-1"
CAMPAIGN_ID = "camp-1"
LEAD_ID = "lead-1"
THREAD_ID = "thr-1"
OUTBOUND_ID = "out-1"
COMMAND_ID = "cmd-1"


def company(**overrides: object) -> ProspectCompany:
    base: dict[str, object] = {
        "company_id": COMPANY_ID,
        "name": "Prospect Ltd",
        "domain": "prospect.example",
        "source": ContactSource.IMPORT,
        "created_at": T0,
        "updated_at": T0,
    }
    return ProspectCompany.model_validate(base | overrides)


def contact(**overrides: object) -> ProspectContact:
    base: dict[str, object] = {
        "contact_id": CONTACT_ID,
        "company_id": COMPANY_ID,
        "email": "partners@prospect.example",
        "department": ContactDepartment.PARTNERSHIPS,
        "contact_type": ContactType.ROLE_ADDRESS,
        "source": ContactSource.IMPORT,
        "source_ref": "import.csv",
        "collected_at": T0,
        "created_at": T0,
        "updated_at": T0,
    }
    return ProspectContact.model_validate(base | overrides)


def campaign(**overrides: object) -> Campaign:
    base: dict[str, object] = {
        "campaign_id": CAMPAIGN_ID,
        "name": "Q1 partnerships",
        "target_filter": CampaignTargetFilter(departments=(ContactDepartment.PARTNERSHIPS,)),
        "allowed_knowledge_domains": (KnowledgeDomain.COMPANY, KnowledgeDomain.OUTBOUND_MESSAGING),
        "sending_mailbox": "outreach@ourco.example",
        "max_follow_ups": 2,
        "min_interval_between_follow_ups": timedelta(days=3),
        "created_by": "operator-1",
        "created_at": T0,
        "updated_at": T0,
    }
    return Campaign.model_validate(base | overrides)


def lead(**overrides: object) -> Lead:
    base: dict[str, object] = {
        "lead_id": LEAD_ID,
        "contact_id": CONTACT_ID,
        "company_id": COMPANY_ID,
        "origin": LeadOrigin.OUTBOUND,
        "campaign_id": CAMPAIGN_ID,
        "stage": LeadStage.NEW,
        "created_at": T0,
        "updated_at": T0,
    }
    return Lead.model_validate(base | overrides)


def thread(**overrides: object) -> EmailThread:
    base: dict[str, object] = {
        "thread_id": THREAD_ID,
        "mailbox": "outreach@ourco.example",
        "participant_addresses": ("partners@prospect.example", "outreach@ourco.example"),
        "subject_normalized": "partnership",
        "lead_id": LEAD_ID,
        "message_ids": ("msg-1",),
    }
    return EmailThread.model_validate(base | overrides)


def inbound_message(**overrides: object) -> EmailMessage:
    base: dict[str, object] = {
        "message_id": "msg-1",
        "rfc_message_id": "<msg-1@prospect.example>",
        "thread_id": THREAD_ID,
        "direction": EmailDirection.INBOUND,
        "mailbox": "outreach@ourco.example",
        "from_address": "partners@prospect.example",
        "to_addresses": ("outreach@ourco.example",),
        "subject": "Re: Partnership",
        "body_text": "Tell me more — ціна?",
        "raw_ref": "raw/msg-1.eml",
        "raw_hash": HASH,
        "references": ("<root@ourco.example>",),
        "received_at": T0 + timedelta(hours=1),
    }
    return EmailMessage.model_validate(base | overrides)


def outbound_message(**overrides: object) -> OutboundMessage:
    base: dict[str, object] = {
        "outbound_id": OUTBOUND_ID,
        "idempotency_key": "camp-1:lead-1:0",
        "kind": OutboundKind.FIRST_TOUCH,
        "lead_id": LEAD_ID,
        "contact_id": CONTACT_ID,
        "campaign_id": CAMPAIGN_ID,
        "sequence_no": 0,
        "draft_id": "draft-1",
        "subject": "Partnership",
        "body_final": "Hello",
        "content_hash": HASH,
        "created_at": T0,
    }
    return OutboundMessage.model_validate(base | overrides)


def follow_up_plan(**overrides: object) -> FollowUpPlan:
    base: dict[str, object] = {
        "plan_id": "plan-1",
        "lead_id": LEAD_ID,
        "campaign_id": CAMPAIGN_ID,
        "anchor_outbound_id": OUTBOUND_ID,
        "max_steps": 2,
        "next_due_at": T0 + timedelta(days=3),
        "created_at": T0,
        "updated_at": T0,
    }
    return FollowUpPlan.model_validate(base | overrides)


def dnc_entry(**overrides: object) -> DoNotContactEntry:
    base: dict[str, object] = {
        "entry_id": "dnc-1",
        "scope": DNCScope.EMAIL,
        "value": "partners@prospect.example",
        "reason": DNCReason.UNSUBSCRIBE_REQUEST,
        "source_ref": EntityRef(kind=RefKind.EMAIL_MESSAGE, id="msg-1"),
        "created_by": "system:prefilter",
        "created_at": T0,
    }
    return DoNotContactEntry.model_validate(base | overrides)


def escalation(**overrides: object) -> Escalation:
    base: dict[str, object] = {
        "escalation_id": "esc-1",
        "lead_id": LEAD_ID,
        "trigger_ref": EntityRef(kind=RefKind.EMAIL_MESSAGE, id="msg-1"),
        "reasons": (EscalationReason.KNOWLEDGE_INSUFFICIENT, EscalationReason.PRICING_OR_COMMERCIAL),
        "summary": "Asked for pricing not in the knowledge base.",
        "created_at": T0,
    }
    return Escalation.model_validate(base | overrides)


def operator_command(**overrides: object) -> OperatorCommand:
    base: dict[str, object] = {
        "command_id": COMMAND_ID,
        "telegram_update_id": 1001,
        "operator_user_id": 42,
        "chat_id": -100123,
        "name": "pause",
        "args": ("camp-1",),
        "kind": OperatorCommandKind.MUTATE,
        "confirmation_nonce": "nonce-1",
        "confirmation_expires_at": T0 + timedelta(minutes=5),
        "received_at": T0,
    }
    return OperatorCommand.model_validate(base | overrides)


def operator_response(**overrides: object) -> OperatorResponse:
    base: dict[str, object] = {
        "command_id": COMMAND_ID,
        "status": OperatorResponseStatus.NEEDS_CONFIRMATION,
        "payload": {"campaign_id": "camp-1", "affected": {"plans": 3, "queued": [1, 2]}},
        "rendered_text": "Confirm pausing camp-1?",
        "created_at": T0,
    }
    return OperatorResponse.model_validate(base | overrides)


def audit_event(**overrides: object) -> AuditEvent:
    base: dict[str, object] = {
        "event_id": "evt-1",
        "occurred_at": T0,
        "actor": Actor(type=ActorType.SYSTEM, id="lead_manager"),
        "event_type": "LEAD_STAGE_CHANGED",
        "subject_refs": (
            EntityRef(kind=RefKind.LEAD, id=LEAD_ID),
            EntityRef(kind=RefKind.CAMPAIGN, id=CAMPAIGN_ID),
        ),
        "before": {"stage": "NEW"},
        "after": {"stage": "CONTACTED"},
        "correlation_id": "corr-1",
        "payload_hash": HASH,
    }
    return AuditEvent.model_validate(base | overrides)


def provenance_record(**overrides: object) -> ProvenanceRecord:
    base: dict[str, object] = {
        "artifact_ref": EntityRef(kind=RefKind.MESSAGE_DRAFT, id="draft-1"),
        "model": "model-x",
        "prompt_template_id": "reply",
        "prompt_template_version": "1",
        "input_refs": (EntityRef(kind=RefKind.EMAIL_MESSAGE, id="msg-1"),),
        "input_hashes": (HASH,),
        "evidence_ids": ("ev-1", "ev-2"),
        "code_version": "69dd42f",
        "created_at": T0,
    }
    return ProvenanceRecord.model_validate(base | overrides)


def knowledge_source(**overrides: object) -> KnowledgeSource:
    base: dict[str, object] = {
        "source_id": "src-pricing",
        "domain": KnowledgeDomain.PRICING_COMMERCIAL,
        "title": "Price list",
        "path": "knowledge_base/pricing/price_list.yaml",
        "version": 1,
        "content_hash": HASH,
        "approval_status": KnowledgeApprovalStatus.APPROVED,
        "external_use": KnowledgeExternalUse.EXTERNAL_OK,
        "approved_by": "head-of-sales",
        "approved_at": T0,
        "effective_from": T0,
        "review_by": T0 + timedelta(days=90),
        "locale": "en",
        "tags": ("pricing", "plans"),
    }
    return KnowledgeSource.model_validate(base | overrides)
