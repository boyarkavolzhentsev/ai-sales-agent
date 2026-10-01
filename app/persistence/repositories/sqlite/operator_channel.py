from app.persistence.records import OperatorChannelFailure, OperatorChannelState, OperatorConfirmation, OperatorNotification
from app.persistence.repositories.sqlite._rows import ensure_updated, load, load_all, require_next_version
from app.persistence.serialization import model_to_json, to_utc_text
from app.persistence.transaction import Transaction


class SqliteOperatorChannelRepository:
    """Operator channel cursor, failures, notifications and confirmations. Persistence
    only; the channel rules live in the provider package (app.integrations.telegram)."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    # ---- Cursor ---------------------------------------------------------------------------

    def get_state(self, provider: str, account: str) -> OperatorChannelState | None:
        row = self._tx.fetch_one("SELECT data FROM operator_channel_states WHERE provider = ? AND account = ?",
                                 (provider, account))
        return load(OperatorChannelState, row)

    def add_state(self, state: OperatorChannelState) -> None:
        self._tx.execute(
            "INSERT INTO operator_channel_states (state_id, provider, account, updated_at, version, data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (state.state_id, state.provider, state.account, to_utc_text(state.updated_at), state.version, model_to_json(state)),
        )

    def update_state(self, state: OperatorChannelState, expected_version: int) -> None:
        require_next_version(state.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE operator_channel_states SET updated_at = ?, version = ?, data = ? WHERE state_id = ? AND version = ?",
            (to_utc_text(state.updated_at), state.version, model_to_json(state), state.state_id, expected_version),
        )
        ensure_updated(cursor, self._tx, "SELECT 1 FROM operator_channel_states WHERE state_id = ?", (state.state_id,),
                       f"operator channel state {state.state_id}")

    # ---- Failures (append-only) -----------------------------------------------------------------

    def add_failure(self, failure: OperatorChannelFailure) -> None:
        self._tx.execute(
            "INSERT OR IGNORE INTO operator_channel_failures (failure_id, provider, account, update_id, failed_at, data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (failure.failure_id, failure.provider, failure.account, failure.update_id, to_utc_text(failure.failed_at),
             model_to_json(failure)),
        )

    def list_failures(self, provider: str, account: str, limit: int) -> list[OperatorChannelFailure]:
        rows = self._tx.fetch_all(
            "SELECT data FROM operator_channel_failures WHERE provider = ? AND account = ? ORDER BY update_id LIMIT ?",
            (provider, account, limit),
        )
        return load_all(OperatorChannelFailure, rows)

    # ---- Notifications ----------------------------------------------------------------------------

    def get_notification(self, notification_id: str) -> OperatorNotification | None:
        row = self._tx.fetch_one("SELECT data FROM operator_notifications WHERE notification_id = ?", (notification_id,))
        return load(OperatorNotification, row)

    def add_notification(self, notification: OperatorNotification) -> None:
        self._tx.execute(
            "INSERT INTO operator_notifications (notification_id, provider, chat_id, status, updated_at, version, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (notification.notification_id, notification.provider, notification.chat_id, notification.status.value,
             to_utc_text(notification.updated_at), notification.version, model_to_json(notification)),
        )

    def update_notification(self, notification: OperatorNotification, expected_version: int) -> None:
        require_next_version(notification.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE operator_notifications SET status = ?, updated_at = ?, version = ?, data = ? "
            "WHERE notification_id = ? AND version = ?",
            (notification.status.value, to_utc_text(notification.updated_at), notification.version,
             model_to_json(notification), notification.notification_id, expected_version),
        )
        ensure_updated(cursor, self._tx, "SELECT 1 FROM operator_notifications WHERE notification_id = ?",
                       (notification.notification_id,), f"operator notification {notification.notification_id}")

    # ---- Confirmations ------------------------------------------------------------------------------

    def get_confirmation(self, confirmation_id: str) -> OperatorConfirmation | None:
        row = self._tx.fetch_one("SELECT data FROM operator_confirmations WHERE confirmation_id = ?", (confirmation_id,))
        return load(OperatorConfirmation, row)

    def add_confirmation(self, confirmation: OperatorConfirmation) -> None:
        self._tx.execute(
            "INSERT INTO operator_confirmations (confirmation_id, provider, operator_id, status, expires_at, version, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (confirmation.confirmation_id, confirmation.provider, confirmation.operator_id, confirmation.status.value,
             to_utc_text(confirmation.expires_at), confirmation.version, model_to_json(confirmation)),
        )

    def update_confirmation(self, confirmation: OperatorConfirmation, expected_version: int) -> None:
        require_next_version(confirmation.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE operator_confirmations SET status = ?, version = ?, data = ? WHERE confirmation_id = ? AND version = ?",
            (confirmation.status.value, confirmation.version, model_to_json(confirmation), confirmation.confirmation_id,
             expected_version),
        )
        ensure_updated(cursor, self._tx, "SELECT 1 FROM operator_confirmations WHERE confirmation_id = ?", (confirmation.confirmation_id,),
                       f"operator confirmation {confirmation.confirmation_id}")
