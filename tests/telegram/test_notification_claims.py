"""Durable, concurrency-safe review-card delivery: a card is claimed (CAS) before it is
sent, the claim moves to SUBMITTING before sendMessage, and only the current claim holder
may settle it. Concurrent passes never both send one card; an ambiguous send is never
repeated automatically; a confirmed failure or an abandoned pre-submit claim is retried."""

import threading
from datetime import timedelta
from pathlib import Path

import pytest

from app.integrations.telegram.console import NOTIFICATION_LEASE, TelegramConsole
from app.integrations.telegram.errors import TelegramCode, TelegramError
from app.persistence import Database, NotificationStatus
from tests.telegram.builders import Console, console
from tests.telegram.fakes import ALICE_CHAT, BOB_CHAT
from tests.telegram.test_races_and_cards import in_parallel, second_runtime
from tests.telegram.test_review import drafted


def rows(db: Database) -> list[tuple[int, str, int]]:
    with db.transaction() as uow:
        return [tuple(r) for r in uow._tx.fetch_all(  # noqa: SLF001
            "SELECT chat_id, status, json_extract(data, '$.claim_count') FROM operator_notifications ORDER BY chat_id")]


def cards(c: Console, chat: int) -> int:
    return sum(1 for s in c.telegram.sent if s.chat_id == chat and s.text.startswith("Campaign draft to review"))


def channel(c: Console) -> TelegramConsole:
    return c.app.services.operator_channel.console


def later(c: Console) -> None:
    c.world.advance(NOTIFICATION_LEASE + timedelta(seconds=1))


# ---- Concurrency ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("attempt", range(12))
def test_two_concurrent_syncs_never_send_one_card_twice(tmp_path: Path, attempt: int) -> None:
    c = console(tmp_path)
    drafted(c)
    c.app.stop()
    c.telegram.updates_barrier = threading.Barrier(2)  # both passes reach the card step together
    c.telegram.send_delay = 0.05  # and overlap while a card is being sent

    def one_process():  # noqa: ANN202 - its own runtime and connection, in its own thread
        app = second_runtime(tmp_path, c.telegram)
        try:
            return app.operator_sync()
        finally:
            app.stop()

    results = in_parallel(one_process, one_process)
    assert not any(isinstance(r, BaseException) for r in results), results
    assert all(r.status.value in ("OK", "PARTIAL") for r in results)  # type: ignore[union-attr]
    assert sum(r.notifications_sent for r in results) == 2  # type: ignore[union-attr]  # one per operator chat
    assert (cards(c, ALICE_CHAT), cards(c, BOB_CHAT)) == (1, 1)
    assert c.telegram.calls.count("send_message") == 2
    with Database(tmp_path / "agent.sqlite3") as db:
        assert rows(db) == [(ALICE_CHAT, "SENT", 1), (BOB_CHAT, "SENT", 1)]  # one identity per recipient


# ---- Claims and crashes -------------------------------------------------------------------------------------


def test_a_sent_card_is_never_resent(tmp_path: Path) -> None:
    c = console(tmp_path)
    drafted(c)
    c.sync()
    for _ in range(3):
        later(c)
        c.sync()
    assert (cards(c, ALICE_CHAT), cards(c, BOB_CHAT)) == (1, 1)
    assert rows(c.db) == [(ALICE_CHAT, "SENT", 1), (BOB_CHAT, "SENT", 1)]


def test_a_crash_before_submission_is_recovered_after_the_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = console(tmp_path)
    drafted(c)
    real = channel(c).card

    def crash(*args: object, **kwargs: object) -> object:
        monkeypatch.setattr(channel(c), "card", real)
        raise KeyboardInterrupt  # the worker dies after claiming, before calling Telegram

    monkeypatch.setattr(channel(c), "card", crash)
    with pytest.raises(KeyboardInterrupt):
        c.sync()
    assert rows(c.db) == [(ALICE_CHAT, "CLAIMED", 1)] and c.telegram.calls.count("send_message") == 0
    c.sync()  # the lease is still held: nobody else sends Alice's card
    assert (cards(c, ALICE_CHAT), cards(c, BOB_CHAT)) == (0, 1)
    later(c)
    c.sync()  # the expired, never-submitted claim is recovered
    assert (cards(c, ALICE_CHAT), cards(c, BOB_CHAT)) == (1, 1)
    assert rows(c.db) == [(ALICE_CHAT, "SENT", 2), (BOB_CHAT, "SENT", 1)]


def test_an_ambiguous_send_is_never_repeated_automatically(tmp_path: Path) -> None:
    c = console(tmp_path)
    drafted(c)
    c.telegram.fail_once["send_message"] = [TelegramError(TelegramCode.NETWORK_ERROR, uncertain=True)]  # read timeout
    result = c.sync()
    assert (result.notifications_sent, result.notifications_failed) == (1, 1)
    assert rows(c.db) == [(ALICE_CHAT, "UNKNOWN", 1), (BOB_CHAT, "SENT", 1)]
    for _ in range(3):
        later(c)
        c.sync()
    assert c.telegram.calls.count("send_message") == 2  # no second attempt for Alice's card
    assert rows(c.db)[0] == (ALICE_CHAT, "UNKNOWN", 1)
    c.telegram.text(ALICE_CHAT, "/queue")  # the operator can still see the item
    c.sync()
    assert "Campaign draft to review" in c.telegram.texts(ALICE_CHAT)[-1]


def test_a_crash_while_submitting_is_never_resent(tmp_path: Path) -> None:
    c = console(tmp_path)
    drafted(c)
    c.telegram.fail_once["send_message"] = [KeyboardInterrupt()]  # type: ignore[list-item]  # dies inside the call
    with pytest.raises(KeyboardInterrupt):
        c.sync()
    assert rows(c.db) == [(ALICE_CHAT, "SUBMITTING", 1)]
    later(c)
    c.sync()
    later(c)
    c.sync()
    assert cards(c, ALICE_CHAT) == 0 and cards(c, BOB_CHAT) == 1
    assert rows(c.db)[0] == (ALICE_CHAT, "SUBMITTING", 1)


@pytest.mark.parametrize("error", [
    TelegramError(TelegramCode.NETWORK_ERROR),  # never connected
    TelegramError(TelegramCode.FORBIDDEN, status=403),  # refused by Telegram
    TelegramError(TelegramCode.RATE_LIMITED, status=429, retry_after=1),
])
def test_a_confirmed_failure_is_retried_by_a_later_pass(tmp_path: Path, error: TelegramError) -> None:
    c = console(tmp_path)
    drafted(c)
    c.telegram.fail_once["send_message"] = [error]
    c.sync()
    assert rows(c.db)[0][:2] == (ALICE_CHAT, "FAILED")
    c.sync()
    assert (cards(c, ALICE_CHAT), cards(c, BOB_CHAT)) == (1, 1)
    assert rows(c.db) == [(ALICE_CHAT, "SENT", 2), (BOB_CHAT, "SENT", 1)]


def test_a_stale_claim_can_neither_submit_nor_settle(tmp_path: Path) -> None:
    c = console(tmp_path)
    drafted(c)
    console_ = channel(c)
    [plan] = console_.operator_plans(10)
    key = "tn_" + "0" * 40
    old = console_._claim(key, ALICE_CHAT, plan)  # noqa: SLF001
    assert old is not None and console_._claim(key, ALICE_CHAT, plan) is None  # noqa: SLF001 - held
    later(c)
    new = console_._claim(key, ALICE_CHAT, plan)  # noqa: SLF001 - the lease expired: another worker takes over
    assert new is not None and new != old
    assert console_._settle(key, old, NotificationStatus.SUBMITTING, phase=NotificationStatus.CLAIMED) is False  # noqa: SLF001
    assert console_._settle(key, old, NotificationStatus.SENT, message_id=7) is False  # noqa: SLF001
    assert console_._settle(key, new, NotificationStatus.SUBMITTING, phase=NotificationStatus.CLAIMED) is True  # noqa: SLF001
    assert console_._settle(key, old, NotificationStatus.SENT, message_id=7) is False  # noqa: SLF001
    assert console_._settle(key, new, NotificationStatus.SENT, message_id=8) is True  # noqa: SLF001
    with c.db.transaction() as uow:
        stored = uow.operator_channel.get_notification(key)
    assert stored is not None and (stored.status, stored.provider_message_id, stored.claim_count) == (NotificationStatus.SENT, 8, 2)


# ---- Recipients and plan versions --------------------------------------------------------------------------


def test_each_operator_chat_gets_its_own_card(tmp_path: Path) -> None:
    c = console(tmp_path)
    drafted(c)
    c.telegram.fail_once["send_message"] = [TelegramError(TelegramCode.NETWORK_ERROR, uncertain=True)]
    c.sync()
    # Alice's ambiguous card never suppresses Bob's: the identity is per recipient.
    assert rows(c.db) == [(ALICE_CHAT, "UNKNOWN", 1), (BOB_CHAT, "SENT", 1)]


def test_a_new_plan_version_gets_a_new_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = console(tmp_path)
    drafted(c)
    console_ = channel(c)
    c.telegram.fail_once["send_message"] = [TelegramError(TelegramCode.NETWORK_ERROR, uncertain=True)]
    c.sync()  # Alice's card for this version is UNKNOWN; Bob's is SENT
    [plan] = console_.operator_plans(10)
    changed = plan.model_copy(update={"fingerprint": "f" * len(plan.fingerprint)})
    monkeypatch.setattr(console_, "operator_plans", lambda limit: [changed])
    c.sync()
    assert (cards(c, ALICE_CHAT), cards(c, BOB_CHAT)) == (1, 2)  # the old identities do not suppress the new version
    statuses = sorted(r[1] for r in rows(c.db))
    assert statuses == ["SENT", "SENT", "SENT", "UNKNOWN"]  # the old rows stay as history
