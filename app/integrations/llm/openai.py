"""OpenAI Responses API (``POST /v1/responses``), direct HTTPS.

The output contract is sent natively as a ``json_schema`` text format (non-strict: our
Pydantic schemas have optional fields, which strict mode cannot express) and is validated
locally anyway. ``store`` is false, no tools are offered. Temperature is not sent: OpenAI's
reasoning models refuse it, and the output is constrained by the schema and validated.
"""

from typing import Any

from app.integrations.llm.base import HttpLLMTransport, Parsed, count, failure
from app.llm import LLMErrorCode, LLMRequest

API = "https://api.openai.com/v1/responses"


class OpenAITransport(HttpLLMTransport):
    provider_name = "openai"

    def _url(self) -> str:
        return API

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key.get_secret_value()}", "Content-Type": "application/json"}

    def _body(self, request: LLMRequest, system: str, user: str) -> dict[str, Any]:
        return {
            "model": self._model,
            "instructions": system,
            "input": [{"role": "user", "content": user}],
            "max_output_tokens": self._max_output,
            "store": False,
            "text": {"format": {"type": "json_schema", "name": request.output_schema_name[:64],
                                "schema": request.output_json_schema, "strict": False}},
        }

    def _error(self, status: int, payload: Any) -> LLMErrorCode:
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        kind = f"{error.get('code') or ''} {error.get('type') or ''}"
        if status in (401, 403):
            return LLMErrorCode.AUTH_INVALID
        if status == 404 or "model_not_found" in kind:
            return LLMErrorCode.MODEL_NOT_FOUND
        if status == 429:
            return LLMErrorCode.QUOTA_EXCEEDED if "insufficient_quota" in kind else LLMErrorCode.RATE_LIMITED
        if status == 413 or "context_length_exceeded" in kind:
            return LLMErrorCode.INPUT_TOO_LARGE
        if "content_policy" in kind or "content_filter" in kind:
            return LLMErrorCode.CONTENT_BLOCKED
        if status >= 500:
            return LLMErrorCode.TEMPORARY_PROVIDER_ERROR
        return LLMErrorCode.BAD_REQUEST

    def _parse(self, payload: dict[str, Any], request_id: str | None) -> Parsed:
        status = payload.get("status")
        if status == "incomplete":
            reason = (payload.get("incomplete_details") or {}).get("reason")
            raise failure({"max_output_tokens": LLMErrorCode.OUTPUT_TRUNCATED,
                           "content_filter": LLMErrorCode.CONTENT_BLOCKED}.get(reason, LLMErrorCode.INVALID_RESPONSE))
        if status == "failed":
            raise failure(LLMErrorCode.TEMPORARY_PROVIDER_ERROR)
        if status != "completed":
            raise failure(LLMErrorCode.INVALID_RESPONSE)
        texts: list[str] = []
        for item in payload["output"]:
            if item.get("type") != "message":
                continue  # e.g. reasoning items: never part of the answer
            for part in item["content"]:
                if part.get("type") == "refusal":
                    raise failure(LLMErrorCode.CONTENT_BLOCKED)
                if part.get("type") == "output_text":
                    texts.append(part["text"])
        if not texts or not all(isinstance(t, str) for t in texts):
            raise failure(LLMErrorCode.INVALID_RESPONSE)
        usage = payload.get("usage") or {}
        return Parsed(text="".join(texts), model=payload.get("model"), request_id=request_id or payload.get("id"),
                      input_tokens=count(usage.get("input_tokens")), output_tokens=count(usage.get("output_tokens")))
