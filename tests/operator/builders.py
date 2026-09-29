"""Builders for operator-workflow tests. Fictional data, fake LLM, local SQLite only."""

from pydantic import SecretStr

from app.core.enums import EscalationResolution, LeadIntent, RefKind
from app.core.models import AuditEvent, EntityRef, OutboundMessage
from app.inbound import InboundResult
from app.llm import LLMTask, SenderIdentity
from app.operator import (
    ApproveDraft,
    DraftDetail,
    OperatorAuthenticator,
    OperatorConfig,
    OperatorCredential,
    OperatorService,
    RejectDraft,
    RejectReason,
    ResolveEscalation,
    TakeOwnership,
)
from app.persistence import Database, FrozenClock
from tests.inbound.builders import NOW, ScriptedTransport, classification, envelope, happy_transport, process

ALICE, BOB = "op-alice", "op-bob"
TOKENS = {"tok-alice": ALICE, "tok-bob": BOB, "tok-mallory": "op-mallory"}


class FakeAuthenticator:
    """Stands in for the trusted boundary (e.g. verified Telegram updates): only a known
    token yields an identity. ``op-mallory`` authenticates but is not authorized."""

    def authenticate(self, credential: OperatorCredential) -> str | None:
        return TOKENS.get(credential.token.get_secret_value()) if credential.scheme == "fake" else None


class ExplodingAuthenticator:
    def authenticate(self, credential: OperatorCredential) -> str | None:
        raise RuntimeError("identity backend down")


def credential(token: str = "tok-alice", scheme: str = "fake") -> OperatorCredential:
    return OperatorCredential(scheme=scheme, token=SecretStr(token))


AS_ALICE, AS_BOB, AS_MALLORY = credential("tok-alice"), credential("tok-bob"), credential("tok-mallory")


def config() -> OperatorConfig:
    return OperatorConfig(
        authorized_operator_ids=(ALICE, BOB),
        sender=SenderIdentity(sender_name="Alex Seller", company_name="Samplewidget Co"),
        max_thread_messages=3,
    )


def operator(db: Database, clock: FrozenClock | None = None, authenticator: OperatorAuthenticator | None = None) -> OperatorService:
    return OperatorService(db, clock or FrozenClock(NOW), config(), authenticator or FakeAuthenticator())


def make_draft(db: Database, provider_message_id: str = "p-1", **envelope_overrides: object) -> InboundResult:
    result = process(db, happy_transport(), envelope(provider_message_id, **envelope_overrides))
    assert result.outbound_id is not None, result
    return result


def make_escalation(db: Database, provider_message_id: str = "p-esc") -> InboundResult:
    transport = ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.NEGOTIATION))
    result = process(db, transport, envelope(provider_message_id, body="Can you do 30% off if we sign today?"))
    assert result.escalation_id is not None, result
    return result


def approve_command(detail: DraftDetail, command_id: str = "cmd-approve", **overrides: object) -> ApproveDraft:
    assert detail.lead is not None
    data: dict[str, object] = {
        "command_id": command_id,
        "correlation_id": f"corr-{command_id}",
        "outbound_id": detail.outbound_id,
        "draft_id": detail.draft_id,
        "content_hash": detail.content_hash,
        "expected_outbound_version": detail.version,
        "expected_lead_version": detail.lead.version,
    }
    return ApproveDraft.model_validate(data | overrides)


def reject_command(detail: DraftDetail, command_id: str = "cmd-reject", **overrides: object) -> RejectDraft:
    data: dict[str, object] = {
        "command_id": command_id,
        "correlation_id": f"corr-{command_id}",
        "outbound_id": detail.outbound_id,
        "draft_id": detail.draft_id,
        "expected_outbound_version": detail.version,
        "reason": RejectReason.INACCURATE,
    }
    return RejectDraft.model_validate(data | overrides)


def ownership_command(lead_id: str, version: int, command_id: str = "cmd-own") -> TakeOwnership:
    return TakeOwnership(command_id=command_id, correlation_id=f"corr-{command_id}", lead_id=lead_id, expected_lead_version=version)


def resolve_command(
    escalation_id: str, version: int = 1, disposition: EscalationResolution = EscalationResolution.NO_ACTION,
    command_id: str = "cmd-resolve", note: str = "Handled by phone.",
) -> ResolveEscalation:
    return ResolveEscalation(
        command_id=command_id, correlation_id=f"corr-{command_id}", escalation_id=escalation_id,
        expected_escalation_version=version, disposition=disposition, note=note,
    )


def outbound(db: Database, outbound_id: str) -> OutboundMessage:
    with db.transaction() as uow:
        found = uow.outbound.get(outbound_id)
    assert found is not None
    return found


def operator_events(db: Database, command_id: str) -> list[AuditEvent]:
    with db.transaction() as uow:
        return uow.audit.list_for_subject(EntityRef(kind=RefKind.OPERATOR_COMMAND, id=command_id))


WRITABLE_TABLES = (
    "outbound_messages", "leads", "escalations", "audit_events", "idempotency_keys", "follow_up_plans",
    "do_not_contact", "quota_reservations", "knowledge_sources_meta", "knowledge_chunks",
)


def snapshot(db: Database) -> tuple[object, ...]:
    """Every row a rejected command could have touched, for 'nothing was written' checks."""
    with db.transaction() as uow:
        rows = []
        for table in WRITABLE_TABLES:
            rows.append(tuple(tuple(row) for row in uow._tx.fetch_all(f"SELECT * FROM {table} ORDER BY 1")))  # noqa: SLF001
    return tuple(rows)
