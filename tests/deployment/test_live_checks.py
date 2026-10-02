"""``llm-check`` / ``embeddings-check``: explicit, billable, exactly one bounded request with
fixed harmless input; no database, no other provider; safe output; stable exit codes."""

from pathlib import Path

import pytest

from tests.deployment.builders import Fakes, production_env, run
from tests.integrations.builders import FAKE_SECRETS
from tests.llm_providers.fakes import PROVIDERS, failure, prompts
from tests.llm_providers.builders import llm_values
from tests.rag.builders import emb_values
from tests.rag.fakes import EMBEDDINGS_KEY
from tests.rag.fakes import failure as emb_failure

OK, INVALID, ERROR = 0, 2, 4


@pytest.mark.parametrize("provider", PROVIDERS)
def test_llm_check_makes_one_tiny_request_and_touches_nothing_else(tmp_path: Path, provider: str,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = Fakes()
    fakes.brain.script("DeploymentCheckReply", {"ok": True})
    code, result = run(["llm-check"], production_env(tmp_path, **llm_values(provider)), fakes, monkeypatch)
    assert code == OK and (result["status"], result["check"], result["billable"], result["requests"]) == ("OK", "LLM", True, 1)
    assert result["provider"] == provider.upper()
    [post] = fakes.brain.session.posts
    system, user = prompts(post)
    assert "connectivity check" in system and "deployment-check" in user and len(system) + len(user) < 2000
    assert fakes.gmail.calls == [] and fakes.telegram.calls == [] and fakes.vendor.session.posts == []
    assert not (tmp_path / "agent.sqlite3").exists()  # no database


def test_llm_check_reports_provider_errors_and_bad_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = Fakes()
    fakes.brain.script("DeploymentCheckReply", failure("openai", "AUTH_INVALID"), {"ok": False})
    environ = production_env(tmp_path)
    code, result = run(["llm-check"], environ, fakes, monkeypatch)
    assert code == ERROR and (result["status"], result["code"]) == ("ERROR", "AUTH_INVALID")
    code, result = run(["llm-check"], environ, fakes, monkeypatch)
    assert code == ERROR and result["code"] == "SCHEMA_VALIDATION_FAILED"
    assert len(fakes.brain.session.posts) == 2  # one request per invocation, no retry storm
    assert not any(secret in str(result) for secret in FAKE_SECRETS) and "secret detail" not in str(result)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_embeddings_check_embeds_one_fixed_text_and_stores_nothing(tmp_path: Path, provider: str,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = Fakes()
    code, result = run(["embeddings-check"], production_env(tmp_path, **emb_values(provider)), fakes, monkeypatch)
    assert code == OK and (result["status"], result["dimensions"], result["requests"]) == ("OK", 8, 1)
    assert fakes.vendor.batches == [["deployment-check"]]
    assert fakes.gmail.calls == [] and fakes.brain.session.posts == [] and not (tmp_path / "agent.sqlite3").exists()
    assert "0." not in str(result)  # no vector values in the output


def test_embeddings_check_failure_is_a_safe_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = Fakes()
    fakes.vendor.script(emb_failure("openai", "RATE_LIMITED"))
    code, result = run(["embeddings-check"], production_env(tmp_path), fakes, monkeypatch)
    assert code == ERROR and result["code"] == "RATE_LIMITED" and EMBEDDINGS_KEY not in str(result)
    assert len(fakes.vendor.session.posts) == 1


def test_a_check_for_an_unconfigured_provider_is_not_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.runtime.builders import env
    code, result = run(["embeddings-check"], env(tmp_path / "a.sqlite3"), Fakes(), monkeypatch)
    assert code == INVALID and result["status"] == "NOT_CONFIGURED" and result["billable"] is False
    code, result = run(["llm-check"], env(tmp_path / "a.sqlite3"), Fakes(), monkeypatch)
    assert code == INVALID and result["status"] == "NOT_CONFIGURED"


def test_startup_health_status_and_init_never_spend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fakes, environ = Fakes(), production_env(tmp_path)
    for command in (["provider-status"], ["init"], ["health"], ["deployment-check"]):
        run(command, environ, fakes, monkeypatch)
    assert fakes.brain.session.posts == [] and fakes.vendor.session.posts == []
