"""Migration v4: contacts/leads rebuilt with an optional company_id."""

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app.core.enums import ContactDepartment, ContactSource, ContactType, LeadOrigin, LeadStage
from app.core.models import Lead, ProspectCompany, ProspectContact
from app.persistence import Database, FrozenClock, IntegrityError
from app.persistence.migrations import MIGRATIONS, apply_migrations, current_version, latest_version
from app.persistence.serialization import model_to_json, to_utc_text
from tests.inbound.builders import NOW

COMPANY = ProspectCompany(company_id="co-1", name="Prospect Ltd", domain="prospect.example", source=ContactSource.IMPORT, created_at=NOW, updated_at=NOW)
CONTACT = ProspectContact(contact_id="ct-1", company_id="co-1", email="a@prospect.example", department=ContactDepartment.SALES, contact_type=ContactType.NAMED_BUSINESS, source=ContactSource.IMPORT, collected_at=NOW, created_at=NOW, updated_at=NOW)
LEAD = Lead(lead_id="ld-1", contact_id="ct-1", company_id="co-1", origin=LeadOrigin.OUTBOUND, stage=LeadStage.NEW, created_at=NOW, updated_at=NOW)


def v3_database(path: Path) -> None:
    raw = sqlite3.connect(path, isolation_level=None)
    try:
        raw.execute("PRAGMA foreign_keys = ON")
        assert apply_migrations(raw, FrozenClock(NOW), MIGRATIONS[:3]) == 3
        raw.execute("INSERT INTO companies (company_id, domain, created_at, updated_at, version, data) VALUES (?, ?, ?, ?, ?, ?)",
                    (COMPANY.company_id, COMPANY.domain, to_utc_text(NOW), to_utc_text(NOW), 1, model_to_json(COMPANY)))
        raw.execute("INSERT INTO contacts (contact_id, company_id, email, created_at, updated_at, version, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (CONTACT.contact_id, CONTACT.company_id, CONTACT.email, to_utc_text(NOW), to_utc_text(NOW), 1, model_to_json(CONTACT)))
        raw.execute("INSERT INTO leads (lead_id, contact_id, company_id, campaign_id, stage, status, created_at, updated_at, version, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (LEAD.lead_id, LEAD.contact_id, LEAD.company_id, None, LEAD.stage.value, LEAD.status.value, to_utc_text(NOW), to_utc_text(NOW), 1, model_to_json(LEAD)))
        with pytest.raises(sqlite3.IntegrityError):  # company is still mandatory at v3
            raw.execute("INSERT INTO contacts (contact_id, company_id, email, created_at, updated_at, version, data) VALUES ('x', NULL, 'x@y.example', 'a', 'a', 1, '{\"version\": 1}')")
    finally:
        raw.close()


def columns_nullable(path: Path, table: str) -> dict[str, bool]:
    raw = sqlite3.connect(path)
    try:
        return {row[1]: row[3] == 0 for row in raw.execute(f"PRAGMA table_info({table})")}
    finally:
        raw.close()


def test_v3_to_v4_preserves_rows_and_makes_company_optional(tmp_path: Path) -> None:
    path = tmp_path / "v3.sqlite3"
    v3_database(path)
    assert not columns_nullable(path, "contacts")["company_id"]
    with Database(path) as db:
        assert db.initialize_schema(FrozenClock(NOW + timedelta(days=1))) == latest_version() == len(MIGRATIONS)
        with db.transaction() as uow:
            assert uow.companies.get("co-1") == COMPANY
            assert uow.contacts.get("ct-1") == CONTACT
            assert uow.leads.get("ld-1") == LEAD
            unresolved = CONTACT.model_copy(update={"contact_id": "ct-2", "company_id": None, "email": "b@freemail.example"})
            uow.contacts.add(unresolved)
            uow.leads.add(LEAD.model_copy(update={"lead_id": "ld-2", "contact_id": "ct-2", "company_id": None}))
        with db.transaction() as uow:
            assert uow.contacts.get("ct-2") == unresolved
            leads = uow.leads.list_by_contact("ct-2")
            assert len(leads) == 1 and leads[0].company_id is None
        # Foreign keys are enforced again after the migration.
        with pytest.raises(IntegrityError), db.transaction() as uow:
            uow.contacts.add(CONTACT.model_copy(update={"contact_id": "ct-3", "company_id": "missing", "email": "c@x.example"}))
    assert columns_nullable(path, "contacts")["company_id"] and columns_nullable(path, "leads")["company_id"]
    assert not columns_nullable(path, "leads")["contact_id"]


def test_fresh_database_reaches_latest_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite3"
    with Database(path) as db:
        assert db.initialize_schema(FrozenClock(NOW)) == latest_version()
        assert db.initialize_schema(FrozenClock(NOW)) == latest_version()
    raw = sqlite3.connect(path, isolation_level=None)
    try:
        assert current_version(raw) == latest_version()
        assert [r[0] for r in raw.execute("SELECT name FROM schema_version ORDER BY version")] == [m.name for m in MIGRATIONS]
        assert raw.execute("SELECT name FROM sqlite_master WHERE name IN ('contacts_v4', 'leads_v4')").fetchall() == []
        indexes = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"contacts_company_idx", "leads_contact_idx", "leads_campaign_idx"} <= indexes
    finally:
        raw.close()


def test_foreign_key_violations_abort_the_migration(tmp_path: Path) -> None:
    path = tmp_path / "broken.sqlite3"
    v3_database(path)
    raw = sqlite3.connect(path, isolation_level=None)
    try:
        # An orphan written while enforcement was off (as a buggy tool might).
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("INSERT INTO leads (lead_id, contact_id, company_id, campaign_id, stage, status, created_at, updated_at, version, data) VALUES ('orphan', 'no-contact', 'co-1', NULL, 'NEW', 'AUTOMATED', 'a', 'a', 1, '{\"version\": 1}')")
    finally:
        raw.close()
    with Database(path) as db:
        with pytest.raises(IntegrityError, match="foreign key"):
            db.initialize_schema(FrozenClock(NOW))
        assert db.schema_version() == 3  # rolled back completely
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("SELECT name FROM sqlite_master WHERE name = 'contacts_v4'").fetchone() is None
    finally:
        raw.close()
