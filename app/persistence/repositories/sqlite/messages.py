from app.core.enums import EmailDirection
from app.core.models import EmailMessage
from app.persistence.repositories.sqlite._rows import load, load_all
from app.persistence.serialization import model_to_json, to_utc_text
from app.persistence.transaction import Transaction


class SqliteEmailMessageRepository:
    """Email messages are immutable once stored: add and read only."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, message: EmailMessage) -> None:
        # The contract guarantees received_at for INBOUND and sent_at for OUTBOUND.
        occurred_at = (
            message.received_at if message.direction is EmailDirection.INBOUND else message.sent_at
        )
        if occurred_at is None:
            raise ValueError("email message is missing its direction timestamp")
        self._tx.execute(
            "INSERT INTO email_messages (message_id, rfc_message_id, thread_id, direction, "
            "occurred_at, data) VALUES (?, ?, ?, ?, ?, ?)",
            (
                message.message_id,
                message.rfc_message_id,
                message.thread_id,
                message.direction.value,
                to_utc_text(occurred_at),
                model_to_json(message),
            ),
        )

    def get(self, message_id: str) -> EmailMessage | None:
        row = self._tx.fetch_one(
            "SELECT data FROM email_messages WHERE message_id = ?", (message_id,)
        )
        return load(EmailMessage, row)

    def get_by_rfc_message_id(self, rfc_message_id: str) -> EmailMessage | None:
        row = self._tx.fetch_one(
            "SELECT data FROM email_messages WHERE rfc_message_id = ?", (rfc_message_id,)
        )
        return load(EmailMessage, row)

    def exists_by_rfc_message_id(self, rfc_message_id: str) -> bool:
        row = self._tx.fetch_one(
            "SELECT 1 FROM email_messages WHERE rfc_message_id = ?", (rfc_message_id,)
        )
        return row is not None

    def list_by_thread(self, thread_id: str) -> list[EmailMessage]:
        rows = self._tx.fetch_all(
            "SELECT data FROM email_messages WHERE thread_id = ? ORDER BY occurred_at, message_id",
            (thread_id,),
        )
        return load_all(EmailMessage, rows)
