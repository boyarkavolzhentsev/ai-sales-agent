from app.core.models import ProspectCompany
from app.core.validation import normalize_domain
from app.persistence.repositories.sqlite._rows import ensure_updated, load, require_next_version
from app.persistence.serialization import model_to_json, to_utc_text
from app.persistence.transaction import SqlValue, Transaction


def _columns(company: ProspectCompany) -> tuple[SqlValue, ...]:
    return (
        company.domain,
        to_utc_text(company.created_at),
        to_utc_text(company.updated_at),
        company.version,
        model_to_json(company),
    )


class SqliteProspectCompanyRepository:
    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, company: ProspectCompany) -> None:
        self._tx.execute(
            "INSERT INTO companies (company_id, domain, created_at, updated_at, version, data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (company.company_id, *_columns(company)),
        )

    def get(self, company_id: str) -> ProspectCompany | None:
        row = self._tx.fetch_one("SELECT data FROM companies WHERE company_id = ?", (company_id,))
        return load(ProspectCompany, row)

    def get_by_domain(self, domain: str) -> ProspectCompany | None:
        row = self._tx.fetch_one(
            "SELECT data FROM companies WHERE domain = ?", (normalize_domain(domain),)
        )
        return load(ProspectCompany, row)

    def update(self, company: ProspectCompany, expected_version: int) -> None:
        require_next_version(company.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE companies SET domain = ?, created_at = ?, updated_at = ?, version = ?, data = ? "
            "WHERE company_id = ? AND version = ?",
            (*_columns(company), company.company_id, expected_version),
        )
        ensure_updated(
            cursor, self._tx, "SELECT 1 FROM companies WHERE company_id = ?",
            (company.company_id,), f"company {company.company_id}",
        )
