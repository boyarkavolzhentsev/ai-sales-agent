"""Explicit live provider checks (Stage 20): ``llm-check`` and ``embeddings-check``.

Each one is BILLABLE and runs only when an operator invokes it: exactly one bounded request
(the provider's configured timeout; the adapters' single safe retry only when nothing was
processed), with fixed harmless input and no customer or knowledge data. No database is
opened, nothing is persisted, and no other provider is touched (Gmail and Telegram are
verified when the runtime starts: one account-profile read and one ``getMe``).

- ``llm-check`` sends a tiny fixed instruction and requires the strict JSON answer
  ``{"ok": true}``: it proves the key, the model id and the structured-output path.
- ``embeddings-check`` embeds the fixed text ``deployment-check`` and validates the vector
  (finite, non-empty, the configured dimensionality); the vector is discarded.
Output: provider, model, outcome code, latency, dimensions. Never a key, prompt or vector.
"""

import time
from typing import Literal

from pydantic import ValidationError

from app.core.models.base import CoreModel
from app.embeddings import EmbeddingError, EmbeddingPurpose
from app.integrations import ProviderConnectors, ProviderUnavailableError, build_embeddings_adapter, build_llm_adapter
from app.llm import LLMError
from app.llm.models import SectionKind
from app.llm.prompts import DEPLOYMENT_CHECK_PROMPT_V1, build_request, section
from app.runtime.config import RuntimeConfig

CHECK_TEXT = "deployment-check"


class DeploymentCheckReply(CoreModel):
    ok: Literal[True]


class LiveCheckResult(CoreModel):
    check: str  # LLM or EMBEDDINGS
    status: str  # OK, ERROR, NOT_CONFIGURED
    billable: bool = True
    provider: str | None = None
    model: str | None = None
    code: str | None = None  # a stable error code on ERROR
    requests: int = 0
    latency_ms: int | None = None
    dimensions: int | None = None


def llm_check(config: RuntimeConfig, connectors: ProviderConnectors | None = None) -> LiveCheckResult:
    provider = config.integrations.llm.provider.value
    try:
        transport = build_llm_adapter(config.integrations, config.secrets, connectors)
    except ProviderUnavailableError as exc:
        return LiveCheckResult(check="LLM", status="ERROR", provider=provider, code=exc.code, billable=False)
    if transport is None:
        return LiveCheckResult(check="LLM", status="NOT_CONFIGURED", billable=False)
    request = build_request(DEPLOYMENT_CHECK_PROMPT_V1, DeploymentCheckReply, correlation_id="llm-check", locale="en",
                            sections=(section(SectionKind.TRUSTED_METADATA, "check", {"purpose": CHECK_TEXT}),)).request
    started = time.monotonic()
    model = config.integrations.llm.model
    try:
        output = transport.generate(request)
    except LLMError as exc:
        return LiveCheckResult(check="LLM", status="ERROR", provider=provider, model=model, code=exc.code.value, requests=1,
                               latency_ms=_ms(started))
    try:
        DeploymentCheckReply.model_validate_json(output.text)
    except ValidationError:
        return LiveCheckResult(check="LLM", status="ERROR", provider=provider, model=model, code="SCHEMA_VALIDATION_FAILED",
                               requests=1, latency_ms=_ms(started))
    return LiveCheckResult(check="LLM", status="OK", provider=provider, model=output.model_name, requests=output.attempt,
                           latency_ms=_ms(started))


def embeddings_check(config: RuntimeConfig, connectors: ProviderConnectors | None = None) -> LiveCheckResult:
    provider = config.integrations.embeddings.provider.value
    try:
        transport = build_embeddings_adapter(config.integrations, config.secrets, connectors)
    except ProviderUnavailableError as exc:
        return LiveCheckResult(check="EMBEDDINGS", status="ERROR", provider=provider, code=exc.code, billable=False)
    if transport is None:
        return LiveCheckResult(check="EMBEDDINGS", status="NOT_CONFIGURED", billable=False)
    started = time.monotonic()
    model = transport.space.model
    try:
        result = transport.embed([CHECK_TEXT], EmbeddingPurpose.QUERY)
    except EmbeddingError as exc:
        return LiveCheckResult(check="EMBEDDINGS", status="ERROR", provider=provider, model=model, code=exc.code.value,
                               requests=1, latency_ms=_ms(started))
    return LiveCheckResult(check="EMBEDDINGS", status="OK", provider=provider, model=model, requests=1,
                           latency_ms=_ms(started), dimensions=result.dimensions)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
