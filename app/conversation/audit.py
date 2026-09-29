"""Audit records for conversation and follow-up transitions (IDs, states and codes only)."""

import hashlib
from datetime import datetime

from pydantic import JsonValue

from app.core.enums import ActorType, RefKind
from app.core.models import Actor, AuditEvent, EntityRef
from app.core.models.types import JsonObject
from app.conversation.ids import stable_id
from app.persistence import UnitOfWork
from app.persistence.serialization import dumps_json

CONVERSATION_ACTOR = Actor(type=ActorType.SYSTEM, id="conversation_service")


def ref(kind: RefKind, entity_id: str) -> EntityRef:
    return EntityRef(kind=kind, id=entity_id)


def append_event(
    uow: UnitOfWork,
    *,
    key: tuple[str, ...],
    event_type: str,
    subjects: tuple[EntityRef, ...],
    after: JsonObject,
    correlation_id: str,
    now: datetime,
    actor: Actor = CONVERSATION_ACTOR,
) -> None:
    """Append once: ``key`` makes the event identity deterministic, so a replayed step never
    records the same fact twice."""
    event_id = stable_id("ae", "conversation", event_type, *key)
    if uow.audit.get(event_id) is not None:
        return
    payload: dict[str, JsonValue] = {"event_type": event_type, "before": None, "after": after}
    uow.audit.append(
        AuditEvent(
            event_id=event_id, occurred_at=now, actor=actor, event_type=event_type,
            subject_refs=tuple(dict.fromkeys(subjects)), after=after, correlation_id=correlation_id,
            payload_hash=hashlib.sha256(dumps_json(payload).encode("utf-8")).hexdigest(),
        )
    )
