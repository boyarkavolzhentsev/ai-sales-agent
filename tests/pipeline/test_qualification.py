"""Qualification: facts with evidence, unknown stays unknown, conflicts instead of silent
overwrites, readiness, gaps, operator approval, idempotent extraction."""

import sqlite3
from pathlib import Path

import pytest

from app.core.enums import (
    ConfidenceBand,
    ConflictResolution,
    ConflictStatus,
    FactSource,
    LeadStage,
    QualificationStatus,
    RefKind,
)
from app.core.models import EntityRef, LeadQualification, QualificationFact
from app.operator import CommandRejectedError, RecordQualificationFact, StaleCommandError
from app.persistence import Database
from app.pipeline import FieldSpec, HookStatus, PipelineConfig, QualificationProfile
from app.pipeline.fake import FakeQualificationExtractor
from app.pipeline.qualification import gaps
from tests.inbound.builders import NOW, envelope, happy_transport, process
from tests.operator.builders import AS_ALICE
from tests.pipeline.builders import (
    REQUIRED,
    approve_qualification,
    extraction,
    inbound,
    lead,
    ops,
    pipeline,
    qualification,
    qualifying_lead,
    record_fact,
    resolve,
)

Q = QualificationStatus


def fact(q: LeadQualification | None, field: str) -> QualificationFact:
    assert q is not None
    found = q.fact(field)
    assert found is not None
    return found


def test_a_lead_without_a_record_is_not_started_and_nothing_is_invented(db: Database) -> None:
    result = inbound(db, facts={})  # the extractor found nothing
    assert result.lead_id is not None and qualification(db, result.lead_id) is None
    view = pipeline(db).view(result.lead_id)
    assert view.qualification_status is Q.NOT_STARTED and lead(db, result.lead_id).stage is LeadStage.INTERESTED


def test_first_facts_start_qualification_and_move_the_lead_to_qualifying(db: Database) -> None:
    lead_id = qualifying_lead(db, {"need": "Automate invoice matching"})
    q = qualification(db, lead_id)
    assert q is not None and q.status is Q.IN_PROGRESS and lead(db, lead_id).stage is LeadStage.QUALIFYING
    [fact] = q.facts
    [evidence] = fact.evidence
    assert (fact.field, fact.value, evidence.source) == ("need", "Automate invoice matching", FactSource.EXTRACTION)
    assert evidence.message_id is not None and evidence.conversation_id is not None


def test_unknown_stays_unknown(db: Database) -> None:
    low = FakeQualificationExtractor(default=extraction(ConfidenceBand.LOW, budget="maybe 50k"))
    result = inbound(db, extractor=low)
    assert result.lead_id is not None and qualification(db, result.lead_id) is None
    off_profile = FakeQualificationExtractor(default=extraction(favourite_colour="blue", need="Automate matching"))
    result = inbound(db, "p-2", extractor=off_profile, sender="second@prospect2.example")
    assert result.lead_id is not None
    q = qualification(db, result.lead_id)
    assert q is not None and [f.field for f in q.facts] == ["need"]  # only the profiled, confident fact


def test_gaps_are_planned_from_the_profile_in_priority_order(db: Database) -> None:
    lead_id = qualifying_lead(db, {"need": "Automate invoice matching", "timeframe": "Q3"})
    planned = pipeline(db).gaps(lead_id)
    assert [(g.field, g.reason) for g in planned] == [
        ("product_interest", "MISSING_REQUIRED"), ("decision_role", "MISSING_REQUIRED"),
        ("budget", "MISSING_OPTIONAL"), ("use_case", "MISSING_OPTIONAL"), ("company_size", "MISSING_OPTIONAL"),
        ("geography", "MISSING_OPTIONAL"),
    ]
    assert [g.safe_to_ask for g in planned if g.field == "budget"] == [False]  # an operator asks about money


def test_all_required_facts_make_it_ready_for_review_and_only_an_operator_qualifies(db: Database) -> None:
    lead_id = qualifying_lead(db)
    q = qualification(db, lead_id)
    assert q is not None and q.status is Q.READY_FOR_REVIEW and lead(db, lead_id).stage is LeadStage.QUALIFYING
    approve_qualification(db, lead_id)
    q = qualification(db, lead_id)
    assert q is not None and (q.status, q.decided_by) == (Q.QUALIFIED, "op-alice")
    assert lead(db, lead_id).stage is LeadStage.QUALIFIED


def test_approval_needs_readiness(db: Database) -> None:
    lead_id = qualifying_lead(db, {"need": "Automate invoice matching"})
    with pytest.raises(CommandRejectedError) as error:
        approve_qualification(db, lead_id)
    assert [c.value for c in error.value.codes] == ["QUALIFICATION_NOT_READY"]


def test_a_disagreeing_fact_becomes_a_conflict_and_never_overwrites(db: Database) -> None:
    lead_id = qualifying_lead(db, REQUIRED | {"budget": "50k EUR"})
    later = FakeQualificationExtractor(default=extraction(budget="20k EUR", need="automate   INVOICE matching"))
    process_and_hook(db, "p-2", later)
    q = qualification(db, lead_id)
    assert q is not None
    assert fact(q, "budget").value == "50k EUR"  # kept
    [conflict] = q.open_conflicts
    assert (conflict.field, conflict.current_value, conflict.proposed_value) == ("budget", "50k EUR", "20k EUR")
    assert len(fact(q, "need").evidence) == 2  # same value once normalized: corroborated
    assert q.status is Q.IN_PROGRESS  # a disputed fact is not ready for review
    process_and_hook(db, "p-3", later)  # the same disagreement again from another message
    q2 = qualification(db, lead_id)
    assert q2 is not None and len(q2.open_conflicts) == 2  # separately evidenced, never merged or overwritten


def process_and_hook(db: Database, provider_message_id: str, extractor: FakeQualificationExtractor) -> None:
    result = process(db, happy_transport(), envelope(provider_message_id, in_reply_to="<p-1@prospect.example>"))
    pipeline(db, extractor).record_inbound(result, correlation_id=f"corr-{provider_message_id}")


@pytest.mark.parametrize(("resolution", "expected"), [(ConflictResolution.KEEP_CURRENT, "50k EUR"),
                                                      (ConflictResolution.ACCEPT_PROPOSED, "20k EUR")])
def test_an_operator_resolves_a_conflict(db: Database, resolution: ConflictResolution, expected: str) -> None:
    lead_id = qualifying_lead(db, REQUIRED | {"budget": "50k EUR"})
    process_and_hook(db, "p-2", FakeQualificationExtractor(default=extraction(budget="20k EUR")))
    q = qualification(db, lead_id)
    assert q is not None
    [conflict] = q.open_conflicts
    resolve(db, lead_id, conflict.conflict_id, resolution)
    q = qualification(db, lead_id)
    assert fact(q, "budget").value == expected
    assert q is not None and q.conflicts[0].status is ConflictStatus.RESOLVED and q.status is Q.READY_FOR_REVIEW
    with pytest.raises(CommandRejectedError) as error:
        resolve(db, lead_id, conflict.conflict_id, resolution, command_id="cmd-resolve-again")
    assert [c.value for c in error.value.codes] == ["CONFLICT_NOT_OPEN"]


def test_a_stale_qualification_command_is_rejected(db: Database) -> None:
    lead_id = qualifying_lead(db, {"need": "Automate invoice matching"})
    q = qualification(db, lead_id)
    assert q is not None
    process_and_hook(db, "p-2", FakeQualificationExtractor(default=extraction(timeframe="Q4")))  # moves the version
    with pytest.raises(StaleCommandError) as error:
        ops(db).record_qualification_fact(AS_ALICE, RecordQualificationFact(
            command_id="cmd-stale", correlation_id="c", lead_id=lead_id, field="budget", value="10k",
            expected_qualification_version=q.version))
    assert [c.value for c in error.value.codes] == ["QUALIFICATION_VERSION_CHANGED"]


def test_operator_facts_are_evidenced_and_an_explicit_replacement_is_audited(db: Database) -> None:
    lead_id = qualifying_lead(db, {"need": "Automate invoice matching"})
    record_fact(db, lead_id, "decision_role", "CFO", command_id="cmd-fact-1")
    record_fact(db, lead_id, "need", "Replace spreadsheets", command_id="cmd-fact-2")
    q = qualification(db, lead_id)
    assert fact(q, "need").value == "Replace spreadsheets"
    assert fact(q, "decision_role").evidence[0].operator_command_id == "cmd-fact-1"
    with db.transaction() as uow:
        events = [e for e in uow.audit.list_by_event_type("QUALIFICATION_UPDATED", 50)]
    assert any((e.before or {}).get("replaced") == {"need": "Automate invoice matching"} for e in events)


def test_extraction_is_applied_once_per_message(db: Database) -> None:
    result = process(db, happy_transport(), envelope("p-1"))
    fake = FakeQualificationExtractor(default=extraction(need="Automate invoice matching"))
    first = pipeline(db, fake).record_inbound(result, correlation_id="c1")
    again = pipeline(db, fake).record_inbound(result, correlation_id="c2")
    assert (first.status, again.status) == (HookStatus.APPLIED, HookStatus.REPLAYED)
    q = qualification(db, result.lead_id or "")
    assert q is not None and q.version == 1 and len(q.facts[0].evidence) == 1


def test_an_extraction_failure_never_fails_inbound_and_records_nothing(db: Database) -> None:
    result = process(db, happy_transport(), envelope("p-1"))
    broken = FakeQualificationExtractor(default=RuntimeError("model unavailable"))
    outcome = pipeline(db, broken).record_inbound(result, correlation_id="c")
    assert outcome.status is HookStatus.EXTRACTION_FAILED and qualification(db, result.lead_id or "") is None
    retried = pipeline(db, FakeQualificationExtractor(default=extraction(need="X"))).record_inbound(result, correlation_id="c")
    assert retried.status is HookStatus.APPLIED  # a replay may try again


def test_the_extractor_only_proposes(db: Database, db_path: Path) -> None:
    """It gets the message text and known facts, never a database handle; while it runs
    nothing has been written, and its proposals only land through validation."""
    seen: list[int] = []

    def count_qualifications(_: object) -> None:
        connection = sqlite3.connect(db_path)
        try:
            seen.append(connection.execute("SELECT COUNT(*) FROM lead_qualifications").fetchone()[0])
        finally:
            connection.close()

    fake = FakeQualificationExtractor(default=extraction(need="Automate invoice matching"), before=count_qualifications)
    result = inbound(db, extractor=fake)
    [request] = fake.requests
    assert seen == [0] and set(type(request).model_fields) == {
        "lead_id", "message_id", "conversation_id", "message_text", "fields", "known_facts"}
    assert qualification(db, result.lead_id or "") is not None


def test_qualification_audit_holds_ids_and_normalized_values_not_bodies(db: Database) -> None:
    lead_id = qualifying_lead(db)
    with db.transaction() as uow:
        events = uow.audit.list_for_subject(EntityRef(kind=RefKind.LEAD_QUALIFICATION, id=lead_id))
    text = " ".join(e.model_dump_json() for e in events)
    assert events and "how much does the Basic plan cost" not in text and "Automate invoice matching" in text


def test_a_profile_is_configuration_not_a_hardcoded_methodology() -> None:
    meddic = PipelineConfig(profile=QualificationProfile(profile_id="meddic-lite", required=(
        FieldSpec(key="metrics", label="Metrics", priority=1), FieldSpec(key="champion", label="Champion", priority=2))))
    empty = LeadQualification(lead_id="ld-x", profile_id="meddic-lite", status=Q.IN_PROGRESS, created_at=NOW, updated_at=NOW)
    assert [g.field for g in gaps(meddic.profile, empty)] == ["metrics", "champion"]
