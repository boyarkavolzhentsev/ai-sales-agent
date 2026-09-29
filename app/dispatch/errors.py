class DispatchError(Exception):
    """Base class for dispatch failures raised to the caller."""


class DispatchNotFoundError(DispatchError):
    """The referenced reply message (or attempt) does not exist."""


class DispatchStateError(DispatchError):
    """Persisted state contradicts the dispatch lifecycle (e.g. an unresolved attempt whose
    message is no longer SENDING). The attempt is left unresolved for an operator."""
