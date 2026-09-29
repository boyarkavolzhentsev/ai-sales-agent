"""Load-bearing: ineligible knowledge must have zero effect on customer-facing ranking.

Metadata filtering happens before ranking, so adding any amount of matching content that
is unapproved, internal-only, stale, retired, not yet effective, in another locale, or in
a domain outside allowed_domains must leave evidence, scores, order and top_k membership
exactly as they were.
"""

from pathlib import Path

import pytest

from app.core.enums import KnowledgeDomain
from app.core.models import KnowledgeEvidence
from app.knowledge import retrieve
from app.persistence import Database
from tests.knowledge.conftest import ingest
from tests.knowledge.sources import NOW, markdown, meta, query, write

D = KnowledgeDomain
QUESTION = "delivery tracking portal notifications"
ALLOWED = (D.FAQ, D.PRODUCTS_SERVICES)

ELIGIBLE = {
    # Chunk A matches three terms, chunk B two, chunk C one: a clear eligible ranking.
    "eligible-a": ("FAQ", "# Delivery\n\nDelivery tracking is available in the customer portal.\n"),
    "eligible-b": ("PRODUCTS_SERVICES", "# Portal\n\nThe portal sends delivery notifications.\n"),
    "eligible-c": ("FAQ", "# Notifications\n\nNotifications can be muted.\n"),
}
NOISE_BODY = "# Noise\n\n" + "delivery tracking portal notifications " * 3 + "\n"

INELIGIBLE_VARIANTS = {
    "draft": {"approval_status": "DRAFT", "approved_by": None, "approved_at": None},
    "internal": {"external_use": "INTERNAL_ONLY"},
    "stale": {"review_by": "2026-05-01T00:00:00+00:00"},
    "future": {"effective_from": "2026-07-01T00:00:00+00:00", "review_by": "2026-12-31T00:00:00+00:00"},
    "other-locale": {"locale": "uk"},
}


def seed_eligible(root: Path) -> None:
    for source_id, (domain, body) in ELIGIBLE.items():
        write(root, domain, f"{source_id}.md", markdown(meta(source_id=source_id, domain=domain), body))


def results(db: Database, top_k: int = 5) -> list[KnowledgeEvidence]:
    with db.transaction() as uow:
        return list(retrieve(uow, query(QUESTION, allowed=ALLOWED, top_k=top_k), NOW))


def fingerprint(evidence: list[KnowledgeEvidence]) -> list[tuple[str, int, float, str]]:
    return [(e.source_id, e.rank, e.score, e.evidence_id) for e in evidence]


@pytest.fixture
def baseline(db: Database, kb_root: Path) -> tuple[Database, Path, list[KnowledgeEvidence], list[KnowledgeEvidence]]:
    seed_eligible(kb_root)
    ingest(db, kb_root)
    full, top2 = results(db), results(db, top_k=2)
    assert [e.source_id for e in full] == ["eligible-a", "eligible-b", "eligible-c"]
    assert [e.source_id for e in top2] == ["eligible-a", "eligible-b"]
    return db, kb_root, full, top2


@pytest.mark.parametrize("variant", sorted(INELIGIBLE_VARIANTS))
def test_ineligible_content_has_zero_effect_on_ranking(
    baseline: tuple[Database, Path, list[KnowledgeEvidence], list[KnowledgeEvidence]], variant: str
) -> None:
    db, root, full, top2 = baseline
    for index in range(40):
        domain = "FAQ" if index % 2 else "PRODUCTS_SERVICES"
        write(
            root, domain, f"noise-{variant}-{index}.md",
            markdown(meta(source_id=f"noise-{variant}-{index}", domain=domain, **INELIGIBLE_VARIANTS[variant]), NOISE_BODY),
        )
    ingest(db, root)
    assert fingerprint(results(db)) == fingerprint(full)
    assert fingerprint(results(db, top_k=2)) == fingerprint(top2)


def test_retired_versions_have_zero_effect_on_ranking(
    baseline: tuple[Database, Path, list[KnowledgeEvidence], list[KnowledgeEvidence]],
) -> None:
    db, root, full, top2 = baseline
    for index in range(20):
        base = meta(source_id=f"retired-{index}")
        write(root, "FAQ", f"retired-{index}-v1.md", markdown(base, NOISE_BODY))
        write(root, "FAQ", f"retired-{index}-v2.md", markdown(meta(source_id=f"retired-{index}", version=2, approval_status="RETIRED"), NOISE_BODY))
    ingest(db, root)
    assert fingerprint(results(db)) == fingerprint(full)
    assert fingerprint(results(db, top_k=2)) == fingerprint(top2)


def test_excluded_domains_have_zero_effect_on_ranking(
    baseline: tuple[Database, Path, list[KnowledgeEvidence], list[KnowledgeEvidence]],
) -> None:
    db, root, full, top2 = baseline
    for index in range(40):
        # Fully approved, current and external: excluded only because PRICING is not allowed.
        write(root, "PRICING_COMMERCIAL", f"pricing-{index}.md", markdown(meta(source_id=f"pricing-{index}", domain="PRICING_COMMERCIAL"), NOISE_BODY))
    ingest(db, root)
    assert fingerprint(results(db)) == fingerprint(full)
    assert fingerprint(results(db, top_k=2)) == fingerprint(top2)


def test_eligible_content_does_affect_ranking(
    baseline: tuple[Database, Path, list[KnowledgeEvidence], list[KnowledgeEvidence]],
) -> None:
    # Control: the same noise, when eligible, changes the corpus and so the results.
    db, root, full, _ = baseline
    write(root, "FAQ", "eligible-noise.md", markdown(meta(source_id="eligible-noise"), NOISE_BODY))
    ingest(db, root)
    assert fingerprint(results(db)) != fingerprint(full)
