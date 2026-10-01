"""Anthropic Messages API (``POST /v1/messages``), direct HTTPS.

The output contract (JSON schema) is part of the system prompt; the answer is parsed and
validated locally (``StructuredLLM``). No tools, no streaming.
"""

from typing import Any

from app.integrations.llm.base import HttpLLMTransport, Parsed, count, failure, temperature
from app.llm import LLMErrorCode, LLMRequest

API = "https://api.anthropic.com/v1/messages"
VERSION = "2023-06-01"


class AnthropicTransport(HttpLLMTransport):
    provider_name = "anthropic"

    def _url(self) -> str:
        return API

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._key.get_secret_value(), "anthropic-version": VERSION, "Content-Type": "application/json"}

    def _body(self, request: LLMRequest, system: str, user: str) -> dict[str, Any]:
        return {"model": self._model, "max_tokens": self._max_output, "system": system,
                "messages": [{"role": "user", "content": user}], "temperature": temperature(request.task)}

    def _error(self, status: int, payload: Any) -> LLMErrorCode:
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        kind = str(error.get("type") or "")
        message = str(error.get("message") or "").lower()  # used to classify only; never surfaced
        if status == 401 or kind == "authentication_error" or status == 403 or kind == "permission_error":
            return LLMErrorCode.AUTH_INVALID
        if status == 404 or kind == "not_found_error":
            return LLMErrorCode.MODEL_NOT_FOUND
        if status == 429 or kind == "rate_limit_error":
            return LLMErrorCode.RATE_LIMITED
        if kind == "billing_error" or "credit balance" in message:
            return LLMErrorCode.QUOTA_EXCEEDED
        if status == 413 or kind == "request_too_large" or "prompt is too long" in message:
            return LLMErrorCode.INPUT_TOO_LARGE
        if status >= 500 or kind in ("api_error", "overloaded_error"):
            return LLMErrorCode.TEMPORARY_PROVIDER_ERROR
        return LLMErrorCode.BAD_REQUEST

    def _parse(self, payload: dict[str, Any], request_id: str | None) -> Parsed:
        if payload.get("type") != "message":
            raise failure(LLMErrorCode.INVALID_RESPONSE)
        stop = payload.get("stop_reason")
        if stop == "max_tokens":
            raise failure(LLMErrorCode.OUTPUT_TRUNCATED)
        if stop == "refusal":
            raise failure(LLMErrorCode.CONTENT_BLOCKED)
        if stop not in ("end_turn", "stop_sequence"):
            raise failure(LLMErrorCode.INVALID_RESPONSE)
        texts = [block["text"] for block in payload["content"] if block.get("type") == "text"]
        if not texts or not all(isinstance(t, str) for t in texts):
            raise failure(LLMErrorCode.INVALID_RESPONSE)
        usage = payload.get("usage") or {}
        return Parsed(text="".join(texts), model=payload.get("model"), request_id=request_id or payload.get("id"),
                      input_tokens=count(usage.get("input_tokens")), output_tokens=count(usage.get("output_tokens")))
