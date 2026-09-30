"""Prices from trusted internal business data, read through a provider-neutral contract.

``KnowledgePriceCatalog`` reads the existing knowledge base (no live RAG, no external
store): only sources of the PRICING_COMMERCIAL domain that the knowledge gate deems usable
now (approved, external use allowed, effective, current, newest version), and only a
structured fact whose key is exactly the item reference, whose value is a plain decimal
and whose unit is a currency code. Anything else is "no known price" (never a guess).
"""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

from app.core.enums import KnowledgeDomain
from app.knowledge.metadata import select_sources
from app.persistence import UnitOfWork

_AMOUNT = re.compile(r"^\d{1,12}(\.\d{1,4})?$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class PriceFact:
    amount: Decimal
    currency: str
    source_id: str
    source_version: int
    fact_key: str


class PriceCatalog(Protocol):
    def price_for(self, uow: UnitOfWork, item_ref: str, now: datetime) -> PriceFact | None: ...


class KnowledgePriceCatalog:
    def __init__(self, locale: str) -> None:
        self._locale = locale

    def price_for(self, uow: UnitOfWork, item_ref: str, now: datetime) -> PriceFact | None:
        selection = select_sources(uow.knowledge_sources.list_by_domain(KnowledgeDomain.PRICING_COMMERCIAL), now, self._locale)
        usable = {(s.source_id, s.version) for s in selection.usable}
        matches = [f for f in uow.knowledge_index.list_facts_for_sources(usable) if f.fact_key == item_ref]
        if len(matches) != 1:
            return None  # unknown, or ambiguous across sources: never pick one
        fact = matches[0]
        if not _AMOUNT.match(fact.value.strip()) or fact.unit is None or not _CURRENCY.match(fact.unit):
            return None
        try:
            amount = Decimal(fact.value.strip())
        except InvalidOperation:
            return None
        return PriceFact(amount=amount, currency=fact.unit, source_id=fact.source_id, source_version=fact.source_version,
                         fact_key=fact.fact_key)
