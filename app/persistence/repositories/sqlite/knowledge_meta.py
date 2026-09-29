from app.core.enums import KnowledgeDomain
from app.core.models import KnowledgeSource
from app.persistence.repositories.sqlite._rows import load, load_all
from app.persistence.serialization import model_to_json
from app.persistence.transaction import Transaction


class SqliteKnowledgeSourceMetaRepository:
    """Knowledge source metadata, immutable per (source_id, version). No content,
    chunks or embeddings are stored here."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, source: KnowledgeSource) -> None:
        self._tx.execute(
            "INSERT INTO knowledge_sources_meta (source_id, version, domain, approval_status, "
            "external_use, content_hash, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                source.source_id,
                source.version,
                source.domain.value,
                source.approval_status.value,
                source.external_use.value,
                source.content_hash,
                model_to_json(source),
            ),
        )

    def get(self, source_id: str, version: int) -> KnowledgeSource | None:
        row = self._tx.fetch_one(
            "SELECT data FROM knowledge_sources_meta WHERE source_id = ? AND version = ?",
            (source_id, version),
        )
        return load(KnowledgeSource, row)

    def get_latest(self, source_id: str) -> KnowledgeSource | None:
        row = self._tx.fetch_one(
            "SELECT data FROM knowledge_sources_meta WHERE source_id = ? "
            "ORDER BY version DESC LIMIT 1",
            (source_id,),
        )
        return load(KnowledgeSource, row)

    def list_by_domain(self, domain: KnowledgeDomain) -> list[KnowledgeSource]:
        rows = self._tx.fetch_all(
            "SELECT data FROM knowledge_sources_meta WHERE domain = ? ORDER BY source_id, version",
            (domain.value,),
        )
        return load_all(KnowledgeSource, rows)
