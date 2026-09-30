"""Migration v9 (commercial decisioning) and the commercial package's boundaries."""

import ast
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app.core.enums import OpportunityStatus, QualificationStatus, RevisionStatus
from app.persistence import AlreadyExistsError, Database, FrozenClock, IntegrityError
from app.persistence.migrations import MIGRATIONS, apply_migrations, current_version, latest_version
from tests.commercial.builders import approve, current, ready_draft
from tests.inbound.builders import NOW
from tests.inbound.conftest import seed_knowledge
from tests.pipeline.builders import active_opportunity, opportunity_lead, qualification

APP = Path(__file__).resolve().parents[2] / "app"


def test_fresh_database_reaches_v9(db_path: Path) -> None:
    raw = sqlite3.connect(db_path)
    try:
        assert current_version(raw) == latest_version() == len(MIGRATIONS) == 9
        names = {r[0] for r in raw.execute("SELECT name FROM sqlite_master")}
        assert MIGRATIONS[-1].name == "commercial_decisioning"
    finally:
        raw.close()
    assert {"proposal_revisions", "commercial_terms", "commercial_term_requests", "objections", "commercial_signals",
            "proposal_revisions_one_open_per_proposal"} <= names


def test_a_v8_database_upgrades_with_its_pipeline_data_intact(tmp_path: Path) -> None:
    path = tmp_path / "stage12.sqlite3"
    raw = sqlite3.connect(path, isolation_level=None)
    raw.execute("PRAGMA foreign_keys = ON")
    try:
        assert apply_migrations(raw, FrozenClock(NOW), MIGRATIONS[:8]) == 8
    finally:
        raw.close()
    with Database(path) as db:
        seed_knowledge(db)
        lead_id = opportunity_lead(db)
        opportunity_id = active_opportunity(db, lead_id).opportunity_id
    with Database(path) as db:
        assert db.initialize_schema(FrozenClock(NOW + timedelta(days=1))) == 9
        assert db.initialize_schema(FrozenClock(NOW + timedelta(days=2))) == 9
        q = qualification(db, lead_id)
        assert q is not None and q.status is QualificationStatus.QUALIFIED
        assert active_opportunity(db, lead_id).status is OpportunityStatus.OPEN
        with db.transaction() as uow:
            assert uow.proposal_revisions.list_for_opportunity(opportunity_id) == []  # nothing backfilled


def test_sql_guards_revision_identity_openness_and_money(db: Database) -> None:
    _, opportunity_id = ready_draft(db)
    draft = current(db, opportunity_id)
    with pytest.raises(AlreadyExistsError), db.transaction() as uow:
        uow.proposal_revisions.add(draft.model_copy(update={"revision_id": "pv-dup"}))  # same (proposal, revision)
    with pytest.raises((AlreadyExistsError, IntegrityError)), db.transaction() as uow:
        uow.proposal_revisions.add(draft.model_copy(update={"revision_id": "pv-2", "revision": 2,
                                                            "predecessor_id": draft.revision_id}))  # a second open one
    approve(db, opportunity_id)
    with pytest.raises(IntegrityError), db.transaction() as uow:
        uow._tx.execute("UPDATE proposal_revisions SET revision = 0")  # noqa: SLF001 - proving the SQL guard
    with pytest.raises(IntegrityError), db.transaction() as uow:
        uow._tx.execute(  # noqa: SLF001
            "UPDATE proposal_revisions SET data = json_set(data, '$.totals.total.amount', '-5')")
    assert current(db, opportunity_id).status is RevisionStatus.APPROVED


# ---- Architecture ------------------------------------------------------------------------------

COMMERCIAL_DIR = APP / "commercial"
COMMERCIAL_MAY_IMPORT = ("app.core", "app.persistence", "app.commercial", "app.inbound", "app.knowledge.metadata",
                         "app.pipeline.guards", "app.pipeline.audit", "app.pipeline.policy")
NEVER = ("smtplib", "imaplib", "socket", "http", "urllib", "requests", "httpx", "openai", "anthropic", "google", "telegram",
         "stripe", "asyncio", "threading", "app.runtime", "app.operator", "app.dispatch", "app.llm", "app.campaign",
         "app.conversation", "app.pipeline.lifecycle")
LOWER = ("core", "persistence", "policy", "knowledge", "llm", "inbound", "dispatch", "conversation", "campaign", "pipeline")


def imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def matches(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == p or name.startswith(f"{p}.") for p in prefixes)


def test_commercial_depends_only_on_contracts_and_never_on_providers() -> None:
    problems = [f"{p.name}: {n}" for p in sorted(COMMERCIAL_DIR.glob("*.py")) for n in imports(p)
                if (n.startswith("app") and not matches(n, COMMERCIAL_MAY_IMPORT)) or matches(n, NEVER)]
    assert problems == []


def test_lower_stages_never_depend_on_commercial() -> None:
    offenders = [f"{p.relative_to(APP)}: {n}" for package in LOWER for p in (APP / package).rglob("*.py")
                 for n in imports(p) if matches(n, ("app.commercial",))]
    assert offenders == []


def test_commercial_never_closes_a_lead_or_decides_for_an_operator() -> None:
    forbidden = {"mark_won", "mark_lost", "disqualify", "apply_transition", "approve_draft", "reserve_quota", "SendPermit"}
    found = []
    for path in sorted(COMMERCIAL_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
            if name in forbidden:
                found.append(f"{path.name}: {name}")
        if "CloseReason" in path.read_text(encoding="utf-8"):
            found.append(f"{path.name}: CloseReason")
    assert found == []


def test_extraction_contracts_cannot_write_and_nothing_loops() -> None:
    for name in ("contracts.py", "fake.py"):
        source = (COMMERCIAL_DIR / name).read_text(encoding="utf-8")
        assert "transaction()" not in source and ".add(" not in source and ".update(" not in source
    for path in COMMERCIAL_DIR.glob("*.py"):
        assert not any(isinstance(n, ast.While) for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))), path.name


def test_no_float_arithmetic_on_money() -> None:
    for name in ("money.py", "pricing.py"):
        source = (COMMERCIAL_DIR / name).read_text(encoding="utf-8")
        assert "float(" not in source


def test_no_new_external_dependencies() -> None:
    lines = [line.strip() for line in (APP.parent / "requirements.txt").read_text(encoding="utf-8").splitlines()]
    assert [line.split(">")[0].split("<")[0].split("=")[0] for line in lines if line and not line.startswith("#")] == [
        "pydantic", "tzdata", "PyYAML"]
