"""Lead qualification: facts with evidence, conflicts, readiness, gaps, operator decisions.

Rules:
- Unknown stays unknown: a field has a fact only when a proposal of sufficient confidence
  (or an operator) supplied it; LOW-confidence and off-profile proposals are ignored.
- Known facts are never overwritten silently. A proposal that agrees adds evidence; one
  that disagrees becomes an OPEN conflict (the known fact stays) for an operator. An
  operator may replace a fact explicitly (versioned, audited with before/after).
- Readiness is deterministic: READY_FOR_REVIEW when every required field is known and no
  conflict is open; otherwise IN_PROGRESS. Only an operator approves (QUALIFIED) or
  disqualifies. A decided qualification keeps recording conflicts for the operator.
- A lead with no qualification record is NOT_STARTED (nothing is backfilled).
"""

from dataclasses import dataclass, field
from datetime import datetime

from pydantic import JsonValue

from app.core.enums import (
    ConfidenceBand,
    ConflictResolution,
    ConflictStatus,
    FactSource,
    LeadStage,
    PipelineTrigger,
    QualificationStatus,
    RefKind,
)
from app.core.models import FactEvidence, Lead, LeadQualification, QualificationConflict, QualificationFact, same_value
from app.core.models.base import CoreModel
from app.core.models.types import EntityId, NonEmptyStr
from app.inbound.models import stable_id
from app.persistence import UnitOfWork
from app.pipeline.audit import PIPELINE_ACTOR, operator_actor, record_event, ref
from app.pipeline.config import QualificationProfile
from app.pipeline.contracts import FactProposal, QualificationExtraction
from app.pipeline.errors import PipelineCode, PipelineError, PipelineNotFoundError
from app.pipeline.policy import RULES, apply_transition

Q = QualificationStatus
DECIDED = frozenset({Q.QUALIFIED, Q.DISQUALIFIED})
_CONFIDENCE_RANK = {ConfidenceBand.LOW: 0, ConfidenceBand.MEDIUM: 1, ConfidenceBand.HIGH: 2}


class QualificationGap(CoreModel):
    """Something still unknown (or disputed). Planning input only, never a message."""

    field: str
    label: NonEmptyStr
    reason: NonEmptyStr  # MISSING_REQUIRED | MISSING_OPTIONAL | CONFLICTING
    priority: int
    # Whether the agent may ask by itself (False: an operator should, e.g. budget or a dispute).
    safe_to_ask: bool
    evidence_message_ids: tuple[EntityId, ...] = ()


@dataclass
class MergeOutcome:
    added: list[str] = field(default_factory=list)
    corroborated: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    ignored: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.corroborated or self.conflicts)


def status_of(qualification: LeadQualification | None) -> QualificationStatus:
    return Q.NOT_STARTED if qualification is None else qualification.status


def readiness(profile: QualificationProfile, qualification: LeadQualification) -> QualificationStatus:
    if qualification.status in DECIDED:
        return qualification.status
    known = {fact.field for fact in qualification.facts}
    if profile.required_keys <= known and not qualification.open_conflicts:
        return Q.READY_FOR_REVIEW
    return Q.IN_PROGRESS


def gaps(profile: QualificationProfile, qualification: LeadQualification | None) -> tuple[QualificationGap, ...]:
    known = {fact.field for fact in qualification.facts} if qualification else set()
    disputed = {c.field: c for c in qualification.open_conflicts} if qualification else {}
    result: list[QualificationGap] = []
    for spec, required in [*((s, True) for s in profile.required), *((s, False) for s in profile.optional)]:
        if spec.key in disputed:
            evidence = disputed[spec.key].evidence.message_id
            result.append(QualificationGap(field=spec.key, label=spec.label, reason="CONFLICTING", priority=spec.priority,
                                           safe_to_ask=False, evidence_message_ids=(evidence,) if evidence else ()))
        elif spec.key not in known:
            result.append(QualificationGap(field=spec.key, label=spec.label,
                                           reason="MISSING_REQUIRED" if required else "MISSING_OPTIONAL",
                                           priority=spec.priority, safe_to_ask=spec.safe_to_ask))
    return tuple(sorted(result, key=lambda g: (g.reason == "MISSING_OPTIONAL", g.priority, g.field)))


def merge(
    profile: QualificationProfile, qualification: LeadQualification | None, lead_id: str,
    proposals: tuple[FactProposal, ...], evidence: FactEvidence, now: datetime, min_confidence: ConfidenceBand,
) -> tuple[LeadQualification | None, MergeOutcome]:
    """Pure: the qualification after applying proposals, and what happened to each."""
    outcome = MergeOutcome()
    facts = {fact.field: fact for fact in qualification.facts} if qualification else {}
    conflicts = list(qualification.conflicts) if qualification else []
    for proposal in proposals:
        if proposal.field not in profile.keys:
            outcome.ignored.append((proposal.field, "FIELD_NOT_IN_PROFILE"))
            continue
        if _CONFIDENCE_RANK[proposal.confidence] < _CONFIDENCE_RANK[min_confidence]:
            outcome.ignored.append((proposal.field, "LOW_CONFIDENCE"))
            continue
        current = facts.get(proposal.field)
        if current is None:
            facts[proposal.field] = QualificationFact(field=proposal.field, value=proposal.value,
                                                      confidence=proposal.confidence, evidence=(evidence,))
            outcome.added.append(proposal.field)
        elif same_value(current.value, proposal.value):
            if not any(_same_source(e, evidence) for e in current.evidence):
                facts[proposal.field] = current.model_copy(update={
                    "evidence": (*current.evidence, evidence),
                    "confidence": max(current.confidence, proposal.confidence, key=_CONFIDENCE_RANK.__getitem__),
                })
                outcome.corroborated.append(proposal.field)
        else:
            conflict_id = stable_id("qc", lead_id, proposal.field, " ".join(proposal.value.casefold().split()),
                                    evidence.message_id or evidence.operator_command_id or "")
            if any(c.conflict_id == conflict_id for c in conflicts):
                continue  # this exact disagreement is already recorded
            conflicts.append(QualificationConflict(conflict_id=conflict_id, field=proposal.field,
                                                   current_value=current.value, proposed_value=proposal.value,
                                                   evidence=evidence))
            outcome.conflicts.append(conflict_id)
    if not outcome.changed:
        return qualification, outcome
    if qualification is None:
        draft = LeadQualification(lead_id=lead_id, profile_id=profile.profile_id, status=Q.IN_PROGRESS,
                                  facts=tuple(facts.values()), conflicts=tuple(conflicts), created_at=now, updated_at=now)
    else:
        draft = qualification.model_copy(update={"facts": tuple(facts.values()), "conflicts": tuple(conflicts),
                                                 "updated_at": max(now, qualification.updated_at),
                                                 "version": qualification.version + 1})
    merged = LeadQualification.model_validate(draft.model_dump() | {"status": readiness(profile, draft)})
    return merged, outcome


def _same_source(a: FactEvidence, b: FactEvidence) -> bool:
    return (a.source, a.message_id, a.operator_command_id) == (b.source, b.message_id, b.operator_command_id)


def save(uow: UnitOfWork, before: LeadQualification | None, after: LeadQualification) -> None:
    if before is None:
        uow.qualifications.add(after)
    else:
        uow.qualifications.update(after, before.version)


def start_if_needed(uow: UnitOfWork, lead: Lead, *, correlation_id: str, now: datetime) -> Lead:
    """Facts started to arrive: an ENGAGED/INTERESTED/MEETING_REQUESTED lead moves to
    QUALIFYING (automatic bookkeeping; no outreach and no commercial judgement)."""
    if lead.stage not in RULES[PipelineTrigger.QUALIFICATION_STARTED].sources:
        return lead
    return apply_transition(uow, lead, PipelineTrigger.QUALIFICATION_STARTED, LeadStage.QUALIFYING, actor=PIPELINE_ACTOR,
                            correlation_id=correlation_id, now=now, reason="QUALIFICATION_FACTS_RECORDED")


def record_extraction(
    uow: UnitOfWork, profile: QualificationProfile, lead: Lead, extraction: QualificationExtraction, *,
    message_id: str, conversation_id: str | None, min_confidence: ConfidenceBand, correlation_id: str, now: datetime,
) -> MergeOutcome:
    """Apply one validated extraction of one customer message (idempotency is the caller's)."""
    current = uow.qualifications.get(lead.lead_id)
    if current is not None and current.status is Q.DISQUALIFIED:
        return MergeOutcome(ignored=[(p.field, "QUALIFICATION_DECIDED") for p in extraction.proposals])
    evidence = FactEvidence(source=FactSource.EXTRACTION, message_id=message_id, conversation_id=conversation_id,
                            recorded_at=now)
    merged, outcome = merge(profile, current, lead.lead_id, extraction.proposals, evidence, now, min_confidence)
    if merged is None or not outcome.changed:
        return outcome
    save(uow, current, merged)
    _audit_update(uow, current, merged, outcome, actor_id=None, correlation_id=correlation_id, now=now,
                  evidence_message_id=message_id)
    start_if_needed(uow, lead, correlation_id=correlation_id, now=now)
    return outcome


def record_operator_fact(
    uow: UnitOfWork, profile: QualificationProfile, lead: Lead, *, field_key: str, value: str,
    expected_version: int | None, operator_id: str, command_id: str, correlation_id: str, now: datetime,
) -> LeadQualification:
    """An operator states a fact (e.g. from a call). Replacing a different known value is
    explicit, versioned and audited; a field with an open conflict must be resolved first."""
    if field_key not in profile.keys:
        raise PipelineError(PipelineCode.QUALIFICATION_FIELD_UNKNOWN)
    current = uow.qualifications.get(lead.lead_id)
    _check_version(current, expected_version)
    if current is not None and current.status is Q.DISQUALIFIED:
        raise PipelineError(PipelineCode.QUALIFICATION_DECIDED)
    if current is not None and any(c.field == field_key for c in current.open_conflicts):
        raise PipelineError(PipelineCode.QUALIFICATION_CONFLICT_OPEN)
    evidence = FactEvidence(source=FactSource.OPERATOR, operator_command_id=command_id, recorded_at=now)
    fact = QualificationFact(field=field_key, value=value, confidence=ConfidenceBand.HIGH, evidence=(evidence,))
    facts = {f.field: f for f in current.facts} if current else {}
    previous = facts.get(field_key)
    if previous is not None and same_value(previous.value, value):
        fact = previous.model_copy(update={"evidence": (*previous.evidence, evidence), "confidence": ConfidenceBand.HIGH})
    facts[field_key] = fact
    if current is None:
        draft = LeadQualification(lead_id=lead.lead_id, profile_id=profile.profile_id, status=Q.IN_PROGRESS,
                                  facts=tuple(facts.values()), created_at=now, updated_at=now)
    else:
        draft = current.model_copy(update={"facts": tuple(facts.values()), "updated_at": max(now, current.updated_at),
                                           "version": current.version + 1})
    updated = LeadQualification.model_validate(draft.model_dump() | {"status": readiness(profile, draft)})
    save(uow, current, updated)
    outcome = MergeOutcome(added=[field_key] if previous is None else [], corroborated=[field_key] if previous else [])
    _audit_update(uow, current, updated, outcome, actor_id=operator_id, correlation_id=correlation_id, now=now,
                  replaced={field_key: previous.value} if previous and not same_value(previous.value, value) else None)
    start_if_needed(uow, lead, correlation_id=correlation_id, now=now)
    return updated


def resolve_conflict(
    uow: UnitOfWork, profile: QualificationProfile, lead: Lead, *, conflict_id: str, resolution: ConflictResolution,
    expected_version: int, operator_id: str, correlation_id: str, now: datetime,
) -> LeadQualification:
    current = uow.qualifications.get(lead.lead_id)
    if current is None:
        raise PipelineNotFoundError(f"lead {lead.lead_id} has no qualification")
    _check_version(current, expected_version)
    conflict = next((c for c in current.conflicts if c.conflict_id == conflict_id), None)
    if conflict is None:
        raise PipelineNotFoundError(f"qualification conflict {conflict_id} not found")
    if conflict.status is not ConflictStatus.OPEN:
        raise PipelineError(PipelineCode.CONFLICT_NOT_OPEN)
    facts = {f.field: f for f in current.facts}
    if resolution is ConflictResolution.ACCEPT_PROPOSED:
        # The operator chose the proposed value; its evidence is the message that stated it.
        # Any other open conflict stays open: nothing else is decided implicitly.
        facts[conflict.field] = QualificationFact(field=conflict.field, value=conflict.proposed_value,
                                                  confidence=ConfidenceBand.HIGH, evidence=(conflict.evidence,))
    resolved = conflict.model_copy(update={"status": ConflictStatus.RESOLVED, "resolution": resolution,
                                           "resolved_by": operator_id, "resolved_at": now})
    conflicts = tuple(resolved if c.conflict_id == conflict_id else c for c in current.conflicts)
    draft = current.model_copy(update={"facts": tuple(facts.values()), "conflicts": conflicts,
                                       "updated_at": max(now, current.updated_at), "version": current.version + 1})
    updated = LeadQualification.model_validate(draft.model_dump() | {"status": readiness(profile, draft)})
    uow.qualifications.update(updated, current.version)
    record_event(uow, key=(lead.lead_id, str(updated.version)), event_type="QUALIFICATION_CONFLICT_RESOLVED",
                 subjects=(ref(RefKind.LEAD_QUALIFICATION, lead.lead_id), ref(RefKind.LEAD, lead.lead_id)),
                 before={"status": current.status.value, "field": conflict.field, "value": conflict.current_value},
                 after={"status": updated.status.value, "conflict_id": conflict_id, "resolution": resolution.value,
                        "value": facts[conflict.field].value, "version": updated.version},
                 actor=operator_actor(operator_id), correlation_id=correlation_id, now=now)
    return updated


def approve(
    uow: UnitOfWork, lead: Lead, *, expected_version: int, operator_id: str, command_id: str, correlation_id: str,
    now: datetime,
) -> tuple[Lead, LeadQualification]:
    current = uow.qualifications.get(lead.lead_id)
    if current is None:
        raise PipelineError(PipelineCode.QUALIFICATION_NOT_STARTED)
    _check_version(current, expected_version)
    if current.open_conflicts:
        raise PipelineError(PipelineCode.QUALIFICATION_CONFLICT_OPEN)
    if current.status is not Q.READY_FOR_REVIEW:
        raise PipelineError(PipelineCode.QUALIFICATION_DECIDED if current.status in DECIDED
                            else PipelineCode.QUALIFICATION_NOT_READY)
    lead = apply_transition(uow, lead, PipelineTrigger.QUALIFICATION_APPROVED, LeadStage.QUALIFIED,
                            actor=operator_actor(operator_id), correlation_id=correlation_id, now=now,
                            reason="QUALIFICATION_APPROVED", command_id=command_id)
    approved = current.model_copy(update={"status": Q.QUALIFIED, "decided_by": operator_id, "decided_at": now,
                                          "updated_at": max(now, current.updated_at), "version": current.version + 1})
    uow.qualifications.update(approved, current.version)
    record_event(uow, key=(lead.lead_id, str(approved.version)), event_type="QUALIFICATION_APPROVED",
                 subjects=(ref(RefKind.LEAD_QUALIFICATION, lead.lead_id), ref(RefKind.LEAD, lead.lead_id)),
                 before={"status": current.status.value}, after={"status": Q.QUALIFIED.value, "version": approved.version},
                 actor=operator_actor(operator_id), correlation_id=correlation_id, now=now)
    return lead, approved


def _check_version(current: LeadQualification | None, expected: int | None) -> None:
    """``expected`` None means "I saw no qualification"; it must still be absent."""
    actual = current.version if current else None
    if actual != expected:
        raise PipelineError(PipelineCode.QUALIFICATION_VERSION_CHANGED)


def _audit_update(
    uow: UnitOfWork, before: LeadQualification | None, after: LeadQualification, outcome: MergeOutcome, *,
    actor_id: str | None, correlation_id: str, now: datetime, evidence_message_id: str | None = None,
    replaced: dict[str, str] | None = None,
) -> None:
    after_state: dict[str, JsonValue] = {
        "status": after.status.value, "version": after.version,
        "facts": {f.field: f.value for f in after.facts if f.field in {*outcome.added, *outcome.corroborated}},
        "conflicts_created": list(outcome.conflicts), "evidence_message_id": evidence_message_id,
    }
    before_state: dict[str, JsonValue] = {"status": before.status.value if before else Q.NOT_STARTED.value,
                                          "replaced": dict(replaced) if replaced else None}
    record_event(uow, key=(after.lead_id, str(after.version)), event_type="QUALIFICATION_UPDATED",
                 subjects=(ref(RefKind.LEAD_QUALIFICATION, after.lead_id), ref(RefKind.LEAD, after.lead_id)),
                 before=before_state, after=after_state,
                 actor=operator_actor(actor_id) if actor_id else PIPELINE_ACTOR, correlation_id=correlation_id, now=now)
