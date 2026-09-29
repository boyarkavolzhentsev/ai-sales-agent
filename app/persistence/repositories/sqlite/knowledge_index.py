from collections.abc import Collection

from app.core.models import KnowledgeChunk
from app.persistence.records import KnowledgeFactRecord
from app.persistence.repositories.sqlite._rows import load_all
from app.persistence.serialization import model_to_json
from app.persistence.transaction import SqlValue, Transaction

SourceKey = tuple[str, int]


def _source_filter(sources: Collection[SourceKey]) -> tuple[str, tuple[SqlValue, ...]]:
    # Only the number of "(?, ?)" markers is built dynamically; values stay parameterized.
    ordered = sorted(sources)
    marks = ", ".join("(?, ?)" for _ in ordered)
    values: tuple[SqlValue, ...] = tuple(value for key in ordered for value in key)
    return f"(c.source_id, c.source_version) IN (VALUES {marks})", values


class SqliteKnowledgeIndexRepository:
    """Knowledge chunks, structured facts and the derived FTS5 search index.

    Chunks and facts are immutable (database triggers reject UPDATE/DELETE). The FTS
    table is derived from knowledge_chunks and can be rebuilt with ``rebuild_search_index``.
    """

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add_chunk(self, chunk: KnowledgeChunk) -> None:
        self._tx.execute(
            "INSERT INTO knowledge_chunks (chunk_id, source_id, source_version, ordinal, "
            "content_hash, data) VALUES (?, ?, ?, ?, ?, ?)",
            (
                chunk.chunk_id,
                chunk.source_id,
                chunk.source_version,
                chunk.ordinal,
                chunk.content_hash,
                model_to_json(chunk),
            ),
        )
        self._tx.execute(
            "INSERT INTO knowledge_chunks_fts (chunk_id, text) VALUES (?, ?)",
            (chunk.chunk_id, chunk.text),
        )

    def add_fact(self, fact: KnowledgeFactRecord) -> None:
        self._tx.execute(
            "INSERT INTO knowledge_facts (source_id, source_version, fact_key, value, unit, "
            "chunk_id) VALUES (?, ?, ?, ?, ?, ?)",
            (fact.source_id, fact.source_version, fact.fact_key, fact.value, fact.unit, fact.chunk_id),
        )

    def list_chunks(self, source_id: str, source_version: int) -> list[KnowledgeChunk]:
        rows = self._tx.fetch_all(
            "SELECT data FROM knowledge_chunks WHERE source_id = ? AND source_version = ? "
            "ORDER BY ordinal",
            (source_id, source_version),
        )
        return load_all(KnowledgeChunk, rows)

    def list_chunks_for_sources(self, sources: Collection[SourceKey]) -> list[KnowledgeChunk]:
        if not sources:
            return []
        condition, values = _source_filter(sources)
        rows = self._tx.fetch_all(
            f"SELECT c.data FROM knowledge_chunks AS c WHERE {condition} "
            "ORDER BY c.source_id, c.source_version, c.ordinal",
            values,
        )
        return load_all(KnowledgeChunk, rows)

    def list_facts_for_sources(self, sources: Collection[SourceKey]) -> list[KnowledgeFactRecord]:
        if not sources:
            return []
        condition, values = _source_filter(sources)
        rows = self._tx.fetch_all(
            "SELECT c.source_id, c.source_version, c.fact_key, c.value, c.unit, c.chunk_id "
            f"FROM knowledge_facts AS c WHERE {condition} "
            "ORDER BY c.fact_key, c.source_id, c.source_version",
            values,
        )
        return [
            KnowledgeFactRecord(
                source_id=row["source_id"],
                source_version=row["source_version"],
                fact_key=row["fact_key"],
                value=row["value"],
                unit=row["unit"],
                chunk_id=row["chunk_id"],
            )
            for row in rows
        ]

    def match_chunks(self, match_expression: str, sources: Collection[SourceKey]) -> list[KnowledgeChunk]:
        """Every chunk of the given sources that matches an FTS5 expression, by chunk_id.

        Candidate matching only: no FTS5 ranking (bm25/rank) is used and nothing is
        truncated, because FTS5 statistics span the whole index, including ineligible
        chunks. Callers rank the returned candidates themselves. ``match_expression`` must
        be a fully quoted FTS5 expression; it is still passed as a bound parameter.
        """
        if not sources:
            return []
        condition, values = _source_filter(sources)
        rows = self._tx.fetch_all(
            "SELECT c.data FROM knowledge_chunks_fts JOIN knowledge_chunks AS c "
            "ON c.chunk_id = knowledge_chunks_fts.chunk_id "
            f"WHERE knowledge_chunks_fts MATCH ? AND {condition} ORDER BY c.chunk_id",
            (match_expression, *values),
        )
        return load_all(KnowledgeChunk, rows)

    def rebuild_search_index(self) -> int:
        """Recreate the derived FTS index from knowledge_chunks; returns the chunk count."""
        self._tx.execute("DELETE FROM knowledge_chunks_fts")
        chunks = load_all(
            KnowledgeChunk, self._tx.fetch_all("SELECT data FROM knowledge_chunks ORDER BY chunk_id")
        )
        for chunk in chunks:
            self._tx.execute(
                "INSERT INTO knowledge_chunks_fts (chunk_id, text) VALUES (?, ?)",
                (chunk.chunk_id, chunk.text),
            )
        return len(chunks)

    def count_search_rows(self) -> int:
        row = self._tx.fetch_one("SELECT COUNT(*) AS n FROM knowledge_chunks_fts")
        return int(row["n"]) if row is not None else 0
