"""The one-shot incremental indexer: only approved/usable chunks, zero requests for unchanged
ones, stale-vector deletion, model/dimension isolation, batch atomicity, bounded failure,
concurrency claims, and the v13 migration."""

import sqlite3
import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from app.embeddings import EmbeddingErrorCode
from app.knowledge import IndexStatus
from app.knowledge.semantic import MAX_EMBEDDING_INPUT_CHARS, all_versions, indexable_sources
from app.persistence import Database, EmbeddingKey, FrozenClock
from app.persistence.migrations import MIGRATIONS, apply_migrations, current_version, latest_version
from tests.inbound.builders import NOW
from tests.knowledge.sources import markdown, meta, write
from tests.llm_providers.fakes import Response
from tests.rag.builders import chunk_ids, indexer, ingest, knowledge_dir, model_of, price_list, seeded_db
from tests.rag.fakes import EMBEDDINGS_KEY, Vendor, failure

INDEXABLE = 8  # chunks of usable sources: price list (body + 1 fact), product (2), company (2), FAQ (2)


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    return knowledge_dir(tmp_path)


@pytest.fixture
def db(tmp_path: Path, kb: Path) -> Iterator[Database]:
    with Database(seeded_db(tmp_path / "rag.sqlite3", kb)) as database:
        yield database


def indexable_chunk_ids(db: Database) -> set[str]:
    with db.transaction() as uow:
        sources = indexable_sources(all_versions(uow), NOW)
        return {c.chunk_id for c in uow.knowledge_index.list_chunks_for_sources({(s.source_id, s.version) for s in sources})}


def all_chunk_texts(db: Database) -> dict[str, str]:
    with db.transaction() as uow:
        rows = uow._tx.fetch_all("SELECT data FROM knowledge_chunks")  # noqa: SLF001
    import json
    return {json.loads(r[0])["chunk_id"]: json.loads(r[0])["text"] for r in rows}


# ---- Selection --------------------------------------------------------------------------------------------


def test_only_usable_approved_knowledge_is_embedded(db: Database) -> None:
    vendor = Vendor()
    report = indexer(db, vendor).run()
    expected = indexable_chunk_ids(db)
    assert report.status is IndexStatus.OK and report.scanned == report.embedded == len(expected) == INDEXABLE
    assert set(chunk_ids(db)) == expected
    sent = " ".join(vendor.texts)
    assert "annual discount" not in sent  # DRAFT objection handling
    assert "legal sign-off" not in sent  # INTERNAL_ONLY
    assert "reduced manual reporting" not in sent  # expired case study
    assert "79 EUR" in sent and "integrates with spreadsheet exports" in sent
    texts = all_chunk_texts(db)
    assert sorted(vendor.texts) == sorted(texts[c] for c in expected)  # the exact chunk text, nothing appended


def test_retired_superseded_and_future_versions_are_not_indexed(tmp_path: Path, db: Database, kb: Path) -> None:
    write(kb, "FAQ", "retired.md", markdown(meta(source_id="faq-retired", version=1), "# Retired\n\nSSO via SAML is supported.\n"))
    write(kb, "FAQ", "retired_v2.md", markdown(meta(source_id="faq-retired", version=2, approval_status="RETIRED"),
                                              "# Retired\n\nWithdrawn.\n"))
    write(kb, "FAQ", "future.md", markdown(meta(source_id="faq-future", effective_from="2026-09-01T00:00:00+00:00"),
                                           "# Future\n\nUptime is guaranteed at 99.99 percent.\n"))
    ingest(db, kb)
    vendor = Vendor()
    indexer(db, vendor).run()
    assert "SAML" not in " ".join(vendor.texts) and "Withdrawn" not in " ".join(vendor.texts)
    assert "99.99" not in " ".join(vendor.texts)


# ---- Incremental ------------------------------------------------------------------------------------------


def test_a_second_unchanged_run_makes_zero_requests(db: Database) -> None:
    vendor = Vendor()
    first = indexer(db, vendor).run()
    second = indexer(db, vendor).run()
    assert first.requests == 1 and first.embedded == INDEXABLE
    assert (second.requests, second.embedded, second.unchanged, second.removed) == (0, 0, INDEXABLE, 0)
    assert len(vendor.session.posts) == 1


def test_batches_are_bounded(db: Database) -> None:
    vendor = Vendor()
    report = indexer(db, vendor, batch_size=3).run()
    assert [len(b) for b in vendor.batches] == [3, 3, 2] and report.requests == 3 and report.embedded == INDEXABLE


def test_changed_knowledge_gets_new_vectors_and_the_old_ones_are_deleted(tmp_path: Path, db: Database, kb: Path) -> None:
    vendor = Vendor()
    indexer(db, vendor).run()
    before = set(chunk_ids(db))
    write(kb, "PRICING_COMMERCIAL", "price_list_v3.yaml", price_list("89", version=3))
    ingest(db, kb)  # v3 supersedes v2 (newest usable version)
    vendor.batches.clear()
    report = indexer(db, vendor).run()
    after = set(chunk_ids(db))
    assert report.embedded == 2 and report.removed == 2 and report.unchanged == INDEXABLE - 2  # body + fact chunk of each version
    assert all("89 EUR" in t or "Invoices" in t for t in vendor.texts) and not any("79 EUR" in t for t in vendor.texts)
    assert after == indexable_chunk_ids(db) and len(after - before) == 2


def test_a_chunk_whose_stored_input_hash_differs_is_re_embedded(db: Database) -> None:
    vendor = Vendor()
    indexer(db, vendor).run()
    with db.transaction() as uow:
        uow._tx.execute("UPDATE knowledge_embeddings SET input_hash = ? WHERE rowid = 1", ("0" * 64,))  # noqa: SLF001
    report = indexer(db, vendor).run()
    assert (report.removed, report.embedded, report.requests) == (1, 1, 1)


def test_changing_the_model_or_dimensions_replaces_every_vector(db: Database) -> None:
    vendor = Vendor()
    indexer(db, vendor).run()
    report = indexer(db, vendor, model="openai-embed-v-next").run()
    assert (report.removed, report.embedded) == (INDEXABLE, INDEXABLE)
    with db.transaction() as uow:
        assert {k.model for k in uow.knowledge_embeddings.list_keys()} == {"openai-embed-v-next"}
    sized = indexer(db, Vendor(dims=10), model="openai-embed-v-next", dims=10).run()
    assert (sized.removed, sized.embedded) == (INDEXABLE, INDEXABLE)
    with db.transaction() as uow:
        assert {(k.requested_dimensions, k.provider) for k in uow.knowledge_embeddings.list_keys()} == {(10, "OPENAI")}
    gemini = indexer(db, Vendor(), "gemini").run()
    assert (gemini.removed, gemini.embedded) == (INDEXABLE, INDEXABLE)


def test_withdrawn_knowledge_vectors_are_deleted_on_the_next_run(db: Database, kb: Path) -> None:
    indexer(db, Vendor()).run()
    write(kb, "PRICING_COMMERCIAL", "price_list_v3.yaml", price_list("79", version=3, approval="RETIRED"))
    ingest(db, kb)
    report = indexer(db, Vendor()).run()
    assert report.removed == 2 and report.embedded == 0 and report.requests == 0
    assert not any("79 EUR" in all_chunk_texts(db)[c] for c in chunk_ids(db))


def test_empty_knowledge_makes_no_request(tmp_path: Path) -> None:
    with Database(tmp_path / "empty.sqlite3") as db:
        db.initialize_schema(FrozenClock(NOW))
        vendor = Vendor()
        report = indexer(db, vendor).run()
    assert report.status is IndexStatus.OK and report.scanned == report.requests == 0 and vendor.session.posts == []


# ---- Failures ---------------------------------------------------------------------------------------------


def test_an_oversized_chunk_is_reported_not_truncated(db: Database, kb: Path) -> None:
    long = "Sampletown " * (MAX_EMBEDDING_INPUT_CHARS // 10)
    write(kb, "FAQ", "long.json", '{"metadata": %s, "facts": [{"key": "long.text", "value": "x", "statement": "%s"}]}'
          % (__import__("json").dumps(meta(source_id="faq-long")), long.strip()))
    ingest(db, kb)
    vendor = Vendor()
    report = indexer(db, vendor).run()
    assert report.status is IndexStatus.ERROR and report.failed == 1 and report.error_code == "INPUT_TOO_LARGE"
    assert report.embedded == INDEXABLE  # every other chunk; the long source's single fact chunk failed
    assert all(len(t) <= MAX_EMBEDDING_INPUT_CHARS for t in vendor.texts)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_a_failed_batch_stores_nothing_releases_claims_and_stops(db: Database, provider: str) -> None:
    vendor = Vendor().script(failure(provider, "TEMPORARY_PROVIDER_ERROR"))
    report = indexer(db, vendor, provider, batch_size=3).run()
    assert report.status is IndexStatus.ERROR and report.error_code == "TEMPORARY_PROVIDER_ERROR"
    assert (report.embedded, report.failed, report.requests) == (0, INDEXABLE, 1)  # no retry storm: the run stops
    assert chunk_ids(db) == []
    with db.transaction() as uow:
        assert uow.knowledge_embeddings.count_claims() == 0
    retry = indexer(db, Vendor(), provider).run()  # the next run retries everything
    assert retry.embedded == INDEXABLE and retry.status is IndexStatus.OK


def test_a_later_batch_failure_keeps_the_earlier_batches(db: Database) -> None:
    vendor = Vendor()
    vendor.script(lambda url, body: vendor_ok(vendor, url, body), failure("openai", "RATE_LIMITED"))
    report = indexer(db, vendor, batch_size=3).run()
    assert (report.embedded, report.failed, report.error_code) == (3, INDEXABLE - 3, "RATE_LIMITED")
    assert len(chunk_ids(db)) == 3


def vendor_ok(vendor: Vendor, url: str, body: dict[str, object]) -> Response:
    from tests.rag.fakes import inputs_of, ok, provider_of
    return ok(provider_of(url), [vendor.vectorize(t, vendor.dims) for t in inputs_of(url, body)], model=body.get("model"))  # type: ignore[arg-type]


@pytest.mark.parametrize("payload", [Response(200, {"object": "list", "data": [], "model": model_of("openai")}),
                                     Response(200, {"data": [{"index": 0, "embedding": [float("nan")]}]})])
def test_an_invalid_response_marks_nothing_indexed(db: Database, payload: Response) -> None:
    report = indexer(db, Vendor().script(payload)).run()
    assert report.error_code == "INVALID_RESPONSE" and chunk_ids(db) == []


def test_new_vectors_of_another_size_are_never_mixed_into_the_space(db: Database, kb: Path) -> None:
    indexer(db, Vendor()).run()
    write(kb, "FAQ", "new.md", markdown(meta(source_id="faq-new"), "# New\n\nOnboarding questions.\n"))
    ingest(db, kb)
    report = indexer(db, Vendor(dims=10)).run()  # same configured space, the model now answers a different size
    assert report.error_code == EmbeddingErrorCode.DIMENSION_MISMATCH.value and report.embedded == 0
    with db.transaction() as uow:
        assert uow.knowledge_embeddings.space_dimensions(("OPENAI", model_of("openai"), 0)) == {8}


# ---- Concurrency --------------------------------------------------------------------------------------------


def test_a_concurrent_indexer_skips_chunks_claimed_by_another(tmp_path: Path, db: Database) -> None:
    other_vendor = Vendor()
    reports = []

    def interleave(texts: list[str]) -> None:
        if not reports:  # while A's first batch is at the provider, B runs to completion
            with Database(db.path) as other:
                reports.append(indexer(other, other_vendor, batch_size=3).run())

    vendor = Vendor(hook=interleave)
    first = indexer(db, vendor, batch_size=3).run()
    [second] = reports
    assert sorted(vendor.texts + other_vendor.texts) == sorted(all_chunk_texts(db)[c] for c in indexable_chunk_ids(db))
    assert first.embedded + second.embedded == INDEXABLE and second.skipped == 3  # each chunk embedded exactly once
    assert len(set(chunk_ids(db))) == len(chunk_ids(db)) == INDEXABLE


def test_threads_racing_two_indexers_embed_each_chunk_once(tmp_path: Path, db: Database) -> None:
    vendors = [Vendor(), Vendor()]
    barrier = threading.Barrier(2)
    reports: list[object] = []
    errors: list[BaseException] = []

    def run(vendor: Vendor) -> None:
        try:
            with Database(db.path, busy_timeout_ms=20_000) as own:
                barrier.wait()
                reports.append(indexer(own, vendor, batch_size=2).run())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    for _ in range(3):
        with db.transaction() as uow:
            uow._tx.execute("DELETE FROM knowledge_embeddings")  # noqa: SLF001
        for v in vendors:
            v.batches.clear()
        reports.clear()
        threads = [threading.Thread(target=run, args=(v,)) for v in vendors]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert errors == []
        texts = vendors[0].texts + vendors[1].texts
        assert len(texts) == len(set(texts)) == INDEXABLE  # one provider call per chunk
        assert len(chunk_ids(db)) == INDEXABLE


def test_a_crashed_claim_blocks_until_its_lease_expires(db: Database) -> None:
    import hashlib
    texts = all_chunk_texts(db)
    some = sorted(indexable_chunk_ids(db))[0]
    key = EmbeddingKey(chunk_id=some, provider="OPENAI", model=model_of("openai"), requested_dimensions=0,
                       input_hash=hashlib.sha256(texts[some].encode()).hexdigest())
    with db.transaction() as uow:
        assert uow.knowledge_embeddings.claim(key, "kec_crashed", NOW + timedelta(minutes=5), NOW)
        assert not uow.knowledge_embeddings.claim(key, "kec_other", NOW + timedelta(minutes=5), NOW)  # held
    blocked = indexer(db, Vendor()).run()
    assert (blocked.embedded, blocked.skipped) == (INDEXABLE - 1, 1)  # the dead worker's chunk waits for its lease
    later = indexer(db, Vendor(), clock=FrozenClock(NOW + timedelta(minutes=6))).run()
    assert (later.embedded, later.skipped, later.requests) == (1, 0, 1)


# ---- Migration ----------------------------------------------------------------------------------------------


def test_a_fresh_database_reaches_v13_with_minimal_tables(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite3"
    with Database(path) as db:
        assert db.initialize_schema(FrozenClock(NOW)) == latest_version() == 13
        assert db.initialize_schema(FrozenClock(NOW)) == 13  # repeatable
    raw = sqlite3.connect(path)
    try:
        assert MIGRATIONS[-1].name == "knowledge_embeddings"
        columns = [r[1] for r in raw.execute("PRAGMA table_info(knowledge_embeddings)")]
        claims = [r[1] for r in raw.execute("PRAGMA table_info(knowledge_embedding_claims)")]
    finally:
        raw.close()
    assert columns == ["chunk_id", "provider", "model", "requested_dimensions", "input_hash", "dimensions", "vector",
                       "created_at"]
    assert claims == ["chunk_id", "provider", "model", "requested_dimensions", "input_hash", "claim_token", "lease_until"]


def test_a_v12_database_upgrades_intact_and_repeatably(tmp_path: Path, kb: Path) -> None:
    from tests.llm_providers.test_recovery import jobs
    from tests.pipeline.builders import active_opportunity, lead, opportunity_lead
    path = tmp_path / "stage18.sqlite3"
    raw = sqlite3.connect(path, isolation_level=None)
    raw.execute("PRAGMA foreign_keys = ON")
    try:
        assert apply_migrations(raw, FrozenClock(NOW), MIGRATIONS[:12]) == 12
        raw.execute("INSERT INTO ai_enrichment_jobs (job_id, kind, message_id, lead_id, status, due_at, updated_at, version, "
                    "data) SELECT 'j', 'QUALIFICATION_EXTRACTION', 'm', 'l', 'PENDING', 'x', 'x', 1, '{\"version\": 1}' "
                    "WHERE 0")  # the v12 table exists (no row: its FK needs a message)
    finally:
        raw.close()
    with Database(path) as db:
        ingest(db, kb)
        lead_id = opportunity_lead(db)
        before = (lead(db, lead_id), active_opportunity(db, lead_id), sorted(all_chunk_texts(db).items()), jobs(db))
    with Database(path) as db:
        assert db.initialize_schema(FrozenClock(NOW + timedelta(days=1))) == 13
        assert db.initialize_schema(FrozenClock(NOW + timedelta(days=2))) == 13
        assert (lead(db, lead_id), active_opportunity(db, lead_id), sorted(all_chunk_texts(db).items()), jobs(db)) == before
        assert chunk_ids(db) == []  # nothing backfilled: indexing is an explicit command
    assert current_version(sqlite3.connect(path)) == 13


def test_no_key_or_text_is_stored_with_the_vectors(tmp_path: Path, db: Database) -> None:
    indexer(db, Vendor()).run()
    with db.transaction() as uow:
        rows = uow._tx.fetch_all("SELECT * FROM knowledge_embeddings")  # noqa: SLF001
    blob = b"".join(bytes(str(tuple(r)), "utf-8") for r in rows)
    assert rows and EMBEDDINGS_KEY.encode() not in blob and b"Basic plan" not in blob and b"79 EUR" not in blob
