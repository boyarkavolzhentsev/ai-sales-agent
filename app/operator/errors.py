"""Operator-layer failures. Every failure leaves the database unchanged.

Messages never contain customer data: an unauthorized caller learns nothing, and an
authorized caller gets entity IDs and stable reason codes only.
"""

from app.operator.models import BlockCode


class OperatorError(Exception):
    """Base class for operator-layer failures."""


class OperatorUnauthorizedError(OperatorError):
    """The caller did not present a trusted, authorized operator identity."""

    def __init__(self) -> None:
        super().__init__("operator not authorized")


class OperatorNotFoundError(OperatorError):
    """The referenced entity does not exist (or is not an operator-reviewable kind)."""


class CommandRejectedError(OperatorError):
    """The command is valid but the current authoritative state does not allow it.

    ``codes`` are stable reason codes; nothing was written.
    """

    def __init__(self, codes: tuple[BlockCode, ...], detail: str = "") -> None:
        self.codes = codes
        super().__init__(", ".join(code.value for code in codes) + (f": {detail}" if detail else ""))


class StaleCommandError(CommandRejectedError):
    """The command was built from a version or content that is no longer current.

    Re-read the entity and decide again; a stale command is never applied to changed
    state. A lost approve/reject race surfaces as this error.
    """


class CommandCollisionError(OperatorError):
    """A command identity was reused with a different payload, kind or operator."""
