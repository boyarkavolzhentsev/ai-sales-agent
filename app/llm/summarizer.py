"""Thread summary for operators (e.g. escalation cards). Advisory context only: it never
replaces the stored thread, and no business decision may rest on it alone."""

from dataclasses import dataclass
from typing import Annotated

from pydantic import AfterValidator, Field, StringConstraints

from app.core.models.base import CoreModel
from app.core.models.types import LocaleTag
from app.core.validation import unique_items
from app.llm.inputs import UntrustedEmail
from app.llm.models import LLMResultMetadata, SectionKind
from app.llm.prompts import THREAD_SUMMARIZER_PROMPT_V1, build_request, section
from app.llm.validation import StructuredLLM

MAX_SUMMARY_MESSAGES = 20
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class ThreadSummaryInput(CoreModel):
    thread: Annotated[tuple[UntrustedEmail, ...], Field(min_length=1, max_length=MAX_SUMMARY_MESSAGES)]
    locale: LocaleTag


class ThreadSummary(CoreModel):
    summary: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    open_questions: Annotated[tuple[ShortText, ...], AfterValidator(unique_items)] = ()


@dataclass(frozen=True)
class SummaryOutcome:
    summary: ThreadSummary
    metadata: LLMResultMetadata


def summarize_thread(llm: StructuredLLM, data: ThreadSummaryInput, *, correlation_id: str) -> SummaryOutcome:
    call = build_request(
        THREAD_SUMMARIZER_PROMPT_V1,
        ThreadSummary,
        correlation_id=correlation_id,
        locale=data.locale,
        sections=[section(SectionKind.UNTRUSTED_DATA, "thread", [m.model_dump(mode="json") for m in data.thread])],
    )
    result = llm.complete_structured(call)
    return SummaryOutcome(summary=result.output, metadata=result.metadata)
