"""Provider-neutral embeddings contract (Stage 19): the ``EmbeddingTransport`` protocol, the
normalized result, stable error codes and the vector rules (validation, cosine similarity,
float32 storage encoding). No provider, network or persistence code: provider adapters live
in ``app.integrations.embeddings`` and are wired only by the runtime."""

from app.embeddings.errors import EmbeddingError, EmbeddingErrorCode
from app.embeddings.models import EmbeddingPurpose, EmbeddingResult, EmbeddingSpace, Vector
from app.embeddings.protocols import EmbeddingTransport
from app.embeddings.vectors import SCORE_DECIMALS, decode, encode, normalize, similarity, validate_batch

__all__ = [
    "SCORE_DECIMALS",
    "EmbeddingError",
    "EmbeddingErrorCode",
    "EmbeddingPurpose",
    "EmbeddingResult",
    "EmbeddingSpace",
    "EmbeddingTransport",
    "Vector",
    "decode",
    "encode",
    "normalize",
    "similarity",
    "validate_batch",
]
