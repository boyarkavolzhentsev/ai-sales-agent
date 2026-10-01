"""The local Gmail token file: atomic writes and a local-only authorization check.

Standard library only, so provider-status can report authorization without importing a
Google library or contacting anything. The file holds Google's "authorized user" JSON
(access token, refresh token, expiry, scopes, OAuth client id/secret): it is local-only
(gitignored, under ``.local/`` or outside the code tree), written atomically with owner-only
permissions where the platform supports them, and its contents never appear in output.
"""

import contextlib
import json
import os
import tempfile
from enum import StrEnum
from pathlib import Path

# Least privilege for what Stage 16 does: send mail; read messages, history and the
# profile (inbound sync, reconciliation of our own sent mail, account identity). No
# modify/labels scope: the agent never archives, labels, marks or deletes anything.
SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
)


class Authorization(StrEnum):
    AUTHORIZED = "AUTHORIZED"  # a token file with a refresh token and the needed scopes (or a refresh secret)
    AUTH_REQUIRED = "AUTH_REQUIRED"  # nothing to authorize with yet
    AUTH_INVALID = "AUTH_INVALID"  # a token file that cannot be used (malformed, no refresh token, scopes)


def read_token(path: Path) -> dict[str, object] | None:
    """The parsed token JSON, None when absent; ValueError when unusable (the reason never
    includes file content)."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("TOKEN_UNREADABLE") from None
    if not isinstance(data, dict):
        raise ValueError("TOKEN_MALFORMED")
    return data


def has_scopes(data: dict[str, object]) -> bool:
    scopes = data.get("scopes")
    if isinstance(scopes, str):
        scopes = scopes.split()
    return isinstance(scopes, list) and set(SCOPES) <= {str(s) for s in scopes}


def local_authorization(token_file: Path, *, refresh_secret: bool) -> Authorization:
    """Local check only (no network): can a client be built without the interactive flow?"""
    try:
        data = read_token(token_file)
    except ValueError:
        return Authorization.AUTH_INVALID
    if data is None:
        return Authorization.AUTHORIZED if refresh_secret else Authorization.AUTH_REQUIRED
    if not data.get("refresh_token") or not has_scopes(data):
        return Authorization.AUTH_INVALID
    return Authorization.AUTHORIZED


def write_token(path: Path, content: str) -> None:
    """Atomic: a temporary file in the same directory, flushed, then renamed over the old
    one. A failed write never truncates or deletes the previous token."""
    handle, temporary = tempfile.mkstemp(prefix=".gmail-token-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
