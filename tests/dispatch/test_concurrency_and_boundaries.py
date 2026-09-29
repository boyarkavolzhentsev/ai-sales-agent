"""D. Concurrency and unsubscribe ordering. I. Architecture boundaries."""

import ast
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from app.core.enums import CloseReason, DNCScope, LeadIntent, LeadStage, OutboundStatus
from app.dispatch import DispatchCode, DispatchOutcome, DispatchResult, FakeBehavior, FakeEmailTransport, FakeStep, TransportRequest
from app.llm import LLMTask
from app.persistence import Database, DispatchAttemptState
from tests.dispatch.builders import approved_reply, dispatcher, send, state
from tests.inbound.builders import NOW, SENDER, ScriptedTransport, classification, envelope
from tests.inbound.builders import service as inbound_service

ROUNDS = 5


def unsubscribe(db: Database, provider_message_id: str = "p-unsub") -> None:
    transport = ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.UNSUBSCRIBE))
    inbound_service(db, transport).process(envelope(provider_message_id, body="Please unsubscribe me."), correlation_id="c-unsub")


def suppressed(db: Database) -> bool:
    with db.transaction() as uow:
        return len(uow.dnc.list_active(DNCScope.EMAIL, SENDER, NOW)) == 1


def run_concurrently(db_path: Path, *jobs: Callable[[Database], object]) -> list[object]:
    barrier = threading.Barrier(len(jobs))
    results: list[object] = [None] * len(jobs)

    def worker(index: int, job: Callable[[Database], object]) -> None:
        with Database(db_path, busy_timeout_ms=10_000) as db:
            barrier.wait()
            try:
                results[index] = job(db)
            except Exception as exc:  # noqa: BLE001 - asserted below
                results[index] = exc

    threads = [threading.Thread(target=worker, args=(i, job)) for i, job in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


# ---- D. Concurrency -------------------------------------------------------------------------


@pytest.mark.parametrize("round_no", range(ROUNDS))
def test_two_workers_submit_at_most_once(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        outbound_id = approved_reply(db)
    transport = FakeEmailTransport()
    results = run_concurrently(
        db_path,
        lambda db: send(dispatcher(db, transport), outbound_id, "corr-a"),
        lambda db: send(dispatcher(db, transport), outbound_id, "corr-b"),
    )
    assert all(isinstance(r, DispatchResult) for r in results), results
    assert len(transport.calls) == 1
    assert sum(1 for r in results if isinstance(r, DispatchResult) and r.transport_called) == 1
    with Database(db_path) as db:
        current = state(db, outbound_id)
    assert current.outbound.status is OutboundStatus.SENT and len(current.attempts) == 1


@pytest.mark.parametrize("round_no", range(ROUNDS))
def test_racing_dispatch_and_unsubscribe_always_preserve_suppression(db_path: Path, round_no: int) -> None:
    with Database(db_path) as db:
        outbound_id = approved_reply(db)
    transport = FakeEmailTransport()
    results = run_concurrently(db_path, lambda db: send(dispatcher(db, transport), outbound_id), lambda db: unsubscribe(db))
    assert not any(isinstance(r, Exception) for r in results), results
    with Database(db_path) as db:
        current = state(db, outbound_id)
        assert suppressed(db)
        if transport.calls:  # the claim committed first: the in-flight submission completed honestly
            assert current.outbound.status is OutboundStatus.SENT and len(transport.calls) == 1
        else:  # the unsubscribe committed first: nothing was submitted
            assert current.outbound.status is OutboundStatus.CANCELLED and current.attempts == []
        # Either way no further submission is possible.
        send(dispatcher(db, transport), outbound_id, "corr-later")
    assert len(transport.calls) <= 1


def test_unsubscribe_before_the_claim_blocks_submission(db: Database) -> None:
    outbound_id = approved_reply(db)
    unsubscribe(db)
    transport = FakeEmailTransport()
    result = send(dispatcher(db, transport), outbound_id)
    assert result.outcome is DispatchOutcome.BLOCKED and result.reason_codes == (DispatchCode.ARTIFACT_CANCELLED,)
    assert transport.calls == [] and suppressed(db)


def test_unsubscribe_during_an_in_flight_submission(db: Database) -> None:
    outbound_id = approved_reply(db)

    def customer_unsubscribes(_: TransportRequest) -> None:
        unsubscribe(db)
        # Stage 6 cannot cancel a message that is already SENDING: the hand-off is in flight.
        assert state(db, outbound_id).outbound.status is OutboundStatus.SENDING

    transport = FakeEmailTransport().script(FakeStep(FakeBehavior.ACCEPT, before=customer_unsubscribes))
    result = send(dispatcher(db, transport), outbound_id)
    assert result.outcome is DispatchOutcome.ACCEPTED and state(db, outbound_id).outbound.status is OutboundStatus.SENT
    assert suppressed(db)
    lead = state(db, outbound_id).outbound.lead_id
    with db.transaction() as uow:
        closed = uow.leads.get(lead)
    assert closed is not None and (closed.stage, closed.close_reason) == (LeadStage.CLOSED, CloseReason.UNSUBSCRIBED)
    assert send(dispatcher(db, transport), outbound_id, "corr-later").transport_called is False


def test_in_flight_rejection_after_unsubscribe_is_never_retried(db: Database) -> None:
    outbound_id = approved_reply(db)
    transport = FakeEmailTransport().script(FakeStep(FakeBehavior.REJECT, retryable=True, before=lambda _: unsubscribe(db)))
    first = send(dispatcher(db, transport), outbound_id)
    assert first.outcome is DispatchOutcome.NOT_ACCEPTED and suppressed(db)
    retry = send(dispatcher(db, transport), outbound_id, "corr-retry")
    assert retry.outcome is DispatchOutcome.BLOCKED and "DNC_EMAIL" in retry.reason_codes
    assert len(transport.calls) == 1
    assert [a.state for a in state(db, outbound_id).attempts] == [DispatchAttemptState.NOT_ACCEPTED]


# ---- I. Boundaries ----------------------------------------------------------------------------

DISPATCH_DIR = Path(__file__).resolve().parents[2] / "app" / "dispatch"
ALLOWED_APP_MODULES = (
    "app.core", "app.persistence", "app.policy", "app.llm", "app.inbound", "app.operator", "app.dispatch", "app.conversation",
    "app.campaign",
)
FORBIDDEN_MODULES = (
    "smtplib", "imaplib", "poplib", "http", "socket", "ssl", "urllib", "requests", "httpx", "aiohttp", "openai",
    "anthropic", "telegram", "email", "asyncio", "app.knowledge.ingestion",
)


def test_dispatch_imports_only_allowed_modules() -> None:
    violations = []
    for path in sorted(DISPATCH_DIR.glob("*.py")):
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


def test_importing_dispatch_has_no_side_effects() -> None:
    code = (
        "import socket, sys\n"
        "def deny(*a, **k): raise AssertionError('network use at import time')\n"
        "socket.socket = deny; socket.create_connection = deny\n"
        "import app.dispatch\n"
        "assert not any(m.split('.')[0] in {'smtplib', 'telegram', 'openai', 'anthropic', 'httpx', 'requests'} for m in sys.modules)\n"
    )
    completed = subprocess.run([sys.executable, "-c", code], cwd=DISPATCH_DIR.parents[1], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr


def test_the_transport_only_sees_the_request() -> None:
    from app.dispatch.transport import EmailTransport

    hints = EmailTransport.submit.__annotations__
    assert set(hints) == {"request", "return"}
