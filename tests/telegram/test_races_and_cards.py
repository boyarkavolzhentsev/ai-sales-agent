"""Concurrency (two operator-sync passes over one database, repeated) and the remaining
review cards: escalations, commercial items. Fake Bot API only."""

import threading
from pathlib import Path

import pytest

from app.core.enums import LeadStatus, OutboundStatus
from app.orchestration import ExecutionAction as A
from app.persistence import Database
from app.runtime import SalesAgentRuntime, load_config
from tests.inbound.builders import NOW
from tests.operator.builders import FakeAuthenticator
from tests.runtime.builders import env
from tests.telegram.builders import Console, console, fake_connectors, telegram_values
from tests.telegram.fakes import ALICE_CHAT, BOB_CHAT, FakeTelegramApi
from tests.telegram.test_review import drafted, status, telegram_events


def second_runtime(tmp_path: Path, api: FakeTelegramApi) -> SalesAgentRuntime:
    from app.runtime import Adapters
    config = load_config(env(tmp_path / "agent.sqlite3", **telegram_values()), now=NOW)
    app = SalesAgentRuntime(config, adapters=Adapters(authenticator=FakeAuthenticator()), connectors=fake_connectors(api))
    app.start()
    return app


def in_parallel(*jobs: object) -> list[object]:
    barrier = threading.Barrier(len(jobs))
    results: list[object] = [None] * len(jobs)

    def run(index: int, job) -> None:  # noqa: ANN001
        barrier.wait()
        try:
            results[index] = job()
        except Exception as exc:  # noqa: BLE001 - asserted below
            results[index] = exc

    threads = [threading.Thread(target=run, args=(i, job)) for i, job in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


@pytest.mark.parametrize("attempt", range(5))
def test_two_passes_racing_on_the_same_press_act_once(tmp_path: Path, attempt: int) -> None:
    c = console(tmp_path)
    outbound_id = drafted(c)
    c.sync()
    [approve] = c.telegram.buttons_for(ALICE_CHAT, "Approve")
    [bob_approve] = c.telegram.buttons_for(BOB_CHAT, "Approve")
    c.telegram.press(ALICE_CHAT, approve)
    c.telegram.press(BOB_CHAT, bob_approve)  # both operators press at the same moment
    c.app.stop()

    def one_process():  # noqa: ANN202 - its own runtime and connection, in its own thread
        app = second_runtime(tmp_path, c.telegram)
        try:
            return app.operator_sync()
        finally:
            app.stop()

    results = in_parallel(one_process, one_process)
    assert not any(isinstance(r, Exception) for r in results), results
    outcomes: dict[str, int] = {}
    for result in results:
        for key, count in result.outcomes.items():  # type: ignore[union-attr]
            outcomes[key] = outcomes.get(key, 0) + count
    assert outcomes.get("ACTION", 0) == 1, outcomes  # exactly one approval, by whoever won the race
    assert set(outcomes) <= {"ACTION", "STALE", "ALREADY_HANDLED", "DUPLICATE"}
    with Database(tmp_path / "agent.sqlite3") as db, db.transaction() as uow:
        assert uow.outbound.get(outbound_id).status is OutboundStatus.OPERATOR_APPROVED  # type: ignore[union-attr]
        commands = uow._tx.fetch_all(  # noqa: SLF001
            "SELECT COUNT(*) FROM idempotency_keys WHERE key LIKE 'operator:command:tg_%'")[0][0]
    assert commands == 1  # one Stage 7 command ran in total


def test_an_escalation_card_resolves_or_takes_ownership(tmp_path: Path) -> None:
    c = console(tmp_path)
    escalated(c)
    plan = c.world.plan()
    assert plan.action is A.ESCALATION_REVIEW
    c.sync()
    card = [s for s in c.telegram.sent if s.chat_id == ALICE_CHAT][-1]
    labels = [label for row in card.buttons for label, _ in row]
    assert labels[:3] == ["Resolve: no action", "Resolve: I replied", "Take ownership"]
    own = c.telegram.buttons_for(ALICE_CHAT, "Take ownership")[-1]
    c.telegram.press(ALICE_CHAT, own)
    assert c.sync().outcomes == {"ACTION": 1}
    assert c.world.lead_row().status is LeadStatus.OPERATOR_OWNED
    resolve = c.telegram.buttons_for(ALICE_CHAT, "Resolve: I replied")[-1]
    c.telegram.press(ALICE_CHAT, resolve)
    assert c.sync().outcomes == {"ACTION": 1}
    with c.db.transaction() as uow:
        rows = uow._tx.fetch_all("SELECT data FROM escalations")  # noqa: SLF001
    assert rows and all('"OPERATOR_REPLIED"' in r[0] for r in rows)


def escalated(c: Console) -> None:
    """A customer reply that Stage 6 escalates (no knowledge answers it)."""
    from app.core.enums import LeadIntent
    from app.llm import LLMTask
    from tests.inbound.builders import classification
    from tests.orchestration.builders import approve_pending, customer_replies, enrolled
    enrolled(c.world)
    c.world.execute()
    approve_pending(c.world)
    c.world.execute(dispatch=True)
    c.world.llm._scripts[LLMTask.INTENT_CLASSIFICATION].appendleft(classification(LeadIntent.NEGOTIATION))  # noqa: SLF001
    customer_replies(c.world, "p-discount", body="Can you do 30% off if we sign today?")


def test_the_reply_count_is_bounded_per_update(tmp_path: Path) -> None:
    c = console(tmp_path)
    drafted(c)
    c.sync()
    before = len(c.telegram.sent)
    [approve] = c.telegram.buttons_for(ALICE_CHAT, "Approve")
    c.telegram.press(ALICE_CHAT, approve)
    c.sync()
    replies = [s for s in c.telegram.sent[before:] if s.chat_id == ALICE_CHAT and not s.text.startswith("Campaign")]
    assert len(replies) == 1 and len(c.telegram.answers) == 1 and len(c.telegram.edited) == 1
    assert telegram_events(c) >= 1
