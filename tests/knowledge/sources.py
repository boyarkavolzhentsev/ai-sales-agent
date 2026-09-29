"""Builders for fictional knowledge source files used in tests."""

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml

from app.core.enums import KnowledgeDomain, KnowledgePurpose
from app.core.models import KnowledgeQuery
from app.knowledge.metadata import DOMAIN_DIRECTORIES

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge_base"


def meta(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "source_id": "src-sample",
        "domain": "FAQ",
        "title": "Sample source (fictional)",
        "version": 1,
        "approval_status": "APPROVED",
        "external_use": "EXTERNAL_OK",
        "approved_by": "sample-approver",
        "approved_at": "2026-01-01T00:00:00+00:00",
        "effective_from": "2026-01-01T00:00:00+00:00",
        "review_by": "2026-12-31T00:00:00+00:00",
        "locale": "en",
        "tags": ["sample"],
    }
    merged = base | overrides
    return {key: value for key, value in merged.items() if value is not None}


def markdown(metadata: dict[str, object], body: str = "# Sample\n\nSample body text.\n") -> str:
    front = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True)
    return f"---\n{front}---\n{body}"


def yaml_doc(metadata: dict[str, object], facts: list[dict[str, str]] | None = None, body: str | None = None) -> str:
    document: dict[str, object] = {"metadata": metadata}
    if facts is not None:
        document["facts"] = facts
    if body is not None:
        document["body"] = body
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


def json_doc(metadata: dict[str, object], facts: list[dict[str, str]] | None = None, body: str | None = None) -> str:
    document: dict[str, object] = {"metadata": metadata}
    if facts is not None:
        document["facts"] = facts
    if body is not None:
        document["body"] = body
    return json.dumps(document, indent=2)


def write(root: Path, domain: str, name: str, text: str) -> Path:
    folder = root / DOMAIN_DIRECTORIES[KnowledgeDomain(domain)]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def fact(key: str, value: str, unit: str | None = None, statement: str | None = None) -> dict[str, str]:
    item = {"key": key, "value": value}
    if unit is not None:
        item["unit"] = unit
    if statement is not None:
        item["statement"] = statement
    return item


def query(
    *questions: str,
    allowed: tuple[KnowledgeDomain, ...] = tuple(KnowledgeDomain),
    required: tuple[KnowledgeDomain, ...] = (),
    locale: str = "en",
    top_k: int = 5,
    query_id: str = "q-1",
) -> KnowledgeQuery:
    return KnowledgeQuery(
        query_id=query_id,
        purpose=KnowledgePurpose.INBOUND_REPLY,
        questions=questions,
        allowed_domains=allowed,
        required_domains=required,
        locale=locale,
        top_k=top_k,
        correlation_id="corr-1",
    )
