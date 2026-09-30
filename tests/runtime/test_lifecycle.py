"""Construction, startup, health/readiness, shutdown and reentrancy."""

import sqlite3
from pathlib import Path

import pytest

from app.dispatch import FakeEmailTransport
from app.persistence import Database, FrozenClock, PersistenceError
from app.persistence.migrations import MIGRATIONS, apply_migrations, latest_version
from app.runtime import (
    CapabilityUnavailableError,
    RuntimeBusyError,
    RuntimeNotReadyError,
    RuntimeState,
    SalesAgentRuntime,
    StartupError,
    offline_adapters,
)
from app.runtime import workers
from tests.inbound.builders import NOW, envelope
from tests.runtime.builders import fake_adapters, runtime, runtime_config

S = RuntimeState
TABLES = ("outbound_messages", "campaign_jobs", "follow_up_jobs", "dispatch_attempts", "audit_events", "conversations")


def counts(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        return {t: connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    finally:
        connection.close()


def test_construction_performs_no_work(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    transport = FakeEmailTransport()
    app = runtime(path, adapters=fake_adapters(transport))
    assert app.state is S.CREATED and not path.exists() and transport.calls == []
    with pytest.raises(RuntimeNotReadyError):
        app.tick()
    assert not app.health().ready


def test_fresh_database_is_migrated_and_ready(tmp_path: Path) -> None:
    app = runtime(tmp_path / "db.sqlite3")
    report = app.start()
    assert app.state is S.READY and report.schema_version == latest_version()
    health = app.health()
    assert (health.ready, health.database_ok, health.schema_version, health.problems) == (True, True, latest_version(), ())
    assert report.recovery.unresolved_dispatch_attempts == 0
    app.stop()


def test_existing_database_starts_and_repeated_startup_is_idempotent(db_path: Path) -> None:
    before = counts(db_path)
    first = runtime(db_path)
    report = first.start()
    assert first.start() is report  # same runtime: no second startup
    first.stop()
    second = runtime(db_path)
    second.start()
    second.stop()
    assert counts(db_path) == before  # startup creates no business work and no audit noise


def test_an_older_schema_is_upgraded_without_losing_data(tmp_path: Path) -> None:
    path = tmp_path / "v6.sqlite3"
    raw = sqlite3.connect(path, isolation_level=None)
    raw.execute("PRAGMA foreign_keys = ON")
    try:
        apply_migrations(raw, FrozenClock(NOW), MIGRATIONS[:6])
        raw.execute("INSERT INTO idempotency_keys (key, operation, created_at) VALUES ('keep-me', 'test', '2026-06-01T12:00:00.000000Z')")
    finally:
        raw.close()
    app = runtime(path)
    assert app.start().schema_version == latest_version()
    with Database(path) as db, db.transaction() as uow:
        assert uow.idempotency.exists("keep-me")
    app.stop()


def test_failed_startup_is_never_ready(tmp_path: Path) -> None:
    app = runtime(tmp_path)  # a directory, not a database file
    with pytest.raises(StartupError) as error:
        app.start()
    assert error.value.code == "DATABASE_PATH_INVALID" and app.state is S.FAILED
    assert not app.health().ready and not app.health().alive
    with pytest.raises(RuntimeNotReadyError):
        app.campaign_tick()
    with pytest.raises(RuntimeNotReadyError):
        app.start()


def test_startup_sends_nothing(db_path: Path) -> None:
    transport = FakeEmailTransport()
    app = runtime(db_path, adapters=fake_adapters(transport))
    app.start()
    assert transport.calls == []
    app.stop()


def test_shutdown_is_idempotent_and_stops_new_work(db_path: Path) -> None:
    app = runtime(db_path)
    app.start()
    app.stop()
    app.stop()
    assert app.state is S.STOPPED and not app.health().ready and not app.health().database_ok
    for work in (app.tick, app.reconcile, app.campaign_tick, app.follow_up_tick, app.dispatch_tick):
        with pytest.raises(RuntimeNotReadyError):
            work()
    with pytest.raises(RuntimeNotReadyError):
        app.start()


def test_database_failure_is_reflected_in_health(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = runtime(db_path)
    app.start()

    def broken(self: Database) -> int:
        raise PersistenceError("disk gone")

    monkeypatch.setattr(Database, "schema_version", broken)
    health = app.health()
    assert (health.ready, health.database_ok) == (False, False) and health.problems == ("DATABASE_UNAVAILABLE:PersistenceError",)
    monkeypatch.undo()
    assert app.health().ready
    app.stop()


def test_ticks_cannot_reenter(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = runtime(db_path)
    app.start()
    original = workers.campaign_pass

    def nested(*args: object, **kwargs: object) -> object:
        app.follow_up_tick()  # a tick started from inside a running tick
        raise AssertionError("unreachable")

    monkeypatch.setattr(workers, "campaign_pass", nested)
    with pytest.raises(RuntimeBusyError):
        app.tick()
    monkeypatch.setattr(workers, "campaign_pass", original)
    assert app.tick().ok  # the guard was released
    app.stop()


def test_offline_runtime_reports_unavailable_capabilities(db_path: Path) -> None:
    app = SalesAgentRuntime(runtime_config(db_path), adapters=offline_adapters(), clock=FrozenClock(NOW))
    report = app.start()
    assert (report.capabilities.dispatch, report.capabilities.reconciliation, report.capabilities.inbound) == (False, False, False)
    tick = app.tick(dispatch_approved=True)
    assert tick.reconciliation.reason == "RECONCILER_NOT_CONFIGURED" and tick.dispatch is not None
    assert tick.dispatch.reason == "EMAIL_TRANSPORT_NOT_CONFIGURED" and tick.ok
    with pytest.raises(CapabilityUnavailableError):
        app.handle_inbound(envelope("p-1"), correlation_id="c")
    app.stop()


# ---- Adversarial review regressions -------------------------------------------------------


def test_stop_during_a_running_tick_lets_it_finish_and_refuses_new_work(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = runtime(db_path)
    app.start()
    original = workers.campaign_pass
    seen: list[RuntimeState] = []

    def stopping_midway(*args: object, **kwargs: object) -> object:
        app.stop()  # e.g. a shutdown signal arrives while the tick runs
        seen.append(app.state)
        with pytest.raises(RuntimeNotReadyError):
            app.campaign_tick()  # no new work once shutdown began
        return original(*args, **kwargs)  # the running phase still completes on an open connection

    monkeypatch.setattr(workers, "campaign_pass", stopping_midway)
    result = app.tick()
    assert seen == [S.STOPPING] and result.campaign.status.value == "OK"  # the running phase completed
    assert result.follow_up.reason == "SHUTTING_DOWN"  # later phases did not start
    assert app.state is S.STOPPED and not app.health().database_ok


def test_a_database_from_newer_code_is_refused_untouched(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    runtime(path).start()
    raw = sqlite3.connect(path, isolation_level=None)
    try:
        raw.execute("INSERT INTO schema_version (version, name, applied_at) VALUES (99, 'future', '2030-01-01T00:00:00.000000Z')")
    finally:
        raw.close()
    before = path.read_bytes()
    app = runtime(path)
    with pytest.raises(StartupError) as error:
        app.start()
    assert error.value.code == "SCHEMA_NEWER_THAN_SUPPORTED" and app.state is S.FAILED
    assert path.read_bytes() == before
