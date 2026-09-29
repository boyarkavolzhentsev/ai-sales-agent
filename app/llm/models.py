"""Provider-neutral LLM request/response contracts.

A request is a list of typed sections. Only INSTRUCTIONS come from versioned prompt
templates; TRUSTED_METADATA and TRUSTED_EVIDENCE come from deterministic application
state; UNTRUSTED_DATA carries prospect-written text (emails, names), which the model must
treat as data, never as instructions. Transports render sections for their provider, but
the section kind always travels with the content.
"""

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Generic, TypeVar

from pydantic import AwareDatetime, BaseModel, Field, PositiveInt

from app.core.models.base import CoreModel
from app.core.models.types import EntityId, JsonObject, LocaleTag, NonEmptyStr, Sha256Hex

T = TypeVar("T", bound=BaseModel)


class LLMTask(StrEnum):
    INTENT_CLASSIFICATION = "INTENT_CLASSIFICATION"
    KNOWLEDGE_SUFFICIENCY = "KNOWLEDGE_SUFFICIENCY"
    REPLY_COMPOSITION = "REPLY_COMPOSITION"
    THREAD_SUMMARY = "THREAD_SUMMARY"


class SectionKind(StrEnum):
    INSTRUCTIONS = "INSTRUCTIONS"
    TRUSTED_METADATA = "TRUSTED_METADATA"
    TRUSTED_EVIDENCE = "TRUSTED_EVIDENCE"
    UNTRUSTED_DATA = "UNTRUSTED_DATA"


class PromptSection(CoreModel):
    kind: SectionKind
    label: NonEmptyStr
    content: str


class LLMRequest(CoreModel):
    """Everything a transport may send to a model. Contains no secrets."""

    task: LLMTask
    prompt_id: NonEmptyStr
    prompt_version: NonEmptyStr
    correlation_id: EntityId
    locale: LocaleTag
    model_hint: NonEmptyStr | None = None
    sections: Annotated[tuple[PromptSection, ...], Field(min_length=2)]
    output_schema_name: NonEmptyStr
    output_json_schema: JsonObject
    input_hash: Sha256Hex


class LLMRawOutput(CoreModel):
    """What a transport returns: the model's text plus identity. Validation happens in
    StructuredLLM, never in the transport."""

    text: str
    model_name: NonEmptyStr
    provider_name: NonEmptyStr
    attempt: PositiveInt = 1


class LLMResultMetadata(CoreModel):
    """Enough for a later ProvenanceRecord."""

    task: LLMTask
    prompt_id: NonEmptyStr
    prompt_version: NonEmptyStr
    model_name: NonEmptyStr
    provider_name: NonEmptyStr
    input_hash: Sha256Hex
    output_hash: Sha256Hex
    attempt: PositiveInt
    created_at: AwareDatetime


@dataclass(frozen=True)
class StructuredLLMRequest(Generic[T]):
    request: LLMRequest
    output_type: type[T]


@dataclass(frozen=True)
class StructuredLLMResult(Generic[T]):
    output: T
    metadata: LLMResultMetadata


def canonical_json(value: object) -> str:
    """Deterministic JSON: sorted keys, fixed separators, UTF-8 text, no NaN."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
