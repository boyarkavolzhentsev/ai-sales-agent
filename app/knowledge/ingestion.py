"""Ingest validated knowledge sources into the local index. No LLM, no summarization,
no automatic approval: metadata is taken exactly as authored and validated.

Fail closed: a directory is fully loaded and validated before anything is written, and
all writes happen in the caller's transaction, so any error leaves the index unchanged.
Re-ingesting an identical source version is a no-op; the same source_id + version with
different content is rejected, because historical versions are immutable.
"""

from collections.abc import Collection
from datetime import datetime
from pathlib import Path

from app.core.enums import KnowledgeDomain
from app.knowledge.chunking import chunk_source
from app.knowledge.errors import DuplicateSourceVersionError, SourceFormatError, SourceValidationError
from app.knowledge.loader import load_source_file
from app.knowledge.metadata import DOMAIN_DIRECTORIES
from app.knowledge.models import IngestResult, IngestStatus, LoadedSource
from app.persistence import UnitOfWork

_DOMAIN_BY_DIRECTORY = {directory: domain for domain, directory in DOMAIN_DIRECTORIES.items()}
_IGNORED_NAMES = frozenset({".gitkeep", "README.md"})


def ingest_loaded(
    uow: UnitOfWork,
    loaded: LoadedSource,
    *,
    now: datetime,
    known_source_ids: Collection[str] = (),
) -> IngestResult:
    """Persist one source version with its chunks, facts and search-index rows."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    source = loaded.source
    if source.approved_at is not None and source.approved_at > now:
        raise SourceValidationError(f"{source.path}: approved_at is in the future")

    existing = uow.knowledge_sources.get(source.source_id, source.version)
    if existing is not None:
        if existing.content_hash == source.content_hash:
            chunk_count = len(uow.knowledge_index.list_chunks(source.source_id, source.version))
            return IngestResult(status=IngestStatus.UNCHANGED, source=existing, chunk_count=chunk_count)
        raise DuplicateSourceVersionError(
            f"{source.path}: {source.source_id} v{source.version} already exists with different content"
        )

    if source.supersedes is not None and source.supersedes not in known_source_ids:
        if uow.knowledge_sources.get_latest(source.supersedes) is None:
            raise SourceValidationError(
                f"{source.path}: supersedes unknown source {source.supersedes!r}"
            )

    chunks, facts = chunk_source(loaded)
    uow.knowledge_sources.add(source)
    for chunk in chunks:
        uow.knowledge_index.add_chunk(chunk)
    for fact in facts:
        uow.knowledge_index.add_fact(fact)
    return IngestResult(status=IngestStatus.INGESTED, source=source, chunk_count=len(chunks))


def load_directory(root: Path) -> list[LoadedSource]:
    """Load and validate every source under ``root`` (a knowledge_base-style tree).

    Every file must live under a domain folder (e.g. ``pricing/``) that matches its
    declared domain. ``README.md`` and ``.gitkeep`` files and hidden files are ignored.
    Raises on the first invalid file; returns sources sorted by relative path.
    """
    loaded: list[LoadedSource] = []
    seen: dict[tuple[str, int], str] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root)
        if path.name in _IGNORED_NAMES or any(part.startswith(".") for part in relative.parts):
            continue
        label = relative.as_posix()
        if len(relative.parts) < 2:
            raise SourceFormatError(f"{label}: sources must be inside a domain folder")
        folder_domain = _DOMAIN_BY_DIRECTORY.get(relative.parts[0])
        if folder_domain is None:
            raise SourceFormatError(f"{label}: unknown domain folder {relative.parts[0]!r}")
        source = load_source_file(path, label=label)
        _check_folder(source, folder_domain, label)
        key = (source.source.source_id, source.source.version)
        if key in seen:
            raise DuplicateSourceVersionError(
                f"{label}: {key[0]} v{key[1]} is also defined in {seen[key]}"
            )
        seen[key] = label
        loaded.append(source)
    return loaded


def _check_folder(source: LoadedSource, folder_domain: KnowledgeDomain, label: str) -> None:
    if source.source.domain is not folder_domain:
        raise SourceValidationError(
            f"{label}: declared domain {source.source.domain} does not match folder "
            f"{DOMAIN_DIRECTORIES[folder_domain]!r}"
        )


def ingest_directory(uow: UnitOfWork, root: Path, *, now: datetime) -> list[IngestResult]:
    """Validate the whole tree first, then ingest it inside the caller's transaction."""
    sources = load_directory(root)
    known = {s.source.source_id for s in sources}
    return [ingest_loaded(uow, source, now=now, known_source_ids=known) for source in sources]
