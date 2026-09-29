from app.core.models import Lead
from app.persistence.repositories.sqlite._rows import (
    ensure_updated,
    load,
    load_all,
    require_next_version,
)
from app.persistence.serialization import model_to_json, to_utc_text
from app.persistence.transaction import Transaction


class SqliteLeadRepository:
    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, lead: Lead) -> None:
        self._tx.execute(
            "INSERT INTO leads (lead_id, contact_id, company_id, campaign_id, stage, status, "
            "version, created_at, updated_at, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lead.lead_id,
                lead.contact_id,
                lead.company_id,
                lead.campaign_id,
                lead.stage.value,
                lead.status.value,
                lead.version,
                to_utc_text(lead.created_at),
                to_utc_text(lead.updated_at),
                model_to_json(lead),
            ),
        )

    def get(self, lead_id: str) -> Lead | None:
        row = self._tx.fetch_one("SELECT data FROM leads WHERE lead_id = ?", (lead_id,))
        return load(Lead, row)

    def update(self, lead: Lead, expected_version: int) -> None:
        require_next_version(lead.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE leads SET contact_id = ?, company_id = ?, campaign_id = ?, stage = ?, "
            "status = ?, version = ?, created_at = ?, updated_at = ?, data = ? "
            "WHERE lead_id = ? AND version = ?",
            (
                lead.contact_id,
                lead.company_id,
                lead.campaign_id,
                lead.stage.value,
                lead.status.value,
                lead.version,
                to_utc_text(lead.created_at),
                to_utc_text(lead.updated_at),
                model_to_json(lead),
                lead.lead_id,
                expected_version,
            ),
        )
        ensure_updated(
            cursor, self._tx, "SELECT 1 FROM leads WHERE lead_id = ?", (lead.lead_id,),
            f"lead {lead.lead_id}",
        )

    def list_by_contact(self, contact_id: str) -> list[Lead]:
        rows = self._tx.fetch_all(
            "SELECT data FROM leads WHERE contact_id = ? ORDER BY created_at, lead_id",
            (contact_id,),
        )
        return load_all(Lead, rows)

    def list_by_campaign(self, campaign_id: str) -> list[Lead]:
        rows = self._tx.fetch_all(
            "SELECT data FROM leads WHERE campaign_id = ? ORDER BY created_at, lead_id",
            (campaign_id,),
        )
        return load_all(Lead, rows)
