"""Deterministic fake transport for tests and offline development. No network, no randomness.

Responses are scripted per task and served first-in, first-out; every request is recorded
in call order. A call with nothing scripted raises LLMNoScriptedResponseError.
"""

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel

from app.llm.errors import LLMNoScriptedResponseError, LLMProviderError, LLMTimeoutError
from app.llm.models import LLMRawOutput, LLMRequest, LLMTask, canonical_json

FAKE_PROVIDER = "fake"
FAKE_MODEL = "fake-model-1"


class _Kind(StrEnum):
    TEXT = "TEXT"
    TIMEOUT = "TIMEOUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    CRASH = "CRASH"


@dataclass(frozen=True)
class FakeResponse:
    kind: _Kind
    text: str = ""

    @classmethod
    def of(cls, value: BaseModel | Mapping[str, object]) -> "FakeResponse":
        """A well-formed response: a model or mapping serialized as canonical JSON."""
        data = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
        return cls(_Kind.TEXT, canonical_json(data))

    @classmethod
    def raw(cls, text: str) -> "FakeResponse":
        """Exactly this text, e.g. malformed JSON or the wrong schema."""
        return cls(_Kind.TEXT, text)

    @classmethod
    def timeout(cls) -> "FakeResponse":
        return cls(_Kind.TIMEOUT)

    @classmethod
    def provider_error(cls) -> "FakeResponse":
        return cls(_Kind.PROVIDER_ERROR)

    @classmethod
    def crash(cls) -> "FakeResponse":
        """Raise a non-LLM exception, as a buggy provider SDK might."""
        return cls(_Kind.CRASH)


class FakeLLMTransport:
    def __init__(self) -> None:
        self._scripts: dict[LLMTask, deque[FakeResponse]] = {}
        self.requests: list[LLMRequest] = []

    def script(self, task: LLMTask, *responses: FakeResponse) -> "FakeLLMTransport":
        self._scripts.setdefault(task, deque()).extend(responses)
        return self

    def pending(self, task: LLMTask) -> int:
        return len(self._scripts.get(task, ()))

    def generate(self, request: LLMRequest) -> LLMRawOutput:
        self.requests.append(request)
        queue = self._scripts.get(request.task)
        if not queue:
            raise LLMNoScriptedResponseError(f"no scripted response for {request.task}")
        response = queue.popleft()
        if response.kind is _Kind.TIMEOUT:
            raise LLMTimeoutError(f"fake timeout for {request.task}")
        if response.kind is _Kind.PROVIDER_ERROR:
            raise LLMProviderError(f"fake provider error for {request.task}")
        if response.kind is _Kind.CRASH:
            raise RuntimeError("fake SDK crash")
        return LLMRawOutput(text=response.text, model_name=FAKE_MODEL, provider_name=FAKE_PROVIDER)
