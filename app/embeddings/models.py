"""Provider-neutral embedding request/result types.

An ``EmbeddingSpace`` is the identity of a vector space: the provider, the exact configured
model string and the requested dimensionality (None: the model's native size). Vectors from
different spaces are never compared, stored under the same identity, or mixed in one search.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints

from app.core.models.base import CoreModel


class EmbeddingPurpose(StrEnum):
    """What the texts are. Providers with asymmetric retrieval embeddings (Gemini task
    types) embed documents and queries differently; others ignore it."""

    DOCUMENT = "DOCUMENT"
    QUERY = "QUERY"


class EmbeddingSpace(CoreModel):
    provider: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,31}$")]
    model: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    dimensions: Annotated[int, Field(ge=1, le=8192)] | None = None

    @property
    def requested_dimensions(self) -> int:
        """The stored form of ``dimensions``: 0 means the model's native size."""
        return self.dimensions or 0


Vector = tuple[float, ...]


@dataclass(frozen=True)
class EmbeddingResult:
    """One validated batch, in input order. Every vector is finite, non-empty, of one
    dimensionality and L2-normalized (unit length), so cosine similarity is a dot product.
    Never carries the input texts."""

    vectors: tuple[Vector, ...]
    space: EmbeddingSpace
    dimensions: int
    request_id: str | None = None
    input_tokens: int | None = None

    def __repr__(self) -> str:  # never dump vectors into logs or errors
        return (f"EmbeddingResult(count={len(self.vectors)}, provider={self.space.provider!r}, "
                f"model={self.space.model!r}, dimensions={self.dimensions})")
