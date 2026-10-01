"""Shared plumbing of the direct-HTTPS LLM adapters (OpenAI, Anthropic, Gemini).

An adapter implements the existing ``LLMTransport``: it only moves one typed request to the
configured model and returns the model's text plus safe diagnostics. It never validates,
repairs or interprets output (``StructuredLLM`` and the domain validators do).

Request: the template's INSTRUCTIONS become the system prompt, followed by the output
contract (the JSON schema of the requested type); every other section is rendered as a
fenced block in one user message, with its kind (UNTRUSTED_DATA stays marked as data). No
tools, no web search, no streaming, no provider-side storage where the API allows opting out.

Bounds: input size (refused locally above ``MAX_INPUT_CHARS``), output tokens (configured),
one request per call with a bounded timeout. At most ONE extra attempt, and only when the
first one certainly produced nothing: the connection was never established, or the
provider answered "overloaded/unavailable" (503/529). Timeouts, 429s, 5xx after processing
and everything else are never retried here: callers fail closed and a later run may retry.

The API key travels only in a request header. Errors carry a stable code; the provider's
message is used to classify only and is never surfaced, logged or stored. Logs carry
provider, model, task, outcome, latency, attempt, request id and token counts: no content.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr
from requests import exceptions as http
from urllib3 import exceptions as wire

from app.llm import LLMError, LLMErrorCode, LLMProviderError, LLMRawOutput, LLMRequest, LLMTask, LLMTimeoutError, SectionKind
from app.llm.models import canonical_json

MAX_INPUT_CHARS = 60_000  # prompt + data; larger requests are refused before any network call
RETRY_STATUSES = frozenset({503, 529})  # "overloaded / unavailable": the request was not processed
# Low randomness everywhere; extraction and classification are as deterministic as the provider allows.
TEMPERATURE = {LLMTask.REPLY_COMPOSITION: 0.3, LLMTask.THREAD_SUMMARY: 0.2}
LOG = logging.getLogger("app.integrations.llm")


@dataclass(frozen=True)
class Reply:
    status: int
    payload: Any
    request_id: str | None


@dataclass(frozen=True)
class Parsed:
    text: str
    model: str | None
    request_id: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None


def count(value: object) -> int | None:
    """A token count, if the provider reported a sane one."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def temperature(task: LLMTask) -> float:
    return TEMPERATURE.get(task, 0.0)


def system_prompt(request: LLMRequest) -> str:
    instructions = "\n\n".join(s.content for s in request.sections if s.kind is SectionKind.INSTRUCTIONS)
    return (f"{instructions}\n\nOutput contract: reply with exactly one JSON object and nothing else (no prose, no "
            f"Markdown). It must be valid against the JSON schema named {request.output_schema_name}:\n"
            f"{canonical_json(request.output_json_schema)}")


def user_prompt(request: LLMRequest) -> str:
    parts = [f"<<<{s.kind}:{s.label}>>>\n{s.content}\n<<<END {s.kind}:{s.label}>>>"
             for s in request.sections if s.kind is not SectionKind.INSTRUCTIONS]
    return "\n\n".join(parts)


def strip_outer_fence(text: str) -> str:
    """Remove exactly one outer Markdown code fence (```json ... ``` or ``` ... ```) around
    the whole answer. Nothing else is touched: no searching for JSON inside prose."""
    stripped = text.strip()
    for opener in ("```json\n", "```JSON\n", "```\n"):
        if stripped.startswith(opener) and stripped.endswith("\n```") and stripped.count("```") == 2:
            return stripped[len(opener):-len("\n```")]
    return text


def failure(code: LLMErrorCode) -> LLMError:
    if code is LLMErrorCode.TIMEOUT:
        return LLMTimeoutError(code=code)
    return LLMProviderError(code=code)


def never_connected(exc: BaseException) -> bool:
    """True only when no connection was established, so nothing was sent."""
    if isinstance(exc, http.ConnectTimeout):
        return True
    if isinstance(exc, http.ConnectionError) and not isinstance(exc, http.ReadTimeout):
        reason = exc.args[0] if exc.args else None
        reason = getattr(reason, "reason", reason)
        return isinstance(reason, wire.NewConnectionError)
    return False


class HttpLLMTransport:
    """One provider adapter. Subclasses translate the request and the response only."""

    provider_name = "abstract"

    def __init__(self, *, api_key: SecretStr, model: str, timeout_seconds: int, max_output_tokens: int,
                 session: Any = None) -> None:
        self._key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._max_output = max_output_tokens
        self._session = session

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self._model!r}, key=<redacted>)"

    # ---- Provider translation (subclasses) -------------------------------------------------

    def _url(self) -> str:
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _body(self, request: LLMRequest, system: str, user: str) -> dict[str, Any]:
        raise NotImplementedError

    def _error(self, status: int, payload: Any) -> LLMErrorCode:
        raise NotImplementedError

    def _parse(self, payload: dict[str, Any], request_id: str | None) -> Parsed:
        raise NotImplementedError

    # ---- LLMTransport ----------------------------------------------------------------------

    def generate(self, request: LLMRequest) -> LLMRawOutput:
        started = time.monotonic()
        attempt = 1
        try:
            system, user = system_prompt(request), user_prompt(request)
            if len(system) + len(user) > MAX_INPUT_CHARS:
                raise failure(LLMErrorCode.INPUT_TOO_LARGE)  # refused locally: nothing is sent
            body = self._body(request, system, user)
            reply, retry = self._post(body)
            if retry:
                attempt = 2
                reply, _ = self._post(body)
            if reply.status == 0 or reply.status >= 400 or not isinstance(reply.payload, dict):
                raise failure(self._status_code(reply))
            try:
                parsed = self._parse(reply.payload, reply.request_id)
            except LLMError:
                raise
            except (KeyError, TypeError, ValueError, AttributeError, IndexError):
                raise failure(LLMErrorCode.INVALID_RESPONSE) from None
        except LLMError as exc:
            self._log(request, exc.code.value, started, attempt, None)
            raise
        output = LLMRawOutput(text=strip_outer_fence(parsed.text), model_name=parsed.model or self._model,
                              provider_name=self.provider_name, attempt=attempt,
                              request_id=(parsed.request_id or None) and parsed.request_id[:200],
                              input_tokens=parsed.input_tokens, output_tokens=parsed.output_tokens)
        self._log(request, "OK", started, attempt, output)
        return output

    def _status_code(self, reply: Reply) -> LLMErrorCode:
        if reply.status == 0:
            return LLMErrorCode.NETWORK_ERROR  # never connected, twice
        if reply.status >= 400:
            return self._error(reply.status, reply.payload if isinstance(reply.payload, dict) else {})
        return LLMErrorCode.INVALID_RESPONSE  # a 2xx that is not a JSON object

    def _post(self, body: dict[str, Any]) -> tuple[Reply, bool]:
        """(reply, worth one retry). Raises a coded LLMError for transport failures."""
        try:
            response = self._http().post(self._url(), json=body, headers=self._headers(),
                                         timeout=(min(10, self._timeout), self._timeout))
        except http.Timeout as exc:
            if never_connected(exc):
                return Reply(0, None, None), True
            raise failure(LLMErrorCode.TIMEOUT) from None
        except (http.RequestException, OSError) as exc:  # never the message: it may hold request details
            if never_connected(exc):
                return Reply(0, None, None), True
            raise failure(LLMErrorCode.NETWORK_ERROR) from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        headers = getattr(response, "headers", None) or {}
        request_id = headers.get("x-request-id") or headers.get("request-id")
        status = int(response.status_code)
        return Reply(status, payload, request_id), status in RETRY_STATUSES

    def _http(self) -> Any:
        if self._session is None:
            import requests

            self._session = requests.Session()  # requests performs no retries by default
        return self._session

    def _log(self, request: LLMRequest, outcome: str, started: float, attempt: int, output: LLMRawOutput | None) -> None:
        LOG.info("llm_call provider=%s model=%s task=%s outcome=%s latency_ms=%d attempt=%d request_id=%s "
                 "input_tokens=%s output_tokens=%s", self.provider_name, self._model, request.task.value, outcome,
                 int((time.monotonic() - started) * 1000), attempt, output.request_id if output else None,
                 output.input_tokens if output else None, output.output_tokens if output else None)
