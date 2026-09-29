"""v5 dispatch_attempts: round trip and the SQL guards behind the dispatch claim."""

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.dispatch import FakeBehavior, FakeEmailTransport
from app.persistence import AlreadyExistsError, ConcurrencyError, Database, DispatchAttempt, DispatchAttemptState, IntegrityError
from app.persistence.migrations import MIGRATIONS, current_version, latest_version
from tests.dispatch.builders import approved_reply, dispatcher, send, state
from tests.inbound.builders import NOW


def claimed(db: Database) -> DispatchAttempt:
    outbound_id = approved_reply(db)
    send(dispatcher(db, FakeEmailTransport().script(FakeBehavior.TIMEOUT)), outbound_id)
    [attempt] = state(db, outbound_id).attempts
    return attempt


def test_schema_reaches_v5_with_the_dispatch_table(db_path: Path) -> None:
    raw = sqlite3.connect(db_path)
    try:
        assert current_version(raw) == latest_version() == len(MIGRATIONS) == 5
        indexes = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE tbl_name = 'dispatch_attempts' AND type = 'index'")}
    finally:
        raw.close()
    assert {"dispatch_attempts_one_unresolved_per_outbound", "dispatch_attempts_one_accepted_per_outbound"} <= indexes


def test_round_trip_and_versioned_update(db: Database) -> None:
    attempt = claimed(db)
    with db.transaction() as uow:
        assert uow.dispatch_attempts.get(attempt.attempt_id) == attempt
        assert uow.dispatch_attempts.get_by_permit_id(attempt.permit.permit_id) == attempt
        assert uow.dispatch_attempts.list_unresolved() == [attempt]
    with pytest.raises(ConcurrencyError), db.transaction() as uow:
        uow.dispatch_attempts.update(attempt.model_copy(update={"version": attempt.version}), attempt.version - 1)  # stale


def test_sql_allows_only_one_unresolved_attempt_per_message(db: Database) -> None:
    attempt = claimed(db)
    second = attempt.model_copy(update={
        "attempt_id": "da_second", "attempt_no": 2,
        "permit": attempt.permit.model_copy(update={"permit_id": "sp_second"}), "version": 1,
    })
    with pytest.raises((IntegrityError, AlreadyExistsError)), db.transaction() as uow:
        uow.dispatch_attempts.add(second)


def test_attempt_invariants() -> None:
    permit: dict[str, object] = {
        "permit_id": "sp_1", "outbound_id": "ob_1", "content_hash": "a" * 64, "checks_passed": ["X"],
        "policy_config_version": "p", "issued_at": NOW, "expires_at": NOW.replace(hour=13), "consumed_at": NOW,
    }
    base: dict[str, object] = {
        "attempt_id": "da_1", "outbound_id": "ob_1", "attempt_no": 1, "reservation_id": "qr_1",
        "recipient": "a@b.example", "sender_mailbox": "s@ourco.example", "content_hash": "a" * 64,
        "rfc_message_id": "<da_1@ourco.example>", "correlation_id": "c", "claimed_at": NOW, "permit": permit,
    }
    DispatchAttempt.model_validate(base)
    with pytest.raises(ValidationError, match="exactly this message"):
        DispatchAttempt.model_validate(base | {"content_hash": "b" * 64})
    with pytest.raises(ValidationError, match="consumed"):
        DispatchAttempt.model_validate(base | {"permit": permit | {"consumed_at": None}})
    with pytest.raises(ValidationError, match="provider_message_id"):
        DispatchAttempt.model_validate(base | {"state": DispatchAttemptState.ACCEPTED, "resolved_at": NOW})
    with pytest.raises(ValidationError, match="retryable"):
        DispatchAttempt.model_validate(base | {"state": DispatchAttemptState.UNKNOWN, "reason_code": "T", "retryable": True})
