"""One-shot, incremental embedding of the approved knowledge index (Stage 19).

``KnowledgeIndexer.run()`` runs once and returns; there is no daemon and nothing runs at
startup:

1. In one transaction: select the indexable source versions (``indexable_sources``: usable
   now for some locale), compute each chunk's embedding input hash, delete every stored
   vector that is stale (its chunk is no longer indexable, it belongs to another embedding
   space, or it was made from other text) and every expired claim.
2. Chunks with a current vector are unchanged: zero provider requests for them. A chunk
   whose text exceeds ``MAX_EMBEDDING_INPUT_CHARS`` is reported as failed, never truncated.
3. The rest is embedded in batches of at most ``BATCH_SIZE`` (one provider request each).
   Before a request the batch is claimed in the database (a lease, ``knowledge_embedding_
   claims``), so a concurrent indexer skips those chunks instead of paying for them again.
   Vectors are validated (count, finite, one dimensionality equal to the index's) and
   written in one transaction per batch, only for claims still held: a batch is all or
   nothing. A provider failure stores nothing for the batch, releases its claims and stops
   the run (the remaining chunks count as failed; the next run retries them). A crash after
   the provider answered but before the commit only costs a re-embed after the lease ends.

Retrieval re-checks usability on every query, so a source withdrawn after indexing is never
returned even before the next run deletes its vectors. Logs: counts, ids, codes, timing.
"""

import logging
import secrets
import time
from datetime import timedelta
from enum import StrEnum

from pydantic import NonNegativeInt

from app.core.models import KnowledgeChunk
from app.core.models.base import CoreModel
from app.embeddings import EmbeddingError, EmbeddingErrorCode, EmbeddingPurpose, EmbeddingTransport, encode
from app.knowledge.semantic import (
    MAX_EMBEDDING_INPUT_CHARS,
    all_versions,
    embedding_input,
    indexable_sources,
    input_hash,
    space_key,
)
from app.persistence import Clock, Database, EmbeddingKey, KnowledgeEmbeddingRecord

LOG = logging.getLogger("app.knowledge.indexing")
BATCH_SIZE = 32  # far inside every provider's per-request input limit (OpenAI 2048, Gemini 100)
DEFAULT_LEASE = timedelta(minutes=5)


class IndexStatus(StrEnum):
    OK = "OK"
    ERROR = "ERROR"  # a provider failure stopped the run, or some chunks could not be embedded


class IndexReport(CoreModel):
    """Counts and codes only: no knowledge text, no vectors."""

    status: IndexStatus
    provider: str
    model: str
    requested_dimensions: NonNegativeInt
    scanned: NonNegativeInt = 0  # chunks of indexable sources
    unchanged: NonNegativeInt = 0  # already had a current vector: no request
    embedded: NonNegativeInt = 0  # new vectors stored by this run
    removed: NonNegativeInt = 0  # stale vectors deleted
    skipped: NonNegativeInt = 0  # claimed or stored by a concurrent indexer meanwhile
    failed: NonNegativeInt = 0  # not embedded: too large, or a provider failure
    requests: NonNegativeInt = 0  # provider requests made
    error_code: str | None = None


class KnowledgeIndexer:
    def __init__(self, db: Database, clock: Clock, transport: EmbeddingTransport, *, batch_size: int = BATCH_SIZE,
                 lease: timedelta = DEFAULT_LEASE) -> None:
        if not 1 <= batch_size <= 100:
            raise ValueError("batch_size must be between 1 and 100")
        self._db = db
        self._clock = clock
        self._transport = transport
        self._batch_size = batch_size
        self._lease = lease

    def run(self) -> IndexReport:
        started = time.monotonic()
        space = self._transport.space
        skey = space_key(space)
        now = self._clock.now()
        with self._db.transaction() as uow:
            sources = indexable_sources(all_versions(uow), now)
            chunks = uow.knowledge_index.list_chunks_for_sources({(s.source_id, s.version) for s in sources})
            wanted = {c.chunk_id: (c, input_hash(embedding_input(c))) for c in chunks}
            fresh: set[str] = set()
            removed = 0
            for key in uow.knowledge_embeddings.list_keys():
                current = wanted.get(key.chunk_id)
                if key.space == skey and current is not None and key.input_hash == current[1]:
                    fresh.add(key.chunk_id)
                else:
                    uow.knowledge_embeddings.delete(key)
                    removed += 1
            uow.knowledge_embeddings.delete_expired_claims(now)
            dimensions = uow.knowledge_embeddings.space_dimensions(skey)

        pending = [wanted[chunk_id] for chunk_id in sorted(wanted) if chunk_id not in fresh]
        too_large = [item for item in pending if len(embedding_input(item[0])) > MAX_EMBEDDING_INPUT_CHARS]
        todo = [item for item in pending if len(embedding_input(item[0])) <= MAX_EMBEDDING_INPUT_CHARS]
        counts = {"scanned": len(chunks), "unchanged": len(fresh), "removed": removed, "failed": len(too_large),
                  "embedded": 0, "skipped": 0, "requests": 0}
        error: EmbeddingErrorCode | None = EmbeddingErrorCode.INPUT_TOO_LARGE if too_large else None

        for start in range(0, len(todo), self._batch_size):
            batch = todo[start:start + self._batch_size]
            outcome = self._embed_batch(batch, dimensions, counts)
            if isinstance(outcome, EmbeddingErrorCode):
                error = outcome
                counts["failed"] += len(todo) - (start + len(batch))  # not attempted: the next run retries them
                break
            if outcome is not None:
                dimensions = {outcome}

        report = IndexReport(status=IndexStatus.ERROR if error or counts["failed"] else IndexStatus.OK,
                             provider=space.provider, model=space.model, requested_dimensions=space.requested_dimensions,
                             error_code=error.value if error else None, **counts)
        LOG.info("knowledge_index provider=%s model=%s status=%s scanned=%d unchanged=%d embedded=%d removed=%d "
                 "skipped=%d failed=%d requests=%d error=%s latency_ms=%d", space.provider, space.model,
                 report.status.value, report.scanned, report.unchanged, report.embedded, report.removed, report.skipped,
                 report.failed, report.requests, report.error_code, int((time.monotonic() - started) * 1000))
        return report

    def _embed_batch(self, batch: list[tuple[KnowledgeChunk, str]], dimensions: set[int],
                     counts: dict[str, int]) -> EmbeddingErrorCode | int | None:
        """Claim, embed, store one batch. Returns the stored dimensionality, None when the
        whole batch was taken by another indexer, or the error code that stops the run."""
        space = self._transport.space
        token = "kec_" + secrets.token_hex(16)
        now = self._clock.now()
        keys = [self._key(chunk, digest) for chunk, digest in batch]
        with self._db.transaction() as uow:
            claimed = [(item, key) for item, key in zip(batch, keys, strict=True)
                       if not uow.knowledge_embeddings.exists(key)
                       and uow.knowledge_embeddings.claim(key, token, now + self._lease, now)]
        counts["skipped"] += len(batch) - len(claimed)
        if not claimed:
            return None
        try:
            counts["requests"] += 1
            result = self._transport.embed([embedding_input(chunk) for (chunk, _), _ in claimed], EmbeddingPurpose.DOCUMENT)
            if result.space != space or len(result.vectors) != len(claimed):
                raise EmbeddingError(EmbeddingErrorCode.INVALID_RESPONSE)
            if dimensions and dimensions != {result.dimensions}:
                raise EmbeddingError(EmbeddingErrorCode.DIMENSION_MISMATCH)  # never mix sizes in one space
        except EmbeddingError as exc:
            self._release(token)
            counts["failed"] += len(claimed)
            return exc.code
        except BaseException:
            self._release(token)
            raise
        now = self._clock.now()
        with self._db.transaction() as uow:
            for (_, key), vector in zip(claimed, result.vectors, strict=True):
                if not uow.knowledge_embeddings.holds(key, token):
                    counts["skipped"] += 1  # our lease expired and another indexer took the chunk over
                    continue
                stored = uow.knowledge_embeddings.add(KnowledgeEmbeddingRecord(
                    key=key, dimensions=result.dimensions, vector=encode(vector), created_at=now))
                counts["embedded" if stored else "skipped"] += 1
            uow.knowledge_embeddings.release(token)
        return result.dimensions

    def _release(self, token: str) -> None:
        with self._db.transaction() as uow:
            uow.knowledge_embeddings.release(token)

    def _key(self, chunk: KnowledgeChunk, digest: str) -> EmbeddingKey:
        space = self._transport.space
        return EmbeddingKey(chunk_id=chunk.chunk_id, provider=space.provider, model=space.model,
                            requested_dimensions=space.requested_dimensions, input_hash=digest)
