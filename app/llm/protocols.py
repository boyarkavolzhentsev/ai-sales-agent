from datetime import datetime
from typing import Protocol, runtime_checkable

from app.llm.models import LLMRawOutput, LLMRequest


@runtime_checkable
class LLMTransport(Protocol):
    """A provider adapter. It only moves a typed request to a model and returns raw text
    plus model identity; it never validates, repairs or interprets output. Adapters raise
    LLMTimeoutError / LLMProviderError; anything else is wrapped by StructuredLLM."""

    def generate(self, request: LLMRequest) -> LLMRawOutput: ...


class NowProvider(Protocol):
    """Anything with ``now() -> aware datetime`` (e.g. the persistence clocks)."""

    def now(self) -> datetime: ...
