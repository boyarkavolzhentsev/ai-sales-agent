from app.persistence.records import UNRESOLVED_ATTEMPT_STATES, DispatchAttempt
from app.persistence.repositories.sqlite._rows import (
    ensure_updated,
    load,
    load_all,
    require_next_version,
)
from app.persistence.serialization import model_to_json, to_utc_text
from app.persistence.transaction import SqlValue, Transaction

_UNRESOLVED = tuple(sorted(state.value for state in UNRESOLVED_ATTEMPT_STATES))


def _columns(attempt: DispatchAttempt) -> tuple[SqlValue, ...]:
    return (
        attempt.outbound_id,
        attempt.attempt_no,
        attempt.permit.permit_id,
        attempt.reservation_id,
        attempt.state.value,
        to_utc_text(attempt.claimed_at),
        attempt.version,
        model_to_json(attempt),
    )


class SqliteDispatchAttemptRepository:
    """Dispatch attempts. Persistence only; dispatch rules live in app.dispatch."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, attempt: DispatchAttempt) -> None:
        self._tx.execute(
            "INSERT INTO dispatch_attempts (attempt_id, outbound_id, attempt_no, permit_id, "
            "reservation_id, state, claimed_at, version, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (attempt.attempt_id, *_columns(attempt)),
        )

    def get(self, attempt_id: str) -> DispatchAttempt | None:
        row = self._tx.fetch_one("SELECT data FROM dispatch_attempts WHERE attempt_id = ?", (attempt_id,))
        return load(DispatchAttempt, row)

    def get_by_permit_id(self, permit_id: str) -> DispatchAttempt | None:
        row = self._tx.fetch_one("SELECT data FROM dispatch_attempts WHERE permit_id = ?", (permit_id,))
        return load(DispatchAttempt, row)

    def list_for_outbound(self, outbound_id: str) -> list[DispatchAttempt]:
        rows = self._tx.fetch_all(
            "SELECT data FROM dispatch_attempts WHERE outbound_id = ? ORDER BY attempt_no", (outbound_id,)
        )
        return load_all(DispatchAttempt, rows)

    def list_unresolved(self) -> list[DispatchAttempt]:
        rows = self._tx.fetch_all(
            "SELECT data FROM dispatch_attempts WHERE state IN (?, ?) ORDER BY claimed_at, attempt_id",
            _UNRESOLVED,
        )
        return load_all(DispatchAttempt, rows)

    def update(self, attempt: DispatchAttempt, expected_version: int) -> None:
        require_next_version(attempt.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE dispatch_attempts SET outbound_id = ?, attempt_no = ?, permit_id = ?, "
            "reservation_id = ?, state = ?, claimed_at = ?, version = ?, data = ? "
            "WHERE attempt_id = ? AND version = ?",
            (*_columns(attempt), attempt.attempt_id, expected_version),
        )
        ensure_updated(
            cursor, self._tx, "SELECT 1 FROM dispatch_attempts WHERE attempt_id = ?",
            (attempt.attempt_id,), f"dispatch attempt {attempt.attempt_id}",
        )
