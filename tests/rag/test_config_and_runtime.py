"""Embeddings configuration, provider status, runtime composition (LLM x embeddings matrix,
NONE mode), the knowledge-index command, and the package boundaries."""

import ast
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.integrations import ProviderCategory
from app.knowledge import LexicalRetriever, SemanticRetriever
from app.runtime import ConfigError, SalesAgentRuntime, cli, inspect_integrations, load_config
from tests.inbound.builders import NOW
from tests.integrations.builders import full_env
from tests.knowledge.sources import markdown, meta, write
from tests.llm_providers.builders import llm_values
from tests.llm_providers.fakes import Brain
from tests.rag.builders import emb_values, knowledge_dir
from tests.rag.fakes import EMBEDDINGS_KEY, Vendor
from tests.runtime.builders import env
from tests.telegram.builders import fake_connectors

APP = Path(__file__).resolve().parents[2] / "app"


def status(**values: str | None):  # noqa: ANN201
    return inspect_integrations(env(Path("unused.sqlite3"), **values)).of(ProviderCategory.EMBEDDINGS)


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


# ---- Configuration and status -----------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_a_complete_selection_is_configured(provider: str) -> None:
    row = status(**emb_values(provider))
    assert (row.provider, row.state.value, row.capability_available, row.problems) == (provider.upper(), "CONFIGURED", True, ())


@pytest.mark.parametrize("missing,problem", [("EMBEDDINGS_API_KEY", "SALES_AGENT_EMBEDDINGS_API_KEY: MISSING_SECRET"),
                                             ("EMBEDDINGS_MODEL", "SALES_AGENT_EMBEDDINGS_MODEL: MISSING_SETTING")])
def test_a_missing_key_or_model_is_invalid(missing: str, problem: str) -> None:
    row = status(**emb_values("openai", **{missing: None}))
    assert row.state.value == "INVALID" and row.problems == (problem,)


def test_the_llm_key_is_never_reused_for_embeddings() -> None:
    row = status(**(llm_values("openai") | emb_values("openai", EMBEDDINGS_API_KEY=None)))
    assert row.state.value == "INVALID" and "SALES_AGENT_EMBEDDINGS_API_KEY: MISSING_SECRET" in row.problems


@pytest.mark.parametrize("variable,value", [("EMBEDDINGS_MODEL", "m"), ("EMBEDDINGS_API_KEY", EMBEDDINGS_KEY),
                                            ("EMBEDDINGS_MIN_SIMILARITY", "0.4"), ("EMBEDDINGS_DIMENSIONS", "256")])
def test_settings_without_a_selected_provider_are_rejected(variable: str, value: str) -> None:
    row = status(**{variable: value})
    assert row.state.value == "INVALID" and row.problems == (f"SALES_AGENT_{variable}: PROVIDER_NOT_SELECTED",)


def test_anthropic_has_no_embeddings_provider() -> None:
    row = status(**emb_values("anthropic"))
    assert "SALES_AGENT_EMBEDDINGS_PROVIDER: UNKNOWN_PROVIDER" in row.problems


@pytest.mark.parametrize("variable,value", [
    ("EMBEDDINGS_MIN_SIMILARITY", "abc"), ("EMBEDDINGS_MIN_SIMILARITY", "1.5"), ("EMBEDDINGS_MIN_SIMILARITY", "-0.1"),
    ("EMBEDDINGS_MIN_SIMILARITY", "nan"), ("EMBEDDINGS_MIN_SIMILARITY", "1e-3"), ("EMBEDDINGS_MIN_SIMILARITY", "0"),
    ("EMBEDDINGS_MIN_SIMILARITY", "1"), ("EMBEDDINGS_DIMENSIONS", "0"), ("EMBEDDINGS_DIMENSIONS", "-5"),
    ("EMBEDDINGS_TIMEOUT_SECONDS", "0"), ("EMBEDDINGS_TIMEOUT_SECONDS", "999"), ("EMBEDDINGS_MODEL", "a/b"),
    ("EMBEDDINGS_MODEL", "models/x?key=1"),
])
def test_malformed_values_are_invalid_and_never_echoed(variable: str, value: str) -> None:
    row = status(**emb_values("openai", **{variable: value}))
    assert row.state.value == "INVALID" and f"SALES_AGENT_{variable}: INVALID_PROVIDER_CONFIG" in row.problems
    assert value not in " ".join(row.problems)


def test_valid_optional_settings_are_parsed(tmp_path: Path) -> None:
    config = load_config(env(tmp_path / "a.sqlite3", **emb_values("gemini", EMBEDDINGS_MIN_SIMILARITY="0.42",
                                                                   EMBEDDINGS_DIMENSIONS="768", EMBEDDINGS_TIMEOUT_SECONDS="12")),
                         now=NOW)
    embeddings = config.integrations.embeddings
    assert (embeddings.min_similarity, embeddings.dimensions, embeddings.timeout_seconds) == (0.42, 768, 12)
    assert EMBEDDINGS_KEY not in repr(config) + config.model_dump_json() + str(config.secrets)


def test_embeddings_are_required_for_production_readiness_only(tmp_path: Path) -> None:
    missing = inspect_integrations(full_env(tmp_path))
    assert missing.valid and not missing.production_ready  # valid for local use, not for production
    assert missing.production_blockers == ("EMBEDDINGS:DISABLED",)
    assert missing.of(ProviderCategory.EMBEDDINGS).state.value == "DISABLED"
    ready = inspect_integrations(full_env(tmp_path, **emb_values("openai")))
    assert ready.production_ready and ready.production_blockers == ()
    broken = inspect_integrations(full_env(tmp_path, **emb_values("openai", EMBEDDINGS_MODEL=None)))
    assert not broken.valid and not broken.production_ready and "EMBEDDINGS:INVALID" in broken.production_blockers
    with pytest.raises(ConfigError):
        load_config(full_env(tmp_path, **emb_values("openai", EMBEDDINGS_MODEL=None)), now=NOW)


# ---- Runtime composition ------------------------------------------------------------------------------------


@pytest.mark.parametrize("llm,emb", [("openai", "openai"), ("anthropic", "openai"), ("gemini", "gemini"),
                                     ("anthropic", "gemini")])
def test_any_llm_pairs_with_any_embeddings_provider_in_production(tmp_path: Path, llm: str, emb: str) -> None:
    brain, vendor = Brain(), Vendor()
    config = load_config(full_env(tmp_path, MODE="production", **(llm_values(llm) | emb_values(emb))), now=NOW)
    app = SalesAgentRuntime(config, connectors=fake_connectors(llm_session=brain.session, embeddings_session=vendor.session))
    report = app.start()
    assert report.capabilities.semantic_retrieval and report.capabilities.inbound
    rows = {p.category.value: p for p in report.integrations.providers}
    assert (rows["LLM"].provider, rows["EMBEDDINGS"].provider) == (llm.upper(), emb.upper())
    assert rows["EMBEDDINGS"].state.value == "CONFIGURED" and report.integrations.production_ready
    assert brain.session.posts == [] and vendor.session.posts == []  # no billable request at startup
    services = app.services
    assert isinstance(services.inbound._knowledge, SemanticRetriever)  # noqa: SLF001
    assert services.inbound._knowledge._transport.space.provider == emb.upper()  # noqa: SLF001
    assert services.knowledge_indexer is not None
    assert app._adapters.llm_transport.provider_name == llm  # type: ignore[union-attr]  # noqa: SLF001
    assert EMBEDDINGS_KEY not in repr(app.__dict__) + app.health().model_dump_json() + repr(app._adapters)  # noqa: SLF001
    app.stop()


def test_without_embeddings_inbound_stays_lexical(tmp_path: Path) -> None:
    config = load_config(full_env(tmp_path), now=NOW)
    app = SalesAgentRuntime(config, connectors=fake_connectors(llm_session=Brain().session))
    report = app.start()
    assert report.capabilities.inbound and not report.capabilities.semantic_retrieval
    assert isinstance(app.services.inbound._knowledge, LexicalRetriever)  # noqa: SLF001
    assert app.services.knowledge_indexer is None
    app.stop()


def test_provider_status_spends_nothing_and_prints_no_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vendor = Vendor()
    monkeypatch.setattr(cli, "CONNECTORS", fake_connectors(embeddings_session=vendor.session))
    out = io.StringIO()
    assert cli.main(["provider-status"], full_env(tmp_path, **emb_values("gemini")), out) == 0
    rows = {r["category"]: r for r in json.loads(out.getvalue())["integrations"]["providers"]}
    assert rows["EMBEDDINGS"]["state"] == "CONFIGURED" and vendor.session.posts == []
    assert EMBEDDINGS_KEY not in out.getvalue()


# ---- knowledge-index --------------------------------------------------------------------------------------


def run_cli(command: str, environ: dict[str, str]) -> tuple[int, dict[str, object]]:
    out = io.StringIO()
    code = cli.main([command], environ, out)
    return code, json.loads(out.getvalue().splitlines()[-1])


def test_knowledge_index_ingests_embeds_incrementally_and_prints_counts_only(tmp_path: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    vendor = Vendor()
    monkeypatch.setattr(cli, "CONNECTORS", fake_connectors(embeddings_session=vendor.session))
    kb = knowledge_dir(tmp_path)
    environ = env(tmp_path / "agent.sqlite3", KNOWLEDGE_DIR=str(kb), **emb_values("openai"))
    assert run_cli("knowledge-index", environ)[1] == {"error": "DATABASE_MISSING", "hint": "run 'init' first"}
    assert run_cli("init", environ)[0] == 0 and vendor.session.posts == []  # startup spends nothing
    code, first = run_cli("knowledge-index", environ)
    assert code == 0 and first["status"] == "OK" and (first["sources_ingested"], first["sources_unchanged"]) == (7, 0)
    assert {k: first["embeddings"][k] for k in ("scanned", "embedded", "unchanged", "failed", "requests")} == {  # type: ignore[index]
        "scanned": 8, "embedded": 8, "unchanged": 0, "failed": 0, "requests": 1}
    code, second = run_cli("knowledge-index", environ)
    assert code == 0 and second["sources_unchanged"] == 7 and second["embeddings"]["requests"] == 0  # type: ignore[index]
    assert len(vendor.session.posts) == 1
    printed = json.dumps([first, second])
    assert "79 EUR" not in printed and "Basic" not in printed and EMBEDDINGS_KEY not in printed and "0.0" not in printed


def test_knowledge_index_reports_a_provider_failure_with_exit_code_4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.rag.fakes import failure
    vendor = Vendor().script(failure("openai", "AUTH_INVALID"))
    monkeypatch.setattr(cli, "CONNECTORS", fake_connectors(embeddings_session=vendor.session))
    environ = env(tmp_path / "agent.sqlite3", KNOWLEDGE_DIR=str(knowledge_dir(tmp_path)), **emb_values("openai"))
    run_cli("init", environ)
    code, result = run_cli("knowledge-index", environ)
    assert code == 4 and result["status"] == "ERROR" and result["reason"] == "AUTH_INVALID"
    assert EMBEDDINGS_KEY not in json.dumps(result) and "secret detail" not in json.dumps(result)


def test_an_invalid_knowledge_directory_ingests_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "CONNECTORS", fake_connectors())
    kb = knowledge_dir(tmp_path)
    write(kb, "FAQ", "broken.md", markdown(meta(source_id="broken", version="one"), "# x\n\nSECRET-ISH CONTENT\n"))
    environ = env(tmp_path / "agent.sqlite3", KNOWLEDGE_DIR=str(kb))
    run_cli("init", environ)
    code, result = run_cli("knowledge-index", environ)
    assert code == 4 and (result["reason"], result["ingestion_error"]) == ("KNOWLEDGE_SOURCE_INVALID", "SourceValidationError")
    assert "SECRET-ISH" not in json.dumps(result)


def test_without_embeddings_knowledge_index_only_ingests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "CONNECTORS", fake_connectors())
    environ = env(tmp_path / "agent.sqlite3", KNOWLEDGE_DIR=str(knowledge_dir(tmp_path)))
    run_cli("init", environ)
    code, result = run_cli("knowledge-index", environ)
    assert code == 0 and (result["status"], result["reason"], result["sources_ingested"]) == ("OK", "EMBEDDINGS_NOT_CONFIGURED", 7)
    assert result["embeddings"] is None
    bare = env(tmp_path / "bare.sqlite3")
    run_cli("init", bare)
    assert run_cli("knowledge-index", bare) == (0, {"status": "SKIPPED", "reason": "EMBEDDINGS_NOT_CONFIGURED",
                                                    "sources_ingested": 0, "sources_unchanged": 0, "ingestion_error": None,
                                                    "embeddings": None})


# ---- Boundaries ---------------------------------------------------------------------------------------------


def test_offline_and_none_runs_load_no_embeddings_provider_code(tmp_path: Path) -> None:
    environ = env(tmp_path / "offline.sqlite3", KNOWLEDGE_DIR=str(knowledge_dir(tmp_path)))
    code = (
        "import sys, io\n"
        "from app.runtime.cli import main\n"
        f"environ = {dict(environ)!r}\n"
        "for c in (['init'], ['provider-status'], ['knowledge-index'], ['tick']): main(c, environ, io.StringIO())\n"
        "loaded = sorted(m for m in sys.modules if m.startswith(('requests', 'urllib3', 'app.integrations.embeddings')))\n"
        "assert loaded == [], loaded\n"
    )
    completed = subprocess.run([sys.executable, "-c", code], cwd=APP.parent, capture_output=True, text=True, timeout=120,
                               env={"PYTHONPATH": str(APP.parent), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")})
    assert completed.returncode == 0, completed.stderr


def test_the_embeddings_contract_and_knowledge_layer_import_no_provider_code() -> None:
    for path in (APP / "embeddings").glob("*.py"):
        bad = [n for n in imports(path) if n.startswith(("app", "requests", "urllib3")) and not matches(n, ("app.embeddings", "app.core"))]
        assert bad == [], (path.name, bad)
    business = ("core", "pipeline", "commercial", "campaign", "conversation", "orchestration", "operator", "dispatch",
                "inbound", "policy", "knowledge", "persistence", "llm", "enrichment", "embeddings", "ai")
    for package in business:
        for path in (APP / package).rglob("*.py"):
            assert not any(matches(n, ("app.integrations", "requests", "urllib3")) for n in imports(path)), path
    for name in ("retriever.py", "semantic.py", "indexing.py"):
        assert not any(matches(n, ("app.llm", "app.ai")) for n in imports(APP / "knowledge" / name)), name  # no LLM coupling


def test_the_embeddings_adapters_depend_only_on_contracts_and_are_used_only_by_the_registry() -> None:
    may = ("app.embeddings", "app.integrations.config", "app.integrations.providers", "app.integrations.secrets",
           "app.integrations.embeddings", "requests", "urllib3")
    for path in (APP / "integrations" / "embeddings").glob("*.py"):
        bad = [n for n in imports(path) if n.startswith(("app", "requests", "urllib3")) and not matches(n, may)]
        assert bad == [], (path.name, bad)
        source = path.read_text(encoding="utf-8")
        assert "time.sleep" not in source and "while " not in source and "max_retries" not in source, path.name
    users = {p.relative_to(APP).as_posix() for p in APP.rglob("*.py")
             if any(matches(n, ("app.integrations.embeddings",)) for n in imports(p))
             and not p.is_relative_to(APP / "integrations" / "embeddings")}
    assert users == {"integrations/registry.py"}


def test_no_vector_database_or_new_dependency() -> None:
    lines = [line.strip() for line in (APP.parent / "requirements.txt").read_text(encoding="utf-8").splitlines()]
    assert [line for line in lines if line and not line.startswith("#")] == [
        "pydantic>=2,<3", "tzdata>=2024.1", "PyYAML>=6,<7", "google-auth[requests]>=2.40,<3", "google-auth-oauthlib>=1.2,<2",
        "requests>=2.31,<3"]
    every = {n for p in APP.rglob("*.py") for n in imports(p)}
    assert not any(matches(n, ("numpy", "faiss", "pinecone", "weaviate", "qdrant_client", "pymilvus", "chromadb",
                               "elasticsearch", "openai", "google.genai", "sentence_transformers")) for n in every)
