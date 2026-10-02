from datetime import datetime

from app.persistence.records import EmbeddingKey, KnowledgeEmbeddingRecord
from app.persistence.serialization import to_utc_text
from app.persistence.transaction import Transaction

_KEY = "chunk_id = ? AND provider = ? AND model = ? AND requested_dimensions = ? AND input_hash = ?"
_SPACE = "provider = ? AND model = ? AND requested_dimensions = ?"
Space = tuple[str, str, int]


def _key(key: EmbeddingKey) -> tuple[str, str, str, int, str]:
    return (key.chunk_id, key.provider, key.model, key.requested_dimensions, key.input_hash)


class SqliteKnowledgeEmbeddingRepository:
    """Stored knowledge vectors (derived, rebuildable; never updated in place) and the
    short-lived claims that let concurrent indexers embed each chunk once. Persistence only:
    which chunks may be indexed or searched is decided in app.knowledge."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    # ---- Vectors --------------------------------------------------------------------------

    def add(self, record: KnowledgeEmbeddingRecord) -> bool:
        """Insert unless the same identity is already stored (then: False, unchanged)."""
        cursor = self._tx.execute(
            "INSERT OR IGNORE INTO knowledge_embeddings (chunk_id, provider, model, requested_dimensions, input_hash, "
            "dimensions, vector, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (*_key(record.key), record.dimensions, record.vector, to_utc_text(record.created_at)),
        )
        return cursor.rowcount == 1

    def exists(self, key: EmbeddingKey) -> bool:
        return self._tx.fetch_one(f"SELECT 1 FROM knowledge_embeddings WHERE {_KEY}", _key(key)) is not None

    def list_keys(self) -> list[EmbeddingKey]:
        rows = self._tx.fetch_all(
            "SELECT chunk_id, provider, model, requested_dimensions, input_hash FROM knowledge_embeddings "
            "ORDER BY provider, model, requested_dimensions, chunk_id, input_hash"
        )
        return [EmbeddingKey(chunk_id=r[0], provider=r[1], model=r[2], requested_dimensions=r[3], input_hash=r[4])
                for r in rows]

    def list_space(self, space: Space) -> list[KnowledgeEmbeddingRecord]:
        rows = self._tx.fetch_all(
            "SELECT chunk_id, provider, model, requested_dimensions, input_hash, dimensions, vector, created_at "
            f"FROM knowledge_embeddings WHERE {_SPACE} ORDER BY chunk_id, input_hash",
            space,
        )
        return [
            KnowledgeEmbeddingRecord(
                key=EmbeddingKey(chunk_id=r[0], provider=r[1], model=r[2], requested_dimensions=r[3], input_hash=r[4]),
                dimensions=r[5], vector=bytes(r[6]), created_at=datetime.fromisoformat(r[7]),
            )
            for r in rows
        ]

    def count_space(self, space: Space) -> int:
        row = self._tx.fetch_one(f"SELECT COUNT(*) FROM knowledge_embeddings WHERE {_SPACE}", space)
        return int(row[0]) if row is not None else 0

    def space_dimensions(self, space: Space) -> set[int]:
        rows = self._tx.fetch_all(f"SELECT DISTINCT dimensions FROM knowledge_embeddings WHERE {_SPACE}", space)
        return {int(r[0]) for r in rows}

    def delete(self, key: EmbeddingKey) -> None:
        self._tx.execute(f"DELETE FROM knowledge_embeddings WHERE {_KEY}", _key(key))

    # ---- Claims ---------------------------------------------------------------------------

    def claim(self, key: EmbeddingKey, token: str, lease_until: datetime, now: datetime) -> bool:
        """Take the claim if nobody holds it or the holder's lease has expired."""
        cursor = self._tx.execute(
            f"UPDATE knowledge_embedding_claims SET claim_token = ?, lease_until = ? WHERE {_KEY} AND lease_until <= ?",
            (token, to_utc_text(lease_until), *_key(key), to_utc_text(now)),
        )
        if cursor.rowcount == 1:
            return True
        cursor = self._tx.execute(
            "INSERT OR IGNORE INTO knowledge_embedding_claims (chunk_id, provider, model, requested_dimensions, "
            "input_hash, claim_token, lease_until) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (*_key(key), token, to_utc_text(lease_until)),
        )
        return cursor.rowcount == 1

    def holds(self, key: EmbeddingKey, token: str) -> bool:
        return self._tx.fetch_one(
            f"SELECT 1 FROM knowledge_embedding_claims WHERE {_KEY} AND claim_token = ?", (*_key(key), token)
        ) is not None

    def release(self, token: str) -> int:
        return self._tx.execute("DELETE FROM knowledge_embedding_claims WHERE claim_token = ?", (token,)).rowcount

    def delete_expired_claims(self, now: datetime) -> int:
        return self._tx.execute(
            "DELETE FROM knowledge_embedding_claims WHERE lease_until <= ?", (to_utc_text(now),)
        ).rowcount

    def count_claims(self) -> int:
        row = self._tx.fetch_one("SELECT COUNT(*) FROM knowledge_embedding_claims")
        return int(row[0]) if row is not None else 0
