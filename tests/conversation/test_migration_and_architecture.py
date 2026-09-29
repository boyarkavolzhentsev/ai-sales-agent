"""Migration v6, SQL constraints of the new tables, and package boundaries."""

import ast
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from app.core.enums import ConversationStatus, FollowUpJobStatus
from app.core.models import FollowUpJob
from app.persistence import AlreadyExistsError, Database, FrozenClock, IntegrityError
from app.persistence.migrations import MIGRATIONS, apply_migrations, current_version, latest_version
from tests.conversation.builders import FIRST_DUE, conversation, jobs, replied_conversation, scheduler
from app.conversation import conversation_id_for
from tests.inbound.builders import NOW, envelope, happy_transport, process
from tests.inbound.test_threads_and_transitions import outbound_history
from tests.inbound.conftest import seed_knowledge


def test_fresh_database_reaches_v6(db_path: Path) -> None:
    raw = sqlite3.connect(db_path)
    try:
        assert current_version(raw) == latest_version() == len(MIGRATIONS) >= 6
        names = {r[0] for r in raw.execute("SELECT name FROM sqlite_master")}
    finally:
        raw.close()
    assert {"conversations", "follow_up_jobs", "follow_up_jobs_one_open_per_conversation", "follow_up_jobs_due_idx",
            "follow_up_jobs_lease_idx", "conversations_lead_idx", "conversations_contact_idx"} <= names


def test_stage8_database_upgrades_and_keeps_its_data(tmp_path: Path) -> None:
    path = tmp_path / "stage8.sqlite3"
    raw = sqlite3.connect(path, isolation_level=None)
    raw.execute("PRAGMA foreign_keys = ON")
    try:
        assert apply_migrations(raw, FrozenClock(NOW), MIGRATIONS[:5]) == 5
    finally:
        raw.close()
    with Database(path) as db:
        # Stage 1-8 rows written on the v5 schema, before conversations exist.
        seed_knowledge(db)
        lead = outbound_history(db)
    with Database(path) as db:
        assert db.initialize_schema(FrozenClock(NOW + timedelta(days=1))) == latest_version()
        with db.transaction() as uow:
            assert uow.leads.get(lead.lead_id) == lead and uow.threads.get("th-out") is not None
            assert uow.conversations.get_by_thread("th-out") is None  # nothing invented by the migration
        result = process(db, happy_transport(), envelope("p-1", in_reply_to="<out-1@ourco.example>"))
        assert result.thread_id == "th-out"
        joined = conversation(db, conversation_id_for("th-out"))
        assert (joined.lead_id, joined.status) == (lead.lead_id, ConversationStatus.ACTIVE)
        replied = replied_conversation(db, "p-9", sender="other@elsewhere.example")
        assert conversation(db, replied.conversation_id).status is ConversationStatus.WAITING_FOR_REPLY
        assert db.initialize_schema(FrozenClock(NOW)) == latest_version()  # idempotent


def test_sql_allows_one_open_job_per_conversation_and_one_job_per_logical_follow_up(db: Database) -> None:
    replied = replied_conversation(db)
    scheduler(db).schedule(replied.conversation_id, correlation_id="c")
    [job] = jobs(db, replied.conversation_id)
    parallel = job.model_copy(update={"follow_up_id": "fu_other", "sequence_no": 2})
    with pytest.raises((IntegrityError, AlreadyExistsError)), db.transaction() as uow:
        uow.follow_up_jobs.add(parallel)  # a second open job
    same_logical = job.model_copy(update={"follow_up_id": "fu_same", "status": FollowUpJobStatus.CANCELLED})
    with pytest.raises((IntegrityError, AlreadyExistsError)), db.transaction() as uow:
        uow.follow_up_jobs.add(same_logical)  # same (conversation, anchor, sequence)


def test_job_and_conversation_invariants() -> None:
    base = {
        "follow_up_id": "fu_1", "conversation_id": "cv_1", "anchor_outbound_id": "ob_1", "sequence_no": 1,
        "basis_conversation_version": 1, "due_at": FIRST_DUE, "created_at": NOW, "updated_at": NOW,
    }
    FollowUpJob.model_validate(base)
    with pytest.raises(ValueError, match="claim_token"):
        FollowUpJob.model_validate(base | {"status": FollowUpJobStatus.CLAIMED})
    with pytest.raises(ValueError, match="outbound_id"):
        FollowUpJob.model_validate(base | {"status": FollowUpJobStatus.COMPLETED})
    with pytest.raises(ValueError, match="block_codes"):
        FollowUpJob.model_validate(base | {"status": FollowUpJobStatus.BLOCKED})


CONVERSATION_DIR = Path(__file__).resolve().parents[2] / "app" / "conversation"
ALLOWED = ("app.core", "app.persistence", "app.policy", "app.llm", "app.conversation")
FORBIDDEN = ("smtplib", "imaplib", "socket", "http", "urllib", "requests", "httpx", "openai", "anthropic", "telegram",
             "email", "asyncio", "threading", "app.inbound", "app.operator", "app.dispatch", "app.policy.reservation")


def test_conversation_package_imports_only_allowed_modules() -> None:
    violations = []
    for path in sorted(CONVERSATION_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
            )
            for name in names:
                if name.startswith("app") and not name.startswith(ALLOWED):
                    violations.append(f"{path.name}: {name}")
                if any(name == m or name.startswith(f"{m}.") for m in FORBIDDEN):
                    violations.append(f"{path.name}: {name}")
    assert violations == []


def test_importing_the_conversation_package_has_no_side_effects() -> None:
    code = (
        "import socket, sys\n"
        "def deny(*a, **k): raise AssertionError('network use at import time')\n"
        "socket.socket = deny; socket.create_connection = deny\n"
        "import app.conversation\n"
        "assert 'app.dispatch' not in sys.modules and 'app.inbound' not in sys.modules\n"
    )
    completed = subprocess.run([sys.executable, "-c", code], cwd=CONVERSATION_DIR.parents[1], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
