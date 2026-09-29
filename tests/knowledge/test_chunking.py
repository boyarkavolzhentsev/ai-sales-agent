from app.knowledge import LoadedSource, chunk_source, parse_source_text
from app.knowledge.chunking import chunk_id_for, markdown_sections
from app.knowledge.loader import sha256_text
from tests.knowledge.sources import fact, markdown, meta, yaml_doc

BODY = """Intro paragraph before any heading.

# Pricing

Overview of plans.

## Basic plan
Basic costs 100 EUR per month.

Second paragraph for basic.

```
# not a heading inside a fence
```

## Team plan
Team costs 250 EUR per month.
"""


def loaded(body: str = BODY, **overrides: object) -> LoadedSource:
    return parse_source_text(markdown(meta(**overrides), body), extension=".md", label="a.md")


def test_sections_follow_heading_hierarchy_and_ignore_fenced_headings() -> None:
    sections = markdown_sections(BODY)
    assert [path for path, _ in sections] == [
        (),
        ("Pricing",),
        ("Pricing", "Basic plan"),
        ("Pricing", "Team plan"),
    ]
    basic = sections[2][1]
    assert basic[-1].startswith("```") and "# not a heading" in basic[-1]


def test_chunks_carry_heading_context() -> None:
    chunks, _ = chunk_source(loaded())
    texts = [c.text for c in chunks]
    assert texts[0].startswith("Sample source (fictional)\n\nIntro paragraph")  # title as context
    assert texts[1].startswith("Pricing\n\nOverview")
    assert texts[2].startswith("Pricing > Basic plan\n\nBasic costs 100 EUR per month.\n\nSecond paragraph")
    assert texts[3].startswith("Pricing > Team plan\n\n")


def test_chunking_is_deterministic_with_stable_ids_and_hashes() -> None:
    first, _ = chunk_source(loaded())
    second, _ = chunk_source(loaded())
    assert first == second
    for chunk in first:
        assert chunk.content_hash == sha256_text(chunk.text)
        assert chunk.chunk_id == chunk_id_for(chunk.source_id, chunk.source_version, chunk.ordinal, chunk.content_hash)
        assert chunk.chunk_id.startswith("kc_") and len(chunk.chunk_id) == 43
    assert [c.ordinal for c in first] == list(range(len(first)))


def test_content_change_changes_only_affected_hashes() -> None:
    before, _ = chunk_source(loaded())
    after, _ = chunk_source(loaded(BODY.replace("250 EUR", "260 EUR")))
    assert before[:3] == after[:3]
    assert before[3].content_hash != after[3].content_hash
    assert before[3].chunk_id != after[3].chunk_id


def test_version_changes_chunk_ids_but_not_hashes() -> None:
    v1, _ = chunk_source(loaded())
    v2, _ = chunk_source(loaded(version=2))
    assert [c.content_hash for c in v1] == [c.content_hash for c in v2]
    assert not {c.chunk_id for c in v1} & {c.chunk_id for c in v2}


def test_long_paragraphs_are_split_within_the_limit() -> None:
    sentence = "The sample widget exports reports every hour. "
    body = "# Long\n\n" + sentence * 120
    chunks, _ = chunk_source(loaded(body), max_chars=400)
    assert len(chunks) > 1
    assert all(len(c.text) <= 400 for c in chunks)
    assert all(c.text.startswith("Long\n\n") for c in chunks)
    joined = " ".join(c.text.removeprefix("Long\n\n") for c in chunks)
    assert joined.count("exports reports") == 120  # nothing lost, nothing duplicated


def test_unbroken_text_is_hard_split() -> None:
    chunks, _ = chunk_source(loaded("# X\n\n" + "a" * 1000), max_chars=300)
    assert all(len(c.text) <= 300 for c in chunks)
    assert "".join(c.text.removeprefix("X\n\n") for c in chunks) == "a" * 1000


def test_each_fact_gets_its_own_chunk_linked_to_the_fact_record() -> None:
    text = yaml_doc(
        meta(domain="PRICING_COMMERCIAL", title="Price list"),
        [fact("plan.basic.price", "100", "EUR", "Basic costs 100 EUR."), fact("plan.team.price", "250", "EUR")],
        "## Billing\nMonthly invoices.",
    )
    chunks, facts = chunk_source(parse_source_text(text, extension=".yaml", label="p.yaml"))
    assert [c.text for c in chunks] == [
        "Billing\n\nMonthly invoices.",
        "Price list\n\nBasic costs 100 EUR.\nplan.basic.price = 100 EUR",
        "Price list\n\nplan.team.price = 250 EUR",
    ]
    assert [(f.fact_key, f.chunk_id) for f in facts] == [
        ("plan.basic.price", chunks[1].chunk_id),
        ("plan.team.price", chunks[2].chunk_id),
    ]
