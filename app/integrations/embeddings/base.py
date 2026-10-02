"""Shared plumbing of the direct-HTTPS embeddings adapters (OpenAI, Gemini).

An adapter implements the provider-neutral ``EmbeddingTransport``: it moves one bounded
batch of texts to the configured model and returns validated, L2-normalized vectors
(``app.embeddings.validate_batch``). It owns no business logic: what is embedded, stored or
searched is decided in ``app.knowledge``.

Bounds: at most ``MAX_BATCH`` texts per call, each non-blank and at most
``MAX_INPUT_CHARS`` characters (refused locally with INPUT_TOO_LARGE before any network
call: nothing is ever truncated); one request per call with the configured timeout. At most
ONE extra attempt, and only when the first one certainly produced nothing: the connection
was never established, or the provider answered 503 "unavailable". Embedding calls change
no provider state but are billable, so timeouts, 429s and everything else are not retried.

The API key travels only in a request header (never in a URL, log, error or repr). Errors
carry a stable code; the provider's message is used to classify only and is never surfaced.
The configured model is authoritative: a response that reports a different model is
INVALID_RESPONSE (a provider revision tag of the same model, ``<model>-v<n>``, is the same
model). Logs carry provider, model, purpose, input count, outcome, latency, attempt, request
id, token count and dimensions: never texts or vectors.
"""

import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr
from requests import exceptions as http
from urllib3 import exceptions as wire

from app.embeddings import (
    EmbeddingError,
    EmbeddingErrorCode,
    EmbeddingPurpose,
    EmbeddingResult,
    EmbeddingSpace,
    validate_batch,
)

MAX_BATCH = 100
MAX_INPUT_CHARS = 8_000
RETRY_STATUSES = frozenset({503})  # "unavailable": the request was not processed
LOG = logging.getLogger("app.integrations.embeddings")


@dataclass(frozen=True)
class Reply:
    status: int
    payload: Any
    request_id: str | None


@dataclass(frozen=True)
class Parsed:
    vectors: Any  # raw, validated by validate_batch
    model: str | None = None
    request_id: str | None = None
    input_tokens: int | None = None


def count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def failure(code: EmbeddingErrorCode) -> EmbeddingError:
    return EmbeddingError(code)


def never_connected(exc: BaseException) -> bool:
    """True only when no connection was established, so nothing was sent."""
    if isinstance(exc, http.ConnectTimeout):
        return True
    if isinstance(exc, http.ConnectionError) and not isinstance(exc, http.ReadTimeout):
        reason = exc.args[0] if exc.args else None
        reason = getattr(reason, "reason", reason)
        return isinstance(reason, wire.NewConnectionError)
    return False


def same_model(configured: str, reported: str) -> bool:
    reported = reported.removeprefix("models/")
    return reported == configured or re.fullmatch(re.escape(configured) + r"-v\d+", reported) is not None


class HttpEmbeddingTransport:
    """One provider adapter. Subclasses translate the request and the response only."""

    provider_id = "ABSTRACT"

    def __init__(self, *, api_key: SecretStr, model: str, dimensions: int | None, timeout_seconds: int,
                 session: Any = None) -> None:
        self._key = api_key
        self._space = EmbeddingSpace(provider=self.provider_id, model=model, dimensions=dimensions)
        self._timeout = timeout_seconds
        self._session = session

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self._space.model!r}, dimensions={self._space.dimensions}, key=<redacted>)"

    @property
    def space(self) -> EmbeddingSpace:
        return self._space

    # ---- Provider translation (subclasses) -------------------------------------------------

    def _url(self) -> str:
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _body(self, texts: list[str], purpose: EmbeddingPurpose) -> dict[str, Any]:
        raise NotImplementedError

    def _error(self, status: int, payload: dict[str, Any]) -> EmbeddingErrorCode:
        raise NotImplementedError

    def _parse(self, payload: dict[str, Any], expected: int, request_id: str | None) -> Parsed:
        raise NotImplementedError

    # ---- EmbeddingTransport ----------------------------------------------------------------

    def embed(self, texts: Sequence[str], purpose: EmbeddingPurpose) -> EmbeddingResult:
        started = time.monotonic()
        attempt = 1
        texts = list(texts)
        try:
            if not texts or len(texts) > MAX_BATCH or not all(isinstance(t, str) and t.strip() for t in texts):
                raise failure(EmbeddingErrorCode.BAD_REQUEST)  # refused locally: nothing is sent
            if any(len(t) > MAX_INPUT_CHARS for t in texts):
                raise failure(EmbeddingErrorCode.INPUT_TOO_LARGE)
            body = self._body(texts, purpose)
            reply, retry = self._post(body)
            if retry:
                attempt = 2
                reply, _ = self._post(body)
            if reply.status == 0 or reply.status >= 400 or not isinstance(reply.payload, dict):
                raise failure(self._status_code(reply))
            try:
                parsed = self._parse(reply.payload, len(texts), reply.request_id)
            except EmbeddingError:
                raise
            except (KeyError, TypeError, ValueError, AttributeError, IndexError):
                raise failure(EmbeddingErrorCode.INVALID_RESPONSE) from None
            if parsed.model is not None and (not isinstance(parsed.model, str)
                                             or not same_model(self._space.model, parsed.model)):
                raise failure(EmbeddingErrorCode.INVALID_RESPONSE)  # vectors of another model: never stored
            vectors = validate_batch(parsed.vectors, count=len(texts), dimensions=self._space.dimensions)
        except EmbeddingError as exc:
            self._log(purpose, len(texts), exc.code.value, started, attempt, None, None, None)
            raise
        request_id = (parsed.request_id or None) and str(parsed.request_id)[:200]
        result = EmbeddingResult(vectors=vectors, space=self._space, dimensions=len(vectors[0]), request_id=request_id,
                                 input_tokens=parsed.input_tokens)
        self._log(purpose, len(texts), "OK", started, attempt, request_id, parsed.input_tokens, result.dimensions)
        return result

    def _status_code(self, reply: Reply) -> EmbeddingErrorCode:
        if reply.status == 0:
            return EmbeddingErrorCode.NETWORK_ERROR  # never connected, twice
        if reply.status >= 400:
            return self._error(reply.status, reply.payload if isinstance(reply.payload, dict) else {})
        return EmbeddingErrorCode.INVALID_RESPONSE  # a 2xx that is not a JSON object

    def _post(self, body: dict[str, Any]) -> tuple[Reply, bool]:
        """(reply, worth one retry). Raises a coded EmbeddingError for transport failures."""
        try:
            response = self._http().post(self._url(), json=body, headers=self._headers(),
                                         timeout=(min(10, self._timeout), self._timeout))
        except http.Timeout as exc:
            if never_connected(exc):
                return Reply(0, None, None), True
            raise failure(EmbeddingErrorCode.TIMEOUT) from None
        except (http.RequestException, OSError) as exc:  # never the message: it may hold request details
            if never_connected(exc):
                return Reply(0, None, None), True
            raise failure(EmbeddingErrorCode.NETWORK_ERROR) from None
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

    def _log(self, purpose: EmbeddingPurpose, inputs: int, outcome: str, started: float, attempt: int,
             request_id: str | None, tokens: int | None, dimensions: int | None) -> None:
        LOG.info("embedding_call provider=%s model=%s purpose=%s inputs=%d outcome=%s latency_ms=%d attempt=%d "
                 "request_id=%s input_tokens=%s dimensions=%s", self._space.provider, self._space.model, purpose.value,
                 inputs, outcome, int((time.monotonic() - started) * 1000), attempt, request_id, tokens, dimensions)
