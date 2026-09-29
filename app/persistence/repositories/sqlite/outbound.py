from app.core.enums import OutboundStatus
from app.core.models import OutboundMessage
from app.persistence.repositories.sqlite._rows import (
    ensure_updated,
    load,
    load_all,
    require_next_version,
)
from app.persistence.serialization import model_to_json, optional_utc_text, to_utc_text
from app.persistence.transaction import SqlValue, Transaction


def _columns(message: OutboundMessage) -> tuple[SqlValue, ...]:
    return (
        message.idempotency_key,
        message.kind.value,
        message.status.value,
        message.lead_id,
        message.contact_id,
        message.campaign_id,
        message.thread_id,
        to_utc_text(message.created_at),
        optional_utc_text(message.sent_at),
        message.version,
        model_to_json(message),
    )


class SqliteOutboundMessageRepository:
    """The send ledger. ``status`` and ``sent_at`` are authoritative for counts."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, message: OutboundMessage) -> None:
        self._tx.execute(
            "INSERT INTO outbound_messages (outbound_id, idempotency_key, kind, status, lead_id, "
            "contact_id, campaign_id, thread_id, created_at, sent_at, version, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message.outbound_id, *_columns(message)),
        )

    def get(self, outbound_id: str) -> OutboundMessage | None:
        row = self._tx.fetch_one(
            "SELECT data FROM outbound_messages WHERE outbound_id = ?", (outbound_id,)
        )
        return load(OutboundMessage, row)

    def get_by_idempotency_key(self, idempotency_key: str) -> OutboundMessage | None:
        row = self._tx.fetch_one(
            "SELECT data FROM outbound_messages WHERE idempotency_key = ?", (idempotency_key,)
        )
        return load(OutboundMessage, row)

    def update(self, message: OutboundMessage, expected_version: int) -> None:
        require_next_version(message.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE outbound_messages SET idempotency_key = ?, kind = ?, status = ?, lead_id = ?, "
            "contact_id = ?, campaign_id = ?, thread_id = ?, created_at = ?, sent_at = ?, "
            "version = ?, data = ? WHERE outbound_id = ? AND version = ?",
            (*_columns(message), message.outbound_id, expected_version),
        )
        ensure_updated(
            cursor, self._tx, "SELECT 1 FROM outbound_messages WHERE outbound_id = ?",
            (message.outbound_id,), f"outbound message {message.outbound_id}",
        )

    def list_by_lead(self, lead_id: str) -> list[OutboundMessage]:
        rows = self._tx.fetch_all(
            "SELECT data FROM outbound_messages WHERE lead_id = ? ORDER BY created_at, outbound_id",
            (lead_id,),
        )
        return load_all(OutboundMessage, rows)

    def list_by_status(self, status: OutboundStatus) -> list[OutboundMessage]:
        rows = self._tx.fetch_all(
            "SELECT data FROM outbound_messages WHERE status = ? ORDER BY created_at, outbound_id",
            (status.value,),
        )
        return load_all(OutboundMessage, rows)
