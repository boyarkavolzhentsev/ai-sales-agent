"""Knowledge base: loading, validation, chunking, local FTS5 indexing, deterministic
retrieval and the deterministic knowledge gate (Stage 4); semantic retrieval over stored
embeddings and the incremental embedding indexer (Stage 19), through the provider-neutral
``app.embeddings`` contract only. No LLM, provider or network code here."""

from app.knowledge.chunking import MAX_CHUNK_CHARS, chunk_source
from app.knowledge.errors import (
    DuplicateSourceVersionError,
    KnowledgeError,
    SourceFormatError,
    SourceValidationError,
)
from app.knowledge.gate import assess
from app.knowledge.indexing import IndexReport, IndexStatus, KnowledgeIndexer
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
    RetrievalInfo,
    RetrievalMethod,
    SourceUsability,
)
from app.knowledge.retrieval import retrieve
from app.knowledge.retriever import (
    DEFAULT_MIN_SIMILARITY,
    KnowledgeRetrievalError,
    KnowledgeRetriever,
    LexicalRetriever,
    SemanticRetriever,
)
from app.knowledge.semantic import SemanticSearch
from app.knowledge.service import evaluate_knowledge

__all__ = [
    "DEFAULT_MIN_SIMILARITY",
    "DOMAIN_DIRECTORIES",
    "MAX_CHUNK_CHARS",
    "SUPPORTED_EXTENSIONS",
    "DiagnosticHit",
    "DuplicateSourceVersionError",
    "Fact",
    "IngestResult",
    "IndexReport",
    "IndexStatus",
    "IngestStatus",
    "KnowledgeError",
    "KnowledgeIndexer",
    "KnowledgeResult",
    "KnowledgeRetrievalError",
    "KnowledgeRetriever",
    "LexicalRetriever",
    "LoadedSource",
    "RetrievalInfo",
    "RetrievalMethod",
    "SemanticRetriever",
    "SemanticSearch",
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
