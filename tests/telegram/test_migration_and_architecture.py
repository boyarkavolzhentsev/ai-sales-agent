"""Migration v11 (operator-channel sync) and the Telegram package's boundaries: business
code never imports it, it only talks to Stage 7/14 contracts, the client has exactly five
Bot API methods, no loop/thread/daemon, plain text only, and offline runs load nothing."""

import ast
import os
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from app.persistence import Database, FrozenClock
from app.persistence.migrations import MIGRATIONS, apply_migrations, current_version, latest_version
from tests.inbound.builders import NOW
from tests.inbound.conftest import seed_knowledge
from tests.pipeline.builders import active_opportunity, lead, opportunity_lead

APP = Path(__file__).resolve().parents[2] / "app"
TELEGRAM = APP / "integrations" / "telegram"
SECRET_WORDS = ("token", "secret", "password", "credential", "refresh", "access", "body", "subject", "text", "raw")
TABLES = ("operator_channel_states", "operator_channel_failures", "operator_notifications", "operator_confirmations")


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


# ---- Migration ---------------------------------------------------------------------------------------------


def test_a_fresh_database_reaches_v11_with_minimal_tables(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite3"
    with Database(path) as db:
        assert db.initialize_schema(FrozenClock(NOW)) == latest_version() == 11
    raw = sqlite3.connect(path)
    try:
        assert current_version(raw) == 11 and MIGRATIONS[-1].name == "operator_channel_sync"
        columns = {table: [row[1] for row in raw.execute(f"PRAGMA table_info({table})")] for table in TABLES}
    finally:
        raw.close()
    assert columns == {
        "operator_channel_states": ["state_id", "provider", "account", "updated_at", "version", "data"],
        "operator_channel_failures": ["failure_id", "provider", "account", "update_id", "failed_at", "data"],
        "operator_notifications": ["notification_id", "provider", "chat_id", "status", "updated_at", "version", "data"],
        "operator_confirmations": ["confirmation_id", "provider", "operator_id", "status", "expires_at", "version", "data"],
    }
    assert all(not any(word in column for word in SECRET_WORDS) for cols in columns.values() for column in cols)


def test_v1_to_v10_are_unchanged_and_a_v10_database_upgrades_intact(tmp_path: Path) -> None:
    assert [m.name for m in MIGRATIONS[:10]] == ["initial_schema", "quota_reservations", "knowledge_index", "optional_company",
                                                  "dispatch_attempts", "conversations", "campaign_execution", "sales_pipeline",
                                                  "commercial_decisioning", "email_provider_sync"]
    path = tmp_path / "stage16.sqlite3"
    raw = sqlite3.connect(path, isolation_level=None)
    raw.execute("PRAGMA foreign_keys = ON")
    try:
        assert apply_migrations(raw, FrozenClock(NOW), MIGRATIONS[:10]) == 10
    finally:
        raw.close()
    with Database(path) as db:
        seed_knowledge(db)
        lead_id = opportunity_lead(db)
        before = (lead(db, lead_id), active_opportunity(db, lead_id))
    with Database(path) as db:
        assert db.initialize_schema(FrozenClock(NOW + timedelta(days=1))) == 11
        assert db.initialize_schema(FrozenClock(NOW + timedelta(days=2))) == 11  # idempotent
        assert (lead(db, lead_id), active_opportunity(db, lead_id)) == before
        with db.transaction() as uow:
            assert uow.operator_channel.get_state("telegram", "424242") is None  # nothing backfilled
            assert uow.mailbox_sync.get_state("gmail", "sales@ourco.example") is None


# ---- Architecture -------------------------------------------------------------------------------------------


def test_only_composition_ever_selects_telegram() -> None:
    allowed = {APP / "integrations" / "registry.py", APP / "runtime" / "container.py", APP / "runtime" / "application.py"}
    offenders = [f"{p.relative_to(APP)}: {n}" for p in APP.rglob("*.py") if not p.is_relative_to(TELEGRAM) and p not in allowed
                 for n in imports(p) if matches(n, ("app.integrations.telegram",))]
    assert offenders == []
    for path in allowed:  # and only lazily, inside functions: nothing Telegram loads at import time
        tree = ast.parse(path.read_text(encoding="utf-8"))
        top = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("app.integrations.telegram")]
        assert top == [], path.name


def test_business_packages_never_know_about_telegram() -> None:
    business = ("pipeline", "commercial", "campaign", "conversation", "orchestration", "operator", "dispatch", "inbound",
                "policy", "llm", "core", "persistence", "knowledge")
    for package in business:
        root = APP / package
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            assert not any(matches(n, ("app.integrations", "requests")) for n in imports(path)), path


def test_the_telegram_package_depends_only_on_contracts() -> None:
    may = ("app.core", "app.integrations", "app.inbound.models", "app.operator", "app.orchestration", "app.persistence",
           "app.commercial.terms", "requests")
    never = ("app.runtime", "app.dispatch", "app.pipeline.service", "app.commercial.service", "app.conversation", "app.campaign",
             "app.integrations.gmail", "telegram", "aiogram", "telebot", "httpx", "aiohttp", "asyncio", "threading")
    problems = []
    for path in sorted(TELEGRAM.glob("*.py")):
        for name in imports(path):
            if name.startswith(("app", "requests")) and not matches(name, may):
                problems.append(f"{path.name}: {name}")
            if matches(name, never):
                problems.append(f"{path.name}: forbidden {name}")
    assert problems == []
    assert [n for n in imports(TELEGRAM / "callbacks.py") + imports(TELEGRAM / "rendering.py") if n.startswith("app")] == []


def test_the_client_has_exactly_five_methods_no_retries_and_no_formatting() -> None:
    source = (TELEGRAM / "client.py").read_text(encoding="utf-8")
    called = {node.args[0].value for node in ast.walk(ast.parse(source))
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_call"
              and node.args and isinstance(node.args[0], ast.Constant)}
    assert called == {"getMe", "getUpdates", "sendMessage", "editMessageText", "answerCallbackQuery"}
    literals = {n.value for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "parse_mode" not in literals and not any("Markdown" in v or "HTML" in v for v in literals)
    for forbidden in ("Retry(", "max_retries=", "setWebhook", "deleteMessage", "sendDocument", "forwardMessage",
                      "HTTPAdapter", "verify=False"):
        assert forbidden not in source, forbidden
    for path in TELEGRAM.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not any(isinstance(n, (ast.While, ast.AsyncFunctionDef, ast.Await)) for n in ast.walk(tree)), path.name
        assert "time.sleep" not in path.read_text(encoding="utf-8"), path.name


def test_offline_and_gmail_only_runs_never_load_telegram_code(tmp_path: Path) -> None:
    from tests.runtime.builders import env
    environ = env(tmp_path / "offline.sqlite3")
    code = (
        "import sys, io\n"
        "from app.runtime.cli import main\n"
        f"environ = {dict(environ)!r}\n"
        "main(['init'], environ, io.StringIO()); main(['provider-status'], environ, io.StringIO())\n"
        "main(['operator-sync'], environ, io.StringIO()); main(['tick'], environ, io.StringIO())\n"
        "loaded = sorted(m for m in sys.modules if m.startswith(('requests', 'app.integrations.telegram')))\n"
        "assert loaded == [], loaded\n"
    )
    completed = subprocess.run([sys.executable, "-c", code], cwd=APP.parent, capture_output=True, text=True, timeout=120,
                               env={"PYTHONPATH": str(APP.parent), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")})
    assert completed.returncode == 0, completed.stderr


def test_requirements_pin_the_only_new_direct_dependency() -> None:
    lines = [line.strip() for line in (APP.parent / "requirements.txt").read_text(encoding="utf-8").splitlines()]
    assert [line for line in lines if line and not line.startswith("#")][-1] == "requests>=2.31,<3"
    assert not any(name in line.lower() for line in lines if not line.startswith("#") for name in ("telegram", "aiogram", "telebot"))
