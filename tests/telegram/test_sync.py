"""The bounded operator-sync pass: durable cursor, crash windows, poison updates, Telegram
failures after a committed business command, rate limits and notification idempotency.
Fake Bot API only."""

import io
import json
from pathlib import Path

import pytest

from app.core.enums import OutboundStatus
from app.integrations.telegram.errors import TelegramCode, TelegramError
from app.integrations.telegram.sync import OperatorChannelSync
from app.persistence import NotificationStatus
from app.runtime import cli
from tests.runtime.builders import runtime
from tests.telegram.builders import Console, console, fake_connectors, telegram_values
from tests.telegram.fakes import ALICE_CHAT, BOB_CHAT, BOT, FakeTelegramApi, rate_limited, unavailable
from tests.telegram.test_review import drafted, status, telegram_events


def cursor(c: Console) -> int | None:
    with c.db.transaction() as uow:
        state = uow.operator_channel.get_state("telegram", str(BOT.bot_id))
    return state.cursor if state else None


def failures(c: Console) -> list:  # noqa: ANN201
    with c.db.transaction() as uow:
        return uow.operator_channel.list_failures("telegram", str(BOT.bot_id), 100)


def notifications(c: Console) -> list[tuple[int, str, str, int]]:
    with c.db.transaction() as uow:
        return [tuple(r) for r in uow._tx.fetch_all(  # noqa: SLF001
            "SELECT chat_id, status, json_extract(data, '$.action'), json_extract(data, '$.claim_count') "
            "FROM operator_notifications ORDER BY chat_id")]


def test_an_empty_pass_creates_the_cursor_and_reports_ok(tmp_path: Path) -> None:
    c = console(tmp_path)
    result = c.sync()
    assert (result.status.value, result.updates, result.notifications_sent, result.cursor) == ("OK", 0, 0, None)
    assert cursor(c) is None and c.telegram.calls == ["get_me", "get_updates"]


def test_the_cursor_is_durable_across_restarts(tmp_path: Path) -> None:
    c = console(tmp_path)
    first = c.telegram.text(ALICE_CHAT, "/help")
    c.sync()
    assert cursor(c) == first.update_id + 1
    c.app.stop()
    again = console(tmp_path, telegram=c.telegram)  # same database, new process
    second = c.telegram.text(ALICE_CHAT, "/help")
    result = again.sync()
    assert result.outcomes == {"COMMAND": 1} and cursor(again) == second.update_id + 1
    assert c.telegram.texts(ALICE_CHAT).count(c.telegram.texts(ALICE_CHAT)[0]) == 2  # each /help answered once


def test_a_get_updates_failure_changes_nothing(tmp_path: Path) -> None:
    c = console(tmp_path)
    c.telegram.text(ALICE_CHAT, "/help")
    c.telegram.fail_once["get_updates"] = [unavailable()]
    result = c.sync()
    assert (result.status.value, result.reason, result.cursor) == ("ERROR", "TELEGRAM_TEMPORARY_PROVIDER_ERROR", None)
    assert c.telegram.sent == [] and cursor(c) is None
    assert c.sync().outcomes == {"COMMAND": 1}  # the next pass simply continues


def test_a_poison_update_is_recorded_without_content_and_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = console(tmp_path)
    sync: OperatorChannelSync = c.app.services.operator_channel
    poison = c.telegram.text(ALICE_CHAT, "/queue secret customer words")
    healthy = c.telegram.text(ALICE_CHAT, "/help")
    real = sync.console.handle

    def handle(update):  # noqa: ANN001, ANN202
        if update.update_id == poison.update_id:
            raise RuntimeError("secret customer words")
        return real(update)

    monkeypatch.setattr(sync.console, "handle", handle)
    result = c.sync()
    assert (result.status.value, result.outcomes, result.failed_updates) == ("PARTIAL", {"FAILED": 1, "COMMAND": 1},
                                                                             (poison.update_id,))
    assert cursor(c) == healthy.update_id + 1  # one bad update never blocks the ones after it
    [failure] = failures(c)
    assert (failure.update_id, failure.update_kind, failure.error_code) == (poison.update_id, "message", "RuntimeError")
    assert "secret" not in failure.model_dump_json()
    notice = "This could not be completed. Check /queue for the current state before trying again."
    assert c.telegram.texts(ALICE_CHAT).count(notice) == 1  # the operator learns it, without detail
    assert c.sync().outcomes == {}  # never replayed from stored content


def test_a_crash_after_the_business_commit_never_duplicates_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = console(tmp_path)
    outbound_id = drafted(c)
    c.sync()
    [approve] = c.telegram.buttons_for(ALICE_CHAT, "Approve")
    press = c.telegram.press(ALICE_CHAT, approve)
    sync: OperatorChannelSync = c.app.services.operator_channel

    def crash(*args: object) -> None:
        raise KeyboardInterrupt  # the process dies after the command committed, before the cursor moved

    monkeypatch.setattr(sync, "_advance", crash)
    with pytest.raises(KeyboardInterrupt):
        c.sync()
    monkeypatch.undo()
    assert status(c, outbound_id) is OutboundStatus.OPERATOR_APPROVED and cursor(c) is None
    events = telegram_events(c)
    result = c.sync()  # Telegram delivers the same update again
    assert result.outcomes == {"ALREADY_HANDLED": 1} and cursor(c) == press.update_id + 1
    assert telegram_events(c) == events


@pytest.mark.parametrize("method", ["answer_callback_query", "edit_message_text", "send_message"])
def test_a_telegram_failure_after_the_commit_never_undoes_or_repeats_it(tmp_path: Path, method: str) -> None:
    c = console(tmp_path)
    outbound_id = drafted(c)
    c.sync()
    [approve] = c.telegram.buttons_for(ALICE_CHAT, "Approve")
    c.telegram.press(ALICE_CHAT, approve)
    c.telegram.fail[method] = TelegramError(TelegramCode.CALLBACK_EXPIRED, status=400)
    result = c.sync()
    assert result.outcomes == {"ACTION": 1}
    assert status(c, outbound_id) is OutboundStatus.OPERATOR_APPROVED
    events = telegram_events(c)
    del c.telegram.fail[method]
    assert c.sync().outcomes == {} and telegram_events(c) == events


def test_rate_limits_end_the_card_pass_and_the_next_pass_continues(tmp_path: Path) -> None:
    c = console(tmp_path)
    drafted(c)
    c.telegram.fail_once["send_message"] = [rate_limited()]
    result = c.sync()
    assert (result.status.value, result.reason, result.notifications_sent, result.notifications_failed) == (
        "PARTIAL", "TELEGRAM_RATE_LIMITED", 0, 1)
    assert c.telegram.calls.count("send_message") == 1  # no busy loop, Bob's card waits too
    assert notifications(c) == [(ALICE_CHAT, "FAILED", "REVIEW_CAMPAIGN_DRAFT", 1)]
    result = c.sync()
    assert (result.status.value, result.notifications_sent) == ("OK", 2)
    assert notifications(c) == [(ALICE_CHAT, "SENT", "REVIEW_CAMPAIGN_DRAFT", 2), (BOB_CHAT, "SENT", "REVIEW_CAMPAIGN_DRAFT", 1)]


def test_a_blocked_chat_does_not_stop_the_other_operators(tmp_path: Path) -> None:
    c = console(tmp_path)
    drafted(c)
    c.telegram.fail_once["send_message"] = [TelegramError(TelegramCode.FORBIDDEN, status=403)]
    result = c.sync()
    assert (result.notifications_sent, result.notifications_failed) == (1, 1)
    assert [s.chat_id for s in c.telegram.sent] == [BOB_CHAT]


def test_cards_are_bounded_per_pass_and_never_storm(tmp_path: Path) -> None:
    c = console(tmp_path)
    drafted(c)
    result = c.sync(limit=1)
    assert result.notifications_sent == 1
    assert c.sync(limit=1).notifications_sent == 1 and c.sync(limit=1).notifications_sent == 0
    for _ in range(5):
        c.sync()
    assert len(c.telegram.sent) == 2  # one card per operator, ever, for this plan version


def test_a_new_plan_version_sends_a_new_card(tmp_path: Path) -> None:
    c = console(tmp_path)
    drafted(c)
    c.sync()
    [approve] = c.telegram.buttons_for(ALICE_CHAT, "Approve")
    c.telegram.press(ALICE_CHAT, approve)
    c.sync()
    assert all(not s.text.startswith("Campaign draft") for s in c.telegram.sent[2:])  # approved: nothing more to review
    assert c.world.plan().action.value == "SEND_APPROVED_MESSAGE"  # an automated step: no card


def test_operator_sync_is_skipped_without_telegram_and_reported_by_the_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = runtime(tmp_path / "plain.sqlite3")
    app.start()
    assert app.operator_sync().model_dump(include={"status", "reason"}) == {"status": "SKIPPED",
                                                                            "reason": "OPERATOR_CHANNEL_NOT_CONFIGURED"}
    from tests.runtime.builders import env
    api = FakeTelegramApi()
    api.text(ALICE_CHAT, "/help")
    monkeypatch.setattr(cli, "CONNECTORS", fake_connectors(api))
    environ = env(tmp_path / "cli.sqlite3", **telegram_values())
    assert cli.main(["init"], environ, io.StringIO()) == 0
    out = io.StringIO()
    code = cli.main(["operator-sync"], env(tmp_path / "cli.sqlite3", **telegram_values()), out)
    report = json.loads(out.getvalue())
    assert code == 0 and report["status"] == "OK" and report["outcomes"] == {"COMMAND": 1}
    api.fail["get_updates"] = unavailable()
    out = io.StringIO()
    assert cli.main(["operator-sync"], env(tmp_path / "cli.sqlite3", **telegram_values()), out) == 4
    assert json.loads(out.getvalue())["reason"] == "TELEGRAM_TEMPORARY_PROVIDER_ERROR"


def test_startup_fails_closed_when_the_bot_token_is_refused(tmp_path: Path) -> None:
    from app.runtime import SalesAgentRuntime, StartupError, load_config
    from tests.inbound.builders import NOW
    from tests.runtime.builders import env
    api = FakeTelegramApi(fail={"get_me": TelegramError(TelegramCode.AUTH_INVALID, status=401)})
    app = SalesAgentRuntime(load_config(env(tmp_path / "x.sqlite3", **telegram_values()), now=NOW), connectors=fake_connectors(api))
    with pytest.raises(StartupError) as error:
        app.start()
    assert error.value.code == "OPERATOR_CHANNEL_PROVIDER_UNAVAILABLE" and "AUTH_INVALID" in str(error.value)
    assert not (tmp_path / "x.sqlite3").exists()  # failed before the database was touched


def test_updates_are_handled_in_id_order_whatever_the_delivery_order(tmp_path: Path) -> None:
    c = console(tmp_path)
    first = c.telegram.text(ALICE_CHAT, "/help")
    second = c.telegram.text(ALICE_CHAT, "/queue")
    c.telegram.pending.reverse()
    result = c.sync()
    assert result.outcomes == {"COMMAND": 2} and result.cursor == second.update_id + 1
    assert c.telegram.texts(ALICE_CHAT)[0].startswith("Commands:") and first.update_id < second.update_id


def test_a_pass_reads_at_most_its_limit(tmp_path: Path) -> None:
    c = console(tmp_path)
    updates = [c.telegram.text(ALICE_CHAT, "/help") for _ in range(5)]
    assert c.sync(limit=2).updates == 2 and cursor(c) == updates[1].update_id + 1
    assert c.sync(limit=10).updates == 3 and cursor(c) == updates[-1].update_id + 1
