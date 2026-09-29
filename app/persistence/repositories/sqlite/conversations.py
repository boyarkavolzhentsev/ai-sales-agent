from datetime import datetime

from app.core.models import Conversation, FollowUpJob
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


def _conversation_columns(conversation: Conversation) -> tuple[SqlValue, ...]:
    return (
        conversation.thread_id,
        conversation.lead_id,
        conversation.contact_id,
        conversation.status.value,
        _optional_time(conversation.next_follow_up_at),
        to_utc_text(conversation.updated_at),
        conversation.version,
        model_to_json(conversation),
    )


class SqliteConversationRepository:
    """Conversation state. Persistence only; transitions live in app.conversation."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, conversation: Conversation) -> None:
        self._tx.execute(
            "INSERT INTO conversations (conversation_id, thread_id, lead_id, contact_id, status, "
            "next_follow_up_at, updated_at, version, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (conversation.conversation_id, *_conversation_columns(conversation)),
        )

    def get(self, conversation_id: str) -> Conversation | None:
        row = self._tx.fetch_one("SELECT data FROM conversations WHERE conversation_id = ?", (conversation_id,))
        return load(Conversation, row)

    def get_by_thread(self, thread_id: str) -> Conversation | None:
        row = self._tx.fetch_one("SELECT data FROM conversations WHERE thread_id = ?", (thread_id,))
        return load(Conversation, row)

    def list_by_contact(self, contact_id: str) -> list[Conversation]:
        rows = self._tx.fetch_all(
            "SELECT data FROM conversations WHERE contact_id = ? ORDER BY conversation_id", (contact_id,)
        )
        return load_all(Conversation, rows)

    def list_by_lead(self, lead_id: str) -> list[Conversation]:
        rows = self._tx.fetch_all("SELECT data FROM conversations WHERE lead_id = ? ORDER BY conversation_id", (lead_id,))
        return load_all(Conversation, rows)

    def update(self, conversation: Conversation, expected_version: int) -> None:
        require_next_version(conversation.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE conversations SET thread_id = ?, lead_id = ?, contact_id = ?, status = ?, "
            "next_follow_up_at = ?, updated_at = ?, version = ?, data = ? "
            "WHERE conversation_id = ? AND version = ?",
            (*_conversation_columns(conversation), conversation.conversation_id, expected_version),
        )
        ensure_updated(
            cursor, self._tx, "SELECT 1 FROM conversations WHERE conversation_id = ?",
            (conversation.conversation_id,), f"conversation {conversation.conversation_id}",
        )


def _job_columns(job: FollowUpJob) -> tuple[SqlValue, ...]:
    return (
        job.conversation_id,
        job.anchor_outbound_id,
        job.sequence_no,
        job.status.value,
        to_utc_text(job.due_at),
        _optional_time(job.lease_expires_at),
        job.outbound_id,
        to_utc_text(job.created_at),
        job.version,
        model_to_json(job),
    )


class SqliteFollowUpJobRepository:
    """Durable follow-up jobs. Persistence only; scheduling rules live in app.conversation."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, job: FollowUpJob) -> None:
        self._tx.execute(
            "INSERT INTO follow_up_jobs (follow_up_id, conversation_id, anchor_outbound_id, sequence_no, "
            "status, due_at, lease_expires_at, outbound_id, created_at, version, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job.follow_up_id, *_job_columns(job)),
        )

    def get(self, follow_up_id: str) -> FollowUpJob | None:
        row = self._tx.fetch_one("SELECT data FROM follow_up_jobs WHERE follow_up_id = ?", (follow_up_id,))
        return load(FollowUpJob, row)

    def get_open_for_conversation(self, conversation_id: str) -> FollowUpJob | None:
        """The SCHEDULED or CLAIMED job; the schema allows at most one."""
        row = self._tx.fetch_one(
            "SELECT data FROM follow_up_jobs WHERE conversation_id = ? AND status IN ('SCHEDULED', 'CLAIMED')",
            (conversation_id,),
        )
        return load(FollowUpJob, row)

    def list_for_conversation(self, conversation_id: str) -> list[FollowUpJob]:
        rows = self._tx.fetch_all(
            "SELECT data FROM follow_up_jobs WHERE conversation_id = ? ORDER BY created_at, follow_up_id",
            (conversation_id,),
        )
        return load_all(FollowUpJob, rows)

    def list_claimable(self, now: datetime, limit: int) -> list[FollowUpJob]:
        """Due SCHEDULED jobs and CLAIMED jobs whose lease expired (a crashed worker)."""
        at = to_utc_text(now)
        rows = self._tx.fetch_all(
            "SELECT data FROM follow_up_jobs WHERE (status = 'SCHEDULED' AND due_at <= ?) "
            "OR (status = 'CLAIMED' AND lease_expires_at <= ?) ORDER BY due_at, follow_up_id LIMIT ?",
            (at, at, limit),
        )
        return load_all(FollowUpJob, rows)

    def update(self, job: FollowUpJob, expected_version: int) -> None:
        require_next_version(job.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE follow_up_jobs SET conversation_id = ?, anchor_outbound_id = ?, sequence_no = ?, status = ?, "
            "due_at = ?, lease_expires_at = ?, outbound_id = ?, created_at = ?, version = ?, data = ? "
            "WHERE follow_up_id = ? AND version = ?",
            (*_job_columns(job), job.follow_up_id, expected_version),
        )
        ensure_updated(
            cursor, self._tx, "SELECT 1 FROM follow_up_jobs WHERE follow_up_id = ?",
            (job.follow_up_id,), f"follow-up job {job.follow_up_id}",
        )
