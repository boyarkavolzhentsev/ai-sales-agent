"""Vector validation, normalization, similarity and storage encoding. Pure Python.

Similarity convention (the only one used anywhere): **cosine similarity**. Every vector is
L2-normalized once, when it is validated at the provider boundary, so stored vectors and
query vectors are unit length and cosine similarity is their dot product (in [-1, 1]).

Validation never repairs: a non-number (including bool), NaN, infinity, an empty vector, a
zero vector, a wrong count or a dimensionality other than the expected one rejects the
whole batch.

Storage encoding: little-endian IEEE-754 float32, ``4 * dimensions`` bytes. Embedding
models produce float32-precision values, so nothing meaningful is lost; decoding checks the
length and that the vector is still finite and unit length (within float32 rounding).
Scores are rounded to ``SCORE_DECIMALS`` so equal inputs rank and serialize identically.
"""

import math
import sys
from array import array
from collections.abc import Sequence

from app.embeddings.errors import EmbeddingError, EmbeddingErrorCode
from app.embeddings.models import Vector

SCORE_DECIMALS = 6
_UNIT_TOLERANCE = 1e-3  # float32 storage rounding stays far inside this


def _reject(code: EmbeddingErrorCode) -> EmbeddingError:
    return EmbeddingError(code)


def normalize(values: Sequence[float]) -> Vector:
    norm = math.sqrt(math.sumprod(values, values))
    if not math.isfinite(norm) or norm == 0.0:
        raise _reject(EmbeddingErrorCode.INVALID_RESPONSE)
    return tuple(value / norm for value in values)


def validate_batch(raw: object, *, count: int, dimensions: int | None) -> tuple[Vector, ...]:
    """``raw``: the provider's vectors in input order. Returns normalized vectors."""
    if not isinstance(raw, (list, tuple)) or len(raw) != count or count == 0:
        raise _reject(EmbeddingErrorCode.INVALID_RESPONSE)
    vectors: list[Vector] = []
    size: int | None = None
    for item in raw:
        if not isinstance(item, (list, tuple)) or not item:
            raise _reject(EmbeddingErrorCode.INVALID_RESPONSE)
        if not all(type(value) in (int, float) for value in item):  # bool and strings are not numbers here
            raise _reject(EmbeddingErrorCode.INVALID_RESPONSE)
        if not all(math.isfinite(value) for value in item):
            raise _reject(EmbeddingErrorCode.INVALID_RESPONSE)
        if size is None:
            size = len(item)
        if len(item) != size or (dimensions is not None and len(item) != dimensions):
            raise _reject(EmbeddingErrorCode.DIMENSION_MISMATCH)
        vectors.append(normalize([float(value) for value in item]))
    return tuple(vectors)


def similarity(a: Vector, b: Vector) -> float:
    """Cosine similarity of two unit vectors, rounded. Mismatched sizes never compare."""
    if len(a) != len(b):
        raise _reject(EmbeddingErrorCode.DIMENSION_MISMATCH)
    return round(math.sumprod(a, b), SCORE_DECIMALS)


def encode(vector: Vector) -> bytes:
    data = array("f", vector)
    if sys.byteorder == "big":
        data.byteswap()
    return data.tobytes()


def decode(blob: bytes, dimensions: int) -> Vector:
    if dimensions < 1 or len(blob) != 4 * dimensions:
        raise _reject(EmbeddingErrorCode.DIMENSION_MISMATCH)
    data = array("f")
    data.frombytes(blob)
    if sys.byteorder == "big":
        data.byteswap()
    vector = tuple(data)
    norm2 = math.sumprod(vector, vector)  # NaN/inf anywhere makes this non-finite
    if not math.isfinite(norm2) or abs(norm2 - 1.0) > _UNIT_TOLERANCE:
        raise _reject(EmbeddingErrorCode.INVALID_RESPONSE)
    return vector
