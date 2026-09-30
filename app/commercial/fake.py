"""Deterministic fakes for the commercial contracts (tests only; no model, no network)."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.commercial.contracts import CommercialExtraction, CommercialExtractionRequest
from app.commercial.pricing import PriceFact
from app.persistence import UnitOfWork


@dataclass
class FakeCommercialExtractor:
    by_message: dict[str, CommercialExtraction | Exception] = field(default_factory=dict)
    default: CommercialExtraction | Exception = field(default_factory=CommercialExtraction)
    requests: list[CommercialExtractionRequest] = field(default_factory=list)
    before: Callable[[CommercialExtractionRequest], None] | None = None

    def extract(self, request: CommercialExtractionRequest) -> CommercialExtraction:
        self.requests.append(request)
        if self.before is not None:
            self.before(request)
        outcome = self.by_message.get(request.message_id, self.default)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@dataclass
class FakePriceCatalog:
    prices: dict[str, PriceFact] = field(default_factory=dict)

    def price_for(self, uow: UnitOfWork, item_ref: str, now: datetime) -> PriceFact | None:
        return self.prices.get(item_ref)
