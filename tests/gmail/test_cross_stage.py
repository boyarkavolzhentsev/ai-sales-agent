"""Gmail through the existing stages: inbound -> Stage 6 -> campaign handoff -> Stage 14;
Stage 14 -> Stage 8 -> Gmail; UNKNOWN -> reconciliation; DNC and approval gates; startup,
CLI and concurrency. Fake Gmail API only."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.commercial.fake import FakeCommercialExtractor
from app.core.enums import CampaignMemberStatus, OutboundStatus
from app.dispatch import DispatchRequest, DispatchService
from app.integrations.gmail.reconciliation import GmailReconciler
from app.integrations.gmail.transport import GmailTransport
from app.orchestration import ExecutionAction as A
from app.orchestration import ExecutionOutcome as X
from app.orchestration import ExecutionOwner as O
from app.persistence import Database, FrozenClock, MailboxSyncStatus
from app.pipeline.fake import FakeQualificationExtractor
from app.runtime import Adapters, StartupError, load_config
from app.runtime import cli
from tests.campaign.builders import PROSPECT, campaign_messages
from tests.conversation.test_races_and_recovery import run_concurrently
from tests.gmail.builders import GMAIL_SECRETS, connectors, gmail_env, gmail_runtime
from tests.gmail.fakes import ACCOUNT, FakeGmailApi, Send, StoredMessage, customer_email
from tests.inbound.builders import NOW
from tests.inbound.conftest import seed_knowledge
from tests.runtime.builders import app_db
from tests.operator.builders import FakeAuthenticator
from tests.orchestration.builders import World, answering_llm, approve_pending, enrolled, suppress
from tests.pipeline.builders import extraction

APP = Path(__file__).resolve().parents[2] / "app"


def gmail_world(tmp_path: Path, api: FakeGmailApi) -> World:
    clock = FrozenClock(NOW)
    llm = answering_llm()
    qualification, commercial = FakeQualificationExtractor(default=extraction()), FakeCommercialExtractor()
    adapters = Adapters(llm_transport=llm, authenticator=FakeAuthenticator(), qualification_extractor=qualification,
                        commercial_extractor=commercial)
    app = gmail_runtime(tmp_path, api, adapters=adapters, clock=clock)
    app.start()
    seed_knowledge(app_db(app))  # the approved knowledge Stage 6 answers from
    return World(app=app, clock=clock, transport=None, llm=llm, qualification=qualification, commercial=commercial)  # type: ignore[arg-type]


def first_touch_sent(w: World, api: FakeGmailApi) -> str:
    """A campaign first touch drafted, approved by the operator and sent through Gmail."""
    enrolled(w)
    assert w.execute().subsystem_outcome == "DRAFT_CREATED"
    approve_pending(w)
    sent = w.execute(dispatch=True)
    assert sent.outcome is X.EXECUTED
    [message] = campaign_messages(w.db, w.lead)
    return message.outbound_id


# ---- Inbound -> Stage 6 -> campaign handoff -> Stage 14 ------------------------------------------


def test_a_gmail_reply_flows_through_stage6_into_a_new_plan(tmp_path: Path) -> None:
    api = FakeGmailApi()
    w = gmail_world(tmp_path, api)
    assert w.app.email_sync().status.value == "INITIALIZED"  # the operator's first sync: checkpoint only
    outbound_id = first_touch_sent(w, api)
    [message] = campaign_messages(w.db, w.lead)
    assert message.status is OutboundStatus.SENT and message.provider_message_id in api.messages
    gmail_copy = api.messages[message.provider_message_id]
    assert "SENT" in gmail_copy.labels and str(gmail_copy.headers["To"]) == PROSPECT
    assert str(gmail_copy.headers["Message-ID"]) == message.rfc_message_id
    api.deliver(customer_email(sender=PROSPECT, message_id="<reply-1@acme-prospect.example>", in_reply_to=message.rfc_message_id))
    synced = w.app.email_sync()
    assert (synced.status.value, synced.processed, synced.filtered) == ("OK", 1, 1)
    assert synced.filtered_reasons == {"LABEL_SENT": 1}  # our own first touch never re-enters as customer mail
    with w.db.transaction() as uow:
        member = uow.campaign_members.get(w.member_id or "")
    assert member is not None and member.status is CampaignMemberStatus.REPLIED  # Stage 10 handoff, untouched
    plan = w.plan()
    assert (plan.owner, plan.action) == (O.OPERATOR, A.REVIEW_REPLY_DRAFT)  # Stage 6 drafted; Stage 14 replanned
    again = w.app.email_sync()
    assert (again.processed, again.duplicates) == (0, 0) and outbound_id
    w.app.stop()


def test_the_same_gmail_message_is_processed_once_even_when_replayed(tmp_path: Path) -> None:
    api = FakeGmailApi()
    w = gmail_world(tmp_path, api)
    w.app.email_sync()
    message_id = api.deliver(customer_email(), record_twice=True)
    assert w.app.email_sync().processed == 1
    with w.db.transaction() as uow:
        state = uow.mailbox_sync.get_state("gmail", ACCOUNT)
        assert state is not None
        uow.mailbox_sync.update_state(state.model_copy(update={"cursor": "1000", "version": state.version + 1}), state.version)
    replay = w.app.email_sync()  # the cursor rewound: the history is read again
    assert (replay.processed, replay.duplicates) == (0, 1) and message_id in api.messages
    w.app.stop()


# ---- Stage 14 -> Stage 8 -> Gmail ------------------------------------------------------------------


def test_an_unknown_gmail_send_is_reconciled_and_never_resent(tmp_path: Path) -> None:
    api = FakeGmailApi(send_script=[Send.ACCEPT_THEN_LOSE_RESPONSE])
    w = gmail_world(tmp_path, api)
    enrolled(w)
    w.execute()
    approve_pending(w)
    unknown = w.execute(dispatch=True)
    assert (unknown.outcome, unknown.subsystem_outcome) == (X.EXECUTED, "UNKNOWN")
    plan = w.plan()
    assert (plan.owner, plan.action, plan.executable) == (O.DISPATCH_RECOVERY, A.RECONCILE_DISPATCH, True)
    [message] = campaign_messages(w.db, w.lead)
    blocked = w.app.services.dispatch.dispatch(DispatchRequest(outbound_id=message.outbound_id, correlation_id="retry"))  # type: ignore[union-attr]
    assert "ATTEMPT_UNRESOLVED" in blocked.reason_codes  # Stage 8 refuses a second copy
    reconciled = w.execute()
    assert (reconciled.outcome, reconciled.subsystem_outcome) == (X.EXECUTED, "ACCEPTED")
    assert (w.plan().owner, w.plan().action) == (O.CUSTOMER, A.WAIT_FOR_CUSTOMER)  # the next legitimate state
    again = w.app.services.dispatch.dispatch(DispatchRequest(outbound_id=message.outbound_id, correlation_id="again"))  # type: ignore[union-attr]
    assert "ALREADY_ACCEPTED" in again.reason_codes and len(api.sent_calls) == 1
    w.app.stop()


def test_late_evidence_resolves_an_attempt_that_was_not_found_before(tmp_path: Path) -> None:
    api = FakeGmailApi(send_script=[Send.SERVER_ERROR])  # Gmail answered 503: acceptance unknown
    w = gmail_world(tmp_path, api)
    enrolled(w)
    w.execute()
    approve_pending(w)
    assert w.execute(dispatch=True).subsystem_outcome == "UNKNOWN"
    still = w.execute()
    assert (still.subsystem_outcome, w.plan().action) == ("UNKNOWN", A.RECONCILE_DISPATCH)  # NOT_FOUND is not a rejection
    raw, _ = api.sent_calls[0]
    api.messages["s-late"] = StoredMessage("s-late", "t-late", ("SENT",), 0, raw)  # Gmail shows the message after all
    assert w.execute().subsystem_outcome == "ACCEPTED"
    [message] = campaign_messages(w.db, w.lead)
    assert (message.status, message.provider_message_id, len(api.sent_calls)) == (OutboundStatus.SENT, "s-late", 1)
    w.app.stop()


def test_dnc_and_operator_approval_hold_before_gmail(tmp_path: Path) -> None:
    api = FakeGmailApi()
    w = gmail_world(tmp_path, api)
    enrolled(w)
    w.execute()
    assert w.app.dispatch_tick().processed == 0 and api.sent_calls == []  # a draft is not approved work
    approve_pending(w)
    suppress(w)
    [message] = campaign_messages(w.db, w.lead)
    refused = w.app.services.dispatch.dispatch(DispatchRequest(outbound_id=message.outbound_id, correlation_id="dnc"))  # type: ignore[union-attr]
    assert refused.outcome.value == "BLOCKED" and api.sent_calls == []  # Stage 8 policy, before any Gmail call
    assert w.plan().action is A.NO_AUTOMATION
    w.app.stop()


# ---- Startup, status, CLI --------------------------------------------------------------------------


def test_without_authorization_startup_fails_closed_and_no_oauth_starts(tmp_path: Path) -> None:
    api = FakeGmailApi()
    app = gmail_runtime(tmp_path, api, token=False)
    with pytest.raises(StartupError) as error:
        app.start()
    assert (error.value.code, str(error.value)) == ("EMAIL_AUTH_REQUIRED", "EMAIL_AUTH_REQUIRED: run: python -m app.runtime gmail-auth")
    assert api.calls == [] and not (tmp_path / "agent.sqlite3").exists()


def test_the_wrong_account_is_refused(tmp_path: Path) -> None:
    app = gmail_runtime(tmp_path, FakeGmailApi(address="someone@else.example"))
    with pytest.raises(StartupError) as error:
        app.start()
    assert str(error.value) == "EMAIL_PROVIDER_UNAVAILABLE: MAILBOX_MISMATCH"


def test_production_still_fails_closed_with_gmail_ready(tmp_path: Path) -> None:
    app = gmail_runtime(tmp_path, FakeGmailApi(), MODE="production")
    with pytest.raises(StartupError) as error:
        app.start()
    assert str(error.value) == "PRODUCTION_NOT_READY: LLM:DISABLED, OPERATOR_CHANNEL:DISABLED, EMBEDDINGS:DISABLED"


def run(argv: list[str], environ: dict[str, str]) -> tuple[int, dict[str, object], str]:
    out = io.StringIO()
    code = cli.main(argv, environ, out)
    return code, json.loads(out.getvalue()), out.getvalue()


def test_provider_status_and_email_sync_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeGmailApi()
    monkeypatch.setattr(cli, "CONNECTORS", connectors(api))
    code, report, _ = run(["provider-status"], gmail_env(tmp_path, token=False))
    email = {row["category"]: row for row in report["integrations"]["providers"]}["EMAIL"]  # type: ignore[index]
    assert (code, email["state"], email["authorization"], email["capability_available"]) == (0, "AUTH_REQUIRED", "AUTH_REQUIRED", False)
    environ = gmail_env(tmp_path)
    code, report, _ = run(["provider-status"], environ)
    email = {row["category"]: row for row in report["integrations"]["providers"]}["EMAIL"]  # type: ignore[index]
    assert (email["state"], email["authorization"], email["capability_available"]) == ("CONFIGURED", "AUTHORIZED", True)
    assert api.calls == []  # provider-status never contacts the provider
    assert run(["init"], environ)[0] == 0
    code, synced, _ = run(["email-sync"], environ)
    assert (code, synced["status"]) == (0, "INITIALIZED")
    api.deliver(customer_email())
    code, synced, _ = run(["email-sync"], environ)
    assert (code, synced["status"], synced["reason"]) == (0, "SKIPPED", "INBOUND_PROCESSING_UNAVAILABLE")  # no LLM yet
    api.expired_before = api.history_id + 10
    with Database(tmp_path / "agent.sqlite3") as db, db.transaction() as uow:  # pretend the cursor is far behind
        state = uow.mailbox_sync.get_state("gmail", ACCOUNT)
        assert state is not None
        uow.mailbox_sync.update_state(state.model_copy(update={"status": MailboxSyncStatus.RECOVERY_REQUIRED,
                                                               "version": state.version + 1}), state.version)
    code, synced, _ = run(["email-sync"], environ)
    assert (code, synced["status"]) == (4, "RECOVERY_REQUIRED")
    code, synced, _ = run(["email-sync", "--recover"], environ)
    assert (code, synced["status"], synced["generation"]) == (0, "RECOVERED", 2)


def test_offline_mode_never_loads_google_code(tmp_path: Path) -> None:
    code = (
        "import sys\n"
        "from app.runtime.cli import main\n"
        "import io\n"
        f"environ = {{k: v for k, v in {dict(cli_env(tmp_path))!r}.items()}}\n"
        "main(['init'], environ, io.StringIO()); main(['provider-status'], environ, io.StringIO())\n"
        "main(['tick'], environ, io.StringIO())\n"
        "loaded = sorted(m for m in sys.modules if m.startswith(('google', 'requests', 'oauthlib', 'app.integrations.gmail.')))\n"
        "assert loaded == [], loaded\n"
    )
    completed = subprocess.run([sys.executable, "-c", code], cwd=APP.parent, capture_output=True, text=True, timeout=120,
                               env={"PYTHONPATH": str(APP.parent), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")})
    assert completed.returncode == 0, completed.stderr


def cli_env(tmp_path: Path) -> dict[str, str]:
    from tests.runtime.builders import env
    return env(tmp_path / "offline.sqlite3")


# ---- Secrets -----------------------------------------------------------------------------------------


def test_no_gmail_secret_leaks_anywhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    import logging
    caplog.set_level(logging.DEBUG)
    api = FakeGmailApi()
    monkeypatch.setattr(cli, "CONNECTORS", connectors(api))
    environ = gmail_env(tmp_path)
    outputs = [run([command], environ)[2] for command in ("provider-status", "init", "email-sync", "tick", "execution-metrics")]
    (tmp_path / "w").mkdir()
    w = gmail_world(tmp_path / "w", api)
    first_touch_sent(w, api)
    outputs += [w.app.health().model_dump_json(), w.app.email_sync().model_dump_json(), repr(w.app.__dict__),
                repr(load_config(environ, now=NOW))]
    for path in (tmp_path / "agent.sqlite3", tmp_path / "w" / "agent.sqlite3"):
        outputs.append(path.read_bytes().decode("latin-1"))
    outputs.append(caplog.text)
    for text in outputs:
        for secret in GMAIL_SECRETS:
            assert secret not in text
    w.app.stop()


# ---- Concurrency ---------------------------------------------------------------------------------------


def gmail_dispatch(db: Database, api: FakeGmailApi) -> DispatchService:
    from tests.runtime.builders import runtime_config
    cfg = runtime_config(db.path)
    return DispatchService(db, FrozenClock(NOW), cfg.dispatch_config(), GmailTransport(api, address=ACCOUNT), GmailReconciler(api))


@pytest.mark.parametrize("round_", range(3))
def test_concurrent_sends_of_one_approved_message_reach_gmail_once(tmp_path: Path, round_: int) -> None:
    api = FakeGmailApi()
    w = gmail_world(tmp_path, api)
    enrolled(w)
    w.execute()
    approve_pending(w)
    [message] = campaign_messages(w.db, w.lead)
    results = run_concurrently(
        Path(w.db.path),
        lambda db: gmail_dispatch(db, api).dispatch(DispatchRequest(outbound_id=message.outbound_id, correlation_id="race-1")),
        lambda db: gmail_dispatch(db, api).dispatch(DispatchRequest(outbound_id=message.outbound_id, correlation_id="race-2")),
    )
    assert not any(isinstance(r, Exception) for r in results), results
    assert len(api.sent_calls) == 1  # Stage 8's claim lets exactly one through
    w.app.stop()


@pytest.mark.parametrize("round_", range(3))
def test_reconciliation_racing_a_retry_never_duplicates(tmp_path: Path, round_: int) -> None:
    api = FakeGmailApi(send_script=[Send.ACCEPT_THEN_LOSE_RESPONSE])
    w = gmail_world(tmp_path, api)
    enrolled(w)
    w.execute()
    approve_pending(w)
    w.execute(dispatch=True)
    [message] = campaign_messages(w.db, w.lead)
    request = DispatchRequest(outbound_id=message.outbound_id, correlation_id="race")
    results = run_concurrently(Path(w.db.path), lambda db: gmail_dispatch(db, api).reconcile(request),
                               lambda db: gmail_dispatch(db, api).dispatch(request))
    assert not any(isinstance(r, Exception) for r in results), results
    assert len(api.sent_calls) == 1 and campaign_messages(w.db, w.lead)[0].status is OutboundStatus.SENT
    w.app.stop()
