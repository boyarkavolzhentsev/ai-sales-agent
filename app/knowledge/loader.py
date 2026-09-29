"""Parse knowledge source files. Fails closed: nothing malformed is repaired.

Formats:
- ``.md``: Markdown body with a mandatory YAML front matter block (``---`` ... ``---``).
- ``.yaml`` / ``.yml`` / ``.json``: a mapping with ``metadata`` (required), ``facts``
  (optional list of {key, value, unit?, statement?}) and ``body`` (optional Markdown).

The source content hash is SHA-256 over the UTF-8 text with line endings normalized to
"\\n", so the same document hashes identically on every platform.
"""

import hashlib
import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.knowledge.errors import SourceFormatError, SourceValidationError
from app.knowledge.metadata import build_knowledge_source
from app.knowledge.models import Fact, LoadedSource

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".md", ".yaml", ".yml", ".json"})
_DOCUMENT_KEYS = frozenset({"metadata", "facts", "body"})
_FRONT_MATTER_FENCE = "---"


class _StrictSafeLoader(yaml.SafeLoader):
    """SafeLoader that keeps timestamps as strings (so offsets are validated explicitly,
    never guessed) and rejects duplicate mapping keys."""


_StrictSafeLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_mapping(loader: _StrictSafeLoader, node: yaml.MappingNode) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_StrictSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def load_source_file(path: Path, *, label: str | None = None) -> LoadedSource:
    """Load one file. ``label`` is the path recorded in KnowledgeSource.path (defaults to
    the file path as given)."""
    display = label if label is not None else path.as_posix()
    extension = path.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise SourceFormatError(f"{display}: unsupported file type {path.suffix or '(none)'}")
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceFormatError(f"{display}: not valid UTF-8") from exc
    return parse_source_text(text, extension=extension, label=display)


def parse_source_text(text: str, *, extension: str, label: str) -> LoadedSource:
    text = normalize_newlines(text)
    if extension == ".md":
        metadata, body = _split_front_matter(text, label)
        facts: object = []
    elif extension in (".yaml", ".yml"):
        metadata, facts, body = _split_document(_parse_yaml(text, label), label)
    elif extension == ".json":
        metadata, facts, body = _split_document(_parse_json(text, label), label)
    else:
        raise SourceFormatError(f"{label}: unsupported file type {extension}")
    if not isinstance(metadata, dict):
        raise SourceFormatError(f"{label}: metadata must be a mapping")

    source = build_knowledge_source(metadata, path=label, content_hash=sha256_text(text))
    try:
        if not isinstance(facts, list):
            raise SourceValidationError(f"{label}: 'facts' must be a list")
        return LoadedSource(
            source=source,
            body=body,
            facts=tuple(Fact.model_validate(fact) for fact in facts),
        )
    except ValidationError as exc:
        raise SourceValidationError(f"{label}: invalid content: {exc}") from exc


def _split_front_matter(text: str, label: str) -> tuple[object, str]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != _FRONT_MATTER_FENCE:
        raise SourceFormatError(f"{label}: Markdown source must start with a '---' front matter block")
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONT_MATTER_FENCE:
            front_matter = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :])
            return _parse_yaml(front_matter, label), body
    raise SourceFormatError(f"{label}: front matter block is not closed with '---'")


def _parse_yaml(text: str, label: str) -> object:
    try:
        return yaml.load(text, Loader=_StrictSafeLoader)  # noqa: S506 - strict SafeLoader subclass
    except yaml.YAMLError as exc:
        raise SourceFormatError(f"{label}: malformed YAML: {exc}") from exc


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SourceFormatError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> object:
    raise SourceFormatError(f"non-standard JSON constant {name}")


def _parse_json(text: str, label: str) -> object:
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    except SourceFormatError as exc:
        raise SourceFormatError(f"{label}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SourceFormatError(f"{label}: malformed JSON: {exc}") from exc


def _split_document(document: object, label: str) -> tuple[object, object, str]:
    if not isinstance(document, dict):
        raise SourceFormatError(f"{label}: a fact document must be a mapping")
    unknown = set(document) - _DOCUMENT_KEYS
    if unknown:
        raise SourceFormatError(f"{label}: unknown top-level keys {sorted(map(str, unknown))}")
    if "metadata" not in document:
        raise SourceFormatError(f"{label}: missing 'metadata'")
    body = document.get("body", "")
    if not isinstance(body, str):
        raise SourceFormatError(f"{label}: 'body' must be a string")
    return document["metadata"], document.get("facts", []), body
