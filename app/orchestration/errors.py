class OrchestrationError(Exception):
    """Base class of execution-coordinator errors."""


class OrchestrationNotFoundError(OrchestrationError):
    """The lead does not exist."""
