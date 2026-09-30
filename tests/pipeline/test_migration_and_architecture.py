"""Migration v8 (sales pipeline) and the pipeline package's boundaries."""

import ast
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app.core.enums import OpportunityStatus, QualificationStatus
from app.core.models import Opportunity
from app.persistence import AlreadyExistsError, Database, FrozenClock, IntegrityError
from app.persistence.migrations import MIGRATIONS, apply_migrations, current_version, latest_version
from app.pipeline import PipelineConfig, PipelineService, SalesRecommendation
from app.pipeline.fake import FakeSalesAdvisor
from app.core.enums import LeadStage, NextActionType
from tests.campaign.builders import CAMPAIGN_ID, activate, add_campaign, enrolled, scheduler
from tests.inbound.builders import NOW, envelope, happy_transport, process
from tests.inbound.conftest import seed_knowledge
from tests.pipeline.builders import opportunity_lead, qualifying_lead

APP = Path(__file__).resolve().parents[2] / "app"


def test_fresh_database_reaches_v8(db_path: Path) -> None:
    raw = sqlite3.connect(db_path)
    try:
        assert current_version(raw) == latest_version() == len(MIGRATIONS) >= 8
        names = {r[0] for r in raw.execute("SELECT name FROM sqlite_master")}
    finally:
        raw.close()
    assert {"lead_qualifications", "opportunities", "opportunities_one_active_per_lead", "leads_stage_idx"} <= names


def test_a_v7_database_upgrades_intact_and_nothing_is_backfilled(tmp_path: Path) -> None:
    path = tmp_path / "stage11.sqlite3"
    raw = sqlite3.connect(path, isolation_level=None)
    raw.execute("PRAGMA foreign_keys = ON")
    try:
        assert apply_migrations(raw, FrozenClock(NOW), MIGRATIONS[:7]) == 7
    finally:
        raw.close()
    with Database(path) as db:
        seed_knowledge(db)
        inbound = process(db, happy_transport(), envelope("p-1"))
        add_campaign(db)
        activate(db)
        member_id = enrolled(db)
    with Database(path) as db:
        assert db.initialize_schema(FrozenClock(NOW + timedelta(days=1))) == latest_version() >= 8
        assert db.initialize_schema(FrozenClock(NOW + timedelta(days=2))) == latest_version()  # repeated: idempotent
        with db.transaction() as uow:
            assert uow.qualifications.count_by_status() == {} and uow.opportunities.count_by_status() == {}
            assert uow.campaign_members.get(member_id) is not None
        view = PipelineService(db, FrozenClock(NOW), PipelineConfig()).view(inbound.lead_id or "")
        assert view.qualification_status is QualificationStatus.NOT_STARTED and view.opportunity_id is None
        assert scheduler(db).schedule(CAMPAIGN_ID, correlation_id="c").scheduled  # earlier stages still work


def test_sql_allows_one_active_opportunity_and_valid_statuses_only(db: Database) -> None:
    lead_id = opportunity_lead(db)
    with db.transaction() as uow:
        active = uow.opportunities.get_active_for_lead(lead_id)
    assert active is not None
    with pytest.raises(AlreadyExistsError), db.transaction() as uow:
        uow.opportunities.add(active.model_copy(update={"opportunity_id": "op-second"}))
    closed = Opportunity.model_validate(active.model_dump() | {"opportunity_id": "op-old", "status": OpportunityStatus.CANCELLED,
                                                               "closed_at": NOW})
    with db.transaction() as uow:
        uow.opportunities.add(closed)  # closed history may accumulate
    with pytest.raises(IntegrityError), db.transaction() as uow:
        uow._tx.execute("UPDATE lead_qualifications SET status = 'NOT_STARTED'")  # noqa: SLF001 - proving the SQL guard


def test_the_advisor_only_recommends(db: Database) -> None:
    lead_id = qualifying_lead(db)
    advisor = FakeSalesAdvisor(SalesRecommendation(proposed_stage=LeadStage.OPPORTUNITY,
                                                   proposed_action=NextActionType.PREPARE_PROPOSAL, recommend_opportunity=True))
    service = PipelineService(db, FrozenClock(NOW), PipelineConfig(), advisor=advisor)
    before = snapshot(db)
    result = service.recommend(lead_id)
    assert result is not None and result.requires_operator and not result.allowed_now  # QUALIFYING -> OPPORTUNITY skips
    assert snapshot(db) == before and advisor.inputs[0].lead_id == lead_id


def snapshot(db: Database) -> tuple[object, ...]:
    with db.transaction() as uow:
        rows = uow._tx.fetch_all(  # noqa: SLF001 - a whole-database fingerprint
            "SELECT (SELECT COUNT(*) FROM audit_events), (SELECT group_concat(version) FROM leads), "
            "(SELECT group_concat(version) FROM lead_qualifications), (SELECT COUNT(*) FROM opportunities)")
    return tuple(rows[0])


# ---- Architecture ---------------------------------------------------------------------------------

PIPELINE_DIR = APP / "pipeline"
PIPELINE_MAY_IMPORT = ("app.core", "app.persistence", "app.pipeline", "app.inbound", "app.policy.suppression",
                       "app.campaign.state", "app.conversation.state", "app.conversation.cancellation")
NEVER = ("smtplib", "imaplib", "socket", "http", "urllib", "requests", "httpx", "openai", "anthropic", "google", "telegram",
         "asyncio", "threading", "app.runtime", "app.operator", "app.dispatch", "app.llm")
DOMAIN_BELOW = ("core", "persistence", "policy", "knowledge", "llm", "inbound", "dispatch", "conversation", "campaign")
MUTATING_CALLS = {"update", "add", "reserve", "append"}


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


def test_pipeline_imports_only_contracts_and_the_existing_cancellation_hooks() -> None:
    problems = [f"{p.name}: {n}" for p in sorted(PIPELINE_DIR.glob("*.py")) for n in imports(p)
                if (n.startswith("app") and not matches(n, PIPELINE_MAY_IMPORT)) or matches(n, NEVER)]
    assert problems == []


def test_lower_stages_never_depend_on_the_pipeline() -> None:
    offenders = [f"{p.relative_to(APP)}: {n}" for package in DOMAIN_BELOW for p in (APP / package).rglob("*.py")
                 for n in imports(p) if matches(n, ("app.pipeline",))]
    assert offenders == []


def test_ai_contract_modules_cannot_write() -> None:
    """contracts.py and fake.py never touch a unit of work: proposals only."""
    for name in ("contracts.py", "fake.py"):
        tree = ast.parse((PIPELINE_DIR / name).read_text(encoding="utf-8"))
        assert not {n for n in imports(PIPELINE_DIR / name) if matches(n, ("app.persistence",))}
        calls = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        assert "transaction" not in calls


def test_only_the_policy_writes_a_lead_stage() -> None:
    """Stage 12 code changes a lead's stage only through apply_transition."""
    offenders = []
    for path in sorted(PIPELINE_DIR.glob("*.py")):
        if path.name == "policy.py":
            continue
        source = path.read_text(encoding="utf-8")
        if '"stage":' in source and "uow.leads.update" in source:
            offenders.append(path.name)
    assert offenders == []


def test_no_automation_path_can_close_a_lead_as_won_lost_or_disqualified() -> None:
    commercial = ("CloseReason.WON", "CloseReason.LOST", "CloseReason.DISQUALIFIED")
    offenders = [f"{p.relative_to(APP)}" for package in ("inbound", "campaign", "conversation", "dispatch", "runtime", "llm")
                 for p in (APP / package).rglob("*.py")
                 if any(f"close_reason={c}" in p.read_text(encoding="utf-8") or f'"close_reason": {c}' in p.read_text(encoding="utf-8")
                        for c in commercial)]
    assert offenders == []


def test_the_runtime_has_no_pipeline_loop() -> None:
    for path in (APP / "runtime").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not any(isinstance(node, ast.While) for node in ast.walk(tree)), path.name
