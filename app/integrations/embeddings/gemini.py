"""Google Gemini API embeddings (``models/{model}:batchEmbedContents``), direct HTTPS.

Retrieval task types are used: documents as ``RETRIEVAL_DOCUMENT``, queries as
``RETRIEVAL_QUERY``. ``outputDimensionality`` is sent only when configured (reduced
vectors are not unit length; every vector is normalized after validation anyway). No
tools, no grounding. The key is sent in a header, never in the URL. Gemini does not
reliably distinguish a rate limit from an exhausted quota, so every 429 is RATE_LIMITED.
The response does not report a model.
"""

from typing import Any
from urllib.parse import quote

from app.embeddings import EmbeddingErrorCode, EmbeddingPurpose
from app.integrations.embeddings.base import HttpEmbeddingTransport, Parsed, failure

API = "https://generativelanguage.googleapis.com/v1beta/models"
TASK_TYPES = {EmbeddingPurpose.DOCUMENT: "RETRIEVAL_DOCUMENT", EmbeddingPurpose.QUERY: "RETRIEVAL_QUERY"}


class GeminiEmbeddings(HttpEmbeddingTransport):
    provider_id = "GEMINI"

    def _url(self) -> str:
        return f"{API}/{quote(self._space.model, safe='')}:batchEmbedContents"

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._key.get_secret_value(), "Content-Type": "application/json"}

    def _body(self, texts: list[str], purpose: EmbeddingPurpose) -> dict[str, Any]:
        requests = []
        for text in texts:
            request: dict[str, Any] = {"model": f"models/{self._space.model}", "content": {"parts": [{"text": text}]},
                                       "taskType": TASK_TYPES[purpose]}
            if self._space.dimensions is not None:
                request["outputDimensionality"] = self._space.dimensions
            requests.append(request)
        return {"requests": requests}

    def _error(self, status: int, payload: dict[str, Any]) -> EmbeddingErrorCode:
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        reasons = {str(d.get("reason")) for d in error.get("details") or () if isinstance(d, dict)}
        if status in (401, 403) or "API_KEY_INVALID" in reasons:
            return EmbeddingErrorCode.AUTH_INVALID
        if status == 404:
            return EmbeddingErrorCode.MODEL_NOT_FOUND
        if status == 429:
            return EmbeddingErrorCode.RATE_LIMITED
        if status == 413:
            return EmbeddingErrorCode.INPUT_TOO_LARGE
        if status >= 500:
            return EmbeddingErrorCode.TEMPORARY_PROVIDER_ERROR
        return EmbeddingErrorCode.BAD_REQUEST

    def _parse(self, payload: dict[str, Any], expected: int, request_id: str | None) -> Parsed:
        items = payload["embeddings"]
        if not isinstance(items, list) or len(items) != expected:
            raise failure(EmbeddingErrorCode.INVALID_RESPONSE)
        return Parsed(vectors=[item["values"] for item in items], request_id=request_id)
