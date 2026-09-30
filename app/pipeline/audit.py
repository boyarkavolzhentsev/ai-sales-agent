"""Pipeline audit records: IDs, normalized before/after state, actor, reason codes and the
correlation id. Never message bodies."""

import hashlib
from datetime import datetime

from pydantic import JsonValue

from app.core.enums import ActorType, RefKind
from app.core.models import Actor, AuditEvent, EntityRef
from app.core.models.types import JsonObject
from app.inbound.models import stable_id
from app.persistence import UnitOfWork
from app.persistence.serialization import dumps_json

PIPELINE_ACTOR = Actor(type=ActorType.SYSTEM, id="pipeline_service")


def ref(kind: RefKind, entity_id: str) -> EntityRef:
    return EntityRef(kind=kind, id=entity_id)


def operator_actor(operator_id: str) -> Actor:
    return Actor(type=ActorType.OPERATOR, id=operator_id)


def record_event(
    uow: UnitOfWork, *, key: tuple[str, ...], event_type: str, subjects: tuple[EntityRef, ...], after: JsonObject,
    correlation_id: str, now: datetime, before: JsonObject | None = None, actor: Actor = PIPELINE_ACTOR,
) -> None:
    """Append once: ``key`` makes the event identity deterministic, so a replayed step never
    records the same fact twice."""
    event_id = stable_id("ae", "pipeline", event_type, *key)
    if uow.audit.get(event_id) is not None:
        return
    payload: dict[str, JsonValue] = {"event_type": event_type, "before": before, "after": after}
    uow.audit.append(AuditEvent(
        event_id=event_id, occurred_at=now, actor=actor, event_type=event_type,
        subject_refs=tuple(dict.fromkeys(subjects)), before=before, after=after, correlation_id=correlation_id,
        payload_hash=hashlib.sha256(dumps_json(payload).encode("utf-8")).hexdigest(),
    ))
