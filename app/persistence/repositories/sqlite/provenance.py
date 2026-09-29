from app.core.models import EntityRef, ProvenanceRecord
from app.persistence.repositories.sqlite._rows import load_all
from app.persistence.serialization import model_to_json, to_utc_text
from app.persistence.transaction import Transaction


class SqliteProvenanceRepository:
    """Append-only provenance log. No update or delete path; triggers enforce it."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def append(self, record: ProvenanceRecord) -> None:
        self._tx.execute(
            "INSERT INTO provenance_records (artifact_kind, artifact_id, created_at, data) "
            "VALUES (?, ?, ?, ?)",
            (
                record.artifact_ref.kind.value,
                record.artifact_ref.id,
                to_utc_text(record.created_at),
                model_to_json(record),
            ),
        )

    def list_for_artifact(self, artifact: EntityRef) -> list[ProvenanceRecord]:
        rows = self._tx.fetch_all(
            "SELECT data FROM provenance_records WHERE artifact_kind = ? AND artifact_id = ? "
            "ORDER BY created_at, record_id",
            (artifact.kind.value, artifact.id),
        )
        return load_all(ProvenanceRecord, rows)
