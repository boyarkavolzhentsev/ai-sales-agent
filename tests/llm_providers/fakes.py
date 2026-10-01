"""Provider-level fakes beneath the LLM adapters (no network; tests/conftest.py blocks
sockets): an HTTP session double that records every POST, envelope builders in each
vendor's real response format, and a schema-aware fake model ("brain") that answers like
a model would, in the vendor's envelope, from the request the adapter actually built."""

import json
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

API_KEY = "test-secret-do-not-use-llm-api-key"
PROVIDERS = ("openai", "anthropic", "gemini")
_SECTION = re.compile(r"<<<(\w+):([^>\n]+)>>>\n(.*?)\n<<<END \1:\2>>>", re.DOTALL)
_SCHEMA = re.compile(r"JSON schema named (\w+):")


@dataclass
class Response:
    status_code: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        if isinstance(self.body, BaseException):
            raise self.body
        return self.body


@dataclass
class Post:
    url: str
    body: dict[str, Any]
    headers: dict[str, str]
    timeout: Any


class FakeSession:
    """Serves scripted responses (or exceptions) in order, or asks ``responder``."""

    def __init__(self, *responses: Any, responder: Callable[[str, dict[str, Any]], Any] | None = None) -> None:
        self.responses = list(responses)
        self.responder = responder
        self.posts: list[Post] = []

    def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: Any) -> Response:  # noqa: A002
        self.posts.append(Post(url, json, dict(headers), timeout))
        item = self.responses.pop(0) if self.responses else self.responder(url, json) if self.responder else None
        if item is None:
            raise AssertionError("no scripted LLM response")
        if isinstance(item, BaseException):
            raise item
        return item


# ---- Vendor envelopes ----------------------------------------------------------------------------------


def ok(provider: str, text: str, *, model: str = "vendor-model-2026-01-01", usage: tuple[int, int] = (120, 30),
       request_id: str = "req_123") -> Response:
    if provider == "openai":
        return Response(200, {"id": "resp_1", "object": "response", "status": "completed", "model": model,
                              "output": [{"type": "reasoning", "summary": []},
                                         {"type": "message", "role": "assistant",
                                          "content": [{"type": "output_text", "text": text, "annotations": []}]}],
                              "usage": {"input_tokens": usage[0], "output_tokens": usage[1], "total_tokens": sum(usage)}},
                        {"x-request-id": request_id})
    if provider == "anthropic":
        return Response(200, {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
                              "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                              "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}}, {"request-id": request_id})
    return Response(200, {"candidates": [{"content": {"role": "model", "parts": [{"text": "thinking...", "thought": True},
                                                                                 {"text": text}]},
                                          "finishReason": "STOP"}],
                          "usageMetadata": {"promptTokenCount": usage[0], "candidatesTokenCount": usage[1]},
                          "modelVersion": model, "responseId": request_id})


def truncated(provider: str) -> Response:
    if provider == "openai":
        return Response(200, {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []})
    if provider == "anthropic":
        return Response(200, {"type": "message", "content": [{"type": "text", "text": '{"intent": "PRI'}],
                              "stop_reason": "max_tokens"})
    return Response(200, {"candidates": [{"content": {"parts": [{"text": '{"intent": "PRI'}]}, "finishReason": "MAX_TOKENS"}]})


def refusal(provider: str) -> Response:
    if provider == "openai":
        return Response(200, {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "refusal", "refusal": "I can't help with that."}]}]})
    if provider == "anthropic":
        return Response(200, {"type": "message", "content": [], "stop_reason": "refusal"})
    return Response(200, {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []})


# (code, http status, vendor error body) per normalized failure.
def failure(provider: str, code: str) -> Response:
    openai = {
        "AUTH_INVALID": (401, {"type": "invalid_request_error", "code": "invalid_api_key"}),
        "RATE_LIMITED": (429, {"type": "requests", "code": "rate_limit_exceeded"}),
        "QUOTA_EXCEEDED": (429, {"type": "insufficient_quota", "code": "insufficient_quota"}),
        "MODEL_NOT_FOUND": (404, {"type": "invalid_request_error", "code": "model_not_found"}),
        "BAD_REQUEST": (400, {"type": "invalid_request_error", "code": "invalid_value"}),
        "INPUT_TOO_LARGE": (400, {"type": "invalid_request_error", "code": "context_length_exceeded"}),
        "CONTENT_BLOCKED": (400, {"type": "invalid_request_error", "code": "content_policy_violation"}),
        "TEMPORARY_PROVIDER_ERROR": (500, {"type": "server_error", "code": None}),
    }
    anthropic = {
        "AUTH_INVALID": (401, "authentication_error"), "RATE_LIMITED": (429, "rate_limit_error"),
        "QUOTA_EXCEEDED": (400, "billing_error"), "MODEL_NOT_FOUND": (404, "not_found_error"),
        "BAD_REQUEST": (400, "invalid_request_error"), "INPUT_TOO_LARGE": (413, "request_too_large"),
        "CONTENT_BLOCKED": None, "TEMPORARY_PROVIDER_ERROR": (500, "api_error"),
    }
    gemini = {
        "AUTH_INVALID": (400, ("INVALID_ARGUMENT", "API_KEY_INVALID")), "RATE_LIMITED": (429, ("RESOURCE_EXHAUSTED", None)),
        "QUOTA_EXCEEDED": None, "MODEL_NOT_FOUND": (404, ("NOT_FOUND", None)),
        "BAD_REQUEST": (400, ("INVALID_ARGUMENT", None)), "INPUT_TOO_LARGE": (413, ("INVALID_ARGUMENT", None)),
        "CONTENT_BLOCKED": None, "TEMPORARY_PROVIDER_ERROR": (500, ("INTERNAL", None)),
    }
    if provider == "openai":
        status, error = openai[code]
        return Response(status, {"error": {"message": f"secret detail {API_KEY}", **error}})
    if provider == "anthropic":
        status, kind = anthropic[code]  # type: ignore[misc]
        return Response(status, {"type": "error", "error": {"type": kind, "message": f"secret detail {API_KEY}"}})
    status, (state, reason) = gemini[code]  # type: ignore[misc]
    details = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}] if reason else []
    return Response(status, {"error": {"code": status, "message": f"secret detail {API_KEY}", "status": state, "details": details}})


def supports(provider: str, code: str) -> bool:
    """Gemini cannot tell a quota from a rate limit; content blocks arrive as 200 envelopes."""
    return not ((provider == "gemini" and code == "QUOTA_EXCEEDED") or (provider != "openai" and code == "CONTENT_BLOCKED"))


# ---- Reading what an adapter sent ---------------------------------------------------------------------


def prompts(post: Post) -> tuple[str, str]:
    body = post.body
    if "instructions" in body:
        return body["instructions"], body["input"][0]["content"]
    if "messages" in body:
        return body["system"], body["messages"][0]["content"]
    return body["systemInstruction"]["parts"][0]["text"], body["contents"][0]["parts"][0]["text"]


def sections(user: str) -> dict[str, Any]:
    return {label: json.loads(content) for _, label, content in _SECTION.findall(user)}


def provider_of(url: str) -> str:
    return "openai" if "openai.com" in url else "anthropic" if "anthropic.com" in url else "gemini"


# ---- A schema-aware fake model -----------------------------------------------------------------------------

Answer = dict[str, Any] | str | Callable[[dict[str, Any]], Any] | Response


class Brain:
    """Answers each request by its output schema: scripted answers first (FIFO per schema),
    else a sensible default. Answers are wrapped in the calling vendor's envelope."""

    def __init__(self) -> None:
        self.scripts: dict[str, deque[Answer]] = {}
        self.calls: list[str] = []
        self.session = FakeSession(responder=self._respond)

    def script(self, schema: str, *answers: Answer) -> "Brain":
        self.scripts.setdefault(schema, deque()).extend(answers)
        return self

    def _respond(self, url: str, body: dict[str, Any]) -> Response:
        provider = provider_of(url)
        system, user = prompts(self.session.posts[-1])
        schema = _SCHEMA.search(system).group(1)  # type: ignore[union-attr]
        self.calls.append(schema)
        data = sections(user)
        queue = self.scripts.get(schema)
        answer: Answer = queue.popleft() if queue else DEFAULTS[schema]
        if isinstance(answer, Response):
            return answer
        if callable(answer):
            answer = answer(data)
        return ok(provider, answer if isinstance(answer, str) else json.dumps(answer))


def _draft(data: dict[str, Any]) -> dict[str, Any]:
    cited = [e["evidence_id"] for e in data["evidence"] if "100 EUR" in e["excerpt"]]
    return {"subject": "Re: Pricing question", "body": "Hi, the Basic plan costs 100 EUR per month.",
            "evidence_ids_used": cited, "proposed_next_step": "ANSWER_QUESTIONS"}


def pricing_intent(intent: str = "PRICING_REQUEST", **extra: Any) -> dict[str, Any]:
    from app.core.enums import LeadIntent
    from tests.inbound.builders import PRICE_QUESTION, classification
    return json.loads(classification(LeadIntent(intent), PRICE_QUESTION, **extra).text)


DEFAULTS: dict[str, Answer] = {
    "IntentClassificationProposal": lambda data: pricing_intent(),
    "KnowledgeSufficiencyOpinion": {"opinion": "SUFFICIENT", "rationale_summary": "Checked."},
    "ReplyDraftProposal": _draft,
    "ThreadSummary": {"summary": "The prospect asked about pricing.", "open_questions": []},
    "QualificationCandidates": {"facts": [], "missing_fields": []},
    "CommercialCandidates": {},
    "SalesRecommendation": {"proposed_action": None, "reasons": ["Qualification facts are still missing."]},
}
