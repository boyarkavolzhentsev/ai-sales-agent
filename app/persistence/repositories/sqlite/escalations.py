from app.core.enums import EscalationStatus
from app.core.models import Escalation
from app.persistence.repositories.sqlite._rows import (
    ensure_updated,
    load,
    load_all,
    require_next_version,
)
from app.persistence.serialization import model_to_json, to_utc_text
from app.persistence.transaction import SqlValue, Transaction


def _columns(escalation: Escalation) -> tuple[SqlValue, ...]:
    return (
        escalation.lead_id,
        escalation.status.value,
        to_utc_text(escalation.created_at),
        escalation.version,
        model_to_json(escalation),
    )


class SqliteEscalationRepository:
    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, escalation: Escalation) -> None:
        self._tx.execute(
            "INSERT INTO escalations (escalation_id, lead_id, status, created_at, version, data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (escalation.escalation_id, *_columns(escalation)),
        )

    def get(self, escalation_id: str) -> Escalation | None:
        row = self._tx.fetch_one(
            "SELECT data FROM escalations WHERE escalation_id = ?", (escalation_id,)
        )
        return load(Escalation, row)

    def update(self, escalation: Escalation, expected_version: int) -> None:
        require_next_version(escalation.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE escalations SET lead_id = ?, status = ?, created_at = ?, version = ?, data = ? "
            "WHERE escalation_id = ? AND version = ?",
            (*_columns(escalation), escalation.escalation_id, expected_version),
        )
        ensure_updated(
            cursor, self._tx, "SELECT 1 FROM escalations WHERE escalation_id = ?",
            (escalation.escalation_id,), f"escalation {escalation.escalation_id}",
        )

    def list_by_lead(self, lead_id: str) -> list[Escalation]:
        rows = self._tx.fetch_all(
            "SELECT data FROM escalations WHERE lead_id = ? ORDER BY created_at, escalation_id",
            (lead_id,),
        )
        return load_all(Escalation, rows)

    def list_by_status(self, status: EscalationStatus) -> list[Escalation]:
        rows = self._tx.fetch_all(
            "SELECT data FROM escalations WHERE status = ? ORDER BY created_at, escalation_id",
            (status.value,),
        )
        return load_all(Escalation, rows)
