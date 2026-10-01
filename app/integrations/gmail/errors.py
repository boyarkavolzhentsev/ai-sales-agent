"""Stable Gmail provider error codes. An error carries a code, an HTTP status when there
was one, and what is known about a send: never a token, request body or provider text."""

from enum import StrEnum


class GmailCode(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"  # no usable authorization: run gmail-auth
    AUTH_INVALID = "AUTH_INVALID"  # the token file is unreadable, malformed or lacks scopes
    AUTH_REFRESH_FAILED = "AUTH_REFRESH_FAILED"
    CLIENT_CONFIG_INVALID = "CLIENT_CONFIG_INVALID"
    AUTHORIZATION_NOT_COMPLETED = "AUTHORIZATION_NOT_COMPLETED"  # the interactive flow did not finish
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RATE_LIMITED = "RATE_LIMITED"
    TEMPORARY_PROVIDER_ERROR = "TEMPORARY_PROVIDER_ERROR"
    INVALID_REQUEST = "INVALID_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    MAILBOX_MISMATCH = "MAILBOX_MISMATCH"  # the authorized account is not the configured address
    HISTORY_EXPIRED = "HISTORY_EXPIRED"
    UNEXPECTED_RESPONSE = "UNEXPECTED_RESPONSE"


class Delivery(StrEnum):
    """What is known about a request that may have changed provider state (a send)."""

    NOT_SENT = "NOT_SENT"  # it never left this process (e.g. refresh failed, connect timeout)
    REJECTED = "REJECTED"  # Gmail answered with an error: it did not perform the operation
    UNKNOWN = "UNKNOWN"  # it may have been performed (timeout after sending, 5xx, lost response)


class GmailError(Exception):
    def __init__(self, code: GmailCode, *, status: int | None = None, delivery: Delivery = Delivery.UNKNOWN) -> None:
        self.code = code
        self.status = status
        self.delivery = delivery
        super().__init__(code.value if status is None else f"{code.value} (HTTP {status})")
