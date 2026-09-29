"""Migration v7, the new SQL constraints, and campaign package boundaries."""

import ast
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from app.core.enums import CampaignJobStatus
from app.persistence import AlreadyExistsError, Database, FrozenClock, IntegrityError
from app.persistence.migrations import MIGRATIONS, apply_migrations, current_version, latest_version
from tests.campaign.builders import CAMPAIGN_ID, activate, add_campaign, add_prospect, enrolled, member, ready_campaign, scheduler
from tests.conversation.builders import replied_conversation
from tests.inbound.builders import NOW
from tests.inbound.conftest import seed_knowledge


def test_fresh_database_reaches_v7(db_path: Path) -> None:
    raw = sqlite3.connect(db_path)
    try:
        assert current_version(raw) == latest_version() == len(MIGRATIONS) >= 7
        names = {r[0] for r in raw.execute("SELECT name FROM sqlite_master")}
    finally:
        raw.close()
    assert {"campaign_members", "campaign_jobs", "campaign_jobs_one_open_per_member", "campaign_jobs_due_idx",
            "campaign_jobs_lease_idx", "campaign_members_status_idx", "campaign_members_contact_idx"} <= names


def test_stage9_database_upgrades_with_its_data(tmp_path: Path) -> None:
    path = tmp_path / "stage9.sqlite3"
    raw = sqlite3.connect(path, isolation_level=None)
    raw.execute("PRAGMA foreign_keys = ON")
    try:
        assert apply_migrations(raw, FrozenClock(NOW), MIGRATIONS[:6]) == 6
    finally:
        raw.close()
    with Database(path) as db:
        seed_knowledge(db)
        with db.transaction() as uow:
            add_campaign_rows = uow._tx.fetch_all("SELECT COUNT(*) FROM campaigns")[0][0]  # noqa: SLF001
        assert add_campaign_rows == 0
    with Database(path) as db:
        assert db.initialize_schema(FrozenClock(NOW + timedelta(days=1))) == latest_version()
        replied = replied_conversation(db)  # Stage 6-9 still work on the upgraded schema
        add_campaign(db)
        activate(db)
        member_id = enrolled(db)
        assert scheduler(db).schedule(CAMPAIGN_ID, correlation_id="c").scheduled
        assert member(db, member_id).lead_id is not None and replied.conversation_id
        assert db.initialize_schema(FrozenClock(NOW)) == latest_version()


def test_sql_enforces_one_membership_and_one_open_job(db: Database) -> None:
    member_id = ready_campaign(db)
    original = member(db, member_id)
    with pytest.raises((IntegrityError, AlreadyExistsError)), db.transaction() as uow:
        uow.campaign_members.add(original.model_copy(update={"member_id": "cm_other"}))  # same (campaign, contact)
    scheduler(db).schedule(CAMPAIGN_ID, correlation_id="c")
    with db.transaction() as uow:
        [job] = uow.campaign_jobs.list_for_member(member_id)
    with pytest.raises((IntegrityError, AlreadyExistsError)), db.transaction() as uow:
        uow.campaign_jobs.add(job.model_copy(update={"job_id": "cj_other", "touch_no": 2}))  # a second open job
    with pytest.raises((IntegrityError, AlreadyExistsError)), db.transaction() as uow:
        uow.campaign_jobs.add(job.model_copy(update={"job_id": "cj_same", "status": CampaignJobStatus.CANCELLED}))  # same touch
    add_prospect(db, "x@x-prospect.example", company_name=None)


CAMPAIGN_DIR = Path(__file__).resolve().parents[2] / "app" / "campaign"
ALLOWED = ("app.core", "app.persistence", "app.policy", "app.knowledge", "app.llm", "app.conversation", "app.campaign")
FORBIDDEN = ("smtplib", "imaplib", "socket", "http", "urllib", "requests", "httpx", "openai", "anthropic", "telegram",
             "email", "asyncio", "threading", "app.inbound", "app.operator", "app.dispatch", "app.policy.reservation",
             "app.conversation.state", "app.conversation.scheduler", "app.conversation.executor")
FORBIDDEN_NAMES = {"EmailTransport", "submit", "reserve_quota", "SendPermit", "DispatchService", "record_inbound_activity"}


def test_campaign_package_imports_only_allowed_modules_and_never_sends() -> None:
    violations = []
    for path in sorted(CAMPAIGN_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
            )
            for name in names:
                if name.startswith("app") and not name.startswith(ALLOWED):
                    violations.append(f"{path.name}: {name}")
                if any(name == m or name.startswith(f"{m}.") for m in FORBIDDEN):
                    violations.append(f"{path.name}: {name}")
            attr = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
            if attr in FORBIDDEN_NAMES:
                violations.append(f"{path.name}: {attr}")
    assert violations == []


def test_importing_the_campaign_package_has_no_side_effects() -> None:
    code = (
        "import socket, sys\n"
        "def deny(*a, **k): raise AssertionError('network use at import time')\n"
        "socket.socket = deny; socket.create_connection = deny\n"
        "import app.campaign\n"
        "assert not {'app.dispatch', 'app.inbound', 'app.operator'} & set(sys.modules)\n"
    )
    completed = subprocess.run([sys.executable, "-c", code], cwd=CAMPAIGN_DIR.parents[1], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
