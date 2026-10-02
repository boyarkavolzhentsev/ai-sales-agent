"""Runtime composition, secret safety and boundaries of the live LLM providers: the same
capabilities for every provider, no request at startup or in provider-status, no key
anywhere but a request header, offline/NONE runs load no provider code, business packages
never import providers or vendor SDKs, and no dependency was added."""

import ast
import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.runtime import ConfigError, SalesAgentRuntime, cli, load_config
from tests.inbound.builders import NOW
from tests.integrations.builders import full_env
from tests.llm_providers.builders import live, llm_values
from tests.llm_providers.fakes import API_KEY, PROVIDERS, Brain, failure
from tests.orchestration.builders import customer_replies
from tests.runtime.builders import env
from tests.telegram.builders import fake_connectors
from tests.llm_providers.test_end_to_end import first_touch

APP = Path(__file__).resolve().parents[2] / "app"
VENDOR_SDKS = ("openai", "anthropic", "google.genai", "google.generativeai", "langchain", "langgraph", "litellm",
               "instructor", "pydantic_ai", "semantic_kernel", "httpx", "aiohttp")


def imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def matches(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == p or name.startswith(f"{p}.") for p in prefixes)


# ---- Composition ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("provider", PROVIDERS)
def test_every_provider_yields_the_same_capabilities_without_any_request(tmp_path: Path, provider: str) -> None:
    brain = Brain()
    config = load_config(full_env(tmp_path, **llm_values(provider)), now=NOW)
    app = SalesAgentRuntime(config, connectors=fake_connectors(llm_session=brain.session))
    report = app.start()
    assert report.capabilities.model_dump() == {
        "dispatch": True, "reconciliation": True, "inbound": True, "email_sync": True, "operator_channel": True,
        "qualification_extraction": True, "commercial_extraction": True, "sales_advice": True,
        "semantic_retrieval": False}  # no embeddings provider selected: lexical retrieval
    assert brain.session.posts == []  # no billable request at startup
    llm = next(p for p in report.integrations.providers if p.category.value == "LLM")
    assert (llm.provider, llm.state.value) == (provider.upper(), "CONFIGURED")
    adapters = app._adapters  # noqa: SLF001
    assert type(adapters.qualification_extractor).__name__ == "LLMQualificationExtractor"  # no fake survives
    assert type(adapters.commercial_extractor).__name__ == "LLMCommercialExtractor"
    assert adapters.llm_transport.provider_name == provider  # type: ignore[union-attr]
    app.stop()


def test_provider_status_spends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    brain = Brain()
    monkeypatch.setattr(cli, "CONNECTORS", fake_connectors(llm_session=brain.session))
    out = io.StringIO()
    assert cli.main(["provider-status"], full_env(tmp_path, **llm_values("anthropic")), out) == 0
    rows = {r["category"]: r for r in json.loads(out.getvalue())["integrations"]["providers"]}
    assert rows["LLM"]["state"] == "CONFIGURED" and brain.session.posts == []


@pytest.mark.parametrize("model", ["../v1/files", "gpt 4", "models/gemini?key=x", "a/b", ""])
def test_a_model_name_is_never_a_path_or_url(tmp_path: Path, model: str) -> None:
    environ = full_env(tmp_path, **llm_values("gemini", LLM_MODEL=model or None))
    with pytest.raises(ConfigError) as error:
        load_config(environ, now=NOW)
    assert "SALES_AGENT_LLM_MODEL" in str(error.value)


def test_an_unknown_model_is_never_replaced(tmp_path: Path) -> None:
    c, brain = live(tmp_path, "openai")
    first_touch(c)
    brain.script("IntentClassificationProposal", failure("openai", "MODEL_NOT_FOUND"))
    result = customer_replies(c.world, "p-model")
    assert result.reply_decision.value == "ESCALATE"
    assert brain.calls.count("IntentClassificationProposal") == 1  # one attempt: no retry, no other model
    assert {p.body["model"] for p in brain.session.posts} == {"openai-model-under-test"}


# ---- Secrets -------------------------------------------------------------------------------------------------


def test_the_api_key_never_leaks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    c, brain = live(tmp_path, "anthropic")
    first_touch(c)
    brain.script("IntentClassificationProposal", failure("anthropic", "AUTH_INVALID"))  # the provider refuses the key
    refused = customer_replies(c.world, "p-auth")
    assert refused.reply_decision.value == "ESCALATE"
    customer_replies(c.world, "p-ok", body="How much is the Basic plan per month, please?")
    renderings = (repr(c.app.__dict__) + c.app.health().model_dump_json() + repr(c.app._adapters)  # noqa: SLF001
                  + "\n".join(s.text for s in c.telegram.sent) + caplog.text)
    assert API_KEY not in renderings
    c.app.stop()
    raw = (tmp_path / "agent.sqlite3").read_bytes()
    assert API_KEY.encode() not in raw and b"secret detail" not in raw  # nor the provider's error text
    out = io.StringIO()
    monkeypatch.setattr(cli, "CONNECTORS", fake_connectors(llm_session=Brain().session))
    environ = env(tmp_path / "cli.sqlite3", **llm_values("openai"))
    for command in (["provider-status"], ["init"], ["health"]):
        cli.main(command, environ, out)
    assert API_KEY not in out.getvalue()
    with pytest.raises(ConfigError) as error:
        load_config(env(tmp_path / "x.sqlite3", **llm_values("openai", LLM_MODEL=None)), now=NOW)
    assert API_KEY not in str(error.value) + repr(error.value)


def test_no_prompt_or_raw_response_is_stored(tmp_path: Path) -> None:
    c, brain = live(tmp_path, "gemini")
    first_touch(c)
    customer_replies(c.world, "p-1", body="Our internal codename is BLUEHERON. How much is the Basic plan per month?")
    c.app.stop()
    raw = (tmp_path / "agent.sqlite3").read_bytes()
    assert b"Output contract" not in raw and b"<<<UNTRUSTED_DATA" not in raw  # no prompt
    assert b"usageMetadata" not in raw and b"candidates" not in raw and b"thinking..." not in raw  # no raw response


# ---- Offline and boundaries ---------------------------------------------------------------------------------


def test_offline_and_none_runs_load_no_llm_provider_code(tmp_path: Path) -> None:
    environ = env(tmp_path / "offline.sqlite3")
    code = (
        "import sys, io\n"
        "from app.runtime.cli import main\n"
        f"environ = {dict(environ)!r}\n"
        "main(['init'], environ, io.StringIO()); main(['provider-status'], environ, io.StringIO())\n"
        "main(['tick'], environ, io.StringIO())\n"
        "loaded = sorted(m for m in sys.modules if m.startswith(('requests', 'urllib3', 'app.integrations.llm', 'app.ai')))\n"
        "assert loaded == [], loaded\n"
    )
    completed = subprocess.run([sys.executable, "-c", code], cwd=APP.parent, capture_output=True, text=True, timeout=120,
                               env={"PYTHONPATH": str(APP.parent), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")})
    assert completed.returncode == 0, completed.stderr


def test_business_packages_never_import_providers_or_vendor_sdks() -> None:
    business = ("core", "pipeline", "commercial", "campaign", "conversation", "orchestration", "operator", "dispatch",
                "inbound", "policy", "knowledge", "persistence", "llm", "enrichment")
    for package in business:
        for path in (APP / package).rglob("*.py"):
            names = imports(path)
            assert not any(matches(n, ("app.integrations", "app.ai", "requests", *VENDOR_SDKS)) for n in names), path
    every = [n for p in APP.rglob("*.py") for n in imports(p)]
    assert not any(matches(n, VENDOR_SDKS) for n in every)  # no vendor SDK or framework anywhere


def test_the_provider_and_ai_packages_depend_only_on_contracts() -> None:
    llm_may = ("app.llm", "app.integrations.config", "app.integrations.providers", "app.integrations.secrets",
               "app.integrations.llm", "requests", "urllib3")
    for path in (APP / "integrations" / "llm").glob("*.py"):
        bad = [n for n in imports(path) if n.startswith(("app", "requests", "urllib3")) and not matches(n, llm_may)]
        assert bad == [], (path.name, bad)
    ai_may = ("app.ai", "app.llm", "app.core", "app.pipeline.contracts", "app.commercial.contracts")
    for path in (APP / "ai").glob("*.py"):
        bad = [n for n in imports(path) if n.startswith("app") and not matches(n, ai_may)]
        assert bad == [], (path.name, bad)
    users = {str(p.relative_to(APP)) for p in APP.rglob("*.py")
             if any(matches(n, ("app.ai", "app.integrations.llm")) for n in imports(p))
             and not p.is_relative_to(APP / "ai") and not p.is_relative_to(APP / "integrations" / "llm")}
    assert users == {"integrations\\registry.py" if os.sep == "\\" else "integrations/registry.py",
                     "runtime\\container.py" if os.sep == "\\" else "runtime/container.py"}  # composition only


def test_no_tools_streaming_or_mutation_paths_in_the_adapters() -> None:
    for path in (APP / "integrations" / "llm").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert not literals & {"tools", "tool_choice", "functionDeclarations", "googleSearch", "google_search", "stream",
                               "web_search", "codeExecution"}, path.name
        assert not any(isinstance(n, (ast.While, ast.AsyncFunctionDef)) for n in ast.walk(tree)), path.name
        assert "time.sleep" not in source and "max_retries" not in source, path.name


def test_requirements_are_unchanged_by_stage18() -> None:
    lines = [line.strip() for line in (APP.parent / "requirements.txt").read_text(encoding="utf-8").splitlines()]
    assert [line for line in lines if line and not line.startswith("#")] == [
        "pydantic>=2,<3", "tzdata>=2024.1", "PyYAML>=6,<7", "google-auth[requests]>=2.40,<3", "google-auth-oauthlib>=1.2,<2",
        "requests>=2.31,<3"]


def test_enrichment_is_provider_neutral_and_used_only_by_composition() -> None:
    may = ("app.core", "app.persistence", "app.inbound", "app.pipeline", "app.commercial", "app.enrichment")
    for path in (APP / "enrichment").glob("*.py"):
        bad = [n for n in imports(path) if n.startswith("app") and not matches(n, may)]
        assert bad == [], (path.name, bad)  # no app.llm, no providers: it only runs the existing hooks
    users = {p.relative_to(APP).as_posix() for p in APP.rglob("*.py")
             if any(matches(n, ("app.enrichment",)) for n in imports(p)) and not p.is_relative_to(APP / "enrichment")}
    assert users == {"runtime/container.py", "runtime/application.py"}
