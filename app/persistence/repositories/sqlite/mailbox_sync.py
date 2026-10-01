from app.persistence.records import MailboxSyncFailure, MailboxSyncFailureStatus, MailboxSyncState
from app.persistence.repositories.sqlite._rows import ensure_updated, load, load_all, require_next_version
from app.persistence.serialization import model_to_json, to_utc_text
from app.persistence.transaction import Transaction


class SqliteMailboxSyncRepository:
    """Inbound mailbox cursors and per-message failures. Persistence only; the sync rules
    live in app.integrations.mailbox."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    # ---- Cursor state -----------------------------------------------------------------------

    def get_state(self, provider: str, mailbox: str) -> MailboxSyncState | None:
        row = self._tx.fetch_one("SELECT data FROM mailbox_sync_states WHERE provider = ? AND mailbox = ?", (provider, mailbox))
        return load(MailboxSyncState, row)

    def add_state(self, state: MailboxSyncState) -> None:
        self._tx.execute(
            "INSERT INTO mailbox_sync_states (state_id, provider, mailbox, status, updated_at, version, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (state.state_id, state.provider, state.mailbox, state.status.value, to_utc_text(state.updated_at), state.version,
             model_to_json(state)),
        )

    def update_state(self, state: MailboxSyncState, expected_version: int) -> None:
        require_next_version(state.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE mailbox_sync_states SET status = ?, updated_at = ?, version = ?, data = ? "
            "WHERE state_id = ? AND version = ?",
            (state.status.value, to_utc_text(state.updated_at), state.version, model_to_json(state), state.state_id,
             expected_version),
        )
        ensure_updated(cursor, self._tx, "SELECT 1 FROM mailbox_sync_states WHERE state_id = ?", (state.state_id,),
                       f"mailbox sync state {state.state_id}")

    # ---- Failures ------------------------------------------------------------------------------

    def get_failure(self, failure_id: str) -> MailboxSyncFailure | None:
        row = self._tx.fetch_one("SELECT data FROM mailbox_sync_failures WHERE failure_id = ?", (failure_id,))
        return load(MailboxSyncFailure, row)

    def add_failure(self, failure: MailboxSyncFailure) -> None:
        self._tx.execute(
            "INSERT INTO mailbox_sync_failures (failure_id, provider, mailbox, provider_message_id, status, first_failed_at, "
            "version, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (failure.failure_id, failure.provider, failure.mailbox, failure.provider_message_id, failure.status.value,
             to_utc_text(failure.first_failed_at), failure.version, model_to_json(failure)),
        )

    def update_failure(self, failure: MailboxSyncFailure, expected_version: int) -> None:
        require_next_version(failure.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE mailbox_sync_failures SET status = ?, version = ?, data = ? WHERE failure_id = ? AND version = ?",
            (failure.status.value, failure.version, model_to_json(failure), failure.failure_id, expected_version),
        )
        ensure_updated(cursor, self._tx, "SELECT 1 FROM mailbox_sync_failures WHERE failure_id = ?", (failure.failure_id,),
                       f"mailbox sync failure {failure.failure_id}")

    def list_open_failures(self, provider: str, mailbox: str, limit: int) -> list[MailboxSyncFailure]:
        """Oldest first, so a failing message never starves the ones that failed after it."""
        rows = self._tx.fetch_all(
            "SELECT data FROM mailbox_sync_failures WHERE provider = ? AND mailbox = ? AND status = ? "
            "ORDER BY first_failed_at, failure_id LIMIT ?",
            (provider, mailbox, MailboxSyncFailureStatus.OPEN.value, limit),
        )
        return load_all(MailboxSyncFailure, rows)
