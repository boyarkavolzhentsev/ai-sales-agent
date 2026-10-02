"""Vector rules: validation without repair, cosine via unit vectors, float32 storage
round-trip that keeps rankings stable."""

import math
import random
import struct

import pytest

from app.embeddings import EmbeddingError, EmbeddingErrorCode, decode, encode, normalize, similarity, validate_batch


def codes(raw: object, *, count: int = 1, dimensions: int | None = None) -> EmbeddingErrorCode:
    with pytest.raises(EmbeddingError) as error:
        validate_batch(raw, count=count, dimensions=dimensions)
    return error.value.code


@pytest.mark.parametrize("raw", [
    [[1.0, float("nan")]], [[1.0, float("inf")]], [[float("-inf"), 0.0]], [[]], [[1.0, "2"]], [[True, 0.5]],
    [[None, 1.0]], [["a"]], [{"values": [1.0]}], ["1.0"], [[0.0, 0.0]], None, {}, [],
])
def test_bad_vectors_are_rejected_never_repaired(raw: object) -> None:
    assert codes(raw) is EmbeddingErrorCode.INVALID_RESPONSE


def test_mixed_and_unexpected_dimensions_are_rejected() -> None:
    assert codes([[1.0, 2.0], [1.0, 2.0, 3.0]], count=2) is EmbeddingErrorCode.DIMENSION_MISMATCH
    assert codes([[1.0, 2.0]], dimensions=3) is EmbeddingErrorCode.DIMENSION_MISMATCH
    assert codes([[1.0, 2.0]], count=2) is EmbeddingErrorCode.INVALID_RESPONSE  # wrong count


def test_vectors_are_normalized_to_unit_length_and_cosine_is_their_dot_product() -> None:
    [a, b] = validate_batch([[3, 4], [4, 3]], count=2, dimensions=2)
    assert a == (0.6, 0.8) and math.isclose(math.hypot(*b), 1.0)
    assert similarity(a, b) == round(0.6 * 0.8 + 0.8 * 0.6, 6) == 0.96
    assert similarity(a, a) == 1.0 and similarity(a, (-0.6, -0.8)) == -1.0
    assert normalize([0, 5]) == (0.0, 1.0)
    with pytest.raises(EmbeddingError) as error:
        similarity(a, (1.0, 0.0, 0.0))  # different sizes never compare
    assert error.value.code is EmbeddingErrorCode.DIMENSION_MISMATCH


def test_float32_storage_round_trip_is_exact_little_endian_and_keeps_rankings() -> None:
    rng = random.Random(19)
    query = normalize([rng.uniform(-1, 1) for _ in range(256)])
    stored = [normalize([rng.uniform(-1, 1) for _ in range(256)]) for _ in range(300)]
    blobs = [encode(v) for v in stored]
    assert all(len(b) == 4 * 256 for b in blobs)
    assert blobs[0][:4] == struct.pack("<f", stored[0][0])  # explicit little-endian float32
    decoded = [decode(b, 256) for b in blobs]
    assert decoded == [decode(encode(v), 256) for v in decoded]  # float32 values survive exactly
    assert max(abs(x - y) for v, d in zip(stored, decoded, strict=True) for x, y in zip(v, d, strict=True)) < 1e-7

    def ranking(vectors: list[tuple[float, ...]]) -> list[int]:
        return sorted(range(len(vectors)), key=lambda i: (-similarity(query, vectors[i]), i))

    assert ranking(decoded)[:20] == ranking([tuple(v) for v in stored])[:20]


@pytest.mark.parametrize("blob,dims", [(b"\x00" * 8, 3), (b"", 0), (encode((1.0, 0.0)), 3)])
def test_decode_refuses_a_wrong_length(blob: bytes, dims: int) -> None:
    with pytest.raises(EmbeddingError) as error:
        decode(blob, dims)
    assert error.value.code is EmbeddingErrorCode.DIMENSION_MISMATCH


@pytest.mark.parametrize("vector", [(1.0, 1.0), (float("nan"), 0.0), (0.0, 0.0)])
def test_decode_refuses_a_corrupt_or_unnormalized_vector(vector: tuple[float, float]) -> None:
    with pytest.raises(EmbeddingError) as error:
        decode(encode(vector), 2)
    assert error.value.code is EmbeddingErrorCode.INVALID_RESPONSE


def test_scores_are_rounded_so_equal_inputs_serialize_identically() -> None:
    a = normalize([1.0, 2.0, 3.0])
    b = normalize([3.0, 2.0, 1.0])
    assert similarity(a, b) == similarity(b, a) and len(repr(similarity(a, b)).split(".")[1]) <= 6
