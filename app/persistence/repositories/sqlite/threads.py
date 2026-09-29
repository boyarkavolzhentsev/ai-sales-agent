from app.core.models import EmailThread
from app.persistence.repositories.sqlite._rows import (
    ensure_updated,
    load,
    load_all,
    require_next_version,
)
from app.persistence.serialization import model_to_json
from app.persistence.transaction import SqlValue, Transaction


def _columns(thread: EmailThread) -> tuple[SqlValue, ...]:
    return (thread.mailbox, thread.lead_id, thread.version, model_to_json(thread))


class SqliteEmailThreadRepository:
    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, thread: EmailThread) -> None:
        self._tx.execute(
            "INSERT INTO email_threads (thread_id, mailbox, lead_id, version, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (thread.thread_id, *_columns(thread)),
        )

    def get(self, thread_id: str) -> EmailThread | None:
        row = self._tx.fetch_one("SELECT data FROM email_threads WHERE thread_id = ?", (thread_id,))
        return load(EmailThread, row)

    def list_by_lead(self, lead_id: str) -> list[EmailThread]:
        rows = self._tx.fetch_all(
            "SELECT data FROM email_threads WHERE lead_id = ? ORDER BY thread_id", (lead_id,)
        )
        return load_all(EmailThread, rows)

    def update(self, thread: EmailThread, expected_version: int) -> None:
        require_next_version(thread.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE email_threads SET mailbox = ?, lead_id = ?, version = ?, data = ? "
            "WHERE thread_id = ? AND version = ?",
            (*_columns(thread), thread.thread_id, expected_version),
        )
        ensure_updated(
            cursor, self._tx, "SELECT 1 FROM email_threads WHERE thread_id = ?",
            (thread.thread_id,), f"email thread {thread.thread_id}",
        )
