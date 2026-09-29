import math
from pathlib import Path

from app.knowledge import retrieve
from app.knowledge.scoring import BM25_B, BM25_K1, EligibleCorpus, content_terms, tokens
from app.persistence import Database
from app.persistence.repositories import protocols
from tests.knowledge.conftest import ingest
from tests.knowledge.sources import NOW, markdown, meta, query, write


def test_score_matches_the_documented_formula() -> None:
    corpus = EligibleCorpus(["red apple", "green apple pie", "blue sky"])
    terms = content_terms("apple pie")
    text = "green apple pie"
    n, avg = 3, (2 + 3 + 2) / 3
    expected = 0.0
    for term, containing in (("apple", 2), ("pie", 1)):
        idf = math.log(1 + (n - containing + 0.5) / (containing + 0.5))
        expected += idf * 1 * (BM25_K1 + 1) / (1 + BM25_K1 * (1 - BM25_B + BM25_B * len(tokens(text)) / avg))
    assert corpus.score(terms, text) == round(expected, 9)


def test_score_semantics() -> None:
    corpus = EligibleCorpus(["delivery portal", "delivery", "portal tracking", "unrelated text"])
    terms = content_terms("delivery portal tracking")
    assert corpus.score(terms, "unrelated text") == 0.0
    assert corpus.score(terms, "delivery portal tracking") > corpus.score(terms, "delivery portal")
    assert corpus.score(terms, "tracking") > corpus.score(terms, "delivery")  # rarer term weighs more
    assert corpus.score(terms, "delivery portal") == corpus.score(terms, "delivery portal")
    assert EligibleCorpus([]).score(terms, "delivery") == 0.0


def test_statistics_come_only_from_the_given_corpus() -> None:
    terms = content_terms("delivery")
    small = EligibleCorpus(["delivery portal", "portal"])
    assert small.score(terms, "delivery portal") == EligibleCorpus(["delivery portal", "portal"]).score(
        terms, "delivery portal"
    )
    assert small.size == 2
    bigger = EligibleCorpus(["delivery portal", "portal", "delivery again"])
    assert bigger.score(terms, "delivery portal") != small.score(terms, "delivery portal")


def test_fts5_is_used_for_matching_only() -> None:
    assert not hasattr(protocols.KnowledgeIndexRepository, "search")
    assert hasattr(protocols.KnowledgeIndexRepository, "match_chunks")


def test_top_k_keeps_the_best_clean_scores_not_the_first_candidates(db: Database, kb_root: Path) -> None:
    # 30 weak matches and one strong one: the strong one must win top_k=1 wherever its
    # chunk_id falls in candidate order, because nothing is truncated before scoring.
    for index in range(30):
        write(kb_root, "FAQ", f"weak-{index}.md", markdown(meta(source_id=f"weak-{index}"), f"# Weak {index}\n\nPortal.\n"))
    write(kb_root, "FAQ", "strong.md", markdown(meta(source_id="strong"), "# Guide\n\nPortal delivery tracking.\n"))
    ingest(db, kb_root)
    with db.transaction() as uow:
        [top] = retrieve(uow, query("portal delivery tracking", top_k=1), NOW)
    assert top.source_id == "strong"
