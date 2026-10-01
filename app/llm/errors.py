"""Typed LLM boundary errors. Raw provider exceptions never cross the boundary: anything
else raised by a transport is wrapped in LLMProviderError. Every error is fail-closed:
callers get no output, never a fabricated fallback.

Every error carries a stable, provider-neutral ``code`` (never a provider payload, a
prompt or a key), so callers and operators see the same codes whichever provider runs."""

from enum import StrEnum


class LLMErrorCode(StrEnum):
    AUTH_INVALID = "AUTH_INVALID"  # the API key was refused (or lacks permission)
    RATE_LIMITED = "RATE_LIMITED"  # 429: try again in a later run, never in a loop
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"  # the account is out of credit/quota: no retry helps
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"  # the configured model does not exist (never replaced)
    BAD_REQUEST = "BAD_REQUEST"
    INPUT_TOO_LARGE = "INPUT_TOO_LARGE"
    CONTENT_BLOCKED = "CONTENT_BLOCKED"  # refused by the provider's or model's safety policy
    TEMPORARY_PROVIDER_ERROR = "TEMPORARY_PROVIDER_ERROR"  # 5xx / overloaded
    TIMEOUT = "TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"  # the provider's envelope was not what its API promises
    OUTPUT_TRUNCATED = "OUTPUT_TRUNCATED"  # the output limit was reached: the answer is incomplete
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"  # the model's JSON is not the requested contract
    CONTRACT_VIOLATION = "CONTRACT_VIOLATION"  # schema-valid, but breaks a rule (e.g. ungrounded)
    INTERNAL_ERROR = "INTERNAL_ERROR"


class LLMError(Exception):
    """Base class for all LLM boundary failures."""

    default_code = LLMErrorCode.INTERNAL_ERROR

    def __init__(self, message: str = "", *, code: LLMErrorCode | None = None) -> None:
        self.code = code or self.default_code
        super().__init__(message or self.code.value)


class LLMTimeoutError(LLMError):
    """The model did not answer in time."""

    default_code = LLMErrorCode.TIMEOUT


class LLMProviderError(LLMError):
    """The provider failed (or raised something unexpected)."""

    default_code = LLMErrorCode.TEMPORARY_PROVIDER_ERROR


class LLMStructuredOutputError(LLMError):
    """The output was not valid JSON for the requested schema. Never repaired."""

    default_code = LLMErrorCode.SCHEMA_VALIDATION_FAILED


class LLMContractViolationError(LLMError):
    """Schema-valid output that breaks a contract rule, e.g. an invented evidence ID or an
    upward knowledge-sufficiency opinion."""

    default_code = LLMErrorCode.CONTRACT_VIOLATION


class LLMNoScriptedResponseError(LLMError):
    """Fake adapter only: no scripted response was configured for the call."""
