"""The OpenAI and Gemini embeddings adapters over a fake HTTP session (no network): request
shape, response parsing, validation, error normalization, bounded retries, secrecy."""

import logging

import pytest
from requests import exceptions as http
from urllib3 import exceptions as wire

from app.embeddings import EmbeddingError, EmbeddingErrorCode, EmbeddingPurpose, EmbeddingTransport
from app.integrations.embeddings.base import MAX_BATCH, MAX_INPUT_CHARS
from tests.llm_providers.fakes import Response
from tests.rag.builders import model_of, transport
from tests.rag.fakes import EMBEDDING_PROVIDERS, EMBEDDINGS_KEY, NATIVE_DIMS, Vendor, failure, ok, supports, topic_vector

D, Q = EmbeddingPurpose.DOCUMENT, EmbeddingPurpose.QUERY
CODES = ("AUTH_INVALID", "RATE_LIMITED", "QUOTA_EXCEEDED", "MODEL_NOT_FOUND", "BAD_REQUEST", "INPUT_TOO_LARGE",
         "TEMPORARY_PROVIDER_ERROR")


def code_of(provider: str, vendor: Vendor, texts: list[str] | None = None, **kwargs: object) -> EmbeddingErrorCode:
    with pytest.raises(EmbeddingError) as error:
        transport(provider, vendor, **kwargs).embed(texts or ["one text"], D)  # type: ignore[arg-type]
    assert EMBEDDINGS_KEY not in str(error.value) + repr(error.value)
    return error.value.code


def never_connected() -> http.ConnectionError:
    return http.ConnectionError(wire.MaxRetryError(None, "/", wire.NewConnectionError(None, "refused")))  # type: ignore[arg-type]


# ---- Requests --------------------------------------------------------------------------------------------


def test_openai_request_shape() -> None:
    vendor = Vendor()
    result = transport("openai", vendor, timeout=17).embed(["Basic plan costs", "SSO login"], D)
    [post] = vendor.session.posts
    assert post.url == "https://api.openai.com/v1/embeddings"
    assert post.body == {"model": model_of("openai"), "input": ["Basic plan costs", "SSO login"], "encoding_format": "float"}
    assert post.headers == {"Authorization": f"Bearer {EMBEDDINGS_KEY}", "Content-Type": "application/json"}
    assert post.timeout == (10, 17)
    assert EMBEDDINGS_KEY not in post.url
    assert (result.space.provider, result.space.model, result.dimensions) == ("OPENAI", model_of("openai"), NATIVE_DIMS)
    assert (result.request_id, result.input_tokens) == ("req_emb_1", 42)


def test_openai_sends_dimensions_only_when_configured() -> None:
    vendor = Vendor(dims=4)
    transport("openai", vendor, dims=4).embed(["Basic plan"], Q)
    assert vendor.session.posts[0].body["dimensions"] == 4


@pytest.mark.parametrize("purpose,task", [(D, "RETRIEVAL_DOCUMENT"), (Q, "RETRIEVAL_QUERY")])
def test_gemini_request_shape_and_task_types(purpose: EmbeddingPurpose, task: str) -> None:
    vendor = Vendor()
    transport("gemini", vendor, timeout=5).embed(["Basic plan", "uptime"], purpose)
    [post] = vendor.session.posts
    assert post.url == f"https://generativelanguage.googleapis.com/v1beta/models/{model_of('gemini')}:batchEmbedContents"
    assert post.body == {"requests": [
        {"model": f"models/{model_of('gemini')}", "content": {"parts": [{"text": "Basic plan"}]}, "taskType": task},
        {"model": f"models/{model_of('gemini')}", "content": {"parts": [{"text": "uptime"}]}, "taskType": task}]}
    assert post.headers == {"x-goog-api-key": EMBEDDINGS_KEY, "Content-Type": "application/json"}
    assert post.timeout == (5, 5) and EMBEDDINGS_KEY not in post.url and "key=" not in post.url


def test_gemini_sends_output_dimensionality_only_when_configured() -> None:
    vendor = Vendor(dims=3)
    transport("gemini", vendor, dims=3).embed(["Basic plan"], D)
    assert vendor.session.posts[0].body["requests"][0]["outputDimensionality"] == 3


@pytest.mark.parametrize("provider", EMBEDDING_PROVIDERS)
def test_both_adapters_implement_the_neutral_contract_without_tools_or_extras(provider: str) -> None:
    vendor = Vendor()
    adapter = transport(provider, vendor)
    assert isinstance(adapter, EmbeddingTransport)
    adapter.embed(["x y"], Q)
    flat = repr(vendor.session.posts[0].body)
    assert not any(word in flat for word in ("tools", "google_search", "googleSearch", "stream", "web_search"))


# ---- Responses --------------------------------------------------------------------------------------------


def test_equivalent_vendor_vectors_normalize_identically() -> None:
    texts = ["Basic plan costs 79 EUR per month", "Do you support SSO?", "What uptime do you guarantee?"]
    results = {p: transport(p, Vendor()).embed(texts, D) for p in EMBEDDING_PROVIDERS}
    assert results["openai"].vectors == results["gemini"].vectors  # same raw vectors -> same normalized result
    assert all(abs(sum(x * x for x in v) - 1.0) < 1e-12 for v in results["openai"].vectors)


def test_openai_results_are_put_back_in_input_order_by_index() -> None:
    texts = ["Basic plan", "SSO login", "uptime sla"]
    result = transport("openai", Vendor()).embed(texts, D)  # the fake answers in reverse order
    expected = transport("gemini", Vendor()).embed(texts, D)
    assert result.vectors == expected.vectors


@pytest.mark.parametrize("data", [
    [{"index": 0, "embedding": [1.0, 0.0]}, {"index": 0, "embedding": [0.0, 1.0]}],  # duplicate index
    [{"index": 0, "embedding": [1.0, 0.0]}, {"index": 2, "embedding": [0.0, 1.0]}],  # out of range
    [{"index": "0", "embedding": [1.0, 0.0]}, {"index": 1, "embedding": [0.0, 1.0]}],  # not an int
    [{"index": 0, "embedding": [1.0, 0.0]}],  # too few
])
def test_openai_malformed_index_sets_are_invalid(data: list[dict[str, object]]) -> None:
    vendor = Vendor().script(Response(200, {"object": "list", "data": data, "model": model_of("openai")}))
    assert code_of("openai", vendor, ["a", "b"]) is EmbeddingErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize("provider", EMBEDDING_PROVIDERS)
@pytest.mark.parametrize("vectors,code", [
    ([[float("nan"), 1.0]], "INVALID_RESPONSE"), ([[1.0, 1e309]], "INVALID_RESPONSE"), ([[]], "INVALID_RESPONSE"),
    ([["0.1", 0.2]], "INVALID_RESPONSE"), ([[0.0, 0.0]], "INVALID_RESPONSE"), ([[1.0, 2.0], [1.0]], "DIMENSION_MISMATCH"),
])
def test_bad_vectors_fail_the_whole_batch(provider: str, vectors: list[list[object]], code: str) -> None:
    vendor = Vendor().script(ok(provider, vectors))  # type: ignore[arg-type]
    assert code_of(provider, vendor, ["t"] * len(vectors)) is EmbeddingErrorCode(code)


@pytest.mark.parametrize("provider", EMBEDDING_PROVIDERS)
def test_a_configured_dimensionality_is_enforced_exactly(provider: str) -> None:
    vendor = Vendor(dims=NATIVE_DIMS)  # the provider ignored the requested size
    assert code_of(provider, vendor, dims=4) is EmbeddingErrorCode.DIMENSION_MISMATCH


@pytest.mark.parametrize("provider", EMBEDDING_PROVIDERS)
@pytest.mark.parametrize("payload", [{}, {"data": None}, {"embeddings": "x"}, {"embeddings": [{"vals": [1.0]}]},
                                     {"data": [{"index": 0}]}])
def test_malformed_envelopes_are_invalid(provider: str, payload: dict[str, object]) -> None:
    assert code_of(provider, Vendor().script(Response(200, payload))) is EmbeddingErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize("provider", EMBEDDING_PROVIDERS)
def test_a_non_json_success_is_invalid(provider: str) -> None:
    assert code_of(provider, Vendor().script(Response(200, ValueError("not json")))) is EmbeddingErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize("reported", ["text-embedding-3-large", "other-model", 7])
def test_vectors_reported_for_another_model_are_refused(reported: object) -> None:
    vendor = Vendor().script(ok("openai", [topic_vector("x")], model=reported))  # type: ignore[arg-type]
    assert code_of("openai", vendor) is EmbeddingErrorCode.INVALID_RESPONSE


def test_a_provider_revision_tag_of_the_same_model_is_accepted() -> None:
    vendor = Vendor().script(ok("openai", [topic_vector("x")], model=f"{model_of('openai')}-v2"))
    assert transport("openai", vendor).embed(["x"], D).dimensions == NATIVE_DIMS


# ---- Errors and retries -----------------------------------------------------------------------------------


@pytest.mark.parametrize("provider", EMBEDDING_PROVIDERS)
@pytest.mark.parametrize("code", CODES)
def test_vendor_errors_normalize_to_stable_codes_with_one_request(provider: str, code: str) -> None:
    if not supports(provider, code):
        pytest.skip("not distinguishable for this vendor")
    vendor = Vendor().script(failure(provider, code))
    assert code_of(provider, vendor) is EmbeddingErrorCode(code)
    assert len(vendor.session.posts) == 1  # never retried


@pytest.mark.parametrize("provider", EMBEDDING_PROVIDERS)
def test_gemini_quota_is_reported_as_rate_limited_and_429_is_never_retried(provider: str) -> None:
    vendor = Vendor().script(Response(429, {"error": {"status": "RESOURCE_EXHAUSTED", "type": "x"}}))
    assert code_of(provider, vendor) is EmbeddingErrorCode.RATE_LIMITED and len(vendor.session.posts) == 1


@pytest.mark.parametrize("provider", EMBEDDING_PROVIDERS)
def test_only_an_unprocessed_request_is_retried_once(provider: str) -> None:
    unavailable = Vendor().script(Response(503, {}))
    assert transport(provider, unavailable).embed(["x"], D).dimensions == NATIVE_DIMS
    assert len(unavailable.session.posts) == 2
    twice = Vendor().script(Response(503, {}), Response(503, {}))
    assert code_of(provider, twice) is EmbeddingErrorCode.TEMPORARY_PROVIDER_ERROR and len(twice.session.posts) == 2
    refused = Vendor().script(never_connected())
    transport(provider, refused).embed(["x"], D)
    assert len(refused.session.posts) == 2
    down = Vendor().script(never_connected(), never_connected())
    assert code_of(provider, down) is EmbeddingErrorCode.NETWORK_ERROR and len(down.session.posts) == 2


@pytest.mark.parametrize("provider", EMBEDDING_PROVIDERS)
@pytest.mark.parametrize("exc,code", [(http.ReadTimeout("slow"), "TIMEOUT"), (http.ConnectionError("reset"), "NETWORK_ERROR"),
                                      (OSError("boom"), "NETWORK_ERROR")])
def test_timeouts_and_broken_connections_are_not_retried(provider: str, exc: Exception, code: str) -> None:
    vendor = Vendor().script(exc)
    assert code_of(provider, vendor) is EmbeddingErrorCode(code) and len(vendor.session.posts) == 1


@pytest.mark.parametrize("provider", EMBEDDING_PROVIDERS)
@pytest.mark.parametrize("texts,code", [([], "BAD_REQUEST"), (["ok", "  "], "BAD_REQUEST"), (["x"] * (MAX_BATCH + 1), "BAD_REQUEST"),
                                        (["y" * (MAX_INPUT_CHARS + 1)], "INPUT_TOO_LARGE")])
def test_inputs_out_of_bounds_are_refused_locally_never_truncated(provider: str, texts: list[str], code: str) -> None:
    vendor = Vendor()
    with pytest.raises(EmbeddingError) as error:
        transport(provider, vendor).embed(texts, D)
    assert error.value.code is EmbeddingErrorCode(code) and vendor.session.posts == []


# ---- Secrecy ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("provider", EMBEDDING_PROVIDERS)
def test_logs_and_reprs_carry_no_key_text_or_vector(provider: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = transport(provider, Vendor())
    result = adapter.embed(["CONFIDENTIAL-KNOWLEDGE-TEXT about the Basic plan"], D)
    with pytest.raises(EmbeddingError):
        transport(provider, Vendor().script(failure(provider, "AUTH_INVALID"))).embed(["CONFIDENTIAL-KNOWLEDGE-TEXT"], Q)
    rendered = caplog.text + repr(adapter) + repr(result) + repr(adapter.space)
    assert EMBEDDINGS_KEY not in rendered and "CONFIDENTIAL" not in rendered and "secret detail" not in rendered
    assert str(result.vectors[0][0]) not in caplog.text and repr(result.vectors[0]) not in rendered
    assert "embedding_call" in caplog.text and "outcome=OK" in caplog.text and "outcome=AUTH_INVALID" in caplog.text
