from app.core.models import AuditEvent, EntityRef
from app.persistence.repositories.sqlite._rows import load, load_all
from app.persistence.serialization import model_to_json, to_utc_text
from app.persistence.transaction import Transaction


class SqliteAuditRepository:
    """Append-only audit log. There is deliberately no update or delete method, and
    database triggers reject UPDATE/DELETE on the underlying tables."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def append(self, event: AuditEvent) -> None:
        self._tx.execute(
            "INSERT INTO audit_events (event_id, occurred_at, event_type, correlation_id, "
            "actor_type, actor_id, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                to_utc_text(event.occurred_at),
                event.event_type,
                event.correlation_id,
                event.actor.type.value,
                event.actor.id,
                model_to_json(event),
            ),
        )
        for subject in event.subject_refs:
            self._tx.execute(
                "INSERT INTO audit_event_subjects (event_id, subject_kind, subject_id) "
                "VALUES (?, ?, ?)",
                (event.event_id, subject.kind.value, subject.id),
            )

    def get(self, event_id: str) -> AuditEvent | None:
        row = self._tx.fetch_one("SELECT data FROM audit_events WHERE event_id = ?", (event_id,))
        return load(AuditEvent, row)

    def list_for_subject(self, subject: EntityRef) -> list[AuditEvent]:
        rows = self._tx.fetch_all(
            "SELECT e.data FROM audit_events AS e "
            "JOIN audit_event_subjects AS s ON s.event_id = e.event_id "
            "WHERE s.subject_kind = ? AND s.subject_id = ? "
            "ORDER BY e.occurred_at, e.event_id",
            (subject.kind.value, subject.id),
        )
        return load_all(AuditEvent, rows)

    def list_by_correlation_id(self, correlation_id: str) -> list[AuditEvent]:
        rows = self._tx.fetch_all(
            "SELECT data FROM audit_events WHERE correlation_id = ? ORDER BY occurred_at, event_id",
            (correlation_id,),
        )
        return load_all(AuditEvent, rows)
