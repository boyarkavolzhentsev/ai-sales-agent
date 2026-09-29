"""The single structured-output boundary. Every model output passes through
StructuredLLM.complete_structured: strict JSON-object parsing, strict Pydantic validation
(unknown fields rejected), no repair, typed errors only."""

import json
from collections.abc import Collection, Iterable
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.llm.errors import (
    LLMContractViolationError,
    LLMError,
    LLMProviderError,
    LLMStructuredOutputError,
)
from app.llm.models import LLMResultMetadata, StructuredLLMRequest, StructuredLLMResult, sha256_hex
from app.llm.protocols import LLMTransport, NowProvider

T = TypeVar("T", bound=BaseModel)


def parse_structured_output(text: str, output_type: type[T]) -> T:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LLMStructuredOutputError(f"{output_type.__name__}: output is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise LLMStructuredOutputError(f"{output_type.__name__}: output must be a JSON object")
    try:
        return output_type.model_validate(payload)
    except ValidationError as exc:
        raise LLMStructuredOutputError(f"{output_type.__name__}: {exc}") from exc


class StructuredLLM:
    """Runs one typed LLM call through a transport and validates the result."""

    def __init__(self, transport: LLMTransport, clock: NowProvider) -> None:
        self._transport = transport
        self._clock = clock

    def complete_structured(self, call: StructuredLLMRequest[T]) -> StructuredLLMResult[T]:
        request = call.request
        try:
            raw = self._transport.generate(request)
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 - raw provider errors must not cross the boundary
            raise LLMProviderError(f"{request.task}: transport failed ({type(exc).__name__})") from exc
        output = parse_structured_output(raw.text, call.output_type)
        metadata = LLMResultMetadata(
            task=request.task,
            prompt_id=request.prompt_id,
            prompt_version=request.prompt_version,
            model_name=raw.model_name,
            provider_name=raw.provider_name,
            input_hash=request.input_hash,
            output_hash=sha256_hex(raw.text),
            attempt=raw.attempt,
            created_at=self._clock.now(),
        )
        return StructuredLLMResult(output=output, metadata=metadata)


def require_known_evidence_ids(used: Iterable[str], known: Collection[str]) -> None:
    """Every cited evidence ID must be one the model was given."""
    unknown = sorted(set(used) - set(known))
    if unknown:
        raise LLMContractViolationError(f"output cites evidence IDs it was not given: {unknown}")
