"""Migration v10 (provider-neutral mailbox sync) and the Gmail provider's boundaries."""

import ast
import sqlite3
from datetime import timedelta
from pathlib import Path

from app.persistence import Database, FrozenClock
from app.persistence.migrations import MIGRATIONS, apply_migrations, current_version, latest_version
from tests.inbound.builders import NOW
from tests.inbound.conftest import seed_knowledge
from tests.pipeline.builders import active_opportunity, lead, opportunity_lead

APP = Path(__file__).resolve().parents[2] / "app"
GMAIL = APP / "integrations" / "gmail"
SECRET_WORDS = ("token", "secret", "password", "credential", "refresh", "access", "body", "subject")


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


# ---- Migration ------------------------------------------------------------------------------------


def test_a_fresh_database_reaches_v10_with_minimal_sync_tables(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite3"
    with Database(path) as db:
        assert db.initialize_schema(FrozenClock(NOW)) == latest_version() >= 10
    raw = sqlite3.connect(path)
    try:
        assert current_version(raw) == latest_version() and MIGRATIONS[9].name == "email_provider_sync"
        columns = {table: [row[1] for row in raw.execute(f"PRAGMA table_info({table})")]
                   for table in ("mailbox_sync_states", "mailbox_sync_failures")}
    finally:
        raw.close()
    assert columns["mailbox_sync_states"] == ["state_id", "provider", "mailbox", "status", "updated_at", "version", "data"]
    assert all(not any(word in column for word in SECRET_WORDS) for cols in columns.values() for column in cols)


def test_v1_to_v9_are_unchanged_and_a_v9_database_upgrades_intact(tmp_path: Path) -> None:
    assert [m.name for m in MIGRATIONS[:9]] == ["initial_schema", "quota_reservations", "knowledge_index", "optional_company",
                                                 "dispatch_attempts", "conversations", "campaign_execution", "sales_pipeline",
                                                 "commercial_decisioning"]
    path = tmp_path / "stage15.sqlite3"
    raw = sqlite3.connect(path, isolation_level=None)
    raw.execute("PRAGMA foreign_keys = ON")
    try:
        assert apply_migrations(raw, FrozenClock(NOW), MIGRATIONS[:9]) == 9
    finally:
        raw.close()
    with Database(path) as db:
        seed_knowledge(db)
        lead_id = opportunity_lead(db)
        before = (lead(db, lead_id), active_opportunity(db, lead_id))
    with Database(path) as db:
        assert db.initialize_schema(FrozenClock(NOW + timedelta(days=1))) == latest_version()
        assert db.initialize_schema(FrozenClock(NOW + timedelta(days=2))) == latest_version()  # idempotent
        assert (lead(db, lead_id), active_opportunity(db, lead_id)) == before
        with db.transaction() as uow:
            assert uow.mailbox_sync.get_state("gmail", "sales@ourco.example") is None  # nothing backfilled


# ---- Architecture ------------------------------------------------------------------------------------


def test_only_composition_ever_selects_gmail() -> None:
    allowed = {APP / "integrations" / "registry.py", APP / "integrations" / "status.py", APP / "runtime" / "cli.py"}
    offenders = [f"{p.relative_to(APP)}: {n}" for p in APP.rglob("*.py") if not p.is_relative_to(GMAIL) and p not in allowed
                 for n in imports(p) if matches(n, ("app.integrations.gmail",))]
    assert offenders == []
    status = (APP / "integrations" / "status.py").read_text(encoding="utf-8")
    assert "gmail.tokens" in status and "gmail.provider" not in status and "gmail.auth" not in status


def test_google_code_lives_only_in_the_gmail_package() -> None:
    # Stages 17/18/19: the Telegram, LLM and embeddings HTTPS clients use requests (no Google code).
    https = (APP / "integrations" / "telegram", APP / "integrations" / "llm", APP / "integrations" / "embeddings")
    offenders = [f"{p.relative_to(APP)}: {n}" for p in APP.rglob("*.py") if not p.is_relative_to(GMAIL)
                 for n in imports(p) if matches(n, ("google", "google_auth_oauthlib", "oauthlib", "httplib2", "googleapiclient"))
                 or (matches(n, ("requests",)) and not any(p.is_relative_to(d) for d in https))]
    assert offenders == []


def test_the_gmail_package_depends_only_on_contracts() -> None:
    may = ("app.core", "app.integrations", "app.dispatch.transport", "app.inbound.models", "google.auth", "google.oauth2",
           "google_auth_oauthlib", "requests")
    never_business = ("app.pipeline", "app.commercial", "app.campaign", "app.conversation", "app.orchestration",
                      "app.operator", "app.runtime", "app.persistence", "app.dispatch.service", "app.inbound.service")
    problems = []
    for path in sorted(GMAIL.glob("*.py")):
        for name in imports(path):
            if name.startswith(("app", "google", "requests")) and not matches(name, may):
                problems.append(f"{path.name}: {name}")
            if matches(name, never_business):
                problems.append(f"{path.name}: business import {name}")
    assert problems == []
    assert [n for n in imports(GMAIL / "tokens.py") if n.startswith(("app", "google", "requests"))] == []  # stdlib only


def test_the_gmail_client_has_no_mailbox_side_effects_or_retries() -> None:
    source = (GMAIL / "client.py").read_text(encoding="utf-8")
    for forbidden in ("/modify", "/trash", "batchModify", "batchDelete", "/labels", "removeLabelIds", "addLabelIds",
                      "num_retries", "Retry(", "/drafts", "/import", "/insert"):
        assert forbidden not in source, forbidden
    assert "refresh_status_codes=()" in source and "max_refresh_attempts=0" in source
    for path in GMAIL.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not any(isinstance(n, ast.While) for n in ast.walk(tree)), path.name


def test_requirements_add_only_the_official_google_auth_libraries() -> None:
    lines = [line.strip() for line in (APP.parent / "requirements.txt").read_text(encoding="utf-8").splitlines()]
    assert [line for line in lines if line and not line.startswith("#")] == [
        "pydantic>=2,<3", "tzdata>=2024.1", "PyYAML>=6,<7", "google-auth[requests]>=2.40,<3", "google-auth-oauthlib>=1.2,<2",
        "requests>=2.31,<3"]  # Stage 17 declares the HTTP stack it imports directly
