"""Commercial (Stage 13) repositories. Persistence only; rules live in app.commercial."""

from collections.abc import Collection

from pydantic import BaseModel

from app.core.enums import ObjectionStatus, RevisionStatus, SignalStatus, TermRequestStatus
from app.core.models import CommercialSignal, CommercialTerm, Objection, ProposalRevision, TermRequest
from app.persistence.repositories.sqlite._rows import ensure_updated, load, load_all, require_next_version
from app.persistence.serialization import model_to_json, to_utc_text
from app.persistence.transaction import SqlValue, Transaction


def _marks(values: Collection[object]) -> str:
    return ", ".join("?" for _ in values)


class SqliteProposalRevisionRepository:
    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, revision: ProposalRevision) -> None:
        self._tx.execute(
            "INSERT INTO proposal_revisions (revision_id, proposal_id, opportunity_id, lead_id, revision, status, "
            "updated_at, version, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (revision.revision_id, revision.proposal_id, revision.opportunity_id, revision.lead_id, revision.revision,
             revision.status.value, to_utc_text(revision.updated_at), revision.version, model_to_json(revision)),
        )

    def get(self, revision_id: str) -> ProposalRevision | None:
        return load(ProposalRevision, self._tx.fetch_one("SELECT data FROM proposal_revisions WHERE revision_id = ?", (revision_id,)))

    def list_for_opportunity(self, opportunity_id: str) -> list[ProposalRevision]:
        rows = self._tx.fetch_all(
            "SELECT data FROM proposal_revisions WHERE opportunity_id = ? ORDER BY revision", (opportunity_id,))
        return load_all(ProposalRevision, rows)

    def latest_for_opportunity(self, opportunity_id: str) -> ProposalRevision | None:
        row = self._tx.fetch_one(
            "SELECT data FROM proposal_revisions WHERE opportunity_id = ? ORDER BY revision DESC LIMIT 1", (opportunity_id,))
        return load(ProposalRevision, row)

    def list_by_status(self, statuses: Collection[RevisionStatus]) -> list[ProposalRevision]:
        rows = self._tx.fetch_all(
            f"SELECT data FROM proposal_revisions WHERE status IN ({_marks(statuses)}) ORDER BY updated_at, revision_id",
            tuple(s.value for s in statuses))
        return load_all(ProposalRevision, rows)

    def count_all(self) -> int:
        row = self._tx.fetch_one("SELECT COUNT(*) AS n FROM proposal_revisions")
        return int(row["n"]) if row else 0

    def update(self, revision: ProposalRevision, expected_version: int) -> None:
        require_next_version(revision.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE proposal_revisions SET status = ?, updated_at = ?, version = ?, data = ? "
            "WHERE revision_id = ? AND version = ?",
            (revision.status.value, to_utc_text(revision.updated_at), revision.version, model_to_json(revision),
             revision.revision_id, expected_version),
        )
        ensure_updated(cursor, self._tx, "SELECT 1 FROM proposal_revisions WHERE revision_id = ?",
                       (revision.revision_id,), f"proposal revision {revision.revision_id}")


class SqliteCommercialTermRepository:
    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, term: CommercialTerm) -> None:
        self._tx.execute(
            "INSERT INTO commercial_terms (term_row_id, opportunity_id, term_type, term_key, updated_at, version, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (term.term_row_id, term.opportunity_id, term.term_type.value, term.term_key, to_utc_text(term.updated_at),
             term.version, model_to_json(term)),
        )

    def get(self, term_row_id: str) -> CommercialTerm | None:
        return load(CommercialTerm, self._tx.fetch_one("SELECT data FROM commercial_terms WHERE term_row_id = ?", (term_row_id,)))

    def list_for_opportunity(self, opportunity_id: str) -> list[CommercialTerm]:
        rows = self._tx.fetch_all(
            "SELECT data FROM commercial_terms WHERE opportunity_id = ? ORDER BY term_type, term_key", (opportunity_id,))
        return load_all(CommercialTerm, rows)

    def update(self, term: CommercialTerm, expected_version: int) -> None:
        require_next_version(term.version, expected_version)
        cursor = self._tx.execute(
            "UPDATE commercial_terms SET updated_at = ?, version = ?, data = ? WHERE term_row_id = ? AND version = ?",
            (to_utc_text(term.updated_at), term.version, model_to_json(term), term.term_row_id, expected_version),
        )
        ensure_updated(cursor, self._tx, "SELECT 1 FROM commercial_terms WHERE term_row_id = ?", (term.term_row_id,),
                       f"commercial term {term.term_row_id}")


class _StatusRepository[M: BaseModel]:
    """Shared plumbing for the status-carrying commercial tables (table/key are literals)."""

    def __init__(self, tx: Transaction, model: type[M], table: str, key: str) -> None:
        self._tx = tx
        self.model = model
        self.table = table
        self.key = key

    def get(self, entity_id: str) -> M | None:
        return load(self.model, self._tx.fetch_one(f"SELECT data FROM {self.table} WHERE {self.key} = ?", (entity_id,)))

    def _list(self, where: str, params: tuple[SqlValue, ...]) -> list[M]:
        return load_all(self.model, self._tx.fetch_all(f"SELECT data FROM {self.table} WHERE {where}", params))

    def _count_by_status(self) -> dict[str, int]:
        rows = self._tx.fetch_all(f"SELECT status, COUNT(*) AS n FROM {self.table} GROUP BY status")
        return {str(row["status"]): int(row["n"]) for row in rows}

    def _update(self, entity_id: str, columns: dict[str, SqlValue], version: int, data: str, expected_version: int) -> None:
        require_next_version(version, expected_version)
        assignments = ", ".join(f"{name} = ?" for name in columns)
        cursor = self._tx.execute(
            f"UPDATE {self.table} SET {assignments}, version = ?, data = ? WHERE {self.key} = ? AND version = ?",
            (*columns.values(), version, data, entity_id, expected_version),
        )
        ensure_updated(cursor, self._tx, f"SELECT 1 FROM {self.table} WHERE {self.key} = ?", (entity_id,),
                       f"{self.table} {entity_id}")


class SqliteTermRequestRepository(_StatusRepository[TermRequest]):
    def __init__(self, tx: Transaction) -> None:
        super().__init__(tx, TermRequest, "commercial_term_requests", "request_id")

    def add(self, request: TermRequest) -> None:
        self._tx.execute(
            "INSERT INTO commercial_term_requests (request_id, opportunity_id, term_type, status, created_at, updated_at, "
            "version, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (request.request_id, request.opportunity_id, request.term_type.value, request.status.value,
             to_utc_text(request.created_at), to_utc_text(request.updated_at), request.version, model_to_json(request)),
        )


    def list_for_opportunity(self, opportunity_id: str) -> list[TermRequest]:
        return self._list("opportunity_id = ? ORDER BY created_at, request_id", (opportunity_id,))

    def count_by_status(self) -> dict[TermRequestStatus, int]:
        return {TermRequestStatus(k): v for k, v in self._count_by_status().items()}

    def update(self, request: TermRequest, expected_version: int) -> None:
        self._update(request.request_id, {"status": request.status.value, "updated_at": to_utc_text(request.updated_at)},
                     request.version, model_to_json(request), expected_version)


class SqliteObjectionRepository(_StatusRepository[Objection]):
    def __init__(self, tx: Transaction) -> None:
        super().__init__(tx, Objection, "objections", "objection_id")

    def add(self, objection: Objection) -> None:
        self._tx.execute(
            "INSERT INTO objections (objection_id, opportunity_id, category, status, created_at, updated_at, version, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (objection.objection_id, objection.opportunity_id, objection.category.value, objection.status.value,
             to_utc_text(objection.created_at), to_utc_text(objection.updated_at), objection.version, model_to_json(objection)),
        )


    def list_for_opportunity(self, opportunity_id: str) -> list[Objection]:
        return self._list("opportunity_id = ? ORDER BY created_at, objection_id", (opportunity_id,))

    def count_by_status(self) -> dict[ObjectionStatus, int]:
        return {ObjectionStatus(k): v for k, v in self._count_by_status().items()}

    def update(self, objection: Objection, expected_version: int) -> None:
        self._update(objection.objection_id, {"status": objection.status.value, "updated_at": to_utc_text(objection.updated_at)},
                     objection.version, model_to_json(objection), expected_version)


class SqliteCommercialSignalRepository(_StatusRepository[CommercialSignal]):
    def __init__(self, tx: Transaction) -> None:
        super().__init__(tx, CommercialSignal, "commercial_signals", "signal_id")

    def add(self, signal: CommercialSignal) -> None:
        self._tx.execute(
            "INSERT INTO commercial_signals (signal_id, opportunity_id, kind, status, message_at, updated_at, version, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (signal.signal_id, signal.opportunity_id, signal.kind.value, signal.status.value, to_utc_text(signal.message_at),
             to_utc_text(signal.updated_at), signal.version, model_to_json(signal)),
        )


    def list_for_opportunity(self, opportunity_id: str) -> list[CommercialSignal]:
        return self._list("opportunity_id = ? ORDER BY message_at, signal_id", (opportunity_id,))

    def count_by_status(self) -> dict[SignalStatus, int]:
        return {SignalStatus(k): v for k, v in self._count_by_status().items()}

    def update(self, signal: CommercialSignal, expected_version: int) -> None:
        self._update(signal.signal_id, {"status": signal.status.value, "updated_at": to_utc_text(signal.updated_at)},
                     signal.version, model_to_json(signal), expected_version)
