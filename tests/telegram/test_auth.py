"""Who may act: only a configured Telegram user, in their own private chat, whose mapped
operator id Stage 7 still authorizes. Everyone else gets a generic reply and no data;
groups get nothing at all; forged callback data never acts."""

from pathlib import Path

import pytest

from app.core.enums import OutboundStatus
from app.integrations.telegram.auth import SchemeAuthenticator, TelegramOperatorAuthenticator, credential_for
from app.integrations.telegram.callbacks import Action, encode
from app.operator import OperatorUnauthorizedError
from app.runtime import ConfigError, load_config
from tests.inbound.builders import NOW
from tests.operator.builders import AS_ALICE, FakeAuthenticator, credential
from tests.runtime.builders import env
from tests.telegram.builders import Console, console, telegram_values
from tests.telegram.fakes import ALICE_CHAT, BOB_CHAT, MALLORY_CHAT
from tests.telegram.test_review import drafted, status, telegram_events


def card_ready(tmp_path: Path) -> tuple[Console, str, str]:
    c = console(tmp_path)
    outbound_id = drafted(c)
    c.sync()
    [approve] = c.telegram.buttons_for(ALICE_CHAT, "Approve")
    return c, outbound_id, approve


def test_an_unknown_user_gets_a_generic_reply_and_no_data(tmp_path: Path) -> None:
    c, outbound_id, approve = card_ready(tmp_path)
    before = len(c.telegram.sent)
    c.telegram.text(MALLORY_CHAT, "/queue")
    c.telegram.text(MALLORY_CHAT, "/status")
    c.telegram.press(MALLORY_CHAT, approve)  # a leaked/forwarded card's data, pressed by someone else
    result = c.sync()
    assert result.outcomes == {"UNAUTHORIZED": 3}
    replies = [s for s in c.telegram.sent[before:] if s.chat_id == MALLORY_CHAT]
    assert [r.text for r in replies] == ["Not authorized.", "Not authorized."] and all(r.buttons == () for r in replies)
    assert c.telegram.answers[-1][1] == "Not authorized."
    assert status(c, outbound_id) is OutboundStatus.DRAFTED and telegram_events(c) == 0


def test_a_configured_user_acting_from_another_chat_is_not_an_operator(tmp_path: Path) -> None:
    c, outbound_id, approve = card_ready(tmp_path)
    c.telegram.press(MALLORY_CHAT, approve, user_id=ALICE_CHAT)  # Alice's user id, someone else's private chat
    c.telegram.press(ALICE_CHAT, approve, user_id=MALLORY_CHAT)  # someone else in "Alice's" chat id
    assert c.sync().outcomes == {"UNAUTHORIZED": 2}
    assert status(c, outbound_id) is OutboundStatus.DRAFTED


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel", None])
def test_groups_and_channels_never_receive_business_data(tmp_path: Path, chat_type: str | None) -> None:
    c, outbound_id, approve = card_ready(tmp_path)
    before = len(c.telegram.sent)
    c.telegram.text(ALICE_CHAT, "/queue", chat_type=chat_type)  # type: ignore[arg-type]
    c.telegram.press(ALICE_CHAT, approve, chat_type=chat_type)  # type: ignore[arg-type]
    assert c.sync().outcomes == {"IGNORED_NOT_PRIVATE": 2}
    assert c.telegram.sent[before:] == []  # nothing written into the group
    assert c.telegram.answers[-1][1] == "Not permitted here."
    assert status(c, outbound_id) is OutboundStatus.DRAFTED


def test_unsupported_updates_are_ignored(tmp_path: Path) -> None:
    c = console(tmp_path)
    c.telegram.other()
    assert c.sync().outcomes == {"IGNORED_UNSUPPORTED": 1} and c.telegram.sent == []


@pytest.mark.parametrize("data", [
    "a|ob_doesnotexist|1", "zz|x|1", "a|x", "cf|0123456789abcdef", "w|ld_doesnotexist|1", "a|ob_x|１", None,
])
def test_forged_callback_data_never_acts(tmp_path: Path, data: str | None) -> None:
    c, outbound_id, _ = card_ready(tmp_path)
    c.telegram.press(ALICE_CHAT, data)  # type: ignore[arg-type]
    outcomes = c.sync().outcomes
    assert set(outcomes) <= {"INVALID_CALLBACK", "NOT_FOUND", "ACTION"}
    assert status(c, outbound_id) is OutboundStatus.DRAFTED and telegram_events(c) == 0


def test_a_forged_version_or_a_different_target_is_revalidated(tmp_path: Path) -> None:
    c, outbound_id, approve = card_ready(tmp_path)
    action, target, version = approve.split("|")
    c.telegram.press(ALICE_CHAT, f"{action}|{target}|{int(version) + 1}")  # a "newer" version than exists
    c.telegram.press(ALICE_CHAT, encode(Action.CREATE_OPPORTUNITY, c.world.lead, 999))
    c.telegram.press(ALICE_CHAT, encode(Action.WON, c.world.lead, c.world.lead_row().version))  # Won before any proposal
    result = c.sync()
    assert result.outcomes["STALE"] == 2
    assert status(c, outbound_id) is OutboundStatus.DRAFTED
    # The WON request may only open a confirmation; confirming it is refused by Stage 7.
    [confirm] = c.telegram.buttons_for(ALICE_CHAT, "Confirm")
    c.telegram.press(ALICE_CHAT, confirm)
    assert c.sync().outcomes == {"REJECTED": 1}
    assert c.world.lead_row().status.value != "WON"
    with c.db.transaction() as uow:  # a refused confirmation is never reusable later
        assert uow.operator_channel.get_confirmation(confirm.split("|")[1]).status.value == "CANCELLED"  # type: ignore[union-attr]
    c.telegram.press(ALICE_CHAT, confirm)
    c.sync()
    assert c.telegram.texts(ALICE_CHAT)[-1] == "Already handled."


def test_a_mapping_to_an_operator_stage7_does_not_know_is_refused_at_configuration(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as error:
        load_config(env(tmp_path / "x.sqlite3", **telegram_values(OPERATOR_IDS="op-alice")), now=NOW)  # op-bob is mapped
    assert "SALES_AGENT_TELEGRAM_OPERATOR_CHAT_IDS: INVALID_PROVIDER_CONFIG" in str(error.value)


def test_stage7_authorization_is_consulted_on_every_update(tmp_path: Path) -> None:
    c, outbound_id, approve = card_ready(tmp_path)
    console_ = c.app.services.operator_channel.console

    def revoked(credential: object) -> str:
        raise OperatorUnauthorizedError()

    console_._authorize = revoked  # noqa: SLF001 - Stage 7 no longer authorizes this operator
    c.telegram.press(ALICE_CHAT, approve)
    c.telegram.text(ALICE_CHAT, "/queue")
    assert c.sync().outcomes == {"UNAUTHORIZED": 2}
    assert status(c, outbound_id) is OutboundStatus.DRAFTED


def test_the_scheme_authenticator_keeps_other_credentials_with_their_own_authenticator() -> None:
    telegram = TelegramOperatorAuthenticator({ALICE_CHAT: "op-alice", BOB_CHAT: "op-bob"})
    composite = SchemeAuthenticator(telegram, FakeAuthenticator())
    assert composite.authenticate(credential_for(ALICE_CHAT, ALICE_CHAT)) == "op-alice"
    assert composite.authenticate(AS_ALICE) == "op-alice"  # the injected authenticator still works
    for forged in (credential_for(ALICE_CHAT, BOB_CHAT), credential_for(MALLORY_CHAT, MALLORY_CHAT),
                   credential("1001", scheme="telegram"), credential("1001:", scheme="telegram"),
                   credential("-1001:-1001", scheme="telegram"), credential("tok-alice", scheme="telegram"),
                   credential("1001:1001", scheme="fake")):
        assert composite.authenticate(forged) is None
    assert telegram.operator_for(ALICE_CHAT, ALICE_CHAT, "group") is None


def test_a_missing_or_malformed_token_is_refused_before_anything_starts(tmp_path: Path) -> None:
    for token, code in ((None, "MISSING_SECRET"), ("not-a-bot-token", "INVALID_PROVIDER_CONFIG")):
        with pytest.raises(ConfigError) as error:
            load_config(env(tmp_path / "x.sqlite3", **telegram_values(TELEGRAM_BOT_TOKEN=token)), now=NOW)
        assert f"SALES_AGENT_TELEGRAM_BOT_TOKEN: {code}" in str(error.value)
        assert "not-a-bot-token" not in str(error.value)


def test_provider_status_reports_telegram_without_contacting_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import json
    from app.runtime import cli
    from tests.telegram.builders import fake_connectors
    from tests.telegram.fakes import FakeTelegramApi
    api = FakeTelegramApi()
    monkeypatch.setattr(cli, "CONNECTORS", fake_connectors(api))
    out = io.StringIO()
    assert cli.main(["provider-status"], env(tmp_path / "x.sqlite3", **telegram_values()), out) == 0
    [status_] = [p for p in json.loads(out.getvalue())["integrations"]["providers"] if p["category"] == "OPERATOR_CHANNEL"]
    assert (status_["provider"], status_["implemented"], status_["state"], status_["authorization"]) == (
        "TELEGRAM", True, "CONFIGURED", "TOKEN_PRESENT")
    assert api.calls == []  # no getMe, no network: verification happens when the runtime starts
