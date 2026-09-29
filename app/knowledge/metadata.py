"""Source metadata schema, mapping onto KnowledgeSource, and usability rules."""

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, Field, StrictInt, ValidationError

from app.core.enums import KnowledgeApprovalStatus, KnowledgeDomain, KnowledgeExternalUse
from app.core.models import KnowledgeSource
from app.core.models.base import CoreModel
from app.core.models.types import EntityId, LocaleTag, NonEmptyStr
from app.core.validation import unique_items
from app.knowledge.errors import SourceValidationError
from app.knowledge.models import SourceUsability

# Folder under knowledge_base/ for each domain. A file's folder must match its domain.
DOMAIN_DIRECTORIES: Mapping[KnowledgeDomain, str] = {
    KnowledgeDomain.COMPANY: "company",
    KnowledgeDomain.PRODUCTS_SERVICES: "products",
    KnowledgeDomain.PRICING_COMMERCIAL: "pricing",
    KnowledgeDomain.ICP: "icp",
    KnowledgeDomain.SALES_PLAYBOOKS: "sales_playbooks",
    KnowledgeDomain.FAQ: "faq",
    KnowledgeDomain.OBJECTIONS: "objections",
    KnowledgeDomain.CASE_STUDIES: "case_studies",
    KnowledgeDomain.OUTBOUND_MESSAGING: "outbound_messaging",
    KnowledgeDomain.INDUSTRY: "industry",
    KnowledgeDomain.COMPETITORS: "competitors",
    KnowledgeDomain.LEGAL_COMPLIANCE: "legal_compliance",
    KnowledgeDomain.CONTACTS_ROUTING: "contacts",
    KnowledgeDomain.MEETING_GUIDANCE: "meeting_guidance",
    KnowledgeDomain.MARKETING_MATERIALS: "marketing_materials",
}

StrictVersion = Annotated[StrictInt, Field(ge=1)]


class SourceMetadata(CoreModel):
    """Front matter / ``metadata`` block of a knowledge source. Unknown keys are rejected.

    Types are strict where YAML could otherwise coerce silently: ``version`` must be an
    integer, never "1" or 1.0; datetimes must be ISO 8601 strings with an offset.
    """

    source_id: EntityId
    domain: KnowledgeDomain
    title: NonEmptyStr
    version: StrictVersion
    approval_status: KnowledgeApprovalStatus
    external_use: KnowledgeExternalUse
    effective_from: AwareDatetime
    review_by: AwareDatetime
    locale: LocaleTag
    tags: Annotated[tuple[NonEmptyStr, ...], AfterValidator(unique_items)]
    approved_by: NonEmptyStr | None = None
    approved_at: AwareDatetime | None = None
    supersedes: EntityId | None = None
    product: NonEmptyStr | None = None
    industry: NonEmptyStr | None = None
    region: NonEmptyStr | None = None
    source_ref: NonEmptyStr | None = None


def build_knowledge_source(
    raw: object, *, path: str, content_hash: str
) -> KnowledgeSource:
    """Validate raw metadata and map it onto the KnowledgeSource contract.

    ``product``, ``industry``, ``region`` and ``source_ref`` become namespaced tags
    (e.g. ``product:basic``) because the contract has no dedicated fields for them.
    """
    try:
        meta = SourceMetadata.model_validate(raw)
        extra_tags = tuple(
            f"{name}:{value}"
            for name, value in (
                ("product", meta.product),
                ("industry", meta.industry),
                ("region", meta.region),
                ("ref", meta.source_ref),
            )
            if value is not None
        )
        return KnowledgeSource(
            source_id=meta.source_id,
            domain=meta.domain,
            title=meta.title,
            path=path,
            version=meta.version,
            content_hash=content_hash,
            approval_status=meta.approval_status,
            external_use=meta.external_use,
            approved_by=meta.approved_by,
            approved_at=meta.approved_at,
            effective_from=meta.effective_from,
            review_by=meta.review_by,
            supersedes=meta.supersedes,
            locale=meta.locale,
            tags=meta.tags + extra_tags,
        )
    except ValidationError as exc:
        raise SourceValidationError(f"{path}: invalid metadata: {exc}") from exc


def is_source_current(source: KnowledgeSource, now: datetime) -> bool:
    """Current means ``effective_from <= now <= review_by`` (review_by is inclusive).
    A source without both dates is never current."""
    _require_aware(now)
    if source.effective_from is None or source.review_by is None:
        return False
    return source.effective_from <= now <= source.review_by


def locale_compatible(source_locale: str, query_locale: str) -> bool:
    """Same primary language subtag, e.g. "en-GB" is compatible with "en"."""
    return source_locale.split("-")[0].casefold() == query_locale.split("-")[0].casefold()


def classify_source(source: KnowledgeSource, now: datetime, query_locale: str) -> SourceUsability:
    """Usability of one version on its own (supersession is decided in select_sources)."""
    if source.approval_status is not KnowledgeApprovalStatus.APPROVED:
        return SourceUsability.NOT_APPROVED
    if source.external_use is not KnowledgeExternalUse.EXTERNAL_OK:
        return SourceUsability.INTERNAL_ONLY
    if not locale_compatible(source.locale, query_locale):
        return SourceUsability.LOCALE_MISMATCH
    if source.effective_from is None or source.effective_from > now:
        return SourceUsability.NOT_YET_EFFECTIVE
    if not is_source_current(source, now):
        return SourceUsability.STALE
    return SourceUsability.USABLE


class SourceSelection(CoreModel):
    """Which source versions may back an answer, and why every other one may not."""

    usable: tuple[KnowledgeSource, ...]
    excluded: tuple[tuple[KnowledgeSource, SourceUsability], ...]


def select_sources(
    versions: Iterable[KnowledgeSource], now: datetime, query_locale: str
) -> SourceSelection:
    """Pick at most one usable version per source_id: the newest usable one.

    Conservative rules:
    - If the newest version of a source_id is RETIRED, every version is WITHDRAWN.
    - Older usable versions are SUPERSEDED by the newest usable one.
    - A usable source named in another usable source's ``supersedes`` is SUPERSEDED.
    """
    _require_aware(now)
    by_id: dict[str, list[KnowledgeSource]] = {}
    for source in versions:
        by_id.setdefault(source.source_id, []).append(source)

    chosen: dict[str, KnowledgeSource] = {}
    excluded: list[tuple[KnowledgeSource, SourceUsability]] = []
    for source_id in sorted(by_id):
        history = sorted(by_id[source_id], key=lambda s: s.version, reverse=True)
        if history[0].approval_status is KnowledgeApprovalStatus.RETIRED:
            excluded.extend((s, SourceUsability.WITHDRAWN) for s in history)
            continue
        for source in history:
            usability = classify_source(source, now, query_locale)
            if usability is SourceUsability.USABLE and source_id not in chosen:
                chosen[source_id] = source
            elif usability is SourceUsability.USABLE:
                excluded.append((source, SourceUsability.SUPERSEDED))
            else:
                excluded.append((source, usability))

    replaced = {s.supersedes for s in chosen.values() if s.supersedes is not None}
    usable: list[KnowledgeSource] = []
    for source_id in sorted(chosen):
        source = chosen[source_id]
        if source_id in replaced:
            excluded.append((source, SourceUsability.SUPERSEDED))
        else:
            usable.append(source)
    excluded.sort(key=lambda item: (item[0].source_id, -item[0].version))
    return SourceSelection(usable=tuple(usable), excluded=tuple(excluded))


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
