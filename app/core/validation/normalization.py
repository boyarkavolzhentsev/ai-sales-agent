"""Explicit normalization for identity-bearing strings (emails, domains).

Normalization is intentionally narrow: surrounding whitespace is stripped and ASCII
case is folded. Anything else that is ambiguous (display names, trailing dots,
non-ASCII/IDN domains that are not punycode, IP literals) is rejected, not rewritten.
"""

import re

_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_EMAIL_LOCAL = re.compile(r"[a-z0-9!#$%&'*+/=?^_`{|}~.-]+")
_MAX_DOMAIN_LENGTH = 253
_MAX_LOCAL_LENGTH = 64


def require_non_blank(value: str) -> str:
    """Reject empty or whitespace-only strings. The value is returned unchanged."""
    if not value.strip():
        raise ValueError("value must not be empty or whitespace-only")
    return value


def normalize_domain(value: str) -> str:
    """Return a lower-cased ASCII domain name, or raise ValueError."""
    candidate = value.strip().lower()
    if not candidate:
        raise ValueError("domain must not be empty")
    if len(candidate) > _MAX_DOMAIN_LENGTH:
        raise ValueError("domain is too long")
    if candidate.endswith("."):
        raise ValueError("domain must not end with a dot")
    labels = candidate.split(".")
    if len(labels) < 2:
        raise ValueError("domain must contain at least two labels")
    for label in labels:
        if not _DOMAIN_LABEL.fullmatch(label):
            raise ValueError(f"invalid domain label: {label!r}")
    if labels[-1].isdigit():
        raise ValueError("top-level domain must not be numeric")
    return candidate


def normalize_email(value: str) -> str:
    """Return a lower-cased bare email address (``local@domain``), or raise ValueError.

    The whole address, including the local part, is lower-cased so it can be used as
    a stable identity for de-duplication and suppression matching.
    """
    candidate = value.strip().lower()
    if not candidate:
        raise ValueError("email must not be empty")
    if candidate.count("@") != 1:
        raise ValueError("email must contain exactly one '@'")
    local, domain = candidate.split("@")
    if not local or len(local) > _MAX_LOCAL_LENGTH:
        raise ValueError("email local part is empty or too long")
    if not _EMAIL_LOCAL.fullmatch(local):
        raise ValueError("email local part contains unsupported characters")
    if local.startswith(".") or local.endswith(".") or ".." in local:
        raise ValueError("email local part has misplaced dots")
    return f"{local}@{normalize_domain(domain)}"
