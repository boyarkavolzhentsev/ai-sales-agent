"""Static deployment rules (Dockerfile, .dockerignore, CI), runbook validation (every command,
flag and variable in the docs exists), the service-tick contract, and CLI log safety."""

import io
import logging
import re
from pathlib import Path

import pytest

from app.runtime import cli
from app.runtime.env import OPTIONAL, REQUIRED
from tests.deployment.builders import Fakes, production_env, run

ROOT = Path(__file__).resolve().parents[2]
DOCS = ("docs/deployment.md", "docs/integrations.md", "README.md", ".env.example")


def text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def cli_commands() -> set[str]:
    source = text("app/runtime/cli.py")
    block = source[source.index('parser.add_argument("command", choices=('):source.index("parser.add_argument(\"--recover\"")]
    names = set(re.findall(r'"([a-z][a-z-]+)"', block))
    return names | set(cli.TICKS) | set(cli.EXECUTION)


# ---- Runbook validation ---------------------------------------------------------------------------------------


def test_every_documented_command_and_flag_exists() -> None:
    commands, flags = cli_commands(), {"--recover", "--dispatch-approved", "--lead-id", "--queue"}
    for name in DOCS:
        for command, rest in re.findall(r"python -m app\.runtime ([a-z][a-z-]*)([^`\n|]*)", text(name)):
            assert command in commands, (name, command)
            for flag in re.findall(r"--[a-z-]+", rest):
                assert flag in flags, (name, command, flag)
    documented = set(re.findall(r"`([a-z]+(?:-[a-z]+)+)[ `]", text("docs/deployment.md"))) & commands
    assert {"service-tick", "deployment-check", "llm-check", "embeddings-check", "knowledge-index", "email-sync",
            "operator-sync", "ai-recovery-tick", "execution-pass", "provider-status", "gmail-auth"} <= documented


def test_every_documented_variable_exists() -> None:
    known = set(REQUIRED) | set(OPTIONAL)
    for name in DOCS:
        for variable in re.findall(r"SALES_AGENT_([A-Z][A-Z0-9_]*[A-Z0-9])", text(name)):
            assert variable in known, (name, variable)


def test_the_env_template_is_grouped_complete_and_fails_safe() -> None:
    template = text(".env.example")
    for group in ("Core", "Providers", "Email", "LLM", "Operator channel", "Knowledge", "Embeddings", "Worker"):
        assert group in template, group
    listed = set(re.findall(r"SALES_AGENT_([A-Z0-9_]+)=", template))
    assert set(REQUIRED) | set(OPTIONAL) <= listed
    assert "SALES_AGENT_KILL_SWITCH=true" in template  # a fresh deployment cannot send by accident


# ---- Container and CI -----------------------------------------------------------------------------------------


def test_dockerfile_is_slim_non_root_and_secret_free() -> None:
    dockerfile = text("Dockerfile")
    assert re.search(r"^FROM python:3\.13-slim\s*$", dockerfile, re.M)
    assert re.search(r"^USER agent\s*$", dockerfile, re.M) and "--uid 10001" in dockerfile
    assert dockerfile.index("USER agent") > dockerfile.index("pip install")  # nothing runs as root at run time
    copies = re.findall(r"^COPY (.+)$", dockerfile, re.M)
    assert copies == ["requirements.txt ./", "app/ ./app/"]  # code only: no .env, .local, tests, data
    assert 'ENTRYPOINT ["python", "-m", "app.runtime"]' in dockerfile and 'VOLUME ["/data"]' in dockerfile
    assert not re.search(r"(?i)^(ENV|ARG) .*(KEY|TOKEN|SECRET|PASSWORD)", dockerfile, re.M)
    assert "pytest" not in dockerfile and "requirements-dev" not in dockerfile


def test_dockerignore_allowlists_runtime_code_and_excludes_secrets() -> None:
    lines = [line.strip() for line in text(".dockerignore").splitlines() if line.strip() and not line.startswith("#")]
    assert lines[:3] == ["*", "!app/", "!requirements.txt"]
    for pattern in (".git", ".env", ".env.*", ".local/", "**/token*.json", "**/credentials*.json", "**/*.sqlite3", "**/*.db",
                    "**/*.log", "**/__pycache__/", ".pytest_cache/", "tests/", ".vscode/", ".idea/"):
        assert pattern in lines, pattern
    assert not any(line.startswith("!") and line not in ("!app/", "!requirements.txt") for line in lines)


def test_ci_runs_the_suite_on_the_image_python_without_secrets() -> None:
    workflow = text(".github/workflows/ci.yml")
    assert 'python-version: "3.13"' in workflow and "pip install -r requirements-dev.txt" in workflow
    assert "python -m pytest" in workflow and "compileall" in workflow and "docker build" in workflow
    assert "secrets." not in workflow and "permissions:\n  contents: read" in workflow
    assert "SALES_AGENT_KILL_SWITCH=true" in workflow  # the container smoke test can never send


def test_runtime_requirements_are_unchanged() -> None:
    lines = [line.strip() for line in text("requirements.txt").splitlines() if line.strip() and not line.startswith("#")]
    assert lines == ["pydantic>=2,<3", "tzdata>=2024.1", "PyYAML>=6,<7", "google-auth[requests]>=2.40,<3",
                     "google-auth-oauthlib>=1.2,<2", "requests>=2.31,<3"]


# ---- service-tick ----------------------------------------------------------------------------------------------


def test_service_tick_runs_the_existing_passes_once_in_order_and_isolates_failures(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.runtime import SalesAgentRuntime, load_config
    from tests.inbound.builders import NOW
    fakes = Fakes()
    app = SalesAgentRuntime(load_config(production_env(tmp_path), now=NOW), connectors=fakes.connectors())
    app.start()
    order: list[str] = []
    for name in ("email_sync", "ai_recovery_tick", "operator_sync", "tick", "knowledge_index"):
        original = getattr(app, name)

        def spy(*args: object, _name: str = name, _original=original, **kwargs: object):  # noqa: ANN202, ANN001
            order.append(_name)
            if _name == "ai_recovery_tick":
                raise RuntimeError("boom")
            return _original(*args, **kwargs)

        monkeypatch.setattr(app, name, spy)
    result = app.service_tick(dispatch_approved=True)
    assert order == ["email_sync", "ai_recovery_tick", "operator_sync", "tick"]  # never knowledge-index (billable)
    assert [(e.subject, e.error_type) for e in result.errors] == [("ai_recovery", "RuntimeError")] and not result.ok
    assert result.tick is not None and result.tick.dispatch is not None  # later phases still ran
    assert fakes.brain.session.posts == [] and fakes.vendor.session.posts == []
    app.stop()


def test_service_tick_cli_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fakes, environ = Fakes(), production_env(tmp_path)
    run(["init"], environ, fakes, monkeypatch)
    code, result = run(["service-tick"], environ, fakes, monkeypatch)
    assert code == 0 and result["email_sync"]["status"] == "INITIALIZED" and result["tick"]["dispatch"] is None
    code, result = run(["service-tick", "--dispatch-approved"], environ, fakes, monkeypatch)
    assert code == 0 and result["tick"]["dispatch"]["status"] == "OK"


def test_every_command_logs_one_safe_line(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.runtime.cli")
    environ = production_env(tmp_path)
    cli.main(["deployment-check"], environ, io.StringIO())
    cli.main(["no-such-command; rm -rf /"], environ, io.StringIO())
    lines = [r.getMessage() for r in caplog.records if r.name == "app.runtime.cli"]
    assert lines[0].startswith("command name=deployment-check exit=3 duration_ms=")
    assert lines[1].startswith("command name=? exit=2")  # arbitrary input is never echoed
    assert str(tmp_path) not in caplog.text
