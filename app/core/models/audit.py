from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, Field, StringConstraints

from app.core.models.base import CoreModel
from app.core.models.refs import Actor, EntityRef
from app.core.models.types import (
    EntityId,
    JsonObject,
    NonEmptyStr,
    Sha256Hex,
    UniqueEntityIds,
    UniqueSha256,
)
from app.core.validation import unique_items

UniqueRefs = Annotated[tuple[EntityRef, ...], AfterValidator(unique_items)]

# Upper snake case, e.g. "LEAD_STAGE_CHANGED".
EventType = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$")]


class ProvenanceRecord(CoreModel):
    """How a generated artefact (classification, draft, summary) was produced. Immutable."""

    artifact_ref: EntityRef
    model: NonEmptyStr
    prompt_template_id: NonEmptyStr
    prompt_template_version: NonEmptyStr
    input_refs: UniqueRefs = ()
    input_hashes: UniqueSha256 = ()
    evidence_ids: UniqueEntityIds = ()
    code_version: NonEmptyStr
    created_at: AwareDatetime


class AuditEvent(CoreModel):
    """Append-only record of something that happened. Immutable."""

    event_id: EntityId
    occurred_at: AwareDatetime
    actor: Actor
    event_type: EventType
    subject_refs: Annotated[UniqueRefs, Field(min_length=1)]
    before: JsonObject | None = None
    after: JsonObject | None = None
    correlation_id: EntityId
    payload_hash: Sha256Hex
