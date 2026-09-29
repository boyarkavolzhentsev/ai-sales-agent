"""Typed LLM boundary errors. Raw provider exceptions never cross the boundary: anything
else raised by a transport is wrapped in LLMProviderError. Every error is fail-closed:
callers get no output, never a fabricated fallback."""


class LLMError(Exception):
    """Base class for all LLM boundary failures."""


class LLMTimeoutError(LLMError):
    """The model did not answer in time."""


class LLMProviderError(LLMError):
    """The provider failed (or raised something unexpected)."""


class LLMStructuredOutputError(LLMError):
    """The output was not valid JSON for the requested schema. Never repaired."""


class LLMContractViolationError(LLMError):
    """Schema-valid output that breaks a contract rule, e.g. an invented evidence ID or an
    upward knowledge-sufficiency opinion."""


class LLMNoScriptedResponseError(LLMError):
    """Fake adapter only: no scripted response was configured for the call."""
