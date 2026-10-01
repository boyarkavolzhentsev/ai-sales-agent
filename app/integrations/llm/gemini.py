"""Google Gemini API (``models/{model}:generateContent``), direct HTTPS.

JSON output is requested (``responseMimeType``); the schema is part of the system
instruction and the answer is validated locally. No tools: no Google Search grounding, no
code execution, no function calling. The key is sent in a header, never in the URL.
Gemini does not reliably distinguish a per-minute rate limit from an exhausted quota, so
every 429 is RATE_LIMITED.
"""

from typing import Any
from urllib.parse import quote

from app.integrations.llm.base import HttpLLMTransport, Parsed, count, failure, temperature
from app.llm import LLMErrorCode, LLMRequest

API = "https://generativelanguage.googleapis.com/v1beta/models"
BLOCKED = frozenset({"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"})


class GeminiTransport(HttpLLMTransport):
    provider_name = "gemini"

    def _url(self) -> str:
        return f"{API}/{quote(self._model, safe='')}:generateContent"

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._key.get_secret_value(), "Content-Type": "application/json"}

    def _body(self, request: LLMRequest, system: str, user: str) -> dict[str, Any]:
        return {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": temperature(request.task), "maxOutputTokens": self._max_output,
                                 "responseMimeType": "application/json"},
        }

    def _error(self, status: int, payload: Any) -> LLMErrorCode:
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        reasons = {str(d.get("reason")) for d in error.get("details") or () if isinstance(d, dict)}
        if status in (401, 403) or "API_KEY_INVALID" in reasons:
            return LLMErrorCode.AUTH_INVALID
        if status == 404:
            return LLMErrorCode.MODEL_NOT_FOUND
        if status == 429:
            return LLMErrorCode.RATE_LIMITED
        if status == 413:
            return LLMErrorCode.INPUT_TOO_LARGE
        if status >= 500:
            return LLMErrorCode.TEMPORARY_PROVIDER_ERROR
        return LLMErrorCode.BAD_REQUEST

    def _parse(self, payload: dict[str, Any], request_id: str | None) -> Parsed:
        if (payload.get("promptFeedback") or {}).get("blockReason"):
            raise failure(LLMErrorCode.CONTENT_BLOCKED)
        candidates = payload.get("candidates") or []
        if not candidates:
            raise failure(LLMErrorCode.INVALID_RESPONSE)
        candidate = candidates[0]
        finish = candidate.get("finishReason")
        if finish == "MAX_TOKENS":
            raise failure(LLMErrorCode.OUTPUT_TRUNCATED)
        if finish in BLOCKED:
            raise failure(LLMErrorCode.CONTENT_BLOCKED)
        if finish != "STOP":
            raise failure(LLMErrorCode.INVALID_RESPONSE)
        texts = [part["text"] for part in candidate["content"]["parts"] if "text" in part and not part.get("thought")]
        if not texts or not all(isinstance(t, str) for t in texts):
            raise failure(LLMErrorCode.INVALID_RESPONSE)
        usage = payload.get("usageMetadata") or {}
        return Parsed(text="".join(texts), model=payload.get("modelVersion"), request_id=request_id or payload.get("responseId"),
                      input_tokens=count(usage.get("promptTokenCount")), output_tokens=count(usage.get("candidatesTokenCount")))
