from app.core.models import ProspectContact
from app.core.validation import normalize_email
from app.persistence.repositories.sqlite._rows import (
    ensure_updated,
    load,
    load_all,
    require_next_version,
)
from app.persistence.serialization import model_to_json, to_utc_text
from app.persistence.transaction import SqlValue, Transaction


def _columns(contact: ProspectContact) -> tuple[SqlValue, ...]:
    return (
        contact.company_id,
        contact.email,
        to_utc_text(contact.created_at),
        to_utc_text(contact.updated_at),
        contact.version,
        model_to_json(contact),
    )


class SqliteProspectContactRepository:
    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, contact: ProspectContact) -> None:
        self._tx.execute(
            "INSERT INTO contacts (contact_id, company_id, email, created_at, updated_at, version, "
            "data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (contact.contact_id, *_columns(contact)),
        )

    def get(self, contact_id: str) -> ProspectContact | None:
        row = self._tx.fetch_one("SELECT data FROM contacts WHERE contact_id = ?", (contact_id,))
        return load(ProspectContact, row)

    def get_by_email(self, email: str) -> ProspectContact | None:
        row = self._tx.fetch_one(
            "SELECT data FROM contacts WHERE email = ?", (normalize_email(email),)
        )
        return load(ProspectContact, row)

    def list_by_company(self, company_id: str) -> list[ProspectContact]:
        rows = self._tx.fetch_all(
            "SELECT data FROM contacts WHERE company_id = ? ORDER BY created_at, contact_id",
            (company_id,),
        )
        return load_all(ProspectContact, rows)

    def update(self, contact: ProspectContact, expected_version: int) -> None:
        require_next_version(contact.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE contacts SET company_id = ?, email = ?, created_at = ?, updated_at = ?, "
            "version = ?, data = ? WHERE contact_id = ? AND version = ?",
            (*_columns(contact), contact.contact_id, expected_version),
        )
        ensure_updated(
            cursor, self._tx, "SELECT 1 FROM contacts WHERE contact_id = ?",
            (contact.contact_id,), f"contact {contact.contact_id}",
        )
