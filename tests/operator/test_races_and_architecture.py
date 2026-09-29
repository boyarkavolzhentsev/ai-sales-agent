"""Threaded races on a file database, and load-bearing architecture checks."""

import ast
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from app.core.enums import DNCScope, LeadIntent, OutboundStatus
from app.llm import LLMTask
from app.operator import CommandRejectedError, CommandResult, DraftDetail, StaleCommandError
from app.persistence import Database
from tests.inbound.builders import NOW, SENDER, ScriptedTransport, classification, envelope
from tests.inbound.builders import service as inbound_service
from tests.operator.builders import AS_ALICE, AS_BOB, approve_command, make_draft, operator, reject_command

ROUNDS = 5


def run_concurrently(db_path: Path, *jobs: Callable[[Database], object]) -> list[object]:
    """Run each job on its own connection, released together; return result or exception."""
    barrier = threading.Barrier(len(jobs))
    results: list[object] = [None] * len(jobs)

    def worker(index: int, job: Callable[[Database], object]) -> None:
        with Database(db_path, busy_timeout_ms=10_000) as db:
            barrier.wait()
            try:
                results[index] = job(db)
            except Exception as exc:  # noqa: BLE001 - collected and asserted below
                results[index] = exc

    threads = [threading.Thread(target=worker, args=(i, job)) for i, job in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


def prepared(db_path: Path) -> tuple[str, DraftDetail]:
    with Database(db_path) as db:
        draft = make_draft(db)
        return draft.outbound_id or "", operator(db).get_draft(AS_ALICE, draft.outbound_id or "")


@pytest.mark.parametrize("round_no", range(ROUNDS))
def test_concurrent_approve_and_reject_have_one_winner(db_path: Path, round_no: int) -> None:
    outbound_id, detail = prepared(db_path)
    results = run_concurrently(
        db_path,
        lambda db: operator(db).approve_draft(AS_ALICE, approve_command(detail)),
        lambda db: operator(db).reject_draft(AS_BOB, reject_command(detail)),
    )
    winners = [r for r in results if isinstance(r, CommandResult)]
    losers = [r for r in results if isinstance(r, StaleCommandError)]
    assert (len(winners), len(losers)) == (1, 1), results
    with Database(db_path) as db, db.transaction() as uow:
        final = uow.outbound.get(outbound_id)
        assert final is not None and final.version == 2
        expected = OutboundStatus.OPERATOR_APPROVED if winners[0].outcome.kind == "APPROVE_DRAFT" else OutboundStatus.CANCELLED
        assert final.status is expected


@pytest.mark.parametrize("round_no", range(ROUNDS))
def test_concurrent_approve_and_unsubscribe_always_end_suppressed_and_cancelled(db_path: Path, round_no: int) -> None:
    outbound_id, detail = prepared(db_path)

    def unsubscribe(db: Database) -> object:
        transport = ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.UNSUBSCRIBE))
        return inbound_service(db, transport).process(envelope("p-unsub", body="Please unsubscribe me."), correlation_id="c-unsub")

    results = run_concurrently(
        db_path,
        lambda db: operator(db).approve_draft(AS_ALICE, approve_command(detail)),
        unsubscribe,
    )
    approval, _ = results
    assert isinstance(approval, (CommandResult, CommandRejectedError)), approval  # approved first, or refused
    with Database(db_path) as db, db.transaction() as uow:
        final = uow.outbound.get(outbound_id)
        assert final is not None and final.status is OutboundStatus.CANCELLED
        assert len(uow.dnc.list_active(DNCScope.EMAIL, SENDER, NOW)) == 1


# ---- Architecture ---------------------------------------------------------------------------------

OPERATOR_DIR = Path(__file__).resolve().parents[2] / "app" / "operator"
ALLOWED_APP_MODULES = (
    "app.core", "app.persistence", "app.knowledge.retrieval", "app.llm", "app.inbound", "app.operator",
    "app.policy.suppression",
)
FORBIDDEN_MODULES = (
    "smtplib", "imaplib", "poplib", "http", "socket", "ssl", "urllib.request", "requests", "httpx", "aiohttp",
    "openai", "anthropic", "telegram", "email.mime", "app.policy.reservation", "app.policy.quota",
    "app.knowledge.ingestion",
)
FORBIDDEN_NAMES = {
    "SendPermit", "send_permit_id", "reserve_quota", "SendGate", "AUTO_REPLY", "SENDING", "SENT",
    "ingest_loaded", "ingest_directory", "add_chunk", "add_fact",
}


def sources() -> list[Path]:
    files = sorted(OPERATOR_DIR.glob("*.py"))
    assert len(files) >= 5
    return files


def test_operator_imports_only_allowed_modules() -> None:
    violations = []
    for path in sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
            )
            for name in names:
                if name.startswith("app") and not name.startswith(ALLOWED_APP_MODULES):
                    violations.append(f"{path.name}: {name}")
                if any(name == m or name.startswith(f"{m}.") for m in FORBIDDEN_MODULES):
                    violations.append(f"{path.name}: {name}")
    assert violations == []


def test_operator_never_sends_permits_reserves_or_ingests() -> None:
    found = []
    for path in sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
            if name in FORBIDDEN_NAMES:
                found.append(f"{path.name}: {name}")
    assert found == []


def test_importing_the_operator_package_has_no_side_effects() -> None:
    import subprocess
    import sys

    code = (
        "import socket, sys\n"
        "def deny(*a, **k): raise AssertionError('network use at import time')\n"
        "socket.socket = deny; socket.create_connection = deny\n"
        "import app.operator\n"
        "assert not any(m.startswith(('telegram', 'openai', 'anthropic', 'smtplib')) for m in sys.modules)\n"
    )
    completed = subprocess.run([sys.executable, "-c", code], cwd=OPERATOR_DIR.parents[1], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
