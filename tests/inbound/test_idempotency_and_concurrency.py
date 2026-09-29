import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from app.core.enums import DNCScope, EscalationReason, LeadIntent, RefKind, ReplyDecision
from app.core.models import EntityRef
from app.inbound import IdempotencyCollisionError, InboundResult
from app.llm import LLMTask
from app.persistence import Database
from tests.inbound.builders import (
    NOW,
    SENDER,
    ScriptedTransport,
    classification,
    envelope,
    happy_transport,
    process,
    service,
)

C = LLMTask.INTENT_CLASSIFICATION


def counts(db: Database, message_id: str) -> dict[str, int]:
    with db.transaction() as uow:
        events = uow.audit.list_for_subject(EntityRef(kind=RefKind.EMAIL_MESSAGE, id=message_id))
        message = uow.messages.get(message_id)
        assert message is not None
        thread_ids = {m.thread_id for m in uow.messages.list_by_thread(message.thread_id)}
        contact = uow.contacts.get_by_email(SENDER)
        return {
            "completed": sum(e.event_type == "PROCESSING_COMPLETED" for e in events),
            "observed": sum(e.event_type == "INBOUND_OBSERVED" for e in events),
            "threads": len(thread_ids),
            "leads": len(uow.leads.list_by_contact(contact.contact_id)) if contact else 0,
            "drafts": sum(len(uow.outbound.list_by_lead(lead.lead_id)) for lead in (uow.leads.list_by_contact(contact.contact_id) if contact else [])),
        }


def test_replay_after_escalation_returns_the_same_outcome(db: Database) -> None:
    transport = ScriptedTransport().script(C, classification(LeadIntent.LEGAL_OR_COMPLAINT))
    first = process(db, transport)
    replay = process(db, ScriptedTransport())
    assert replay.replayed and replay.escalation_id == first.escalation_id
    with db.transaction() as uow:
        assert len(uow.escalations.list_by_lead(first.lead_id or "")) == 1


def test_crash_after_observation_is_resumed_safely(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    crashing = service(db, happy_transport())

    def crash(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt  # not caught by the service: simulates a killed process

    monkeypatch.setattr(crashing, "_analyze", crash)
    with pytest.raises(KeyboardInterrupt):
        crashing.process(envelope(), correlation_id="corr-1")

    resumed = process(db, happy_transport())
    assert resumed.reply_decision is ReplyDecision.DRAFT_FOR_REVIEW and not resumed.replayed
    assert counts(db, resumed.message_id) == {"completed": 1, "observed": 1, "threads": 1, "leads": 1, "drafts": 1}
    again = process(db, ScriptedTransport())
    assert again.replayed and again.draft_id == resumed.draft_id


def test_same_provider_id_with_different_content_is_a_collision(db: Database) -> None:
    first = process(db, happy_transport())
    with pytest.raises(IdempotencyCollisionError):
        process(db, ScriptedTransport(), envelope(body="Completely different text"))
    with db.transaction() as uow:
        message = uow.messages.get(first.message_id)
    assert message is not None and "Basic plan" in message.body_text  # stored observation unchanged


def test_same_internet_message_id_twice_is_a_duplicate(db: Database) -> None:
    first = process(db, happy_transport())
    redelivery = envelope("p-other", internet_message_id="<p-1@prospect.example>", raw_hash=envelope().raw_hash)
    duplicate = process(db, ScriptedTransport(), redelivery)
    assert duplicate.duplicate and duplicate.message_id == first.message_id
    assert duplicate.reply_decision is first.reply_decision
    with db.transaction() as uow:
        assert uow.messages.get_by_rfc_message_id("<p-1@prospect.example>") is not None
    assert counts(db, first.message_id)["drafts"] == 1
    # Replaying the duplicate delivery is stable too.
    assert process(db, ScriptedTransport(), redelivery).duplicate


def test_same_message_id_with_different_content_is_a_collision(db: Database) -> None:
    process(db, happy_transport())
    with pytest.raises(IdempotencyCollisionError):
        process(db, ScriptedTransport(), envelope("p-other", body="Different", internet_message_id="<p-1@prospect.example>"))


def test_same_body_with_different_ids_is_not_a_duplicate(db: Database) -> None:
    first = process(db, happy_transport(), envelope("p-1"))
    second = process(db, happy_transport(), envelope("p-2"))
    assert first.message_id != second.message_id and not second.duplicate
    assert second.reply_decision is ReplyDecision.DRAFT_FOR_REVIEW


def test_missing_internet_message_id_uses_the_provider_identity(db: Database) -> None:
    first = process(db, happy_transport(), envelope(internet_message_id=None))
    assert process(db, ScriptedTransport(), envelope(internet_message_id=None)).replayed
    with db.transaction() as uow:
        message = uow.messages.get(first.message_id)
    assert message is not None and message.rfc_message_id == "<fake.p-1@sales@ourco.example>"


# ---- 35. Concurrency: two workers, one message ----------------------------------------------------


def run_workers(db_path: Path, make_transport: Callable[[], ScriptedTransport], body: str | None = None) -> list[InboundResult]:
    barrier = threading.Barrier(2)
    results: list[InboundResult] = []
    errors: list[BaseException] = []

    def worker() -> None:
        with Database(db_path, busy_timeout_ms=10_000) as db:
            transport = make_transport()
            barrier.wait()
            try:
                results.append(service(db, transport).process(envelope(body=body) if body else envelope(), correlation_id="corr-1"))
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    return results


def test_two_workers_produce_one_canonical_outcome(db_path: Path) -> None:
    results = run_workers(db_path, happy_transport)
    assert len(results) == 2
    a, b = (r.model_copy(update={"replayed": False}) for r in results)
    assert a == b and a.reply_decision is ReplyDecision.DRAFT_FOR_REVIEW
    with Database(db_path) as db:
        assert counts(db, a.message_id) == {"completed": 1, "observed": 1, "threads": 1, "leads": 1, "drafts": 1}


def test_two_workers_unsubscribing_create_one_dnc(db_path: Path) -> None:
    def unsubscribe() -> ScriptedTransport:
        return ScriptedTransport().script(C, classification(LeadIntent.UNSUBSCRIBE))

    results = run_workers(db_path, unsubscribe, body="Please unsubscribe me.")
    assert {r.reply_decision for r in results} == {ReplyDecision.NO_ACTION}
    with Database(db_path) as db, db.transaction() as uow:
        assert len(uow.dnc.list_for_value(DNCScope.EMAIL, SENDER)) == 1
        assert len(uow.dnc.list_active(DNCScope.EMAIL, SENDER, NOW)) == 1
    assert EscalationReason.INTERNAL_ERROR not in {r for res in results for r in res.escalation_reasons}
