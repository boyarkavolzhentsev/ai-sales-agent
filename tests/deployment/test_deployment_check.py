"""``deployment-check``: read-only, network-free readiness with stable blockers and exit codes,
validated along the documented first-deployment sequence (docs/deployment.md, section 4)."""

import os
import sqlite3
from pathlib import Path

import pytest

from app.persistence import FrozenClock
from app.persistence.migrations import MIGRATIONS, apply_migrations
from app.runtime import deployment
from tests.deployment.builders import Fakes, fingerprint, production_env, run
from tests.inbound.builders import NOW
from tests.integrations.builders import FAKE_SECRETS
from tests.rag.builders import knowledge_dir
from tests.rag.fakes import EMBEDDINGS_KEY
from tests.runtime.builders import env

OK, INVALID, NOT_READY = 0, 2, 3


def db_path(tmp_path: Path) -> Path:
    return tmp_path / "agent.sqlite3"


def test_the_documented_first_deployment_sequence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fakes, environ = Fakes(), production_env(tmp_path)
    code, status = run(["provider-status"], environ, fakes, monkeypatch)
    assert code == OK and status["production_ready"] is True and fakes.network_calls() == 0

    code, report = run(["deployment-check"], environ, fakes, monkeypatch)
    assert code == NOT_READY and report["config_ready"] is True and report["blockers"] == ["DATABASE_MISSING"]
    assert not db_path(tmp_path).exists()  # never creates the database

    code, _ = run(["init"], environ, fakes, monkeypatch)
    assert code == OK and fakes.brain.session.posts == [] and fakes.vendor.session.posts == []  # nothing billable

    before, calls = fingerprint(db_path(tmp_path)), fakes.network_calls()
    code, report = run(["deployment-check"], environ, fakes, monkeypatch)
    assert code == NOT_READY and report["blockers"] == ["KNOWLEDGE_EMPTY"]  # init does not ingest knowledge
    assert (report["database_ready"], report["schema_version"], report["knowledge_index_ready"]) == (True, 13, False)
    assert fingerprint(db_path(tmp_path)) == before and fakes.network_calls() == calls  # read-only, offline

    code, indexed = run(["knowledge-index"], environ, fakes, monkeypatch)
    assert code == OK and indexed["embeddings"]["embedded"] == 8

    before, calls = fingerprint(db_path(tmp_path)), fakes.network_calls()
    code, report = run(["deployment-check"], environ, fakes, monkeypatch)
    assert code == OK and report["ready"] is True and report["blockers"] == []
    assert report["knowledge_index"] == {"method": "SEMANTIC", "eligible_chunks": 8, "indexed_chunks": 8, "ready": True}
    assert report["operations"]["unresolved_dispatch_attempts"] == 0 and report["operations"]["embedding_claims"] == 0
    assert fingerprint(db_path(tmp_path)) == before and fakes.network_calls() == calls
    assert run(["health"], environ, fakes, monkeypatch)[0] == OK


def test_an_index_made_partly_stale_is_reported_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fakes, environ = Fakes(), production_env(tmp_path)
    run(["init"], environ, fakes, monkeypatch)
    run(["knowledge-index"], environ, fakes, monkeypatch)
    from tests.rag.builders import price_list
    (tmp_path / "kb" / "pricing" / "price_list_v3.yaml").write_text(price_list("89", version=3), encoding="utf-8")
    from app.knowledge import ingest_directory
    from app.persistence import Database
    with Database(db_path(tmp_path)) as db, db.transaction() as uow:
        ingest_directory(uow, tmp_path / "kb", now=NOW)
    code, report = run(["deployment-check"], environ, fakes, monkeypatch)
    assert code == NOT_READY and report["blockers"] == ["KNOWLEDGE_INDEX_INCOMPLETE"]
    assert (report["knowledge_index"]["eligible_chunks"], report["knowledge_index"]["indexed_chunks"]) == (8, 6)


def test_production_without_embeddings_is_a_blocker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environ = production_env(tmp_path, EMBEDDINGS_PROVIDER=None, EMBEDDINGS_MODEL=None, EMBEDDINGS_API_KEY=None)
    code, report = run(["deployment-check"], environ, Fakes(), monkeypatch)
    assert code == NOT_READY and report["config_ready"] is False
    assert "PRODUCTION_NOT_READY:EMBEDDINGS:DISABLED" in report["blockers"]


def test_a_missing_gmail_credential_file_is_invalid_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environ = production_env(tmp_path, GMAIL_CLIENT_ID=None, GMAIL_CLIENT_SECRET=None,
                             GMAIL_CREDENTIALS_FILE=str(tmp_path / "credentials" / "gone.json"))
    code, report = run(["deployment-check"], environ, Fakes(), monkeypatch)
    assert code == INVALID and report["blockers"] == ["CONFIG_INVALID"] and report["mode"] is None
    assert "SALES_AGENT_GMAIL_CREDENTIALS_FILE: CREDENTIAL_FILE_MISSING" in report["problems"]
    assert str(tmp_path) not in str(report)  # names and codes only, never a path


def test_an_unwritable_database_directory_is_a_blocker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environ = production_env(tmp_path)
    real = os.access
    monkeypatch.setattr(deployment.os, "access", lambda p, mode: False if Path(p) == tmp_path else real(p, mode))
    code, report = run(["deployment-check"], environ, Fakes(), monkeypatch)
    assert code == NOT_READY and report["blockers"] == ["DATABASE_DIRECTORY_NOT_WRITABLE"]


@pytest.mark.skipif(os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="POSIX permissions")
def test_a_read_only_directory_is_detected_on_posix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "ro"
    data.mkdir()
    data.chmod(0o500)
    try:
        code, report = run(["deployment-check"], production_env(tmp_path, DATABASE_PATH=str(data / "a.sqlite3")), Fakes(),
                            monkeypatch)
    finally:
        data.chmod(0o700)
    assert code == NOT_READY and "DATABASE_DIRECTORY_NOT_WRITABLE" in report["blockers"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_a_broadly_readable_gmail_token_is_warned_on_posix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environ = production_env(tmp_path)
    token = tmp_path / "tokens" / "gmail-token.json"
    token.write_text("{}", encoding="utf-8")
    token.chmod(0o644)
    _, report = run(["deployment-check"], environ, Fakes(), monkeypatch)
    assert "GMAIL_TOKEN_FILE_PERMISSIONS_BROAD" in report["warnings"]


def test_an_outdated_schema_is_reported_and_never_migrated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = sqlite3.connect(db_path(tmp_path), isolation_level=None)
    try:
        apply_migrations(raw, FrozenClock(NOW), MIGRATIONS[:12])
    finally:
        raw.close()
    before = fingerprint(db_path(tmp_path))
    code, report = run(["deployment-check"], production_env(tmp_path), Fakes(), monkeypatch)
    assert code == NOT_READY and report["blockers"] == ["SCHEMA_NOT_CURRENT"] and report["schema_version"] == 12
    assert fingerprint(db_path(tmp_path)) == before


def test_local_lexical_mode_is_ready_without_embeddings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = Fakes()
    environ = env(db_path(tmp_path), KNOWLEDGE_DIR=str(knowledge_dir(tmp_path)))
    run(["init"], environ, fakes, monkeypatch)
    assert run(["deployment-check"], environ, fakes, monkeypatch)[1]["blockers"] == ["KNOWLEDGE_EMPTY"]
    run(["knowledge-index"], environ, fakes, monkeypatch)
    code, report = run(["deployment-check"], environ, fakes, monkeypatch)
    assert code == OK and report["knowledge_index"] == {"method": "LEXICAL", "eligible_chunks": 8, "indexed_chunks": None,
                                                        "ready": True}
    assert report["production_ready"] is False and report["mode"] == "LOCAL"  # informational outside production


def test_kill_switch_and_operational_states_are_warnings_not_blockers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fakes, environ = Fakes(), production_env(tmp_path, KILL_SWITCH="true", KILL_SWITCH_REASON="smoke test pending")
    run(["init"], environ, fakes, monkeypatch)
    run(["knowledge-index"], environ, fakes, monkeypatch)
    code, report = run(["deployment-check"], environ, fakes, monkeypatch)
    assert code == OK and "KILL_SWITCH_ON" in report["warnings"]


def test_a_corrupt_index_is_a_blocker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fakes, environ = Fakes(), production_env(tmp_path)
    run(["init"], environ, fakes, monkeypatch)
    run(["knowledge-index"], environ, fakes, monkeypatch)
    raw = sqlite3.connect(db_path(tmp_path))
    raw.execute("UPDATE knowledge_embeddings SET dimensions = 2, vector = ? WHERE rowid = 1", (b"\x00\x00\x80\x3f" + b"\x00" * 4,))
    raw.commit()
    raw.close()
    code, report = run(["deployment-check"], environ, fakes, monkeypatch)
    assert code == NOT_READY and "KNOWLEDGE_INDEX_CORRUPT" in report["blockers"]


def test_no_secret_or_path_appears_in_the_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fakes, environ = Fakes(), production_env(tmp_path)
    run(["init"], environ, fakes, monkeypatch)
    _, report = run(["deployment-check"], environ, fakes, monkeypatch)
    text = str(report)
    assert not any(secret in text for secret in (*FAKE_SECRETS, EMBEDDINGS_KEY)) and str(tmp_path) not in text
