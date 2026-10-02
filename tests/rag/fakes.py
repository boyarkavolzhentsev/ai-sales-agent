"""Embeddings-provider fakes beneath the adapters (no network; tests/conftest.py blocks
sockets): an HTTP session that answers in each vendor's real embeddings envelope, with
deterministic "topic" vectors so relevance is controlled and explainable in tests."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tests.llm_providers.fakes import FakeSession, Response

EMBEDDINGS_KEY = "test-secret-do-not-use-embeddings-key"
EMBEDDING_PROVIDERS = ("openai", "gemini")

# One axis per topic; a text's vector counts its topic words, plus a small constant axis so
# no vector is zero. Unrelated topics are orthogonal: similarity ~ 0.
TOPICS: tuple[frozenset[str], ...] = (
    frozenset({"price", "prices", "pricing", "cost", "costs", "much", "eur", "plan", "plans", "basic", "team", "billing",
               "invoices", "monthly", "month"}),
    frozenset({"sso", "saml", "okta", "login", "sign"}),
    frozenset({"uptime", "sla", "availability", "guarantee", "guaranteed"}),
    frozenset({"case", "study", "retailer", "reduced", "results", "customers"}),
    frozenset({"integrate", "integrates", "integrations", "webhook", "api", "export", "exports", "spreadsheet"}),
    frozenset({"onboarding", "weeks", "hours", "working"}),
    frozenset({"company", "headquarters", "offices", "sampletown", "fictional"}),
)
BASE = 0.05
NATIVE_DIMS = len(TOPICS) + 1


def topic_vector(text: str, dims: int = NATIVE_DIMS) -> list[float]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    vector = [float(sum(word in topic for word in words)) for topic in TOPICS] + [BASE]
    return (vector + [0.0] * dims)[:dims]


def inputs_of(url: str, body: dict[str, Any]) -> list[str]:
    if "openai.com" in url:
        return list(body["input"])
    return [r["content"]["parts"][0]["text"] for r in body["requests"]]


def provider_of(url: str) -> str:
    return "openai" if "openai.com" in url else "gemini"


def ok(provider: str, vectors: list[list[float]], *, model: str | None = None, tokens: int = 42) -> Response:
    if provider == "openai":
        # Deliberately out of order: the adapter must restore input order by ``index``.
        data = [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)][::-1]
        return Response(200, {"object": "list", "data": data, "model": model or "openai-embed-under-test",
                              "usage": {"prompt_tokens": tokens, "total_tokens": tokens}}, {"x-request-id": "req_emb_1"})
    return Response(200, {"embeddings": [{"values": v} for v in vectors]})


def failure(provider: str, code: str) -> Response:
    """The vendor's error envelope for a normalized failure code (message carries the key, to
    prove it is never surfaced)."""
    detail = f"secret detail {EMBEDDINGS_KEY}"
    openai = {  # (status, type, code, message prefix)
        "AUTH_INVALID": (401, "invalid_request_error", "invalid_api_key", ""),
        "RATE_LIMITED": (429, "requests", "rate_limit_exceeded", ""),
        "QUOTA_EXCEEDED": (429, "insufficient_quota", "insufficient_quota", ""),
        "MODEL_NOT_FOUND": (404, "invalid_request_error", "model_not_found", ""),
        "BAD_REQUEST": (400, "invalid_request_error", "invalid_value", ""),
        "INPUT_TOO_LARGE": (400, "invalid_request_error", None, "This model's maximum context length is 8192 tokens. "),
        "TEMPORARY_PROVIDER_ERROR": (500, "server_error", None, ""),
    }
    gemini = {
        "AUTH_INVALID": (400, ("INVALID_ARGUMENT", "API_KEY_INVALID")), "RATE_LIMITED": (429, ("RESOURCE_EXHAUSTED", None)),
        "MODEL_NOT_FOUND": (404, ("NOT_FOUND", None)), "BAD_REQUEST": (400, ("INVALID_ARGUMENT", None)),
        "INPUT_TOO_LARGE": (413, ("INVALID_ARGUMENT", None)), "TEMPORARY_PROVIDER_ERROR": (500, ("INTERNAL", None)),
    }
    if provider == "openai":
        status, kind, error_code, prefix = openai[code]
        return Response(status, {"error": {"message": prefix + detail, "type": kind, "code": error_code}})
    status, (state, reason) = gemini[code]
    details = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}] if reason else []
    return Response(status, {"error": {"code": status, "message": detail, "status": state, "details": details}})


def supports(provider: str, code: str) -> bool:
    return not (provider == "gemini" and code == "QUOTA_EXCEEDED")  # Gemini cannot tell quota from a rate limit


@dataclass
class Vendor:
    """A fake embeddings API for both vendors: answers each request with topic vectors in the
    calling vendor's envelope. ``script`` queues explicit responses/exceptions first;
    ``hook`` runs before each answer (e.g. to interleave a concurrent indexer)."""

    dims: int = NATIVE_DIMS
    vectorize: Callable[[str, int], list[float]] = topic_vector
    hook: Callable[[list[str]], None] | None = None
    scripted: list[Any] = field(default_factory=list)
    batches: list[list[str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.session = FakeSession(responder=self._respond)

    def script(self, *items: Any) -> "Vendor":
        self.scripted.extend(items)
        return self

    @property
    def texts(self) -> list[str]:
        return [text for batch in self.batches for text in batch]

    def _respond(self, url: str, body: dict[str, Any]) -> Any:
        texts = inputs_of(url, body)
        self.batches.append(texts)
        if self.hook is not None:
            self.hook(texts)
        if self.scripted:
            item = self.scripted.pop(0)
            if callable(item) and not isinstance(item, Response):
                item = item(url, body)
            if isinstance(item, BaseException):
                raise item
            return item
        return ok(provider_of(url), [self.vectorize(text, self.dims) for text in texts], model=body.get("model"))
