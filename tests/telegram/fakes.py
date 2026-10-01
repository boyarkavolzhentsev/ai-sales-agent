"""A fake Telegram Bot API beneath the adapters (implements ``TelegramApi``): no network.
It models pending updates (with re-delivery until confirmed by offset), sent/edited
messages, callback answers and scripted failures."""

import threading
import time
from dataclasses import dataclass, field

from app.integrations.telegram.client import BotIdentity, SentMessage, Update
from app.integrations.telegram.errors import TelegramCode, TelegramError

ALICE_CHAT, BOB_CHAT, MALLORY_CHAT = 1001, 2002, 6666
BOT = BotIdentity(bot_id=424242, username="sales_agent_test_bot")


@dataclass
class Sent:
    chat_id: int
    text: str
    buttons: tuple[tuple[tuple[str, str], ...], ...]
    message_id: int


@dataclass
class FakeTelegramApi:
    bot: BotIdentity = BOT
    pending: list[Update] = field(default_factory=list)
    redeliver: list[Update] = field(default_factory=list)  # returned once even below the offset
    sent: list[Sent] = field(default_factory=list)
    edited: list[tuple[int, int, str]] = field(default_factory=list)
    answers: list[tuple[str, str]] = field(default_factory=list)
    fail: dict[str, TelegramError] = field(default_factory=dict)  # method -> error on every call
    fail_once: dict[str, list[TelegramError]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    # Concurrency hooks: every get_updates waits for the other passes; sendMessage takes a
    # while, so concurrent passes overlap in the card step.
    updates_barrier: threading.Barrier | None = None
    send_delay: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _next_update: int = 500
    _next_message: int = 1

    # ---- Test helpers ---------------------------------------------------------------------------

    def _check(self, method: str) -> None:
        with self._lock:
            self.calls.append(method)
            if method in self.fail:
                raise self.fail[method]
            queue = self.fail_once.get(method)
            if queue:
                raise queue.pop(0)

    def text(self, chat_id: int, text: str, *, user_id: int | None = None, chat_type: str = "private") -> Update:
        self._next_update += 1
        update = Update(update_id=self._next_update, kind="message", user_id=user_id or chat_id, chat_id=chat_id,
                        chat_type=chat_type, text=text)
        self.pending.append(update)
        return update

    def press(self, chat_id: int, data: str, *, user_id: int | None = None, chat_type: str = "private",
              message_id: int | None = 1) -> Update:
        self._next_update += 1
        update = Update(update_id=self._next_update, kind="callback_query", user_id=user_id or chat_id, chat_id=chat_id,
                        chat_type=chat_type, callback_id=f"cb{self._next_update}", callback_data=data, message_id=message_id)
        self.pending.append(update)
        return update

    def other(self) -> Update:
        self._next_update += 1
        update = Update(update_id=self._next_update, kind="other")
        self.pending.append(update)
        return update

    def buttons_for(self, chat_id: int, label: str) -> list[str]:
        """Callback data of every button with this label sent to this chat (newest last)."""
        return [data for s in self.sent if s.chat_id == chat_id for row in s.buttons for text, data in row if text == label]

    def texts(self, chat_id: int) -> list[str]:
        return [s.text for s in self.sent if s.chat_id == chat_id]

    # ---- TelegramApi -------------------------------------------------------------------------------

    def get_me(self) -> BotIdentity:
        self._check("get_me")
        return self.bot

    def get_updates(self, *, offset: int | None, limit: int, timeout: int) -> tuple[Update, ...]:
        self._check("get_updates")
        if self.updates_barrier is not None:
            self.updates_barrier.wait(timeout=30)
        if offset is not None:  # Telegram forgets everything below the confirmed offset
            self.pending = [u for u in self.pending if u.update_id >= offset]
        stale, self.redeliver = self.redeliver, []  # misbehaving delivery: ignores the offset
        return tuple(sorted(self.pending + stale, key=lambda u: u.update_id)[:limit])

    def send_message(self, chat_id: int, text: str, *, buttons: tuple[tuple[tuple[str, str], ...], ...] = ()) -> SentMessage:
        self._check("send_message")
        assert len(text) <= 4096, "Telegram would refuse this message"
        assert all(len(data.encode()) <= 64 for row in buttons for _, data in row), "callback data over 64 bytes"
        if self.send_delay:
            time.sleep(self.send_delay)
        with self._lock:
            self._next_message += 1
            self.sent.append(Sent(chat_id, text, buttons, self._next_message))
            return SentMessage(message_id=self._next_message)

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        self._check("edit_message_text")
        self.edited.append((chat_id, message_id, text))

    def answer_callback_query(self, callback_id: str, text: str) -> None:
        self._check("answer_callback_query")
        self.answers.append((callback_id, text))


def rate_limited() -> TelegramError:
    return TelegramError(TelegramCode.RATE_LIMITED, status=429, retry_after=3)


def unavailable() -> TelegramError:
    return TelegramError(TelegramCode.TEMPORARY_PROVIDER_ERROR, status=502)
