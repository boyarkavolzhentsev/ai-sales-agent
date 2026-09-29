from datetime import datetime

from app.core.models import CampaignJob, CampaignMember
from app.persistence.repositories.sqlite._rows import (
    ensure_updated,
    load,
    load_all,
    require_next_version,
)
from app.persistence.serialization import model_to_json, to_utc_text
from app.persistence.transaction import SqlValue, Transaction


def _optional_time(value: datetime | None) -> str | None:
    return to_utc_text(value) if value is not None else None


def _member_columns(member: CampaignMember) -> tuple[SqlValue, ...]:
    return (
        member.campaign_id,
        member.contact_id,
        member.lead_id,
        member.status.value,
        _optional_time(member.next_action_at),
        to_utc_text(member.updated_at),
        member.version,
        model_to_json(member),
    )


class SqliteCampaignMemberRepository:
    """Campaign memberships. Persistence only; rules live in app.campaign."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, member: CampaignMember) -> None:
        self._tx.execute(
            "INSERT INTO campaign_members (member_id, campaign_id, contact_id, lead_id, status, next_action_at, "
            "updated_at, version, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (member.member_id, *_member_columns(member)),
        )

    def get(self, member_id: str) -> CampaignMember | None:
        return load(CampaignMember, self._tx.fetch_one("SELECT data FROM campaign_members WHERE member_id = ?", (member_id,)))

    def get_for(self, campaign_id: str, contact_id: str) -> CampaignMember | None:
        row = self._tx.fetch_one(
            "SELECT data FROM campaign_members WHERE campaign_id = ? AND contact_id = ?", (campaign_id, contact_id)
        )
        return load(CampaignMember, row)

    def list_by_campaign(self, campaign_id: str) -> list[CampaignMember]:
        rows = self._tx.fetch_all("SELECT data FROM campaign_members WHERE campaign_id = ? ORDER BY member_id", (campaign_id,))
        return load_all(CampaignMember, rows)

    def list_by_contact(self, contact_id: str) -> list[CampaignMember]:
        rows = self._tx.fetch_all("SELECT data FROM campaign_members WHERE contact_id = ? ORDER BY member_id", (contact_id,))
        return load_all(CampaignMember, rows)

    def get_by_lead(self, lead_id: str) -> CampaignMember | None:
        return load(CampaignMember, self._tx.fetch_one("SELECT data FROM campaign_members WHERE lead_id = ?", (lead_id,)))

    def update(self, member: CampaignMember, expected_version: int) -> None:
        require_next_version(member.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE campaign_members SET campaign_id = ?, contact_id = ?, lead_id = ?, status = ?, next_action_at = ?, "
            "updated_at = ?, version = ?, data = ? WHERE member_id = ? AND version = ?",
            (*_member_columns(member), member.member_id, expected_version),
        )
        ensure_updated(
            cursor, self._tx, "SELECT 1 FROM campaign_members WHERE member_id = ?", (member.member_id,),
            f"campaign member {member.member_id}",
        )


def _job_columns(job: CampaignJob) -> tuple[SqlValue, ...]:
    return (
        job.member_id,
        job.campaign_id,
        job.touch_no,
        job.status.value,
        to_utc_text(job.due_at),
        _optional_time(job.lease_expires_at),
        job.outbound_id,
        to_utc_text(job.created_at),
        job.version,
        model_to_json(job),
    )


class SqliteCampaignJobRepository:
    """Durable campaign touch jobs. Persistence only."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, job: CampaignJob) -> None:
        self._tx.execute(
            "INSERT INTO campaign_jobs (job_id, member_id, campaign_id, touch_no, status, due_at, lease_expires_at, "
            "outbound_id, created_at, version, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job.job_id, *_job_columns(job)),
        )

    def get(self, job_id: str) -> CampaignJob | None:
        return load(CampaignJob, self._tx.fetch_one("SELECT data FROM campaign_jobs WHERE job_id = ?", (job_id,)))

    def get_open_for_member(self, member_id: str) -> CampaignJob | None:
        row = self._tx.fetch_one(
            "SELECT data FROM campaign_jobs WHERE member_id = ? AND status IN ('SCHEDULED', 'CLAIMED')", (member_id,)
        )
        return load(CampaignJob, row)

    def list_for_member(self, member_id: str) -> list[CampaignJob]:
        rows = self._tx.fetch_all("SELECT data FROM campaign_jobs WHERE member_id = ? ORDER BY touch_no", (member_id,))
        return load_all(CampaignJob, rows)

    def list_open_for_campaign(self, campaign_id: str) -> list[CampaignJob]:
        rows = self._tx.fetch_all(
            "SELECT data FROM campaign_jobs WHERE campaign_id = ? AND status IN ('SCHEDULED', 'CLAIMED') ORDER BY job_id",
            (campaign_id,),
        )
        return load_all(CampaignJob, rows)

    def list_claimable(self, now: datetime, limit: int) -> list[CampaignJob]:
        at = to_utc_text(now)
        rows = self._tx.fetch_all(
            "SELECT data FROM campaign_jobs WHERE (status = 'SCHEDULED' AND due_at <= ?) "
            "OR (status = 'CLAIMED' AND lease_expires_at <= ?) ORDER BY due_at, job_id LIMIT ?",
            (at, at, limit),
        )
        return load_all(CampaignJob, rows)

    def update(self, job: CampaignJob, expected_version: int) -> None:
        require_next_version(job.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE campaign_jobs SET member_id = ?, campaign_id = ?, touch_no = ?, status = ?, due_at = ?, "
            "lease_expires_at = ?, outbound_id = ?, created_at = ?, version = ?, data = ? WHERE job_id = ? AND version = ?",
            (*_job_columns(job), job.job_id, expected_version),
        )
        ensure_updated(
            cursor, self._tx, "SELECT 1 FROM campaign_jobs WHERE job_id = ?", (job.job_id,), f"campaign job {job.job_id}",
        )
