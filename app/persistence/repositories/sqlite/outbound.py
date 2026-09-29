import sqlite3
from collections.abc import Collection
from datetime import datetime

from pydantic import ValidationError

from app.core.enums import OutboundStatus
from app.core.models import OutboundMessage
from app.persistence.errors import CorruptRecordError
from app.persistence.records import LedgerEntry
from app.persistence.repositories.sqlite._rows import (
    ensure_updated,
    load,
    load_all,
    require_next_version,
)
from app.persistence.serialization import (
    from_utc_text,
    model_to_json,
    optional_utc_text,
    to_utc_text,
)
from app.persistence.transaction import SqlValue, Transaction

# Mailbox resolution for ledger entries: campaign messages use the campaign's sending
# mailbox; replies (no campaign) use their thread's mailbox.
_LEDGER_SELECT = (
    "SELECT o.outbound_id, o.kind, o.status, o.contact_id, o.campaign_id, o.sending_at, "
    "COALESCE(json_extract(c.data, '$.sending_mailbox'), t.mailbox) AS mailbox "
    "FROM outbound_messages AS o "
    "LEFT JOIN campaigns AS c ON c.campaign_id = o.campaign_id "
    "LEFT JOIN email_threads AS t ON t.thread_id = o.thread_id "
)


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
        optional_utc_text(message.sending_at),
        optional_utc_text(message.sent_at),
        message.version,
        model_to_json(message),
    )


def _status_placeholders(statuses: Collection[OutboundStatus]) -> tuple[str, tuple[str, ...]]:
    # Only the number of "?" markers is built dynamically; values stay parameterized.
    if not statuses:
        raise ValueError("at least one status is required")
    values = tuple(sorted(status.value for status in statuses))
    return ", ".join("?" for _ in values), values


def _ledger_entry(row: sqlite3.Row) -> LedgerEntry:
    if row["mailbox"] is None or row["sending_at"] is None:
        raise CorruptRecordError(f"ledger entry {row['outbound_id']} lacks mailbox or sending_at")
    try:
        return LedgerEntry(
            outbound_id=row["outbound_id"],
            kind=row["kind"],
            status=row["status"],
            contact_id=row["contact_id"],
            campaign_id=row["campaign_id"],
            mailbox=row["mailbox"],
            sending_at=from_utc_text(row["sending_at"]),
        )
    except ValidationError as exc:
        raise CorruptRecordError(f"ledger entry {row['outbound_id']} failed validation") from exc


class SqliteOutboundMessageRepository:
    """The send ledger. ``status`` and ``sending_at``/``sent_at`` are authoritative for counts."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, message: OutboundMessage) -> None:
        self._tx.execute(
            "INSERT INTO outbound_messages (outbound_id, idempotency_key, kind, status, lead_id, "
            "contact_id, campaign_id, thread_id, created_at, sending_at, sent_at, version, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            "contact_id = ?, campaign_id = ?, thread_id = ?, created_at = ?, sending_at = ?, "
            "sent_at = ?, version = ?, data = ? WHERE outbound_id = ? AND version = ?",
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

    def list_ledger_entries(
        self,
        statuses: Collection[OutboundStatus],
        sending_from: datetime,
        sending_before: datetime,
    ) -> list[LedgerEntry]:
        """Messages in ``statuses`` whose sending_at is in [sending_from, sending_before)."""
        marks, values = _status_placeholders(statuses)
        rows = self._tx.fetch_all(
            _LEDGER_SELECT + f"WHERE o.status IN ({marks}) "
            "AND o.sending_at >= ? AND o.sending_at < ? ORDER BY o.sending_at, o.outbound_id",
            (*values, to_utc_text(sending_from), to_utc_text(sending_before)),
        )
        return [_ledger_entry(row) for row in rows]

    def list_ledger_entries_for_contact(
        self, contact_id: str, statuses: Collection[OutboundStatus]
    ) -> list[LedgerEntry]:
        """All messages in ``statuses`` ever dispatched to one contact."""
        marks, values = _status_placeholders(statuses)
        rows = self._tx.fetch_all(
            _LEDGER_SELECT + f"WHERE o.contact_id = ? AND o.status IN ({marks}) "
            "ORDER BY o.sending_at, o.outbound_id",
            (contact_id, *values),
        )
        return [_ledger_entry(row) for row in rows]
