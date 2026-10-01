from datetime import datetime

from app.persistence.records import AIEnrichmentJob, EnrichmentJobStatus
from app.persistence.repositories.sqlite._rows import ensure_updated, load, load_all, require_next_version
from app.persistence.serialization import model_to_json, to_utc_text
from app.persistence.transaction import Transaction

CLAIMABLE = (EnrichmentJobStatus.PENDING, EnrichmentJobStatus.RETRY_WAIT, EnrichmentJobStatus.CLAIMED)


class SqliteEnrichmentJobRepository:
    """Durable AI enrichment jobs. Persistence only; the lifecycle lives in app.enrichment."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def get(self, job_id: str) -> AIEnrichmentJob | None:
        return load(AIEnrichmentJob, self._tx.fetch_one("SELECT data FROM ai_enrichment_jobs WHERE job_id = ?", (job_id,)))

    def add(self, job: AIEnrichmentJob) -> None:
        self._tx.execute(
            "INSERT INTO ai_enrichment_jobs (job_id, kind, message_id, lead_id, status, due_at, updated_at, version, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job.job_id, job.kind.value, job.message_id, job.lead_id, job.status.value, to_utc_text(job.due_at),
             to_utc_text(job.updated_at), job.version, model_to_json(job)),
        )

    def update(self, job: AIEnrichmentJob, expected_version: int) -> None:
        require_next_version(job.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE ai_enrichment_jobs SET status = ?, due_at = ?, updated_at = ?, version = ?, data = ? "
            "WHERE job_id = ? AND version = ?",
            (job.status.value, to_utc_text(job.due_at), to_utc_text(job.updated_at), job.version, model_to_json(job),
             job.job_id, expected_version),
        )
        ensure_updated(cursor, self._tx, "SELECT 1 FROM ai_enrichment_jobs WHERE job_id = ?", (job.job_id,),
                       f"AI enrichment job {job.job_id}")

    def list_due(self, now: datetime, limit: int) -> list[AIEnrichmentJob]:
        """PENDING / RETRY_WAIT jobs that are due and CLAIMED jobs whose lease expired."""
        marks = ", ".join("?" for _ in CLAIMABLE)
        rows = self._tx.fetch_all(
            f"SELECT data FROM ai_enrichment_jobs WHERE status IN ({marks}) AND due_at <= ? ORDER BY due_at, job_id LIMIT ?",
            (*(s.value for s in CLAIMABLE), to_utc_text(now), limit),
        )
        return load_all(AIEnrichmentJob, rows)

    def counts(self) -> dict[str, int]:
        rows = self._tx.fetch_all("SELECT status, COUNT(*) FROM ai_enrichment_jobs GROUP BY status")
        return {str(row[0]): int(row[1]) for row in rows}
