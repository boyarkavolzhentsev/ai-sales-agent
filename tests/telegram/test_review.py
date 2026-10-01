"""Draft review through Telegram: cards from the Stage 14 operator queue, Approve/Reject via
the real Stage 7 commands, replays, stale cards, other operators, and the dispatch step
that (only) Stage 8 performs after approval. Fake Bot API, fake email transport."""

from pathlib import Path

from app.core.enums import OutboundStatus
from app.orchestration import ExecutionAction as A
from app.orchestration import ExecutionOutcome as X
from app.persistence import NotificationStatus
from tests.orchestration.builders import enrolled
from tests.telegram.builders import Console, console
from tests.telegram.fakes import ALICE_CHAT, BOB_CHAT


def drafted(c: Console) -> str:
    """A campaign first touch waiting for review; returns its outbound id."""
    enrolled(c.world)
    assert c.world.execute().subsystem_outcome == "DRAFT_CREATED"
    assert c.world.plan().action is A.REVIEW_CAMPAIGN_DRAFT
    [message] = c.world.messages()
    return message.outbound_id


def status(c: Console, outbound_id: str) -> OutboundStatus:
    with c.db.transaction() as uow:
        return uow.outbound.get(outbound_id).status  # type: ignore[union-attr]


def test_a_pending_draft_becomes_one_card_per_operator_with_only_valid_buttons(tmp_path: Path) -> None:
    c = console(tmp_path)
    outbound_id = drafted(c)
    result = c.sync()
    assert (result.status.value, result.notifications_sent, result.notifications_failed) == ("OK", 2, 0)
    for chat in (ALICE_CHAT, BOB_CHAT):
        [card] = [s for s in c.telegram.sent if s.chat_id == chat]
        assert card.text.startswith("Campaign draft to review") and "Draft first touch" in card.text
        labels = [label for row in card.buttons for label, _ in row]
        assert labels == ["Approve", "Reject", "Lost…", "Do not contact…"]  # no Won before an accepted proposal
        assert all(outbound_id in data for label, data in card.buttons[0])
    assert c.sync().notifications_sent == 0  # the same plan version is never sent twice
    with c.db.transaction() as uow:
        assert all(uow.operator_channel.get_notification(n.notification_id).status is NotificationStatus.SENT  # type: ignore[union-attr]
                   for n in _notifications(uow))


def _notifications(uow):  # noqa: ANN001, ANN202
    rows = uow._tx.fetch_all("SELECT notification_id FROM operator_notifications")  # noqa: SLF001
    assert rows
    return [type("N", (), {"notification_id": r[0]}) for r in rows]


def test_approve_runs_the_stage7_command_and_only_stage8_sends(tmp_path: Path) -> None:
    c = console(tmp_path)
    outbound_id = drafted(c)
    c.sync()
    [approve] = c.telegram.buttons_for(ALICE_CHAT, "Approve")
    press = c.telegram.press(ALICE_CHAT, approve)
    result = c.sync()
    assert result.outcomes == {"ACTION": 1} and result.cursor == press.update_id + 1
    assert status(c, outbound_id) is OutboundStatus.OPERATOR_APPROVED  # a human decision only: Telegram never sends
    assert c.world.transport.calls == []
    assert (press.callback_id, "Approved") in c.telegram.answers
    assert c.telegram.edited and c.telegram.edited[-1][0] == ALICE_CHAT  # the card is closed
    assert c.world.plan().action is A.SEND_APPROVED_MESSAGE
    sent = c.world.execute(dispatch=True)  # the normal dispatch step (Stage 14 -> Stage 8)
    assert sent.outcome is X.EXECUTED and status(c, outbound_id) is OutboundStatus.SENT


def test_a_replayed_or_repeated_press_never_runs_twice(tmp_path: Path) -> None:
    c = console(tmp_path)
    outbound_id = drafted(c)
    c.sync()
    [approve] = c.telegram.buttons_for(ALICE_CHAT, "Approve")
    first = c.telegram.press(ALICE_CHAT, approve)
    c.sync()
    audited = telegram_events(c)
    assert audited >= 1
    # Telegram re-delivers the same update (as after a crash before the cursor moved).
    c.telegram.redeliver.append(first)
    assert c.sync().outcomes == {"DUPLICATE": 1}
    # A crash between the business commit and the cursor: the console sees it again.
    assert c.app.services.operator_channel.console.handle(first) == "ALREADY_HANDLED"
    # The operator presses the old card's button again: a new update, but the version moved on.
    c.telegram.press(ALICE_CHAT, approve)
    assert c.sync().outcomes == {"STALE": 1}
    # Bob presses his copy of the same card: also stale, nothing runs twice.
    [bob_approve] = c.telegram.buttons_for(BOB_CHAT, "Approve")
    c.telegram.press(BOB_CHAT, bob_approve)
    assert c.sync().outcomes == {"STALE": 1}
    assert status(c, outbound_id) is OutboundStatus.OPERATOR_APPROVED
    assert telegram_events(c) == audited  # nothing ran a second time


def telegram_events(c: Console) -> int:
    with c.db.transaction() as uow:
        return uow._tx.fetch_all(  # noqa: SLF001
            "SELECT COUNT(*) FROM audit_events WHERE correlation_id LIKE 'telegram-%'")[0][0]


def test_reject_asks_for_a_reason_then_rejects(tmp_path: Path) -> None:
    c = console(tmp_path)
    outbound_id = drafted(c)
    c.sync()
    [reject] = c.telegram.buttons_for(ALICE_CHAT, "Reject")
    c.telegram.press(ALICE_CHAT, reject)
    c.sync()
    assert status(c, outbound_id) is OutboundStatus.DRAFTED  # choosing a reason is not a decision yet
    assert c.telegram.texts(ALICE_CHAT)[-1] == "Why is the draft rejected?"
    reasons = c.telegram.sent[-1].buttons
    assert len(reasons) >= 2 and all(len(data.encode()) <= 64 for row in reasons for _, data in row)
    c.telegram.press(ALICE_CHAT, reasons[0][0][1])
    assert c.sync().outcomes == {"ACTION": 1}
    assert status(c, outbound_id) is OutboundStatus.CANCELLED


def test_a_changed_draft_makes_the_old_card_stale_and_a_fresh_card_follows(tmp_path: Path) -> None:
    from tests.operator.builders import AS_BOB, reject_command
    c = console(tmp_path)
    outbound_id = drafted(c)
    c.sync()
    [approve] = c.telegram.buttons_for(ALICE_CHAT, "Approve")
    ops = c.world.ops
    ops.reject_draft(AS_BOB, reject_command(ops.get_draft(AS_BOB, outbound_id), "cmd-bob-rejects"))  # elsewhere, meanwhile
    c.telegram.press(ALICE_CHAT, approve)
    result = c.sync()
    assert result.outcomes == {"STALE": 1}
    assert status(c, outbound_id) is OutboundStatus.CANCELLED
    assert "changed since the card was sent" in "\n".join(c.telegram.texts(ALICE_CHAT))


def test_queue_status_help_and_unknown_commands(tmp_path: Path) -> None:
    c = console(tmp_path)
    for text in ("/start", "/help", "/queue", "/status", "/whatever", "hello"):
        c.telegram.text(ALICE_CHAT, text)
    assert c.sync().outcomes == {"COMMAND": 6}
    start, help_, queue, status_text, unknown, hello = c.telegram.texts(ALICE_CHAT)
    assert start.startswith("You are authorized") and "/queue" in help_
    assert queue == "Nothing needs you right now."
    assert "OPERATOR_CHANNEL: TELEGRAM CONFIGURED" in status_text and "operator_channel" in status_text
    assert unknown.startswith("Unknown command") and hello.startswith("Unknown command")
    drafted(c)
    c.telegram.text(ALICE_CHAT, "/queue@sales_agent_test_bot")
    c.sync()
    assert any(t.startswith("1 item(s) need you") and "Campaign draft to review" in t for t in c.telegram.texts(ALICE_CHAT))
