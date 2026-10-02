from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from app.embeddings.models import EmbeddingPurpose, EmbeddingResult, EmbeddingSpace


@runtime_checkable
class EmbeddingTransport(Protocol):
    """Embeds texts in one vector space. Implementations make at most one bounded provider
    request per call (plus at most one safe retry), validate and normalize every vector
    (``app.embeddings.vectors.validate_batch``) and raise ``EmbeddingError`` on any failure.
    They own no business logic: no prompts, no query building, no retrieval."""

    @property
    def space(self) -> EmbeddingSpace: ...

    def embed(self, texts: Sequence[str], purpose: EmbeddingPurpose) -> EmbeddingResult: ...
