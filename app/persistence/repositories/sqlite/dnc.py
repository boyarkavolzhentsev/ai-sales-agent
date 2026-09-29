from datetime import datetime

from app.core.enums import DNCScope
from app.core.models import DoNotContactEntry
from app.core.validation import normalize_domain, normalize_email
from app.persistence.repositories.sqlite._rows import load, load_all
from app.persistence.serialization import model_to_json, optional_utc_text, to_utc_text
from app.persistence.transaction import Transaction


def _normalize(scope: DNCScope, value: str) -> str:
    return normalize_email(value) if scope is DNCScope.EMAIL else normalize_domain(value)


class SqliteDoNotContactRepository:
    """Append-only suppression registry (database triggers reject UPDATE and DELETE).

    Expiry is recorded on the entry itself; lifting an entry early is not supported yet.
    """

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, entry: DoNotContactEntry) -> None:
        self._tx.execute(
            "INSERT INTO do_not_contact (entry_id, scope, value, reason, created_at, expires_at, "
            "data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                entry.entry_id,
                entry.scope.value,
                entry.value,
                entry.reason.value,
                to_utc_text(entry.created_at),
                optional_utc_text(entry.expires_at),
                model_to_json(entry),
            ),
        )

    def get(self, entry_id: str) -> DoNotContactEntry | None:
        row = self._tx.fetch_one("SELECT data FROM do_not_contact WHERE entry_id = ?", (entry_id,))
        return load(DoNotContactEntry, row)

    def list_for_value(self, scope: DNCScope, value: str) -> list[DoNotContactEntry]:
        """Full history for one scope/value, including expired entries."""
        rows = self._tx.fetch_all(
            "SELECT data FROM do_not_contact WHERE scope = ? AND value = ? "
            "ORDER BY created_at, entry_id",
            (scope.value, _normalize(scope, value)),
        )
        return load_all(DoNotContactEntry, rows)

    def list_active(self, scope: DNCScope, value: str, at: datetime) -> list[DoNotContactEntry]:
        """Entries in force at ``at``: created at or before it, and not yet expired."""
        at_text = to_utc_text(at)
        rows = self._tx.fetch_all(
            "SELECT data FROM do_not_contact WHERE scope = ? AND value = ? AND created_at <= ? "
            "AND (expires_at IS NULL OR expires_at > ?) ORDER BY created_at, entry_id",
            (scope.value, _normalize(scope, value), at_text, at_text),
        )
        return load_all(DoNotContactEntry, rows)
