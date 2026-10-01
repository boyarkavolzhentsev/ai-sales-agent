"""Runtimes with a live-provider LLM adapter over the fake HTTP session (no network), plus
Telegram (fake Bot API) and Gmail or the fake email transport. The selected provider
replaces every injected AI adapter, so these runtimes exercise the real adapters, the
real prompts and the real validators end to end."""

from pathlib import Path

from pydantic import SecretStr

from app.integrations.config import LLMProviderConfig
from app.integrations.llm.provider import build_llm
from app.integrations.providers import LLMProviderId
from app.integrations.secrets import LLMSecrets
from app.llm import LLMTransport, StructuredLLM
from app.persistence import FrozenClock
from tests.inbound.builders import NOW
from tests.llm_providers.fakes import API_KEY, Brain, FakeSession
from tests.telegram.builders import Console, console


def llm_values(provider: str = "openai", **overrides: str | None) -> dict[str, str | None]:
    return {"LLM_PROVIDER": provider, "LLM_MODEL": f"{provider}-model-under-test", "LLM_API_KEY": API_KEY} | overrides


def transport(provider: str, session: FakeSession, *, timeout: int = 30, max_output: int = 4096) -> LLMTransport:
    config = LLMProviderConfig(provider=LLMProviderId(provider.upper()), model=f"{provider}-model-under-test",
                               timeout_seconds=timeout, max_output_tokens=max_output)
    return build_llm(config, LLMSecrets(api_key=SecretStr(API_KEY)), session=session)


def structured(provider: str, session: FakeSession) -> StructuredLLM:
    return StructuredLLM(transport(provider, session), FrozenClock(NOW))


def live(tmp_path: Path, provider: str = "openai", brain: Brain | None = None, *, gmail: bool = False,
         **overrides: str | None) -> tuple[Console, Brain]:
    brain = brain or Brain()
    return console(tmp_path, gmail=gmail, llm_session=brain.session, **(llm_values(provider) | overrides)), brain
