"""Stage 14 boundaries: dependency direction, no loops, no operator impersonation, no
direct subsystem writes, no providers, no schema change, no new dependencies, and nothing
runs on its own (import, startup) or through the CLI beyond one bounded pass."""

import ast
import io
import json
import os
import subprocess
import sys
from pathlib import Path

from app.persistence.migrations import MIGRATIONS, latest_version
from app.runtime.cli import main
from tests.campaign.builders import campaign_messages
from tests.orchestration.builders import enrolled, world
from tests.runtime.builders import env

APP = Path(__file__).resolve().parents[2] / "app"
ORCHESTRATION = APP / "orchestration"
MAY_IMPORT = ("app.core", "app.persistence", "app.pipeline", "app.commercial", "app.conversation", "app.campaign",
              "app.dispatch", "app.policy", "app.operator.models", "app.orchestration")
NEVER = ("smtplib", "imaplib", "poplib", "socket", "ssl", "http", "urllib", "requests", "httpx", "aiohttp", "openai",
         "anthropic", "google", "telegram", "stripe", "asyncio", "threading", "subprocess", "multiprocessing", "sched",
         "app.runtime", "app.inbound", "app.llm", "app.knowledge", "app.operator.service")
# Operator decisions, provider submission, quota, terminal transitions: never from here.
FORBIDDEN_NAMES = {
    "approve_draft", "reject_draft", "approve_qualification", "create_opportunity", "approve_proposal", "approve_term_request",
    "mark_proposal_presented", "mark_proposal_accepted", "mark_proposal_declined", "mark_lead_won", "mark_lead_lost",
    "reopen_lead", "disqualify_lead", "resolve_escalation", "take_ownership", "apply_transition", "close_for_lead",
    "submit", "reserve_quota", "consume_reservation", "SendPermit", "OperatorService", "OperatorCredential",
}
SUBSYSTEMS = ("core", "persistence", "policy", "knowledge", "llm", "inbound", "operator", "dispatch", "conversation",
              "campaign", "pipeline", "commercial")


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


def test_orchestration_sits_above_the_subsystems_only() -> None:
    problems = [f"{p.name}: {n}" for p in sorted(ORCHESTRATION.glob("*.py")) for n in imports(p)
                if (n.startswith("app") and not matches(n, MAY_IMPORT)) or matches(n, NEVER)]
    assert problems == []


def test_no_subsystem_depends_on_the_orchestration_layer() -> None:
    offenders = [f"{p.relative_to(APP)}: {n}" for package in SUBSYSTEMS for p in (APP / package).rglob("*.py")
                 for n in imports(p) if matches(n, ("app.orchestration",))]
    assert offenders == []


def test_no_loops_no_operator_decisions_and_no_direct_writes() -> None:
    problems = []
    for path in sorted(ORCHESTRATION.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.While):
                problems.append(f"{path.name}: while loop at {node.lineno}")
            name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
            if name in FORBIDDEN_NAMES:
                problems.append(f"{path.name}: {name}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("add", "update", "append"):
                owner = node.func.value
                # In-memory list building is fine; a repository is never written directly.
                if isinstance(owner, ast.Attribute) and isinstance(owner.value, ast.Name) and owner.value.id == "uow":
                    problems.append(f"{path.name}: uow.{owner.attr}.{node.func.attr}")
    assert problems == []
    source = (ORCHESTRATION / "service.py").read_text(encoding="utf-8")
    assert source.count("uow.idempotency.reserve(") == 1  # the only write: an optional execution identity


def test_no_schema_migration_and_no_new_dependency() -> None:
    # Stage 14 added no migration (v9 stayed v9); v10 belongs to Stage 16's mailbox sync.
    assert MIGRATIONS[8].name == "commercial_decisioning" and [m.name for m in MIGRATIONS[9:]] == ["email_provider_sync"]
    assert latest_version() == len(MIGRATIONS) == 10
    lines = [line.strip() for line in (APP.parent / "requirements.txt").read_text(encoding="utf-8").splitlines()]
    assert [line.split(">")[0].split("<")[0].split("=")[0] for line in lines if line and not line.startswith("#")] == [
        "pydantic", "tzdata", "PyYAML", "google-auth[requests]", "google-auth-oauthlib"]  # Stage 16: Gmail OAuth only


def test_importing_the_orchestration_layer_has_no_side_effects() -> None:
    code = (
        "import socket, sqlite3, threading, os\n"
        "def deny(*a, **k): raise AssertionError('side effect at import time')\n"
        "socket.socket = deny; socket.create_connection = deny; sqlite3.connect = deny; threading.Thread.start = deny\n"
        "before = set(os.listdir('.'))\n"
        "import app.orchestration\n"
        "assert set(os.listdir('.')) == before, 'files created at import'\n"
    )
    completed = subprocess.run([sys.executable, "-c", code], cwd=APP.parent, capture_output=True, text=True, timeout=60,
                               env={"PYTHONPATH": str(APP.parent), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")})
    assert completed.returncode == 0, completed.stderr


def test_startup_executes_nothing(db_path: Path) -> None:
    w = world(db_path)
    enrolled(w)
    w.app.stop()
    restarted = world(db_path)  # a fresh start over a database that has actionable work
    restarted.lead_id = w.lead_id
    assert restarted.plan().executable and campaign_messages(restarted.db, restarted.lead) == []
    restarted.app.stop()


def run(*argv: str, environ: dict[str, str]) -> tuple[int, dict[str, object]]:
    out = io.StringIO()
    code = main(argv, environ, out)
    return code, json.loads(out.getvalue())


def test_cli_execution_commands_are_one_shot_and_offline(tmp_path: Path) -> None:
    environ = env(tmp_path / "agent.sqlite3")
    assert run("init", environ=environ)[0] == 0
    code, metrics = run("execution-metrics", environ=environ)
    assert code == 0 and metrics["open_leads"] == 0
    code, queue = run("execution-queue", "--queue", "ACTIONABLE", environ=environ)
    assert code == 0 and queue == {"queue": "ACTIONABLE", "plans": []}
    code, result = run("execution-pass", environ=environ)
    assert code == 0 and (result["considered"], result["attempted"]) == (0, 0)
    assert run("execution-plan", environ=environ) == (2, {"error": "LEAD_ID_REQUIRED"})
    assert run("execution-plan", "--lead-id", "ld_missing", environ=environ) == (2, {"error": "LEAD_NOT_FOUND"})
