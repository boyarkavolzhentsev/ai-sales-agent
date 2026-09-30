from app.core.enums import OpportunityStatus, QualificationStatus
from app.core.models import LeadQualification, Opportunity
from app.persistence.repositories.sqlite._rows import ensure_updated, load, load_all, require_next_version
from app.persistence.serialization import model_to_json, to_utc_text
from app.persistence.transaction import Transaction


class SqliteLeadQualificationRepository:
    """Lead qualifications. Persistence only; rules live in app.pipeline."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, qualification: LeadQualification) -> None:
        self._tx.execute(
            "INSERT INTO lead_qualifications (lead_id, status, updated_at, version, data) VALUES (?, ?, ?, ?, ?)",
            (qualification.lead_id, qualification.status.value, to_utc_text(qualification.updated_at),
             qualification.version, model_to_json(qualification)),
        )

    def get(self, lead_id: str) -> LeadQualification | None:
        row = self._tx.fetch_one("SELECT data FROM lead_qualifications WHERE lead_id = ?", (lead_id,))
        return load(LeadQualification, row)

    def update(self, qualification: LeadQualification, expected_version: int) -> None:
        require_next_version(qualification.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE lead_qualifications SET status = ?, updated_at = ?, version = ?, data = ? "
            "WHERE lead_id = ? AND version = ?",
            (qualification.status.value, to_utc_text(qualification.updated_at), qualification.version,
             model_to_json(qualification), qualification.lead_id, expected_version),
        )
        ensure_updated(cursor, self._tx, "SELECT 1 FROM lead_qualifications WHERE lead_id = ?",
                       (qualification.lead_id,), f"qualification of lead {qualification.lead_id}")

    def count_by_status(self) -> dict[QualificationStatus, int]:
        rows = self._tx.fetch_all("SELECT status, COUNT(*) AS n FROM lead_qualifications GROUP BY status")
        return {QualificationStatus(row["status"]): int(row["n"]) for row in rows}


class SqliteOpportunityRepository:
    """Opportunities. Persistence only; rules live in app.pipeline."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, opportunity: Opportunity) -> None:
        self._tx.execute(
            "INSERT INTO opportunities (opportunity_id, lead_id, status, created_at, updated_at, version, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (opportunity.opportunity_id, opportunity.lead_id, opportunity.status.value,
             to_utc_text(opportunity.created_at), to_utc_text(opportunity.updated_at), opportunity.version,
             model_to_json(opportunity)),
        )

    def get(self, opportunity_id: str) -> Opportunity | None:
        row = self._tx.fetch_one("SELECT data FROM opportunities WHERE opportunity_id = ?", (opportunity_id,))
        return load(Opportunity, row)

    def get_active_for_lead(self, lead_id: str) -> Opportunity | None:
        row = self._tx.fetch_one(
            "SELECT data FROM opportunities WHERE lead_id = ? AND status IN ('OPEN', 'NEGOTIATING')", (lead_id,)
        )
        return load(Opportunity, row)

    def list_by_lead(self, lead_id: str) -> list[Opportunity]:
        rows = self._tx.fetch_all(
            "SELECT data FROM opportunities WHERE lead_id = ? ORDER BY created_at, opportunity_id", (lead_id,)
        )
        return load_all(Opportunity, rows)

    def update(self, opportunity: Opportunity, expected_version: int) -> None:
        require_next_version(opportunity.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE opportunities SET lead_id = ?, status = ?, created_at = ?, updated_at = ?, version = ?, data = ? "
            "WHERE opportunity_id = ? AND version = ?",
            (opportunity.lead_id, opportunity.status.value, to_utc_text(opportunity.created_at),
             to_utc_text(opportunity.updated_at), opportunity.version, model_to_json(opportunity),
             opportunity.opportunity_id, expected_version),
        )
        ensure_updated(cursor, self._tx, "SELECT 1 FROM opportunities WHERE opportunity_id = ?",
                       (opportunity.opportunity_id,), f"opportunity {opportunity.opportunity_id}")

    def count_by_status(self) -> dict[OpportunityStatus, int]:
        rows = self._tx.fetch_all("SELECT status, COUNT(*) AS n FROM opportunities GROUP BY status")
        return {OpportunityStatus(row["status"]): int(row["n"]) for row in rows}
