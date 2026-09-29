class KnowledgeError(Exception):
    """Base class for knowledge-base failures. Ingestion fails closed on any of these."""


class SourceFormatError(KnowledgeError):
    """Unsupported file type, bad encoding, or malformed front matter / YAML / JSON."""


class SourceValidationError(KnowledgeError):
    """Well-formed file whose metadata or content breaks the knowledge-source rules."""


class DuplicateSourceVersionError(KnowledgeError):
    """The same source_id + version exists twice with different content."""
