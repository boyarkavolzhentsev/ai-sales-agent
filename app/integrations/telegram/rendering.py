"""Plain-text operator messages. No parse mode is ever used, so customer-controlled text
cannot become formatting, links, mentions or buttons; long content is truncated
deterministically and marked."""

import unicodedata

MAX_MESSAGE = 4096  # Telegram's limit (characters)
TRUNCATED = " […truncated]"


def clean(text: str) -> str:
    """Printable text only: control and format characters (bidi overrides, zero-width
    joiners, ...) are dropped; newlines and tabs are kept."""
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c)[0] != "C")


def excerpt(text: str | None, limit: int) -> str:
    value = clean(text or "").strip()
    return value if len(value) <= limit else value[: max(0, limit - len(TRUNCATED))].rstrip() + TRUNCATED


def fit(text: str) -> str:
    """The whole message within Telegram's limit."""
    return excerpt(text, MAX_MESSAGE)


def short(entity_id: str) -> str:
    return entity_id[:12]
