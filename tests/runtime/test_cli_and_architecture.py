"""The one-shot CLI, and the runtime's architectural boundaries."""

import ast
import io
import os
import json
import subprocess
import sys
from pathlib import Path

from app.campaign import CampaignEnroller
from app.core.enums import CampaignStatus, OutboundStatus
from app.persistence import Database, SystemClock
from app.persistence.migrations import latest_version
from app.runtime.cli import INVALID_CONFIG, OK, UNHEALTHY, main
from tests.campaign.builders import CAMPAIGN_ID, add_campaign, add_prospect
from tests.inbound.conftest import seed_knowledge
from tests.runtime.builders import env

SECRET = "sk-live-never-print-9d1c"
APP = Path(__file__).resolve().parents[2] / "app"


def run(*argv: str, environ: dict[str, str]) -> tuple[int, dict[str, object]]:
    out = io.StringIO()
    code = main(list(argv), environ, out)
    text = out.getvalue()
    assert SECRET not in text
    return code, json.loads(text) if text.strip() else {}


def test_init_then_read_only_health(tmp_path: Path) -> None:
    path = tmp_path / "agent.sqlite3"
    environ = env(path, LLM_API_KEY=SECRET)
    code, report = run("health", environ=environ)
    assert (code, report["problem"]) == (UNHEALTHY, "DATABASE_MISSING") and not path.exists()  # health never creates it
    code, report = run("init", environ=environ)
    startup = report["startup"]
    assert code == OK and isinstance(startup, dict) and startup["schema_version"] == latest_version()
    before = path.read_bytes()
    code, report = run("health", environ=environ)
    assert (code, report["schema_current"], report["database_ok"]) == (OK, True, True)
    assert path.read_bytes() == before  # read-only


def test_tick_drafts_but_never_approves_or_fabricates_sends(tmp_path: Path) -> None:
    path = tmp_path / "agent.sqlite3"
    environ = env(path)
    assert run("init", environ=environ)[0] == OK
    with Database(path) as db:
        seed_knowledge(db)
        add_campaign(db, status=CampaignStatus.ACTIVE)  # activated by an operator beforehand
        contact = add_prospect(db)
        CampaignEnroller(db, SystemClock()).enroll(CAMPAIGN_ID, contact.contact_id, correlation_id="c")
    code, result = run("tick", "--dispatch-approved", environ=environ)
    assert code == OK
    reconciliation, dispatch = result["reconciliation"], result["dispatch"]
    assert isinstance(reconciliation, dict) and reconciliation["status"] == "SKIPPED"
    assert isinstance(dispatch, dict) and dispatch["reason"] == "EMAIL_TRANSPORT_NOT_CONFIGURED"
    with Database(path) as db, db.transaction() as uow:
        statuses = {m.status for m in uow.outbound.list_by_status(OutboundStatus.DRAFTED)}
        assert uow.outbound.list_by_status(OutboundStatus.SENT) == [] and uow.outbound.list_by_status(OutboundStatus.OPERATOR_APPROVED) == []
    assert statuses <= {OutboundStatus.DRAFTED}


def test_invalid_configuration_and_usage_fail_without_printing_secrets(tmp_path: Path) -> None:
    code, report = run("tick", environ=env(tmp_path / "a.sqlite3", LLM_API_KEY=SECRET, KILL_SWITCH="maybe"))
    assert code == INVALID_CONFIG and report["error"] == "INVALID_CONFIGURATION"
    assert run("reset-database", environ=env(tmp_path / "a.sqlite3"))[0] == INVALID_CONFIG  # no such (destructive) command
    assert not (tmp_path / "a.sqlite3").exists()


def test_ticks_never_create_a_database_only_init_does(tmp_path: Path) -> None:
    path = tmp_path / "agent.sqlite3"
    code, report = run("tick", environ=env(path))
    assert (code, report["error"]) == (UNHEALTHY, "DATABASE_MISSING") and not path.exists()


def test_every_single_tick_command_runs_once_and_exits(tmp_path: Path) -> None:
    environ = env(tmp_path / "agent.sqlite3")
    assert run("init", environ=environ)[0] == OK
    for command in ("reconcile", "campaign-tick", "follow-up-tick", "dispatch-tick"):
        code, result = run(command, environ=environ)
        assert code == OK and result["status"] in ("OK", "SKIPPED")


# ---- Architecture ---------------------------------------------------------------------------

DOMAIN = ("core", "persistence", "policy", "knowledge", "llm", "inbound", "operator", "dispatch", "conversation", "campaign")
RUNTIME_FORBIDDEN = ("smtplib", "imaplib", "poplib", "socket", "ssl", "http", "urllib", "requests", "httpx", "aiohttp",
                     "openai", "anthropic", "telegram", "googleapiclient", "msgraph", "email", "asyncio", "threading",
                     "subprocess", "multiprocessing")
RUNTIME_FORBIDDEN_CALLS = {"approve_draft", "submit", "reserve_quota", "SendPermit", "consume_reservation"}


def imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_no_domain_package_depends_on_the_runtime() -> None:
    offenders = [f"{path.relative_to(APP)}: {name}" for package in DOMAIN for path in (APP / package).rglob("*.py")
                 for name in imports(path) if name == "app.runtime" or name.startswith("app.runtime.")]
    assert offenders == []


def test_runtime_has_no_provider_network_or_thread_code_and_no_shortcuts() -> None:
    problems = []
    for path in sorted((APP / "runtime").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for name in imports(path):
            if any(name == m or name.startswith(f"{m}.") for m in RUNTIME_FORBIDDEN):
                problems.append(f"{path.name}: import {name}")
        for node in ast.walk(ast.parse(source)):
            attr = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
            if attr in RUNTIME_FORBIDDEN_CALLS:
                problems.append(f"{path.name}: {attr}")
            if isinstance(node, ast.While):
                problems.append(f"{path.name}: loop at line {node.lineno}")  # one-shot only: no polling loops
    assert problems == []


def test_importing_the_runtime_has_no_side_effects(tmp_path: Path) -> None:
    code = (
        "import socket, sqlite3, threading, os\n"
        "def deny(*a, **k): raise AssertionError('side effect at import time')\n"
        "socket.socket = deny; socket.create_connection = deny; sqlite3.connect = deny; threading.Thread.start = deny\n"
        "before = set(os.listdir('.'))\n"
        "import app.runtime, app.runtime.cli, app.runtime.__main__\n"
        "assert set(os.listdir('.')) == before, 'files created at import'\n"
    )
    completed = subprocess.run([sys.executable, "-c", code], cwd=APP.parent, capture_output=True, text=True, timeout=60,
                               env={"PYTHONPATH": str(APP.parent), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")})
    assert completed.returncode == 0, completed.stderr
