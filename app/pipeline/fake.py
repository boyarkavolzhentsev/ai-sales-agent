"""Deterministic fakes of the pipeline AI contracts (tests only; no model, no network)."""

from collections.abc import Callable
from dataclasses import dataclass, field

from app.pipeline.contracts import (
    AdvisorInput,
    ExtractionRequest,
    QualificationExtraction,
    SalesRecommendation,
)


@dataclass
class FakeQualificationExtractor:
    """Returns the scripted extraction for a message id (or ``default``); a scripted
    exception is raised instead. Records every request."""

    by_message: dict[str, QualificationExtraction | Exception] = field(default_factory=dict)
    default: QualificationExtraction | Exception = field(default_factory=QualificationExtraction)
    requests: list[ExtractionRequest] = field(default_factory=list)
    before: Callable[[ExtractionRequest], None] | None = None

    def extract(self, request: ExtractionRequest) -> QualificationExtraction:
        self.requests.append(request)
        if self.before is not None:
            self.before(request)
        outcome = self.by_message.get(request.message_id, self.default)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@dataclass
class FakeSalesAdvisor:
    recommendation: SalesRecommendation = field(default_factory=SalesRecommendation)
    inputs: list[AdvisorInput] = field(default_factory=list)

    def recommend(self, data: AdvisorInput) -> SalesRecommendation:
        self.inputs.append(data)
        return self.recommendation
