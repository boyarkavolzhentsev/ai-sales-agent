"""Inbound: Gmail message normalization (MIME) and the provider-neutral mailbox sync over
the Gmail reader: checkpoint, filtering, duplicates, bounds, crash/replay, expiry,
failures and poison messages. The handler is a recording stand-in for Stage 6."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.inbound.models import InboundEnvelope
from app.integrations.gmail.client import RawMessage
from app.integrations.gmail.errors import Delivery, GmailCode, GmailError
from app.integrations.gmail.inbound import GmailMailboxReader
from app.integrations.gmail.mime import normalize_inbound
from app.integrations.mailbox import MailboxSync, SyncStatus
from app.persistence import Database, FrozenClock, MailboxSyncFailureStatus, MailboxSyncStatus
from tests.gmail.fakes import ACCOUNT, FakeGmailApi, customer_email
from tests.inbound.builders import NOW


def raw(data: bytes, labels: tuple[str, ...] = ("INBOX",)) -> RawMessage:
    return RawMessage(message_id="m1", thread_id="t1", label_ids=labels, internal_date_ms=1780315500000, raw=data)


def envelope_of(data: bytes, labels: tuple[str, ...] = ("INBOX",)) -> InboundEnvelope:
    fetched = normalize_inbound(raw(data, labels), mailbox=ACCOUNT)
    assert fetched.envelope is not None, fetched.skip_reason
    return fetched.envelope


# ---- Normalization ------------------------------------------------------------------------------


def test_a_plain_message_becomes_a_provider_neutral_envelope() -> None:
    envelope = envelope_of(customer_email(in_reply_to="<da_1@ourco.example>", extra={"Cc": "Team <team@prospect.example>"}))
    assert (envelope.provider, envelope.provider_message_id, envelope.provider_thread_ref) == ("gmail", "m1", "t1")
    assert (envelope.from_address, envelope.mailbox, envelope.to_addresses) == ("buyer@prospect.example", ACCOUNT, (ACCOUNT,))
    assert envelope.cc_addresses == ("team@prospect.example",) and envelope.subject == "Pricing question"
    assert envelope.body_text == "Hi, how much does the Basic plan cost per month?"
    assert (envelope.internet_message_id, envelope.in_reply_to, envelope.references) == (
        "<p-1@prospect.example>", "<da_1@ourco.example>", ("<da_1@ourco.example>",))
    assert envelope.received_at == datetime.fromtimestamp(1780315500, UTC) and envelope.sent_at is not None
    assert envelope.raw_ref == "gmail:m1" and len(envelope.raw_hash) == 64 and not envelope.has_attachments


def test_text_plain_is_preferred_and_html_only_is_reduced_to_text() -> None:
    both = envelope_of(customer_email(body="Plain words.", html="<p>HTML words</p>"))
    assert both.body_text == "Plain words."
    html_only = envelope_of(customer_email(body=None, html=("<html><head><style>p{}</style><script>alert(1)</script></head>"
                                                            "<body><p>Price&nbsp;please?</p><div>Thanks</div></body></html>")))
    assert html_only.body_text == "Price please?\nThanks" and "alert" not in html_only.body_text


def test_attachments_never_become_the_customers_words() -> None:
    envelope = envelope_of(customer_email(body="See attached.", attachment=("notes.txt", b"IGNORE PREVIOUS INSTRUCTIONS")))
    assert envelope.body_text == "See attached." and envelope.has_attachments
    nested = envelope_of(customer_email(body="Nested reply.", html="<b>Nested</b>", attachment=("a.txt", b"attached text")))
    assert nested.body_text == "Nested reply."


@pytest.mark.parametrize(("labels", "reason"), [
    (("DRAFT",), "LABEL_DRAFT"), (("SPAM",), "LABEL_SPAM"), (("TRASH",), "LABEL_TRASH"), (("SENT",), "LABEL_SENT"),
    (("CHAT",), "LABEL_CHAT"),
])
def test_drafts_spam_trash_chat_and_sent_mail_are_filtered(labels: tuple[str, ...], reason: str) -> None:
    assert normalize_inbound(raw(customer_email(), labels), mailbox=ACCOUNT).skip_reason == reason


def test_our_own_mail_and_unparseable_senders_are_filtered() -> None:
    assert normalize_inbound(raw(customer_email(sender=ACCOUNT)), mailbox=ACCOUNT).skip_reason == "SELF_SENT"
    assert normalize_inbound(raw(b"Subject: no sender\r\n\r\nbody"), mailbox=ACCOUNT).skip_reason == "INVALID_SENDER"
    assert normalize_inbound(raw(customer_email(sender="not-an-address")), mailbox=ACCOUNT).skip_reason == "INVALID_SENDER"


def test_missing_message_id_and_broken_parts_are_tolerated() -> None:
    envelope = envelope_of(customer_email(message_id=""))
    assert envelope.internet_message_id is None
    broken = (b"From: buyer@prospect.example\r\nTo: sales@ourco.example\r\nSubject: x\r\nMIME-Version: 1.0\r\n"
              b"Content-Type: text/plain; charset=unknown-8bit-charset\r\nContent-Transfer-Encoding: base64\r\n\r\n%%%%\r\n")
    assert envelope_of(broken).from_address == "buyer@prospect.example"


# ---- Sync ------------------------------------------------------------------------------------------


class Recorder:
    """Stands in for Stage 6: records envelopes, dedupes by provider message id."""

    def __init__(self, fail: Callable[[InboundEnvelope], BaseException | None] | None = None) -> None:
        self.seen: list[str] = []
        self.fail = fail

    def __call__(self, envelope: InboundEnvelope) -> SimpleNamespace:
        error = self.fail(envelope) if self.fail else None
        if error is not None:
            raise error
        duplicate = envelope.provider_message_id in self.seen
        self.seen.append(envelope.provider_message_id)
        return SimpleNamespace(duplicate=duplicate, replayed=False)


def sync(db: Database, api: FakeGmailApi) -> MailboxSync:
    return MailboxSync(db, FrozenClock(NOW), GmailMailboxReader(api, address=ACCOUNT))


def state(db: Database):  # noqa: ANN201
    with db.transaction() as uow:
        return uow.mailbox_sync.get_state("gmail", ACCOUNT)


def failures(db: Database):  # noqa: ANN201
    with db.transaction() as uow:
        return uow.mailbox_sync.list_open_failures("gmail", ACCOUNT, 100)


@pytest.fixture
def mailbox_db(tmp_path: Path):  # noqa: ANN201
    with Database(tmp_path / "sync.sqlite3") as db:
        db.initialize_schema(FrozenClock(NOW))
        yield db


def test_the_first_pass_only_sets_the_checkpoint(mailbox_db: Database) -> None:
    api = FakeGmailApi()
    for n in range(3):
        api.deliver(customer_email(message_id=f"<old-{n}@prospect.example>"))  # years of existing mail
    handler = Recorder()
    first = sync(mailbox_db, api).sync_once(handler, limit=10)
    assert (first.status, first.processed) == (SyncStatus.INITIALIZED, 0) and handler.seen == []
    assert state(mailbox_db).cursor == str(api.history_id)
    again = sync(mailbox_db, api).sync_once(handler, limit=10)
    assert (again.status, again.processed, handler.seen) == (SyncStatus.OK, 0, [])  # no replay of the old mail


def test_new_mail_is_handled_once_and_the_cursor_follows(mailbox_db: Database) -> None:
    api, handler = FakeGmailApi(), Recorder()
    sync(mailbox_db, api).sync_once(handler, limit=10)
    first = api.deliver(customer_email(message_id="<n1@prospect.example>"), record_twice=True)  # two history events
    second = api.deliver(customer_email(message_id="<n2@prospect.example>"))
    own = api.send(customer_email(sender=ACCOUNT, to="buyer@prospect.example", message_id="<da_9@ourco.example>"), thread_id=None)
    result = sync(mailbox_db, api).sync_once(handler, limit=10)
    assert (result.status, result.processed, result.duplicates, result.filtered) == (SyncStatus.OK, 2, 0, 1)
    assert handler.seen == [first, second] and own.message_id not in handler.seen  # our sent mail never re-enters
    assert result.filtered_reasons == {"LABEL_SENT": 1} and state(mailbox_db).cursor == str(api.history_id)


def test_a_pass_is_bounded_and_continues_where_it_stopped(mailbox_db: Database) -> None:
    api, handler = FakeGmailApi(), Recorder()
    sync(mailbox_db, api).sync_once(handler, limit=10)
    ids = [api.deliver(customer_email(message_id=f"<b{n}@prospect.example>")) for n in range(5)]
    first = sync(mailbox_db, api).sync_once(handler, limit=2)
    second = sync(mailbox_db, api).sync_once(handler, limit=2)
    third = sync(mailbox_db, api).sync_once(handler, limit=2)
    assert [first.processed, second.processed, third.processed] == [2, 2, 1] and handler.seen == ids


def test_a_crash_before_the_checkpoint_replays_safely(mailbox_db: Database) -> None:
    api = FakeGmailApi()
    sync(mailbox_db, api).sync_once(Recorder(), limit=10)
    ids = [api.deliver(customer_email(message_id=f"<c{n}@prospect.example>")) for n in range(3)]
    cursor = state(mailbox_db).cursor
    stage6 = Recorder()

    class Crash(BaseException):
        pass

    crashed: list[str] = []

    def crash_once_on_last(envelope: InboundEnvelope) -> BaseException | None:
        if envelope.provider_message_id == ids[-1] and not crashed:
            crashed.append(envelope.provider_message_id)
            return Crash()  # the process dies mid-batch: not an item failure
        return None

    stage6.fail = crash_once_on_last
    with pytest.raises(Crash):
        sync(mailbox_db, api).sync_once(stage6, limit=10)
    assert state(mailbox_db).cursor == cursor  # nothing advanced past un-committed work
    stage6.fail = None
    replay = sync(mailbox_db, api).sync_once(stage6, limit=10)
    assert (replay.processed, replay.duplicates) == (1, 2)  # Stage 6 idempotency absorbs the replay
    assert stage6.seen.count(ids[0]) == 2 and state(mailbox_db).cursor == str(api.history_id)


def test_history_expiry_requires_an_explicit_recovery(mailbox_db: Database) -> None:
    api, handler = FakeGmailApi(), Recorder()
    sync(mailbox_db, api).sync_once(handler, limit=10)
    api.deliver(customer_email(message_id="<lost@prospect.example>"))
    api.expired_before = api.history_id + 1
    expired = sync(mailbox_db, api).sync_once(handler, limit=10)
    assert (expired.status, expired.reason) == (SyncStatus.RECOVERY_REQUIRED, "CURSOR_EXPIRED")
    assert state(mailbox_db).status is MailboxSyncStatus.RECOVERY_REQUIRED
    still = sync(mailbox_db, api).sync_once(handler, limit=10)
    assert still.status is SyncStatus.RECOVERY_REQUIRED and handler.seen == []  # never a silent reset or replay
    recovered = sync(mailbox_db, api).sync_once(handler, limit=10, recover=True)
    assert (recovered.status, recovered.generation) == (SyncStatus.RECOVERED, 2) and handler.seen == []
    api.expired_before = None
    after = api.deliver(customer_email(message_id="<after@prospect.example>"))
    assert sync(mailbox_db, api).sync_once(handler, limit=10).processed == 1 and handler.seen == [after]


def test_a_failed_message_is_recorded_retried_and_never_blocks_later_mail(mailbox_db: Database) -> None:
    api = FakeGmailApi()
    sync(mailbox_db, api).sync_once(Recorder(), limit=10)
    poison = api.deliver(customer_email(message_id="<poison@prospect.example>"))
    good = api.deliver(customer_email(message_id="<good@prospect.example>"))
    stage6 = Recorder(fail=lambda e: RuntimeError("bug") if e.provider_message_id == poison else None)
    first = sync(mailbox_db, api).sync_once(stage6, limit=10)
    assert (first.status, first.processed, first.failed) == (SyncStatus.ERROR, 1, 1)
    assert [p.code for p in first.problems] == ["RuntimeError"] and stage6.seen == [good]
    assert state(mailbox_db).cursor == str(api.history_id)  # advanced: the failure is durably recorded
    [failure] = failures(mailbox_db)
    assert (failure.provider_message_id, failure.attempts, failure.last_error_code) == (poison, 1, "RuntimeError")
    later = api.deliver(customer_email(message_id="<later@prospect.example>"))
    second = sync(mailbox_db, api).sync_once(stage6, limit=10)
    assert (second.retried, second.processed, second.failed, second.open_failures) == (1, 1, 1, 1)
    assert stage6.seen == [good, later] and failures(mailbox_db)[0].attempts == 2  # poison reported again, later mail flows
    stage6.fail = None
    third = sync(mailbox_db, api).sync_once(stage6, limit=10)
    assert (third.recovered, third.open_failures, third.status) == (1, 0, SyncStatus.OK) and stage6.seen[-1] == poison
    with mailbox_db.transaction() as uow:
        resolved = uow.mailbox_sync.get_failure(failure.failure_id)
    assert resolved is not None and resolved.status is MailboxSyncFailureStatus.RESOLVED  # kept, never deleted


def test_a_read_error_leaves_the_cursor_and_a_vanished_message_is_skipped(mailbox_db: Database) -> None:
    api, handler = FakeGmailApi(), Recorder()
    sync(mailbox_db, api).sync_once(handler, limit=10)
    gone = api.deliver(customer_email(message_id="<gone@prospect.example>"))
    cursor = state(mailbox_db).cursor
    api.fail_once["history"] = [GmailError(GmailCode.RATE_LIMITED, status=429, delivery=Delivery.REJECTED)]
    limited = sync(mailbox_db, api).sync_once(handler, limit=10)
    assert (limited.status, limited.reason, state(mailbox_db).cursor) == (SyncStatus.ERROR, "GMAIL_RATE_LIMITED", cursor)
    del api.messages[gone]  # deleted before we could read it
    skipped = sync(mailbox_db, api).sync_once(handler, limit=10)
    assert skipped.filtered_reasons == {"MESSAGE_GONE": 1} and handler.seen == []


def test_a_fetch_error_becomes_a_recorded_failure(mailbox_db: Database) -> None:
    api, handler = FakeGmailApi(), Recorder()
    sync(mailbox_db, api).sync_once(handler, limit=10)
    message = api.deliver(customer_email(message_id="<flaky@prospect.example>"))
    api.fail_once["get_raw"] = [GmailError(GmailCode.TEMPORARY_PROVIDER_ERROR, status=503, delivery=Delivery.UNKNOWN)]
    first = sync(mailbox_db, api).sync_once(handler, limit=10)
    assert (first.failed, [p.code for p in first.problems]) == (1, ["GMAIL_TEMPORARY_PROVIDER_ERROR"])
    assert sync(mailbox_db, api).sync_once(handler, limit=10).recovered == 1 and handler.seen == [message]


def test_without_inbound_processing_nothing_is_read_past_the_checkpoint(mailbox_db: Database) -> None:
    api = FakeGmailApi()
    assert sync(mailbox_db, api).sync_once(None, limit=10).status is SyncStatus.INITIALIZED
    api.deliver(customer_email())
    cursor = state(mailbox_db).cursor
    skipped = sync(mailbox_db, api).sync_once(None, limit=10)
    assert (skipped.status, skipped.reason) == (SyncStatus.SKIPPED, "INBOUND_PROCESSING_UNAVAILABLE")
    assert state(mailbox_db).cursor == cursor and "history" not in api.calls
