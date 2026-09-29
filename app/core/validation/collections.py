from collections.abc import Hashable
from typing import TypeVar

H = TypeVar("H", bound=Hashable)


def unique_items(values: tuple[H, ...]) -> tuple[H, ...]:
    """Reject tuples containing duplicate items. Order is preserved; nothing is dropped."""
    seen: set[H] = set()
    duplicates: list[H] = []
    for item in values:
        if item in seen and item not in duplicates:
            duplicates.append(item)
        seen.add(item)
    if duplicates:
        raise ValueError(f"duplicate values are not allowed: {duplicates!r}")
    return values
