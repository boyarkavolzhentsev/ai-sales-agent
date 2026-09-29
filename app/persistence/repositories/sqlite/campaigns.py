from app.core.enums import CampaignStatus
from app.core.models import Campaign
from app.persistence.repositories.sqlite._rows import (
    ensure_updated,
    load,
    load_all,
    require_next_version,
)
from app.persistence.serialization import model_to_json, to_utc_text
from app.persistence.transaction import SqlValue, Transaction


def _columns(campaign: Campaign) -> tuple[SqlValue, ...]:
    return (
        campaign.status.value,
        campaign.config_version,
        to_utc_text(campaign.created_at),
        to_utc_text(campaign.updated_at),
        campaign.version,
        model_to_json(campaign),
    )


class SqliteCampaignRepository:
    """``version`` is the concurrency token; ``config_version`` is the semantic version of
    the campaign configuration and plays no part in concurrency control."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, campaign: Campaign) -> None:
        self._tx.execute(
            "INSERT INTO campaigns (campaign_id, status, config_version, created_at, updated_at, "
            "version, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (campaign.campaign_id, *_columns(campaign)),
        )

    def get(self, campaign_id: str) -> Campaign | None:
        row = self._tx.fetch_one("SELECT data FROM campaigns WHERE campaign_id = ?", (campaign_id,))
        return load(Campaign, row)

    def update(self, campaign: Campaign, expected_version: int) -> None:
        require_next_version(campaign.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE campaigns SET status = ?, config_version = ?, created_at = ?, updated_at = ?, "
            "version = ?, data = ? WHERE campaign_id = ? AND version = ?",
            (*_columns(campaign), campaign.campaign_id, expected_version),
        )
        ensure_updated(
            cursor, self._tx, "SELECT 1 FROM campaigns WHERE campaign_id = ?",
            (campaign.campaign_id,), f"campaign {campaign.campaign_id}",
        )

    def list_by_status(self, status: CampaignStatus) -> list[Campaign]:
        rows = self._tx.fetch_all(
            "SELECT data FROM campaigns WHERE status = ? ORDER BY created_at, campaign_id",
            (status.value,),
        )
        return load_all(Campaign, rows)
