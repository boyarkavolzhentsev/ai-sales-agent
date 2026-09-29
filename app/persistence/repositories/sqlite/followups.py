from datetime import datetime

from app.core.models import FollowUpPlan
from app.persistence.repositories.sqlite._rows import (
    ensure_updated,
    load,
    load_all,
    require_next_version,
)
from app.persistence.serialization import model_to_json, optional_utc_text, to_utc_text
from app.persistence.transaction import SqlValue, Transaction


def _columns(plan: FollowUpPlan) -> tuple[SqlValue, ...]:
    return (
        plan.lead_id,
        plan.campaign_id,
        plan.anchor_outbound_id,
        plan.status.value,
        optional_utc_text(plan.next_due_at),
        plan.version,
        to_utc_text(plan.created_at),
        to_utc_text(plan.updated_at),
        model_to_json(plan),
    )


class SqliteFollowUpPlanRepository:
    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, plan: FollowUpPlan) -> None:
        self._tx.execute(
            "INSERT INTO follow_up_plans (plan_id, lead_id, campaign_id, anchor_outbound_id, "
            "status, next_due_at, version, created_at, updated_at, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (plan.plan_id, *_columns(plan)),
        )

    def get(self, plan_id: str) -> FollowUpPlan | None:
        row = self._tx.fetch_one("SELECT data FROM follow_up_plans WHERE plan_id = ?", (plan_id,))
        return load(FollowUpPlan, row)

    def update(self, plan: FollowUpPlan, expected_version: int) -> None:
        require_next_version(plan.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE follow_up_plans SET lead_id = ?, campaign_id = ?, anchor_outbound_id = ?, "
            "status = ?, next_due_at = ?, version = ?, created_at = ?, updated_at = ?, data = ? "
            "WHERE plan_id = ? AND version = ?",
            (*_columns(plan), plan.plan_id, expected_version),
        )
        ensure_updated(
            cursor, self._tx, "SELECT 1 FROM follow_up_plans WHERE plan_id = ?", (plan.plan_id,),
            f"follow-up plan {plan.plan_id}",
        )

    def get_open_for_lead(self, lead_id: str) -> FollowUpPlan | None:
        """The ACTIVE or PAUSED plan for a lead; the schema allows at most one."""
        row = self._tx.fetch_one(
            "SELECT data FROM follow_up_plans "
            "WHERE lead_id = ? AND status IN ('ACTIVE', 'PAUSED')",
            (lead_id,),
        )
        return load(FollowUpPlan, row)

    def list_active_due(self, due_at_or_before: datetime, limit: int) -> list[FollowUpPlan]:
        """ACTIVE plans whose next_due_at is at or before the given instant, oldest first.

        A query primitive only; eligibility is decided elsewhere.
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        rows = self._tx.fetch_all(
            "SELECT data FROM follow_up_plans WHERE status = 'ACTIVE' AND next_due_at <= ? "
            "ORDER BY next_due_at, plan_id LIMIT ?",
            (to_utc_text(due_at_or_before), limit),
        )
        return load_all(FollowUpPlan, rows)
