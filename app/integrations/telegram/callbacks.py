"""Compact callback data: ``<action>|<target id>|<version>[|<argument>]`` or
``<action>|<token>`` for confirmations. Opaque ids and codes only (no text, prices or
secrets), at most 64 bytes (Telegram's limit). Callback data is attacker-controlled: it
is parsed strictly and is only ever a request that the console revalidates."""

import re
from dataclasses import dataclass
from enum import StrEnum

MAX_BYTES = 64


class Action(StrEnum):
    APPROVE_DRAFT = "a"
    REJECT_DRAFT = "r"  # opens the reason choice
    REJECT_DRAFT_REASON = "rr"
    TAKE_OWNERSHIP = "eo"
    RESOLVE_NO_ACTION = "en"
    RESOLVE_REPLIED = "er"
    APPROVE_QUALIFICATION = "qa"
    CREATE_OPPORTUNITY = "op"
    APPROVE_PROPOSAL = "pa"
    MARK_PRESENTED = "pp"
    CONFIRM_ACCEPTANCE = "pc"
    MARK_DECLINED = "pd"
    APPROVE_TERM = "ta"
    REJECT_TERM = "tr"
    ACKNOWLEDGE_OBJECTION = "oa"
    DISMISS_SIGNAL = "sd"
    WON = "w"  # terminal: asks for confirmation
    LOST = "l"  # terminal: opens the reason choice
    LOST_REASON = "lr"  # terminal: asks for confirmation
    DNC = "d"  # terminal: asks for confirmation
    CONFIRM = "cf"
    CANCEL = "cx"


TOKEN_ACTIONS = frozenset({Action.CONFIRM, Action.CANCEL})
ARGUMENT_ACTIONS = frozenset({Action.REJECT_DRAFT_REASON, Action.LOST_REASON})
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TOKEN = re.compile(r"^[0-9a-f]{16}$")
_NUMBER = re.compile(r"^[0-9]{1,9}$")  # ASCII only: str.isdigit() would accept other scripts


@dataclass(frozen=True)
class Callback:
    action: Action
    target: str
    version: int | None = None
    argument: int | None = None


def encode(action: Action, target: str, version: int | None = None, argument: int | None = None) -> str:
    parts = [action.value, target, *([] if version is None else [str(version)]), *([] if argument is None else [str(argument)])]
    data = "|".join(parts)
    if len(data.encode("utf-8")) > MAX_BYTES:
        raise ValueError("callback data exceeds Telegram's 64 bytes")
    return data


def decode(data: str | None) -> Callback | None:
    """None for anything malformed or unknown."""
    if not data or len(data.encode("utf-8")) > MAX_BYTES:
        return None
    parts = data.split("|")
    try:
        action = Action(parts[0])
    except ValueError:
        return None
    if action in TOKEN_ACTIONS:
        return Callback(action, parts[1]) if len(parts) == 2 and _TOKEN.fullmatch(parts[1]) else None
    expected = 4 if action in ARGUMENT_ACTIONS else 3
    if len(parts) != expected or not _ID.fullmatch(parts[1]) or not all(_NUMBER.fullmatch(p) for p in parts[2:]):
        return None
    return Callback(action, parts[1], int(parts[2]), int(parts[3]) if expected == 4 else None)
