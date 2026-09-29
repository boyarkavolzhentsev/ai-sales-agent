"""Builders for the append-only audit and provenance records the inbound flow writes.

Event and record identities are deterministic, so a replayed or concurrent finalization
can never append the same fact twice. Email bodies are never copied into audit payloads;
payloads hold references, decisions and hashes.
"""

import hashlib
from datetime import datetime

from pydantic import JsonValue

from app.core.enums import ActorType, RefKind
from app.core.models import Actor, AuditEvent, EntityRef, ProvenanceRecord
from app.core.models.types import JsonObject
from app.inbound.models import stable_id
from app.llm import LLMResultMetadata
from app.persistence.serialization import dumps_json

INBOUND_ACTOR = Actor(type=ActorType.SYSTEM, id="inbound_service")


class Events:
    """Stable audit event types."""

    INBOUND_OBSERVED = "INBOUND_OBSERVED"
    THREAD_CREATED = "THREAD_CREATED"
    THREAD_RESOLVED = "THREAD_RESOLVED"
    CONTACT_CREATED = "CONTACT_CREATED"
    CONTACT_RESOLVED = "CONTACT_RESOLVED"
    LEAD_CREATED = "LEAD_CREATED"
    LEAD_RESOLVED = "LEAD_RESOLVED"
    CLASSIFICATION_COMPLETED = "CLASSIFICATION_COMPLETED"
    KNOWLEDGE_ASSESSED = "KNOWLEDGE_ASSESSED"
    DRAFT_CREATED = "DRAFT_CREATED"
    ESCALATION_CREATED = "ESCALATION_CREATED"
    LEAD_STAGE_CHANGED = "LEAD_STAGE_CHANGED"
    DNC_ADDED = "DNC_ADDED"
    DRAFTS_CANCELLED = "DRAFTS_CANCELLED"
    PROCESSING_COMPLETED = "PROCESSING_COMPLETED"


def ref(kind: RefKind, entity_id: str) -> EntityRef:
    return EntityRef(kind=kind, id=entity_id)


def audit_event(
    *,
    message_id: str,
    event_type: str,
    subjects: tuple[EntityRef, ...],
    after: JsonObject,
    correlation_id: str,
    occurred_at: datetime,
    before: JsonObject | None = None,
) -> AuditEvent:
    payload: dict[str, JsonValue] = {"event_type": event_type, "before": before, "after": after}
    return AuditEvent(
        event_id=stable_id("ae", message_id, event_type),
        occurred_at=occurred_at,
        actor=INBOUND_ACTOR,
        event_type=event_type,
        subject_refs=tuple(dict.fromkeys(subjects)),
        before=before,
        after=after,
        correlation_id=correlation_id,
        payload_hash=hashlib.sha256(dumps_json(payload).encode("utf-8")).hexdigest(),
    )


def llm_call_summary(meta: LLMResultMetadata) -> JsonObject:
    """Everything provenance needs that the ProvenanceRecord contract has no field for."""
    return {
        "task": meta.task.value,
        "prompt_id": meta.prompt_id,
        "prompt_version": meta.prompt_version,
        "provider_name": meta.provider_name,
        "model_name": meta.model_name,
        "input_hash": meta.input_hash,
        "output_hash": meta.output_hash,
        "attempt": meta.attempt,
    }


def provenance_record(
    meta: LLMResultMetadata,
    *,
    artifact: EntityRef,
    inputs: tuple[EntityRef, ...],
    evidence_ids: tuple[str, ...],
    code_version: str,
) -> ProvenanceRecord:
    """``model`` is recorded as "<provider>/<model>" because the contract has one field."""
    return ProvenanceRecord(
        artifact_ref=artifact,
        model=f"{meta.provider_name}/{meta.model_name}",
        prompt_template_id=meta.prompt_id,
        prompt_template_version=meta.prompt_version,
        input_refs=tuple(dict.fromkeys(inputs)),
        input_hashes=(meta.input_hash,),
        evidence_ids=tuple(dict.fromkeys(evidence_ids)),
        code_version=code_version,
        created_at=meta.created_at,
    )
