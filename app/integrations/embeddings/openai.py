"""OpenAI Embeddings API (``POST /v1/embeddings``), direct HTTPS.

Float encoding is requested explicitly; ``dimensions`` is sent only when configured. The
response's ``data`` items are put back into input order by their ``index`` (every index
exactly once, or the response is invalid). Documents and queries are embedded the same way.
"""

from typing import Any

from app.embeddings import EmbeddingErrorCode, EmbeddingPurpose
from app.integrations.embeddings.base import HttpEmbeddingTransport, Parsed, count, failure

API = "https://api.openai.com/v1/embeddings"


class OpenAIEmbeddings(HttpEmbeddingTransport):
    provider_id = "OPENAI"

    def _url(self) -> str:
        return API

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key.get_secret_value()}", "Content-Type": "application/json"}

    def _body(self, texts: list[str], purpose: EmbeddingPurpose) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self._space.model, "input": texts, "encoding_format": "float"}
        if self._space.dimensions is not None:
            body["dimensions"] = self._space.dimensions
        return body

    def _error(self, status: int, payload: dict[str, Any]) -> EmbeddingErrorCode:
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        kind = f"{error.get('code') or ''} {error.get('type') or ''}"
        message = str(error.get("message") or "").lower()  # used to classify only; never surfaced
        if status in (401, 403):
            return EmbeddingErrorCode.AUTH_INVALID
        if status == 404 or "model_not_found" in kind:
            return EmbeddingErrorCode.MODEL_NOT_FOUND
        if status == 429:
            return EmbeddingErrorCode.QUOTA_EXCEEDED if "insufficient_quota" in kind else EmbeddingErrorCode.RATE_LIMITED
        if status == 413 or "context_length_exceeded" in kind or "maximum context length" in message:
            return EmbeddingErrorCode.INPUT_TOO_LARGE
        if status >= 500:
            return EmbeddingErrorCode.TEMPORARY_PROVIDER_ERROR
        return EmbeddingErrorCode.BAD_REQUEST

    def _parse(self, payload: dict[str, Any], expected: int, request_id: str | None) -> Parsed:
        items = payload["data"]
        if not isinstance(items, list) or len(items) != expected:
            raise failure(EmbeddingErrorCode.INVALID_RESPONSE)
        by_index: dict[int, Any] = {}
        for item in items:
            index = item["index"]
            if type(index) is not int or index in by_index or not 0 <= index < expected:
                raise failure(EmbeddingErrorCode.INVALID_RESPONSE)
            by_index[index] = item["embedding"]
        usage = payload.get("usage") or {}
        return Parsed(vectors=[by_index[i] for i in range(expected)], model=payload.get("model"), request_id=request_id,
                      input_tokens=count(usage.get("prompt_tokens")))
