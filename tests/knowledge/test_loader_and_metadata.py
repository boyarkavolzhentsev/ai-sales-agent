from datetime import timedelta
from pathlib import Path

import pytest

from app.core.enums import KnowledgeDomain
from app.core.models import KnowledgeSource
from app.knowledge import (
    SourceFormatError,
    SourceUsability,
    SourceValidationError,
    classify_source,
    is_source_current,
    load_source_file,
    parse_source_text,
    select_sources,
)
from app.knowledge.loader import sha256_text
from tests.knowledge.sources import NOW, fact, json_doc, markdown, meta, yaml_doc

# ---- A. Loader -----------------------------------------------------------------------


def test_markdown_with_front_matter() -> None:
    loaded = parse_source_text(markdown(meta(), "# Hello\n\nWorld.\n"), extension=".md", label="faq/a.md")
    source = loaded.source
    assert (source.source_id, source.domain, source.version) == ("src-sample", KnowledgeDomain.FAQ, 1)
    assert (source.path, source.locale, source.tags) == ("faq/a.md", "en", ("sample",))
    assert loaded.body == "# Hello\n\nWorld.\n"
    assert loaded.facts == ()


def test_yaml_fact_document() -> None:
    text = yaml_doc(meta(domain="PRICING_COMMERCIAL"), [fact("plan.basic.price", "100", "EUR", "Basic is 100 EUR.")], "## Notes\nMonthly.")
    loaded = parse_source_text(text, extension=".yaml", label="pricing/p.yaml")
    assert loaded.source.domain is KnowledgeDomain.PRICING_COMMERCIAL
    assert [(f.key, f.value, f.unit) for f in loaded.facts] == [("plan.basic.price", "100", "EUR")]
    assert loaded.body.startswith("## Notes")


def test_json_fact_document() -> None:
    loaded = parse_source_text(json_doc(meta(), [fact("support.hours", "9-17")]), extension=".json", label="faq/f.json")
    assert loaded.facts[0].key == "support.hours"
    assert loaded.body == ""


def test_optional_metadata_becomes_namespaced_tags() -> None:
    loaded = parse_source_text(
        markdown(meta(product="widget", industry="retail", region="EU", source_ref="doc-7")), extension=".md", label="x.md"
    )
    assert loaded.source.tags == ("sample", "product:widget", "industry:retail", "region:EU", "ref:doc-7")


def test_content_hash_is_deterministic_and_newline_insensitive() -> None:
    text = markdown(meta())
    unix = parse_source_text(text, extension=".md", label="a.md").source.content_hash
    windows = parse_source_text(text.replace("\n", "\r\n"), extension=".md", label="a.md").source.content_hash
    assert unix == windows == sha256_text(text)
    changed = parse_source_text(text.replace("Sample body", "Other body"), extension=".md", label="a.md")
    assert changed.source.content_hash != unix


@pytest.mark.parametrize(
    ("text", "extension"),
    [
        ("# No front matter\n", ".md"),
        ("---\nsource_id: x\n# never closed\n", ".md"),
        ("---\n: : bad yaml [\n---\nbody", ".md"),
        ("---\n- a list\n---\nbody", ".md"),
        ("metadata: {source_id: a, source_id: b}\n", ".yaml"),
        ("[1, 2]", ".yaml"),
        ("metadata: {}\nextra: 1\n", ".yaml"),
        ("facts: []\n", ".yaml"),
        ('{"metadata": {}, "metadata": {}}', ".json"),
        ('{"metadata": {"version": NaN}}', ".json"),
        ("{not json", ".json"),
        ('{"metadata": {}, "body": 5}', ".json"),
    ],
)
def test_malformed_sources_rejected(text: str, extension: str) -> None:
    with pytest.raises(SourceFormatError):
        parse_source_text(text, extension=extension, label="bad")


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_id": "   "},
        {"source_id": "has space"},
        {"domain": "WEATHER"},
        {"domain": "faq"},  # enum values are case-sensitive
        {"version": 0},
        {"version": "1"},  # no silent coercion
        {"version": 1.0},
        {"approval_status": "APPROVED", "approved_by": None, "approved_at": None},
        {"approval_status": "DRAFT"},  # draft with approval metadata
        {"approved_at": None},  # approved_by without approved_at
        {"review_by": "2025-12-31T00:00:00+00:00"},  # precedes effective_from
        {"review_by": "2026-01-01T00:00:00+00:00"},  # equals effective_from
        {"effective_from": "2026-01-01T00:00:00"},  # naive
        {"effective_from": "2026-01-01"},  # date without time/offset
        {"effective_from": None},
        {"review_by": None},
        {"tags": None},
        {"tags": ["a", "a"]},
        {"locale": False},  # YAML "no" style booleans are not locales
        {"unexpected": "key"},
    ],
)
def test_invalid_metadata_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(SourceValidationError):
        parse_source_text(markdown(meta(**overrides)), extension=".md", label="bad.md")


def test_unquoted_yaml_timestamps_are_validated_not_guessed() -> None:
    text = markdown(meta()).replace("'2026-01-01T00:00:00+00:00'", "2026-01-01T00:00:00+00:00")
    assert parse_source_text(text, extension=".md", label="a.md").source.effective_from is not None
    naive = markdown(meta()).replace("effective_from: '2026-01-01T00:00:00+00:00'", "effective_from: 2026-01-01")
    with pytest.raises(SourceValidationError):
        parse_source_text(naive, extension=".md", label="a.md")


@pytest.mark.parametrize(
    ("text", "extension"),
    [
        (markdown(meta(), "   \n\n"), ".md"),
        (yaml_doc(meta(), [], "  "), ".yaml"),
        (json_doc(meta()), ".json"),
    ],
)
def test_empty_content_rejected(text: str, extension: str) -> None:
    with pytest.raises(SourceValidationError, match="no content"):
        parse_source_text(text, extension=extension, label="empty")


@pytest.mark.parametrize(
    "facts",
    [
        [fact("Plan Price", "1")],  # invalid key
        [fact("a.b", "1"), fact("a.b", "2")],  # duplicate key
        [{"key": "a.b", "value": 100}],  # values must be strings
        [{"key": "a.b", "value": "  "}],
        [{"key": "a.b", "value": "1", "extra": "x"}],
    ],
)
def test_invalid_facts_rejected(facts: list[dict[str, object]]) -> None:
    with pytest.raises(SourceValidationError):
        parse_source_text(yaml_doc(meta(), facts), extension=".yaml", label="f.yaml")  # type: ignore[arg-type]


def test_unsupported_extension_and_bad_encoding(tmp_path: Path) -> None:
    for name in ("notes.txt", "sheet.csv", "noext"):
        (tmp_path / name).write_text("x", encoding="utf-8")
        with pytest.raises(SourceFormatError, match="unsupported"):
            load_source_file(tmp_path / name)
    (tmp_path / "latin.md").write_bytes(b"---\ntitle: caf\xe9\n---\n")
    with pytest.raises(SourceFormatError, match="UTF-8"):
        load_source_file(tmp_path / "latin.md")


def test_load_source_file_uses_label(tmp_path: Path) -> None:
    path = tmp_path / "a.md"
    path.write_text(markdown(meta()), encoding="utf-8")
    assert load_source_file(path, label="faq/a.md").source.path == "faq/a.md"


# ---- B. Metadata / usability ----------------------------------------------------------


def source(**overrides: object) -> KnowledgeSource:
    return parse_source_text(markdown(meta(**overrides)), extension=".md", label="x.md").source


def test_approved_current_external_source_is_usable() -> None:
    assert classify_source(source(), NOW, "en") is SourceUsability.USABLE
    assert classify_source(source(locale="en-GB"), NOW, "en") is SourceUsability.USABLE


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"effective_from": "2026-07-01T00:00:00+00:00"}, SourceUsability.NOT_YET_EFFECTIVE),
        ({"review_by": "2026-05-31T00:00:00+00:00"}, SourceUsability.STALE),
        ({"external_use": "INTERNAL_ONLY"}, SourceUsability.INTERNAL_ONLY),
        ({"approval_status": "DRAFT", "approved_by": None, "approved_at": None}, SourceUsability.NOT_APPROVED),
        ({"approval_status": "RETIRED"}, SourceUsability.NOT_APPROVED),
        ({"locale": "uk"}, SourceUsability.LOCALE_MISMATCH),
    ],
)
def test_unusable_sources(overrides: dict[str, object], expected: SourceUsability) -> None:
    assert classify_source(source(**overrides), NOW, "en") is expected


def test_is_source_current_boundaries() -> None:
    s = source()
    assert s.effective_from is not None and s.review_by is not None
    assert is_source_current(s, s.effective_from)
    assert is_source_current(s, s.review_by)  # review_by is inclusive
    assert not is_source_current(s, s.review_by + timedelta(microseconds=1))
    assert not is_source_current(s, s.effective_from - timedelta(microseconds=1))
    with pytest.raises(ValueError):
        is_source_current(s, NOW.replace(tzinfo=None))


# ---- D. Versions and supersession (selection rules) --------------------------------------


def test_newest_usable_version_wins_and_older_is_superseded() -> None:
    v1, v2 = source(), source(version=2)
    draft_v3 = source(version=3, approval_status="DRAFT", approved_by=None, approved_at=None)
    selection = select_sources([v1, draft_v3, v2], NOW, "en")
    assert selection.usable == (v2,)
    assert dict((s.version, u) for s, u in selection.excluded) == {
        3: SourceUsability.NOT_APPROVED,
        1: SourceUsability.SUPERSEDED,
    }


def test_newer_version_not_yet_effective_keeps_older_in_use() -> None:
    v1 = source()
    future_v2 = source(version=2, effective_from="2026-09-01T00:00:00+00:00")
    assert select_sources([v1, future_v2], NOW, "en").usable == (v1,)


def test_retired_newest_version_withdraws_the_source() -> None:
    selection = select_sources([source(), source(version=2, approval_status="RETIRED")], NOW, "en")
    assert selection.usable == ()
    assert {u for _, u in selection.excluded} == {SourceUsability.WITHDRAWN}


def test_cross_source_supersedes() -> None:
    old = source(source_id="old-faq")
    new = source(source_id="new-faq", supersedes="old-faq")
    selection = select_sources([old, new], NOW, "en")
    assert selection.usable == (new,)
    assert selection.excluded == ((old, SourceUsability.SUPERSEDED),)
    draft_new = source(source_id="new-faq", supersedes="old-faq", approval_status="DRAFT", approved_by=None, approved_at=None)
    assert select_sources([old, draft_new], NOW, "en").usable == (old,)


def test_selection_is_order_independent() -> None:
    items = [source(source_id="b"), source(source_id="a", version=2), source(source_id="a")]
    assert select_sources(items, NOW, "en") == select_sources(list(reversed(items)), NOW, "en")
    assert [s.source_id for s in select_sources(items, NOW, "en").usable] == ["a", "b"]
