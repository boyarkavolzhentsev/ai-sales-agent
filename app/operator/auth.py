"""Operator authentication and authorization boundary.

Two separate steps, both required for every read and every command:
1. Authentication: a trusted boundary (later the Telegram adapter, which verifies the
   update came from Telegram) turns an opaque credential into an operator ID. Callers
   never pass an operator ID directly, so an arbitrary actor ID proves nothing.
2. Authorization: that operator ID must be in ``OperatorConfig.authorized_operator_ids``.

Any failure, including an authenticator error, is the same OperatorUnauthorizedError,
raised before any database access.
"""

from typing import Protocol

from app.operator.errors import OperatorUnauthorizedError
from app.operator.models import OperatorConfig, OperatorCredential


class OperatorAuthenticator(Protocol):
    def authenticate(self, credential: OperatorCredential) -> str | None:
        """Return the verified operator ID, or None when the credential is not valid."""
        ...


def authorize(authenticator: OperatorAuthenticator, config: OperatorConfig, credential: object) -> str:
    if not isinstance(credential, OperatorCredential):
        raise OperatorUnauthorizedError()
    try:
        operator_id = authenticator.authenticate(credential)
    except Exception:  # noqa: BLE001 - an authenticator failure must fail closed
        raise OperatorUnauthorizedError() from None
    if not isinstance(operator_id, str) or operator_id not in config.authorized_operator_ids:
        raise OperatorUnauthorizedError()
    return operator_id
