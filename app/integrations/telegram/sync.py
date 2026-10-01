"""One bounded operator-channel pass (no daemon, no background thread):

1. read at most ``limit`` updates after the cursor (``getUpdates``, short poll);
2. handle each in update-id order through the console; after each one, the cursor moves
   to ``update_id + 1`` in its own transaction (with a failure record if it could not be
   handled). A crash before that replays the update, which the console's deterministic
   command ids make safe. An update that fails is recorded (id, kind, error code: never its
   content) and skipped, so one bad update never blocks the ones after it; an authorized
   operator is told (without detail) and can check /queue and press again;
3. send at most ``limit`` new review cards (each once per chat and plan version, claimed
   durably first, so concurrent passes never both send one card; see ``TelegramConsole.notify``).
A rate limit or a Telegram outage ends the pass (no busy loop); the next pass continues.
"""

from dataclasses import dataclass

from app.inbound.models import stable_id
from app.integrations.channel import OperatorSyncResult, OperatorSyncStatus
from app.integrations.telegram.client import BotIdentity, TelegramApi
from app.integrations.telegram.console import PROVIDER, TelegramConsole
from app.integrations.telegram.errors import TelegramError
from app.persistence import Clock, Database, OperatorChannelFailure, OperatorChannelState


@dataclass
class OperatorChannelSync:
    db: Database
    clock: Clock
    api: TelegramApi
    console: TelegramConsole
    bot: BotIdentity

    @property
    def account(self) -> str:
        return str(self.bot.bot_id)

    def sync_once(self, *, limit: int) -> OperatorSyncResult:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        state = self._state()
        try:
            updates = self.api.get_updates(offset=state.cursor, limit=min(limit, 100), timeout=0)
        except TelegramError as exc:
            return OperatorSyncResult(status=OperatorSyncStatus.ERROR, reason=f"TELEGRAM_{exc.code.value}", cursor=state.cursor)
        outcomes: dict[str, int] = {}
        failed: list[int] = []
        cursor = state.cursor
        seen: set[int] = set()
        for update in sorted(updates, key=lambda u: u.update_id)[:limit]:
            if update.update_id in seen or (cursor is not None and update.update_id < cursor):
                outcomes["DUPLICATE"] = outcomes.get("DUPLICATE", 0) + 1  # already handled: never twice
                continue
            seen.add(update.update_id)
            try:
                outcome = self.console.handle(update)
                error = None
            except Exception as exc:  # noqa: BLE001 - recorded (code only) and skipped, never silent
                outcome, error = "FAILED", type(exc).__name__
                failed.append(update.update_id)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            cursor = update.update_id + 1
            self._advance(cursor, update.update_id, update.kind, error)
            if error is not None:
                self.console.failed(update)  # after the failure record and cursor are durable
        sent, not_sent, stop = self.console.notify(limit)
        partial = bool(failed or not_sent or stop)
        return OperatorSyncResult(status=OperatorSyncStatus.PARTIAL if partial else OperatorSyncStatus.OK,
                                  reason=f"TELEGRAM_{stop}" if stop else None, updates=sum(outcomes.values()),
                                  outcomes=outcomes, failed_updates=tuple(failed), notifications_sent=sent,
                                  notifications_failed=not_sent, cursor=cursor)

    def _state(self) -> OperatorChannelState:
        now = self.clock.now()
        with self.db.transaction() as uow:
            state = uow.operator_channel.get_state(PROVIDER, self.account)
            if state is None:
                state = OperatorChannelState(state_id=stable_id("oc", PROVIDER, self.account), provider=PROVIDER,
                                             account=self.account, created_at=now, updated_at=now)
                uow.operator_channel.add_state(state)
        return state

    def _advance(self, cursor: int, update_id: int, kind: str, error: str | None) -> None:
        now = self.clock.now()
        with self.db.transaction() as uow:
            if error is not None:
                uow.operator_channel.add_failure(OperatorChannelFailure(
                    failure_id=stable_id("of", PROVIDER, self.account, str(update_id)), provider=PROVIDER, account=self.account,
                    update_id=update_id, update_kind=kind, error_code=error, failed_at=now))
            current = uow.operator_channel.get_state(PROVIDER, self.account)
            if current is None or (current.cursor is not None and current.cursor >= cursor):
                return  # another pass already moved past it
            uow.operator_channel.update_state(OperatorChannelState.model_validate(current.model_dump() | {
                "cursor": cursor, "last_synced_at": now, "updated_at": max(now, current.updated_at),
                "version": current.version + 1}), current.version)


__all__ = ["OperatorChannelSync", "OperatorSyncResult", "OperatorSyncStatus"]
