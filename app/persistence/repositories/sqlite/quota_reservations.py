from datetime import date

from app.persistence.records import QuotaReservation, QuotaReservationState
from app.persistence.repositories.sqlite._rows import (
    ensure_updated,
    load,
    load_all,
    require_next_version,
)
from app.persistence.serialization import model_to_json, to_utc_text
from app.persistence.transaction import SqlValue, Transaction

_LIVE = ("ACTIVE", "CONSUMED")


def _columns(reservation: QuotaReservation) -> tuple[SqlValue, ...]:
    return (
        reservation.outbound_id,
        reservation.kind.value,
        reservation.policy_date.isoformat(),
        reservation.mailbox,
        reservation.campaign_id,
        reservation.contact_id,
        reservation.state.value,
        to_utc_text(reservation.created_at),
        to_utc_text(reservation.updated_at),
        reservation.version,
        model_to_json(reservation),
    )


class SqliteQuotaReservationRepository:
    """Quota slot reservations. Persistence only; limits are evaluated in app.policy."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, reservation: QuotaReservation) -> None:
        self._tx.execute(
            "INSERT INTO quota_reservations (reservation_id, outbound_id, kind, policy_date, "
            "mailbox, campaign_id, contact_id, state, created_at, updated_at, version, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (reservation.reservation_id, *_columns(reservation)),
        )

    def get(self, reservation_id: str) -> QuotaReservation | None:
        row = self._tx.fetch_one(
            "SELECT data FROM quota_reservations WHERE reservation_id = ?", (reservation_id,)
        )
        return load(QuotaReservation, row)

    def get_live_for_outbound(self, outbound_id: str) -> QuotaReservation | None:
        """The ACTIVE or CONSUMED reservation for a message; the schema allows at most one."""
        row = self._tx.fetch_one(
            "SELECT data FROM quota_reservations WHERE outbound_id = ? AND state IN (?, ?)",
            (outbound_id, *_LIVE),
        )
        return load(QuotaReservation, row)

    def list_active_for_date(self, policy_date: date) -> list[QuotaReservation]:
        rows = self._tx.fetch_all(
            "SELECT data FROM quota_reservations WHERE policy_date = ? AND state = ? "
            "ORDER BY created_at, reservation_id",
            (policy_date.isoformat(), QuotaReservationState.ACTIVE.value),
        )
        return load_all(QuotaReservation, rows)

    def list_active_for_contact(self, contact_id: str) -> list[QuotaReservation]:
        rows = self._tx.fetch_all(
            "SELECT data FROM quota_reservations WHERE contact_id = ? AND state = ? "
            "ORDER BY created_at, reservation_id",
            (contact_id, QuotaReservationState.ACTIVE.value),
        )
        return load_all(QuotaReservation, rows)

    def update(self, reservation: QuotaReservation, expected_version: int) -> None:
        require_next_version(reservation.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE quota_reservations SET outbound_id = ?, kind = ?, policy_date = ?, "
            "mailbox = ?, campaign_id = ?, contact_id = ?, state = ?, created_at = ?, "
            "updated_at = ?, version = ?, data = ? WHERE reservation_id = ? AND version = ?",
            (*_columns(reservation), reservation.reservation_id, expected_version),
        )
        ensure_updated(
            cursor, self._tx, "SELECT 1 FROM quota_reservations WHERE reservation_id = ?",
            (reservation.reservation_id,), f"quota reservation {reservation.reservation_id}",
        )
