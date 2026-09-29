import sqlite3
from pathlib import Path

import pytest

from app.core.enums import KnowledgeDomain
from app.knowledge import (
    DuplicateSourceVersionError,
    IngestStatus,
    SourceFormatError,
    SourceValidationError,
    ingest_directory,
    load_directory,
)
from app.persistence import Database, FrozenClock
from tests.knowledge.conftest import ingest
from tests.knowledge.sources import FIXTURE_ROOT, NOW, fact, markdown, meta, write, yaml_doc

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ingest_fixture_knowledge_base(db: Database) -> None:
    with db.transaction() as uow:
        results = ingest_directory(uow, FIXTURE_ROOT, now=NOW)
    assert {r.status for r in results} == {IngestStatus.INGESTED}
    assert sorted(r.source.source_id for r in results) == [
        "sample-case-study",
        "sample-company-overview",
        "sample-faq",
        "sample-legal-internal",
        "sample-objection-draft",
        "sample-price-list",
        "sample-product-widget",
    ]
    with db.transaction() as uow:
        price_list = uow.knowledge_sources.get("sample-price-list", 2)
        assert price_list is not None and price_list.path == "pricing/price_list.yaml"
        chunks = uow.knowledge_index.list_chunks("sample-price-list", 2)
        assert len(chunks) == 3  # billing body + two facts
        facts = uow.knowledge_index.list_facts_for_sources({("sample-price-list", 2)})
        assert [f.fact_key for f in facts] == ["plan.basic.monthly_price", "plan.team.monthly_price"]
        assert uow.knowledge_index.count_search_rows() == sum(r.chunk_count for r in results)


def test_production_knowledge_base_contains_no_sources() -> None:
    # The real knowledge_base/ holds only guidance until the business adds approved content.
    assert load_directory(REPO_ROOT / "knowledge_base") == []


def test_reingesting_identical_content_is_a_no_op(fixture_db: Database) -> None:
    with fixture_db.transaction() as uow:
        before = uow.knowledge_index.count_search_rows()
        results = ingest_directory(uow, FIXTURE_ROOT, now=NOW)
        assert {r.status for r in results} == {IngestStatus.UNCHANGED}
        assert uow.knowledge_index.count_search_rows() == before


def test_same_version_with_different_content_is_rejected(db: Database, kb_root: Path) -> None:
    path = write(kb_root, "FAQ", "a.md", markdown(meta()))
    ingest(db, kb_root)
    path.write_text(markdown(meta(), "# Changed\n\nDifferent text.\n"), encoding="utf-8")
    with pytest.raises(DuplicateSourceVersionError):
        ingest(db, kb_root)


def test_duplicate_source_version_within_one_batch_is_rejected(kb_root: Path) -> None:
    write(kb_root, "FAQ", "a.md", markdown(meta()))
    write(kb_root, "FAQ", "b.md", markdown(meta(), "# Other\n\nOther text.\n"))
    with pytest.raises(DuplicateSourceVersionError, match="also defined"):
        load_directory(kb_root)


def test_new_version_keeps_history(db: Database, kb_root: Path) -> None:
    write(kb_root, "FAQ", "a_v1.md", markdown(meta()))
    ingest(db, kb_root)
    write(kb_root, "FAQ", "a_v2.md", markdown(meta(version=2), "# Updated\n\nUpdated answer.\n"))
    ingest(db, kb_root)
    with db.transaction() as uow:
        v1 = uow.knowledge_sources.get("src-sample", 1)
        latest = uow.knowledge_sources.get_latest("src-sample")
        assert v1 is not None and latest is not None and latest.version == 2
        assert uow.knowledge_index.list_chunks("src-sample", 1)  # v1 chunks retained
        assert uow.knowledge_index.list_chunks("src-sample", 2)


def test_ingested_knowledge_is_immutable_in_the_database(tmp_path: Path) -> None:
    path = tmp_path / "kb.sqlite3"
    with Database(path) as db:
        db.initialize_schema(FrozenClock(NOW))
        ingest(db, FIXTURE_ROOT)
    raw = sqlite3.connect(path, isolation_level=None)
    try:
        for statement in (
            "UPDATE knowledge_sources_meta SET approval_status = 'DRAFT'",
            "DELETE FROM knowledge_sources_meta",
            "UPDATE knowledge_chunks SET ordinal = 99",
            "DELETE FROM knowledge_chunks",
            "UPDATE knowledge_facts SET value = '1'",
            "DELETE FROM knowledge_facts",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                raw.execute(statement)
    finally:
        raw.close()


def test_failed_batch_writes_nothing(db: Database, kb_root: Path) -> None:
    write(kb_root, "FAQ", "a.md", markdown(meta(source_id="good")))
    write(kb_root, "FAQ", "b.md", markdown(meta(source_id="bad", version=0)))
    with pytest.raises(SourceValidationError):
        ingest(db, kb_root)
    with db.transaction() as uow:
        assert uow.knowledge_sources.get_latest("good") is None
        assert uow.knowledge_index.count_search_rows() == 0


def test_folder_must_match_declared_domain(kb_root: Path) -> None:
    (kb_root / "pricing").mkdir()
    (kb_root / "pricing" / "a.md").write_text(markdown(meta(domain="FAQ")), encoding="utf-8")
    with pytest.raises(SourceValidationError, match="does not match folder"):
        load_directory(kb_root)


@pytest.mark.parametrize("relative", ["loose.md", "unknown_domain/a.md"])
def test_files_outside_domain_folders_are_rejected(kb_root: Path, relative: str) -> None:
    path = kb_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown(meta()), encoding="utf-8")
    with pytest.raises(SourceFormatError):
        load_directory(kb_root)


def test_unsupported_file_in_domain_folder_is_rejected(kb_root: Path) -> None:
    write(kb_root, "FAQ", "notes.txt", "free text")
    with pytest.raises(SourceFormatError, match="unsupported"):
        load_directory(kb_root)


def test_readme_gitkeep_and_hidden_files_are_ignored(kb_root: Path) -> None:
    (kb_root / "README.md").write_text("# guide", encoding="utf-8")
    faq = kb_root / "faq"
    faq.mkdir()
    (faq / ".gitkeep").write_text("", encoding="utf-8")
    (faq / ".draft.md").write_text("junk", encoding="utf-8")
    assert load_directory(kb_root) == []


def test_supersedes_must_reference_a_known_source(db: Database, kb_root: Path) -> None:
    write(kb_root, "FAQ", "new.md", markdown(meta(source_id="new-faq", supersedes="missing-faq")))
    with pytest.raises(SourceValidationError, match="supersedes unknown"):
        ingest(db, kb_root)
    write(kb_root, "FAQ", "old.md", markdown(meta(source_id="missing-faq")))
    ingest(db, kb_root)  # known within the same batch


def test_approval_in_the_future_is_rejected(db: Database, kb_root: Path) -> None:
    write(kb_root, "FAQ", "a.md", markdown(meta(approved_at="2026-07-01T00:00:00+00:00")))
    with pytest.raises(SourceValidationError, match="future"):
        ingest(db, kb_root)


def test_search_index_can_be_rebuilt(fixture_db: Database) -> None:
    with fixture_db.transaction() as uow:
        expected = uow.knowledge_index.count_search_rows()
        assert uow.knowledge_index.rebuild_search_index() == expected
        assert uow.knowledge_index.rebuild_search_index() == expected
        assert uow.knowledge_index.count_search_rows() == expected


def test_fact_documents_ingest_facts(db: Database, kb_root: Path) -> None:
    write(kb_root, "PRICING_COMMERCIAL", "p.yaml", yaml_doc(meta(domain="PRICING_COMMERCIAL"), [fact("a.b", "1", "EUR")]))
    ingest(db, kb_root)
    with db.transaction() as uow:
        [record] = uow.knowledge_index.list_facts_for_sources({("src-sample", 1)})
        assert (record.fact_key, record.value, record.unit) == ("a.b", "1", "EUR")
        assert uow.knowledge_sources.list_by_domain(KnowledgeDomain.PRICING_COMMERCIAL)[0].source_id == "src-sample"
