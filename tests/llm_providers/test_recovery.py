"""Durable AI enrichment recovery: a Stage 12/13 extraction that fails transiently is
retried by the one-shot ``ai-recovery-tick`` from the stored Stage 6 result, after a
restart, without any mailbox redelivery; claims keep it to one model call at a time; only
transient failures are retried, with bounded backoff; nothing is applied twice."""

import io
import json
import sqlite3
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from app.enrichment import EnrichmentConfig, job_id_for
from app.orchestration import ExecutionAction as A
from app.orchestration import ExecutionOwner as O
from app.persistence import AIEnrichmentJob, Database, EnrichmentJobStatus, EnrichmentKind, FrozenClock
from app.persistence.migrations import MIGRATIONS, apply_migrations, current_version, latest_version
from app.runtime import Adapters, SalesAgentRuntime, cli, load_config
from tests.campaign.builders import PROSPECT, campaign_messages
from tests.inbound.builders import NOW
from tests.llm_providers.builders import live, llm_values
from tests.llm_providers.fakes import API_KEY, Brain, failure, ok
from tests.llm_providers.test_end_to_end import FACTS_MESSAGE, GROUNDED_FACTS, first_touch, qualified_with_presented_proposal
from tests.operator.builders import FakeAuthenticator
from tests.orchestration.builders import customer_replies
from tests.runtime.builders import env
from tests.telegram.builders import Console, console, fake_connectors, telegram_values
from tests.telegram.fakes import ALICE_CHAT
from tests.telegram.test_end_to_end import plan, press
from tests.telegram.test_races_and_cards import in_parallel

Q, C = EnrichmentKind.QUALIFICATION_EXTRACTION, EnrichmentKind.COMMERCIAL_EXTRACTION
S = EnrichmentJobStatus
FIRST_BACKOFF = EnrichmentConfig().backoff[0]


def jobs(db: Database) -> list[AIEnrichmentJob]:
    with db.transaction() as uow:
        rows = uow._tx.fetch_all("SELECT job_id FROM ai_enrichment_jobs ORDER BY rowid")  # noqa: SLF001
        return [uow.enrichment_jobs.get(r[0]) for r in rows]  # type: ignore[misc]


def job(db: Database, kind: EnrichmentKind) -> AIEnrichmentJob:
    [found] = [j for j in jobs(db) if j.kind is kind]
    return found


def facts(db: Database, lead_id: str) -> int:
    with db.transaction() as uow:
        record = uow.qualifications.get(lead_id)
    return len(record.facts) if record else 0


def outage_on_facts(tmp_path: Path, error: str = "TEMPORARY_PROVIDER_ERROR", provider: str = "openai") -> tuple[Console, Brain]:
    c, brain = live(tmp_path, provider)
    first_touch(c)
    brain.script("QualificationCandidates", failure(provider, error))
    customer_replies(c.world, "p-facts", body=FACTS_MESSAGE)
    return c, brain


# ---- The core regression ---------------------------------------------------------------------------------


def test_gmail_inbound_survives_an_outage_and_a_restart_without_redelivery(tmp_path: Path) -> None:
    from tests.gmail.fakes import customer_email
    c, brain = live(tmp_path, "anthropic", gmail=True)
    assert c.app.email_sync().status.value == "INITIALIZED"
    first_touch(c)
    [first] = campaign_messages(c.db, c.world.lead)
    brain.script("QualificationCandidates", failure("anthropic", "TEMPORARY_PROVIDER_ERROR"))
    c.gmail.deliver(customer_email(sender=PROSPECT, body=FACTS_MESSAGE, message_id="<facts@acme-prospect.example>",
                                   in_reply_to=first.rfc_message_id))
    synced = c.app.email_sync()
    assert (synced.status.value, synced.processed) == ("OK", 1)  # stored; the cursor moved on normally
    lead = c.world.lead
    pending = job(c.db, Q)
    assert (pending.status, pending.attempts, pending.last_error_code) == (S.RETRY_WAIT, 1, "TEMPORARY_PROVIDER_ERROR")
    assert facts(c.db, lead) == 0  # nothing applied, nothing invented
    assert c.app.email_sync().processed == 0  # Gmail never delivers it again
    at = c.world.clock.now() + FIRST_BACKOFF
    c.app.stop()
    # A new process later; the provider has recovered. Same mailbox, nothing redelivered.
    healthy = Brain().script("QualificationCandidates", GROUNDED_FACTS)
    again = console(tmp_path, gmail=True, at=at, telegram=c.telegram, gmail_api=c.gmail, llm_session=healthy.session,
                    **llm_values("anthropic"))
    assert again.app.email_sync().processed == 0  # no history replay, no cursor rewind
    recovered = again.app.ai_recovery_tick()
    assert (recovered.due, recovered.completed, recovered.retry_scheduled, recovered.failed_final) == (1, 1, 0, 0)
    assert healthy.calls == ["QualificationCandidates"]  # Stage 6 never re-ran: the stored result was used
    done = job(again.db, Q)
    assert (done.status, done.outcome, done.attempts) == (S.COMPLETED, "APPLIED", 2)
    assert facts(again.db, lead) == 4
    assert again.app.ai_recovery_tick().due == 0  # never again
    assert facts(again.db, lead) == 4 and healthy.calls == ["QualificationCandidates"]
    # Stage 14 sees the new state; the operator gets the card in Telegram.
    view = again.app.services.pipeline.view(lead)
    assert view.qualification_status.value == "READY_FOR_REVIEW"
    again.sync()
    assert any(s.chat_id == ALICE_CHAT for s in again.telegram.sent)


def test_commercial_extraction_recovers_and_still_needs_the_operator(tmp_path: Path) -> None:
    c, brain = live(tmp_path, "gemini")
    qualified_with_presented_proposal(c, brain)
    brain.script("CommercialCandidates", failure("gemini", "RATE_LIMITED"),
                 {"acceptance_quote": "We accept your proposal."})
    customer_replies(c.world, "p-accept", body="We accept your proposal. What does the Basic plan cost per month?")
    pending = job(c.db, C) if len([j for j in jobs(c.db) if j.kind is C]) == 1 else [j for j in jobs(c.db) if j.kind is C][-1]
    assert (pending.status, pending.last_error_code) == (S.RETRY_WAIT, "RATE_LIMITED")
    calls = len(brain.session.posts)
    assert c.app.ai_recovery_tick().due == 0 and len(brain.session.posts) == calls  # not yet due: no request, no wait
    c.world.advance(FIRST_BACKOFF)
    assert c.app.ai_recovery_tick().completed == 1
    assert c.world.plan().action in (A.REVIEW_REPLY_DRAFT, A.CONFIRM_ACCEPTANCE)
    assert press(c, "Approve") == {"ACTION": 1}
    c.world.execute(dispatch=True)
    plan(c, O.OPERATOR, A.CONFIRM_ACCEPTANCE)  # a signal for the operator, never WON
    assert c.world.lead_row().stage.value != "CLOSED"


# ---- Retry policy -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("error", ["AUTH_INVALID", "MODEL_NOT_FOUND", "BAD_REQUEST"])
def test_non_transient_failures_are_final_after_one_call(tmp_path: Path, error: str) -> None:
    c, brain = outage_on_facts(tmp_path, error)
    final = job(c.db, Q)
    assert (final.status, final.last_error_code, final.attempts) == (S.FAILED_FINAL, error, 1)
    calls = brain.calls.count("QualificationCandidates")
    for _ in range(3):
        c.world.advance(timedelta(days=1))
        assert c.app.ai_recovery_tick().due == 0
    assert brain.calls.count("QualificationCandidates") == calls == 1  # no retry storm
    c.telegram.text(ALICE_CHAT, "/status")
    c.sync()
    [status] = [t for t in c.telegram.texts(ALICE_CHAT) if t.startswith("Runtime:")]
    assert "AI enrichment: 0 waiting to retry, 1 failed (need an operator)" in status  # operator-visible
    c.app.stop()
    assert API_KEY.encode() not in (tmp_path / "agent.sqlite3").read_bytes()


@pytest.mark.parametrize(("answer", "code"), [
    ("{not json", "SCHEMA_VALIDATION_FAILED"),
    (json.dumps({"facts": [{"field": "need", "value": "Payroll", "confidence": "HIGH", "quote": "we want payroll"}]}),
     "CONTRACT_VIOLATION"),
])
def test_invalid_model_output_is_final_and_applies_nothing(tmp_path: Path, answer: str, code: str) -> None:
    c, brain = live(tmp_path, "openai")
    first_touch(c)
    brain.script("QualificationCandidates", ok("openai", answer))
    customer_replies(c.world, "p-facts", body=FACTS_MESSAGE)
    final = job(c.db, Q)
    assert (final.status, final.last_error_code) == (S.FAILED_FINAL, code)
    assert facts(c.db, c.world.lead) == 0  # no fabricated or partial apply
    c.world.advance(timedelta(days=2))
    assert c.app.ai_recovery_tick().due == 0 and brain.calls.count("QualificationCandidates") == 1


def test_transient_failures_back_off_and_stop_after_the_last_attempt(tmp_path: Path) -> None:
    config = EnrichmentConfig()
    c, brain = outage_on_facts(tmp_path)
    brain.script("QualificationCandidates", *[failure("openai", "TEMPORARY_PROVIDER_ERROR")] * 10)
    waits = []
    for attempt in range(2, config.max_attempts + 1):
        current = job(c.db, Q)
        waits.append(current.due_at - c.world.clock.now())
        c.world.advance(current.due_at - c.world.clock.now() - timedelta(seconds=1))
        assert c.app.ai_recovery_tick().due == 0  # one second early: nothing runs
        c.world.advance(timedelta(seconds=1))
        assert c.app.ai_recovery_tick().due == 1
        assert job(c.db, Q).attempts == attempt
    assert waits == list(config.backoff)  # deterministic, growing backoff
    final = job(c.db, Q)
    assert (final.status, final.attempts) == (S.FAILED_FINAL, config.max_attempts)
    c.world.advance(timedelta(days=7))
    assert c.app.ai_recovery_tick().due == 0
    assert brain.calls.count("QualificationCandidates") == config.max_attempts  # bounded, exactly


def test_an_abandoned_claim_is_recovered_after_its_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c, brain = live(tmp_path, "openai")
    first_touch(c)
    pipeline = c.app.services.pipeline

    def crash(*args: object, **kwargs: object) -> object:
        raise KeyboardInterrupt  # the process dies while it holds the claim

    real = pipeline.record_inbound
    monkeypatch.setattr(pipeline, "record_inbound", crash)
    with pytest.raises(KeyboardInterrupt):
        customer_replies(c.world, "p-facts", body=FACTS_MESSAGE)
    monkeypatch.setattr(pipeline, "record_inbound", real)
    assert job(c.db, Q).status is S.CLAIMED
    assert c.app.ai_recovery_tick().due == 0  # the lease still holds: nobody calls the model
    c.world.advance(EnrichmentConfig().lease)
    brain.script("QualificationCandidates", GROUNDED_FACTS)
    assert c.app.ai_recovery_tick().completed == 1
    assert facts(c.db, c.world.lead) == 4


def test_a_stale_claim_cannot_settle(tmp_path: Path) -> None:
    c, _ = outage_on_facts(tmp_path)
    enrichment = c.app.services.enrichment
    c.world.advance(FIRST_BACKOFF)
    first = enrichment._claim(job_id_for(Q, job(c.db, Q).message_id))  # noqa: SLF001
    assert first is not None and enrichment._claim(first[1].job_id) is None  # noqa: SLF001 - held
    c.world.advance(EnrichmentConfig().lease)
    second = enrichment._claim(first[1].job_id)  # noqa: SLF001 - the lease expired: another worker
    assert second is not None and second[0] != first[0]
    assert enrichment._settle(first[1], first[0], S.COMPLETED, outcome="APPLIED") == "not_claimed"  # noqa: SLF001
    assert job(c.db, Q).status is S.CLAIMED
    assert enrichment._settle(second[1], second[0], S.COMPLETED, outcome="APPLIED") == "completed"  # noqa: SLF001


# ---- Concurrency --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("attempt", range(12))
def test_two_recovery_workers_make_one_model_call(tmp_path: Path, attempt: int) -> None:
    c, _ = outage_on_facts(tmp_path)
    lead, at = c.world.lead, c.world.clock.now() + FIRST_BACKOFF
    c.app.stop()
    brains = [Brain().script("QualificationCandidates", _slow(GROUNDED_FACTS)) for _ in range(2)]
    barrier = threading.Barrier(2)

    def worker(brain: Brain):  # noqa: ANN202 - its own runtime, connection and provider session
        config = load_config(env(tmp_path / "agent.sqlite3", **(telegram_values() | llm_values("openai"))), now=NOW)
        app = SalesAgentRuntime(config, adapters=Adapters(authenticator=FakeAuthenticator()), clock=FrozenClock(at),
                                connectors=fake_connectors(llm_session=brain.session))
        app.start()
        try:
            barrier.wait(timeout=30)
            return app.ai_recovery_tick()
        finally:
            app.stop()

    results = in_parallel(lambda: worker(brains[0]), lambda: worker(brains[1]))
    assert not any(isinstance(r, BaseException) for r in results), results
    assert sum(len(b.session.posts) for b in brains) == 1  # exactly one model request
    assert sorted(r.completed for r in results) == [0, 1]  # type: ignore[union-attr]
    with Database(tmp_path / "agent.sqlite3") as db:
        done = job(db, Q)
        assert (done.status, done.outcome) == (S.COMPLETED, "APPLIED") and facts(db, lead) == 4


def _slow(answer: dict):  # noqa: ANN202
    def respond(data: dict) -> dict:
        time.sleep(0.05)  # the request is in flight while the other worker tries to claim
        return answer
    return respond


# ---- Runtime surface ----------------------------------------------------------------------------------------


def test_the_recovery_command_is_one_bounded_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.runtime.builders import runtime
    offline = runtime(tmp_path / "offline.sqlite3", adapters=Adapters(authenticator=FakeAuthenticator()))  # no LLM
    offline.start()
    assert offline.ai_recovery_tick().model_dump(include={"status", "reason"}) == {"status": "SKIPPED",
                                                                                  "reason": "LLM_NOT_CONFIGURED"}
    offline.stop()
    monkeypatch.setattr(cli, "CONNECTORS", fake_connectors(llm_session=Brain().session))
    environ = env(tmp_path / "cli.sqlite3", **(telegram_values() | llm_values("openai")))
    assert cli.main(["init"], environ, io.StringIO()) == 0
    out = io.StringIO()
    assert cli.main(["ai-recovery-tick"], environ, out) == 0
    assert json.loads(out.getvalue()) == {"status": "OK", "reason": None, "due": 0, "completed": 0, "retry_scheduled": 0,
                                          "failed_final": 0, "not_claimed": 0}


def test_a_replay_of_a_completed_message_calls_no_model(tmp_path: Path) -> None:
    c, brain = live(tmp_path, "openai")
    first_touch(c)
    brain.script("QualificationCandidates", GROUNDED_FACTS)
    customer_replies(c.world, "p-facts", body=FACTS_MESSAGE)
    calls = list(brain.calls)
    from tests.inbound.builders import envelope
    replay = c.world.app.handle_inbound(envelope("p-facts", sender=PROSPECT, body=FACTS_MESSAGE,
                                                 received_at=c.world.clock.now()), correlation_id="redelivery")
    assert replay.replayed and brain.calls == calls  # neither Stage 6 nor an enrichment job called the model again
    assert facts(c.db, c.world.lead) == 4


# ---- Migration ---------------------------------------------------------------------------------------------


def test_a_fresh_database_reaches_v12_with_a_minimal_job_table(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite3"
    with Database(path) as db:
        assert db.initialize_schema(FrozenClock(NOW)) == latest_version() >= 12
    raw = sqlite3.connect(path)
    try:
        assert current_version(raw) == latest_version() and MIGRATIONS[11].name == "ai_enrichment_jobs"
        columns = [row[1] for row in raw.execute("PRAGMA table_info(ai_enrichment_jobs)")]
    finally:
        raw.close()
    assert columns == ["job_id", "kind", "message_id", "lead_id", "status", "due_at", "updated_at", "version", "data"]
    fields = set(AIEnrichmentJob.model_fields)
    assert not fields & {"prompt", "response", "body", "text", "api_key", "key", "raw", "model_output"}


def test_a_v11_database_upgrades_intact_and_repeatably(tmp_path: Path) -> None:
    from tests.inbound.conftest import seed_knowledge
    from tests.pipeline.builders import active_opportunity, lead, opportunity_lead
    path = tmp_path / "stage17.sqlite3"
    raw = sqlite3.connect(path, isolation_level=None)
    raw.execute("PRAGMA foreign_keys = ON")
    try:
        assert apply_migrations(raw, FrozenClock(NOW), MIGRATIONS[:11]) == 11
    finally:
        raw.close()
    with Database(path) as db:
        seed_knowledge(db)
        lead_id = opportunity_lead(db)
        before = (lead(db, lead_id), active_opportunity(db, lead_id))
    with Database(path) as db:
        assert db.initialize_schema(FrozenClock(NOW + timedelta(days=1))) == latest_version()
        assert db.initialize_schema(FrozenClock(NOW + timedelta(days=2))) == latest_version()  # idempotent
        assert (lead(db, lead_id), active_opportunity(db, lead_id)) == before
        assert jobs(db) == []  # nothing backfilled
        with db.transaction() as uow:
            assert uow.operator_channel.get_state("telegram", "424242") is None


def test_jobs_hold_no_content_prompt_response_or_key(tmp_path: Path) -> None:
    c, _ = outage_on_facts(tmp_path)
    with c.db.transaction() as uow:
        rows = [r[0] for r in uow._tx.fetch_all("SELECT data FROM ai_enrichment_jobs")]  # noqa: SLF001
    blob = "\n".join(rows)
    assert rows and "invoice" not in blob and "Head of finance" not in blob and API_KEY not in blob
    assert "secret detail" not in blob and "Output contract" not in blob
