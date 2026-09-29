"""Knowledge base: loading, validation, chunking, local FTS5 indexing, deterministic
retrieval and the deterministic knowledge gate. No LLM, embeddings or network access."""

from app.knowledge.chunking import MAX_CHUNK_CHARS, chunk_source
from app.knowledge.errors import (
    DuplicateSourceVersionError,
    KnowledgeError,
    SourceFormatError,
    SourceValidationError,
)
from app.knowledge.gate import assess
from app.knowledge.ingestion import ingest_directory, ingest_loaded, load_directory
from app.knowledge.loader import SUPPORTED_EXTENSIONS, load_source_file, parse_source_text
from app.knowledge.metadata import (
    DOMAIN_DIRECTORIES,
    SourceMetadata,
    classify_source,
    is_source_current,
    select_sources,
)
from app.knowledge.models import (
    DiagnosticHit,
    Fact,
    IngestResult,
    IngestStatus,
    KnowledgeResult,
    LoadedSource,
    SourceUsability,
)
from app.knowledge.retrieval import retrieve
from app.knowledge.service import evaluate_knowledge

__all__ = [
    "DOMAIN_DIRECTORIES",
    "MAX_CHUNK_CHARS",
    "SUPPORTED_EXTENSIONS",
    "DiagnosticHit",
    "DuplicateSourceVersionError",
    "Fact",
    "IngestResult",
    "IngestStatus",
    "KnowledgeError",
    "KnowledgeResult",
    "LoadedSource",
    "SourceFormatError",
    "SourceMetadata",
    "SourceUsability",
    "SourceValidationError",
    "assess",
    "chunk_source",
    "classify_source",
    "evaluate_knowledge",
    "ingest_directory",
    "ingest_loaded",
    "is_source_current",
    "load_directory",
    "load_source_file",
    "parse_source_text",
    "retrieve",
    "select_sources",
]
