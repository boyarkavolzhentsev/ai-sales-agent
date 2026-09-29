"""The authoring guide's example documents must stay valid under the real loader."""

import re
from pathlib import Path

from app.core.enums import KnowledgeDomain
from app.knowledge import DOMAIN_DIRECTORIES, parse_source_text

README = Path(__file__).resolve().parents[2] / "knowledge_base" / "README.md"


def _block(language: str) -> str:
    match = re.search(rf"```{language}\n(.*?)```", README.read_text(encoding="utf-8"), re.S)
    assert match is not None, f"no {language} example in the guide"
    return match.group(1)


def test_markdown_example_is_valid() -> None:
    loaded = parse_source_text(_block("markdown"), extension=".md", label="faq/example.md")
    assert loaded.source.domain is KnowledgeDomain.FAQ


def test_fact_document_example_is_valid() -> None:
    loaded = parse_source_text(_block("yaml"), extension=".yaml", label="pricing/example.yaml")
    assert [f.key for f in loaded.facts] == ["plan.basic.monthly_price"]


def test_guide_lists_every_domain_with_its_folder() -> None:
    text = README.read_text(encoding="utf-8")
    for domain, folder in DOMAIN_DIRECTORIES.items():
        assert f"`{domain.value}` | `{folder}/`" in text
