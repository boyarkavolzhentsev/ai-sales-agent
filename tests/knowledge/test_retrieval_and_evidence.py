from pathlib import Path

from app.core.enums import KnowledgeApprovalStatus, KnowledgeDomain, KnowledgeExternalUse
from app.core.models import KnowledgeEvidence
from app.knowledge import retrieve
from app.knowledge.retrieval import evidence_id_for
from app.persistence import Database
from tests.knowledge.conftest import ingest
from tests.knowledge.sources import NOW, markdown, meta, query, write

D = KnowledgeDomain
PRICE_QUESTION = "What does the Basic plan cost per month?"


def run(db: Database, *questions: str, **kwargs: object) -> list[KnowledgeEvidence]:
    with db.transaction() as uow:
        return list(retrieve(uow, query(*questions, **kwargs), NOW))  # type: ignore[arg-type]


# ---- E. Retrieval --------------------------------------------------------------------


def test_relevant_usable_evidence_is_returned(fixture_db: Database) -> None:
    evidence = run(fixture_db, PRICE_QUESTION)
    assert evidence[0].source_id == "sample-price-list"
    assert "100 EUR per month" in evidence[0].excerpt


def test_allowed_domains_are_enforced(fixture_db: Database) -> None:
    assert run(fixture_db, PRICE_QUESTION, allowed=(D.FAQ, D.COMPANY)) == []  # pricing is out of scope
    evidence = run(fixture_db, "Where is the fictional headquarters?", allowed=(D.FAQ, D.COMPANY))
    assert evidence and {e.domain for e in evidence} <= {D.FAQ, D.COMPANY}


def test_required_domains_do_not_change_ranking_scope(fixture_db: Database) -> None:
    allowed = (D.PRICING_COMMERCIAL, D.FAQ, D.PRODUCTS_SERVICES)
    plain = run(fixture_db, PRICE_QUESTION, allowed=allowed)
    required = run(fixture_db, PRICE_QUESTION, allowed=allowed, required=(D.PRODUCTS_SERVICES,))
    assert plain == required


def test_top_k_is_enforced_per_question(fixture_db: Database) -> None:
    assert len(run(fixture_db, "Sample Widget plan", top_k=1)) == 1
    assert len(run(fixture_db, "Sample Widget plan", top_k=2)) == 2
    two_questions = run(fixture_db, "Sample Widget plan", "support working days", top_k=1)
    assert 1 <= len(two_questions) <= 2


def test_ordering_is_stable_and_ranked(fixture_db: Database) -> None:
    first = run(fixture_db, "Sample Widget plan price")
    second = run(fixture_db, "Sample Widget plan price")
    assert first == second
    assert [e.rank for e in first] == list(range(1, len(first) + 1))
    keys = [(-e.score, e.chunk_id) for e in first]
    assert keys == sorted(keys)


def test_only_usable_sources_are_returned(fixture_db: Database) -> None:
    for question in (
        "Do custom contract discounts require legal sign-off?",  # INTERNAL_ONLY
        "What if the widget is too expensive?",  # DRAFT
        "How did the retailer reduce manual reporting time?",  # stale case study
    ):
        evidence = run(fixture_db, question)
        assert all(e.source_id not in {"sample-legal-internal", "sample-objection-draft", "sample-case-study"} for e in evidence)
    for item in run(fixture_db, "Sample Widget plan price support"):
        assert (item.approval_status, item.external_use) == (KnowledgeApprovalStatus.APPROVED, KnowledgeExternalUse.EXTERNAL_OK)


def test_locale_handling(db: Database, kb_root: Path) -> None:
    write(kb_root, "FAQ", "en.md", markdown(meta(source_id="faq-en"), "# Delivery\n\nDelivery takes three days.\n"))
    write(kb_root, "FAQ", "uk.md", markdown(meta(source_id="faq-uk", locale="uk"), "# Delivery\n\nDelivery takes three days.\n"))
    ingest(db, kb_root)
    assert {e.source_id for e in run(db, "delivery days")} == {"faq-en"}
    assert {e.source_id for e in run(db, "delivery days", locale="en-GB")} == {"faq-en"}
    assert {e.source_id for e in run(db, "delivery days", locale="uk")} == {"faq-uk"}


def test_newest_usable_version_is_retrieved(db: Database, kb_root: Path) -> None:
    write(kb_root, "FAQ", "v1.md", markdown(meta(), "# Delivery\n\nDelivery takes five days.\n"))
    write(kb_root, "FAQ", "v2.md", markdown(meta(version=2), "# Delivery\n\nDelivery takes three days.\n"))
    ingest(db, kb_root)
    evidence = run(db, "delivery days")
    assert [(e.source_id, e.source_version) for e in evidence] == [("src-sample", 2)]


def test_fts_syntax_in_questions_is_inert(fixture_db: Database) -> None:
    evidence = run(fixture_db, 'price" OR NEAR(plan, 5) AND text: * ^basic -')
    assert evidence  # no FTS syntax error; the words are searched literally


def test_no_usable_sources_returns_nothing(db: Database) -> None:
    assert run(db, PRICE_QUESTION) == []


# ---- G. Evidence snapshots -------------------------------------------------------------


def test_evidence_ids_are_deterministic_per_query_and_chunk(fixture_db: Database) -> None:
    first = run(fixture_db, PRICE_QUESTION)
    assert [e.evidence_id for e in first] == [evidence_id_for("q-1", e.chunk_id) for e in first]
    other = run(fixture_db, PRICE_QUESTION, query_id="q-2")
    assert {e.evidence_id for e in first}.isdisjoint({e.evidence_id for e in other})
    assert [e.chunk_id for e in first] == [e.chunk_id for e in other]


def test_snapshot_preserves_source_version_and_metadata(db: Database, kb_root: Path) -> None:
    write(kb_root, "FAQ", "v1.md", markdown(meta(), "# Delivery\n\nDelivery takes five days.\n"))
    ingest(db, kb_root)
    [snapshot] = run(db, "delivery days")
    assert (snapshot.source_version, snapshot.domain, snapshot.review_by) == (1, D.FAQ, NOW.replace(year=2026, month=12, day=31, hour=0))
    assert snapshot.excerpt == "Delivery\n\nDelivery takes five days."
    write(kb_root, "FAQ", "v2.md", markdown(meta(version=2), "# Delivery\n\nDelivery takes three days.\n"))
    ingest(db, kb_root)
    [current] = run(db, "delivery days")
    assert current.source_version == 2 and "three" in current.excerpt
    # The earlier snapshot is an immutable value: it still shows exactly what was used.
    assert snapshot.source_version == 1 and "five" in snapshot.excerpt
