"""Deployment-level continuity in PRODUCTION mode over fakes beneath every provider:
Gmail -> semantic retrieval -> LLM draft -> Stage 14 -> Telegram -> Stage 7 approval -> Stage 8
-> Gmail, driven by ``service-tick`` like the scheduler, with process restarts (a new runtime
over the same database) at the risky points. Exactly one logical result, never a duplicate
send, nothing lost."""

import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.core.enums import EscalationReason
from app.orchestration import ExecutionAction as A
from app.persistence import Database, EmbeddingKey
from tests.deployment.builders import Fakes, scripted
from tests.gmail.fakes import FakeGmailApi, Send, customer_email
from tests.inbound.builders import NOW
from tests.llm_providers.builders import llm_values
from tests.llm_providers.fakes import failure
from tests.rag.builders import emb_values, intent, knowledge_dir, seeded_db
from tests.telegram.builders import Console, console
from tests.telegram.fakes import ALICE_CHAT

SENDER = "buyer@prospect.example"


def boot(tmp_path: Path, fakes: Fakes, at: datetime = NOW) -> Console:
    """A production runtime process over the shared database and the shared fake providers."""
    kb = tmp_path / "kb"
    if not kb.exists():
        seeded_db(tmp_path / "agent.sqlite3", knowledge_dir(tmp_path))
    c = console(tmp_path, gmail=True, at=at, telegram=fakes.telegram, gmail_api=fakes.gmail, llm_session=fakes.brain.session,
                embeddings_session=fakes.vendor.session, MODE="production", KNOWLEDGE_DIR=str(kb),
                **(llm_values("openai") | emb_values("openai")))
    with c.db.transaction() as uow:
        leads = [r[0] for r in uow._tx.fetch_all("SELECT lead_id FROM leads")]  # noqa: SLF001
    c.world.lead_id = leads[0] if leads else None
    return c


def started(tmp_path: Path, fakes: Fakes) -> Console:
    c = boot(tmp_path, fakes)
    assert c.app.knowledge_index().status.value == "OK"
    assert c.app.email_sync().status.value == "INITIALIZED"  # the first pass only sets the cursor
    return c


def restart(c: Console, tmp_path: Path, fakes: Fakes, after: timedelta = timedelta(0)) -> Console:
    now = c.world.clock.now() + after
    c.app.stop()
    return boot(tmp_path, fakes, at=now)


def ask(c: Console, fakes: Fakes, question: str = "How much is the Basic plan?", *, kind: str = "PRICING_REQUEST",
        message_id: str = "<q-1@prospect.example>", body: str | None = None) -> None:
    fakes.brain.script("IntentClassificationProposal", intent(question, kind))
    fakes.gmail.deliver(customer_email(sender=SENDER, body=body or f"Hello, {question}", message_id=message_id))


def sent(fakes: Fakes) -> list[bytes]:
    return [raw for raw, _ in fakes.gmail.sent_calls]


def approve_draft(c: Console) -> None:
    c.sync()
    [*_, button] = c.telegram.buttons_for(ALICE_CHAT, "Approve")
    c.telegram.press(ALICE_CHAT, button)
    assert c.sync().outcomes == {"ACTION": 1}  # Stage 7 approval committed


def unresolved(c: Console) -> int:
    with c.db.transaction() as uow:
        return len(uow.dispatch_attempts.list_unresolved())


# ---- Full pipeline and restarts -----------------------------------------------------------------------------


def test_full_pipeline_with_a_restart_after_approval_sends_exactly_once(tmp_path: Path) -> None:
    fakes = Fakes()
    scripted(fakes.brain)
    c = started(tmp_path, fakes)
    ask(c, fakes)
    cycle = c.app.service_tick()  # scheduler, no dispatch flag
    assert cycle.ok and cycle.email_sync["processed"] == 1  # type: ignore[index]
    c.world.lead_id = boot_lead(c)
    assert c.world.plan().action is A.REVIEW_REPLY_DRAFT  # Stage 14: the operator owns it
    card = [s for s in fakes.telegram.sent if s.chat_id == ALICE_CHAT][-1]
    assert card.text.startswith("Reply draft to review") and "79 EUR" in card.text  # sent in the same cycle
    approve_draft(c)
    assert sent(fakes) == []  # approval alone never sends

    c = restart(c, tmp_path, fakes)  # the process stops between approval and dispatch
    first = c.app.service_tick(dispatch_approved=True)
    assert first.ok and first.tick.dispatch.accepted == 1  # type: ignore[union-attr]
    again = c.app.service_tick(dispatch_approved=True)
    assert again.tick.dispatch.processed == 0  # type: ignore[union-attr]
    [raw] = sent(fakes)
    assert b"79 EUR" in raw and unresolved(c) == 0
    assert c.world.plan().action is A.WAIT_FOR_CUSTOMER
    [reply] = [m for m in c.world.messages() if m.kind.value == "REPLY"]
    assert reply.status.value == "SENT"
    c.app.stop()


def boot_lead(c: Console) -> str:
    with c.db.transaction() as uow:
        [lead_id] = [r[0] for r in uow._tx.fetch_all("SELECT lead_id FROM leads")]  # noqa: SLF001
    return lead_id


def test_an_unknown_send_survives_a_restart_and_is_reconciled_never_resent(tmp_path: Path) -> None:
    fakes = Fakes(gmail=FakeGmailApi(send_script=[Send.ACCEPT_THEN_LOSE_RESPONSE]))
    scripted(fakes.brain)
    c = started(tmp_path, fakes)
    ask(c, fakes)
    c.app.service_tick()
    approve_draft(c)
    unknown = c.app.service_tick(dispatch_approved=True)
    assert unknown.tick.dispatch.unknown == 1 and unresolved(c) == 1  # type: ignore[union-attr]
    c = restart(c, tmp_path, fakes)
    healed = c.app.service_tick(dispatch_approved=True)
    assert healed.tick.reconciliation.accepted == 1  # type: ignore[union-attr]
    assert len(fakes.gmail.sent_calls) == 1 and unresolved(c) == 0  # reconciled against Sent, never resent
    c.app.service_tick(dispatch_approved=True)
    assert len(fakes.gmail.sent_calls) == 1
    c.app.stop()


def test_a_transient_extraction_failure_is_recovered_after_a_restart(tmp_path: Path) -> None:
    fakes = Fakes()
    scripted(fakes.brain)
    c = started(tmp_path, fakes)
    fakes.brain.script("QualificationCandidates", failure("openai", "TEMPORARY_PROVIDER_ERROR"))
    ask(c, fakes)
    c.app.service_tick()

    def statuses(console_: Console) -> list[str]:
        with console_.db.transaction() as uow:
            return [r[0] for r in uow._tx.fetch_all("SELECT status FROM ai_enrichment_jobs ORDER BY job_id")]  # noqa: SLF001

    assert "RETRY_WAIT" in statuses(c)
    calls = fakes.brain.calls.count("QualificationCandidates")
    c = restart(c, tmp_path, fakes, after=timedelta(minutes=10))  # past the first backoff
    assert c.app.service_tick().ai_recovery["status"] == "OK"  # type: ignore[index]
    assert "RETRY_WAIT" not in statuses(c) and set(statuses(c)) == {"COMPLETED"}
    assert fakes.brain.calls.count("QualificationCandidates") == calls + 1  # exactly one more attempt
    c.app.service_tick()
    assert fakes.brain.calls.count("QualificationCandidates") == calls + 1  # completed jobs never run again
    c.app.stop()


def test_an_index_run_killed_mid_batch_recovers_after_restart(tmp_path: Path) -> None:
    fakes = Fakes()
    c = boot(tmp_path, fakes)

    def crash(texts: list[str]) -> None:
        raise KeyboardInterrupt  # the process is interrupted while the provider call is in flight

    fakes.vendor.hook = crash
    with pytest.raises(KeyboardInterrupt):
        c.app.knowledge_index()
    fakes.vendor.hook = None
    with c.db.transaction() as uow:
        assert uow.knowledge_embeddings.count_claims() == 0 and uow.knowledge_embeddings.count_space(("OPENAI", "openai-embed-under-test", 0)) == 0
    # A hard kill (no cleanup at all) leaves its claims behind:
    import hashlib
    import json
    with c.db.transaction() as uow:
        chunks = [json.loads(r[0]) for r in uow._tx.fetch_all("SELECT data FROM knowledge_chunks")]  # noqa: SLF001
        for chunk in chunks:
            key = EmbeddingKey(chunk_id=chunk["chunk_id"], provider="OPENAI", model="openai-embed-under-test",
                               requested_dimensions=0, input_hash=hashlib.sha256(chunk["text"].encode()).hexdigest())
            uow.knowledge_embeddings.claim(key, "kec_killed", NOW + timedelta(minutes=5), NOW)
    c = restart(c, tmp_path, fakes)
    blocked = c.app.knowledge_index()
    assert blocked.embeddings.embedded == 0 and blocked.embeddings.skipped == 8  # type: ignore[union-attr]
    c = restart(c, tmp_path, fakes, after=timedelta(minutes=6))  # the dead claims' leases expired
    healed = c.app.knowledge_index()
    assert (healed.status.value, healed.embeddings.embedded) == ("OK", 8)  # type: ignore[union-attr]
    from tests.rag.builders import query, retriever
    with Database(tmp_path / "agent.sqlite3") as db:
        result = retriever(db, fakes.vendor).evaluate(query("What does the Basic plan cost per month?"), NOW)
    assert any("79 EUR" in e.excerpt for e in result.evidence)  # vectors are intact and searchable
    c.app.stop()


def test_an_unsupported_capability_question_is_never_drafted_or_sent(tmp_path: Path) -> None:
    fakes = Fakes()
    fakes.brain.script("ReplyDraftProposal", {"subject": "Re", "body": "Yes, we support SSO with Okta.",
                                              "evidence_ids_used": [], "proposed_next_step": "ANSWER_QUESTIONS"})
    c = started(tmp_path, fakes)
    ask(c, fakes, "Do you support SSO?", kind="INFO_REQUEST")
    c.app.service_tick(dispatch_approved=True)
    c.app.service_tick(dispatch_approved=True)
    assert "ReplyDraftProposal" not in fakes.brain.calls and sent(fakes) == []
    with c.db.transaction() as uow:
        reasons = {r for row in uow._tx.fetch_all("SELECT data FROM escalations")  # noqa: SLF001
                   for r in __import__("json").loads(row[0])["reasons"]}
    assert EscalationReason.KNOWLEDGE_INSUFFICIENT.value in reasons
    c.app.stop()


def test_overlapping_scheduler_runs_send_an_approved_reply_once(tmp_path: Path) -> None:
    fakes = Fakes()
    scripted(fakes.brain)
    c = started(tmp_path, fakes)
    ask(c, fakes)
    c.app.service_tick()
    approve_draft(c)
    at = c.world.clock.now()
    c.app.stop()
    errors: list[BaseException] = []
    results: list[object] = []
    barrier = threading.Barrier(2)

    def process() -> None:  # one scheduler invocation: its own runtime and connection
        try:
            own = boot(tmp_path, fakes, at=at)
            barrier.wait()
            results.append(own.app.tick(dispatch_approved=True))
            own.app.stop()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    for _ in range(3):  # repeat the race; later rounds find nothing left to send
        threads = [threading.Thread(target=process) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
    assert errors == [] and len(sent(fakes)) == 1, [r.dispatch for r in results]  # type: ignore[attr-defined]
    assert all(r.dispatch.status.value != "ERROR" for r in results)  # type: ignore[attr-defined]


def test_the_kill_switch_stops_sends_without_losing_the_approval(tmp_path: Path) -> None:
    fakes = Fakes()
    scripted(fakes.brain)
    c = started(tmp_path, fakes)
    ask(c, fakes)
    c.app.service_tick()
    approve_draft(c)
    c.app.stop()
    from tests.telegram.builders import console as make
    held = make(tmp_path, gmail=True, at=c.world.clock.now(), telegram=fakes.telegram, gmail_api=fakes.gmail,
                llm_session=fakes.brain.session, embeddings_session=fakes.vendor.session, MODE="production",
                KNOWLEDGE_DIR=str(tmp_path / "kb"), KILL_SWITCH="true", KILL_SWITCH_REASON="incident",
                **(llm_values("openai") | emb_values("openai")))
    held.app.service_tick(dispatch_approved=True)
    assert sent(fakes) == []  # refused at the Stage 8 gate
    held.app.stop()
    resumed = boot(tmp_path, fakes, at=c.world.clock.now())
    resumed.app.service_tick(dispatch_approved=True)
    assert len(sent(fakes)) == 1  # the approval was kept; sent once the switch is off
    resumed.app.stop()
