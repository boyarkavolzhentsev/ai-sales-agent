"""Typed embeddings boundary errors. Raw provider exceptions never cross the boundary.

Every error carries a stable, provider-neutral ``code`` (never a provider payload, an input
text, a vector or a key), so callers and operators see the same codes whichever provider
runs. Every error is fail-closed: callers get no vectors, never a fabricated fallback."""

from enum import StrEnum


class EmbeddingErrorCode(StrEnum):
    AUTH_INVALID = "AUTH_INVALID"  # the API key was refused (or lacks permission)
    RATE_LIMITED = "RATE_LIMITED"  # 429: a later run may try again, never a loop
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"  # out of credit/quota: no retry helps
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"  # the configured model does not exist (never replaced)
    BAD_REQUEST = "BAD_REQUEST"
    INPUT_TOO_LARGE = "INPUT_TOO_LARGE"  # refused locally or by the provider; never truncated
    TEMPORARY_PROVIDER_ERROR = "TEMPORARY_PROVIDER_ERROR"  # 5xx / overloaded
    TIMEOUT = "TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"  # malformed envelope, wrong count, NaN/inf, empty or non-numeric vector
    DIMENSION_MISMATCH = "DIMENSION_MISMATCH"  # not the expected dimensionality, or mixed within a batch / index
    INTERNAL_ERROR = "INTERNAL_ERROR"


class EmbeddingError(Exception):
    """Every embeddings boundary failure. The message is the code only."""

    def __init__(self, code: EmbeddingErrorCode) -> None:
        self.code = code
        super().__init__(code.value)
