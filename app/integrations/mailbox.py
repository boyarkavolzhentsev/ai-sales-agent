"""Provider-neutral inbound mailbox synchronization (one bounded pass, no daemon).

A provider supplies a ``MailboxReader`` (positions, changes, normalized envelopes); this
module owns the durable cursor and the rules that keep mail from being lost:

- First run: the cursor is set to the mailbox's current position and NOTHING is ingested
  (no mailbox replay). ``recover=True`` is the only other way a cursor is (re)set.
- A pass first retries recorded failures (oldest first), then reads at most ``limit``
  changes after the cursor and hands each message to the inbound handler (Stage 6 via the
  runtime). A message is handled, filtered (self-sent, draft, spam, ...: counted and
  named), or recorded as a failure. The failure records and the cursor advance commit in
  ONE transaction, after every message of the batch reached one of those outcomes; a crash
  before it replays the batch, which Stage 6's per-message idempotency makes safe.
- A failed message never blocks or loses later mail: it is recorded (provider message id
  and an error code only) and retried by every later pass until it succeeds; nothing is
  deleted, archived or relabelled. A provider read error before any message was handled
  leaves the cursor where it was.
- If the provider no longer serves changes since the cursor (Gmail history expired), the
  state becomes RECOVERY_REQUIRED and stays so until an explicit recovery: never a silent
  reset that would skip or replay mail.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.core.models.base import CoreModel
from app.core.models.types import NonEmptyStr
from app.inbound.models import InboundEnvelope, InboundResult, stable_id
from app.persistence import (
    Clock,
    Database,
    MailboxSyncFailure,
    MailboxSyncFailureStatus,
    MailboxSyncState,
    MailboxSyncStatus,
)


class MailboxReadError(Exception):
    """A provider read failed; ``code`` is a stable code, never provider text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class MailboxCursorExpired(MailboxReadError):
    def __init__(self) -> None:
        super().__init__("CURSOR_EXPIRED")


@dataclass(frozen=True)
class MailboxChange:
    message_ref: str  # the provider message id
    skip_reason: str | None = None  # known from the change itself (e.g. a SENT label)


@dataclass(frozen=True)
class MailboxChanges:
    changes: tuple[MailboxChange, ...]
    # Where to resume once every change above has been dealt with.
    next_position: str


@dataclass(frozen=True)
class FetchedMessage:
    envelope: InboundEnvelope | None = None
    skip_reason: str | None = None


class MailboxReader(Protocol):
    provider: str
    address: str

    def start_position(self) -> str: ...
    def changes(self, position: str, limit: int) -> MailboxChanges: ...
    def fetch(self, message_ref: str) -> FetchedMessage: ...


Handler = Callable[[InboundEnvelope], InboundResult]


class SyncStatus(StrEnum):
    OK = "OK"
    INITIALIZED = "INITIALIZED"  # the first cursor was set; nothing ingested
    RECOVERED = "RECOVERED"  # an explicit recovery set a new cursor; nothing ingested
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"


class SyncItemProblem(CoreModel):
    provider_message_id: NonEmptyStr
    code: NonEmptyStr  # an error code or exception type name; never message content


class MailboxSyncResult(CoreModel):
    status: SyncStatus
    reason: str | None = None
    mailbox: str
    generation: int | None = None
    changes: int = 0
    processed: int = 0
    duplicates: int = 0
    filtered: int = 0
    failed: int = 0
    retried: int = 0
    recovered: int = 0  # earlier failures that succeeded now
    open_failures: int = 0
    problems: tuple[SyncItemProblem, ...] = ()
    filtered_reasons: dict[str, int] = {}


class MailboxSync:
    def __init__(self, db: Database, clock: Clock, reader: MailboxReader) -> None:
        self._db = db
        self._clock = clock
        self._reader = reader

    @property
    def mailbox(self) -> str:
        return self._reader.address

    def sync_once(self, handler: Handler | None, *, limit: int, recover: bool = False) -> MailboxSyncResult:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        provider, mailbox = self._reader.provider, self._reader.address
        with self._db.transaction() as uow:
            state = uow.mailbox_sync.get_state(provider, mailbox)
        if state is None or recover:
            return self._establish(state)
        if state.status is MailboxSyncStatus.RECOVERY_REQUIRED:
            return MailboxSyncResult(status=SyncStatus.RECOVERY_REQUIRED, reason="CURSOR_EXPIRED", mailbox=mailbox,
                                     generation=state.generation)
        if handler is None:  # e.g. no LLM provider yet: nothing is read, the cursor stays
            return MailboxSyncResult(status=SyncStatus.SKIPPED, reason="INBOUND_PROCESSING_UNAVAILABLE", mailbox=mailbox,
                                     generation=state.generation)
        return _Pass(self, handler, state, limit).run()

    # ---- Cursor (re)initialization ------------------------------------------------------------

    def _establish(self, state: MailboxSyncState | None) -> MailboxSyncResult:
        provider, mailbox = self._reader.provider, self._reader.address
        try:
            position = self._reader.start_position()
        except MailboxReadError as exc:
            return MailboxSyncResult(status=SyncStatus.ERROR, reason=exc.code, mailbox=mailbox)
        now = self._clock.now()
        with self._db.transaction() as uow:
            if state is None:
                created = MailboxSyncState(state_id=stable_id("ms", provider, mailbox), provider=provider, mailbox=mailbox,
                                           cursor=position, created_at=now, updated_at=now)
                uow.mailbox_sync.add_state(created)
                return MailboxSyncResult(status=SyncStatus.INITIALIZED, mailbox=mailbox, generation=created.generation)
            current = uow.mailbox_sync.get_state(provider, mailbox) or state
            reset = MailboxSyncState.model_validate(current.model_dump() | {
                "cursor": position, "status": MailboxSyncStatus.ACTIVE, "generation": current.generation + 1,
                "updated_at": max(now, current.updated_at), "version": current.version + 1})
            uow.mailbox_sync.update_state(reset, current.version)
        return MailboxSyncResult(status=SyncStatus.RECOVERED, mailbox=mailbox, generation=reset.generation)


class _Pass:
    """One bounded pass: retry failures, then new changes, then one commit."""

    def __init__(self, sync: MailboxSync, handler: Handler, state: MailboxSyncState, limit: int) -> None:
        self.sync, self.handler, self.state, self.limit = sync, handler, state, limit
        self.reader = sync._reader  # noqa: SLF001 - the same component
        self.counts = {"processed": 0, "duplicates": 0, "filtered": 0, "failed": 0, "retried": 0, "recovered": 0}
        self.filtered: dict[str, int] = {}
        self.problems: list[SyncItemProblem] = []
        self.new_failures: dict[str, str] = {}  # provider message id -> code
        self.resolved: list[str] = []
        self.still_failing: dict[str, str] = {}
        self.open_failures = 0

    def run(self) -> MailboxSyncResult:
        provider, mailbox = self.state.provider, self.state.mailbox
        with self.sync._db.transaction() as uow:  # noqa: SLF001
            pending = uow.mailbox_sync.list_open_failures(provider, mailbox, self.limit)
        for failure in pending:  # earlier failures first: they are older mail
            self.counts["retried"] += 1
            code = self._handle(failure.provider_message_id, None)
            if code is None:
                self.resolved.append(failure.failure_id)
                self.counts["recovered"] += 1
            else:
                self.still_failing[failure.failure_id] = code
        budget = self.limit - len(pending)
        next_position = self.state.cursor
        changes_seen = 0
        if budget > 0:
            try:
                batch = self.reader.changes(self.state.cursor, budget)
            except MailboxCursorExpired:
                self._commit(self.state.cursor, expired=True)
                return self._result(SyncStatus.RECOVERY_REQUIRED, "CURSOR_EXPIRED", 0)
            except MailboxReadError as exc:
                self._commit(self.state.cursor)
                return self._result(SyncStatus.ERROR, exc.code, 0)
            seen: set[str] = set()
            for change in batch.changes:
                if change.message_ref in seen:  # the same message in several history events: once
                    continue
                seen.add(change.message_ref)
                code = self._handle(change.message_ref, change.skip_reason)
                if code is not None:
                    self.new_failures[change.message_ref] = code
            changes_seen = len(seen)
            next_position = batch.next_position
        self._commit(next_position)
        status = SyncStatus.ERROR if self.problems else SyncStatus.OK
        return self._result(status, None, changes_seen)

    def _handle(self, message_ref: str, known_skip: str | None) -> str | None:
        """None when the message reached a final outcome (handled or filtered), else an
        error code (the message is then recorded/kept as a failure)."""
        if known_skip is not None:
            return self._filter(known_skip)
        try:
            fetched = self.reader.fetch(message_ref)
        except MailboxReadError as exc:
            return self._problem(message_ref, exc.code)
        except Exception as exc:  # noqa: BLE001 - isolated, reported by type, retried later
            return self._problem(message_ref, type(exc).__name__)
        if fetched.envelope is None:
            return self._filter(fetched.skip_reason or "FILTERED")
        try:
            result = self.handler(fetched.envelope)
        except Exception as exc:  # noqa: BLE001 - the Stage 6 step rolled back; retried later
            return self._problem(message_ref, type(exc).__name__)
        key = "duplicates" if (result.duplicate or result.replayed) else "processed"
        self.counts[key] += 1
        return None

    def _filter(self, reason: str) -> None:
        self.counts["filtered"] += 1
        self.filtered[reason] = self.filtered.get(reason, 0) + 1
        return None

    def _problem(self, message_ref: str, code: str) -> str:
        self.counts["failed"] += 1
        self.problems.append(SyncItemProblem(provider_message_id=message_ref, code=code))
        return code

    def _commit(self, next_position: str, *, expired: bool = False) -> None:
        """Failure records and the cursor advance (or the RECOVERY_REQUIRED mark), atomically."""
        now = self.sync._clock.now()  # noqa: SLF001
        provider, mailbox = self.state.provider, self.state.mailbox
        with self.sync._db.transaction() as uow:  # noqa: SLF001
            for failure_id in self.resolved:
                found = uow.mailbox_sync.get_failure(failure_id)
                if found is not None and found.status is MailboxSyncFailureStatus.OPEN:
                    uow.mailbox_sync.update_failure(_bump(found, status=MailboxSyncFailureStatus.RESOLVED,
                                                          resolved_at=now), found.version)
            for failure_id, code in self.still_failing.items():
                found = uow.mailbox_sync.get_failure(failure_id)
                if found is not None and found.status is MailboxSyncFailureStatus.OPEN:
                    uow.mailbox_sync.update_failure(_bump(found, attempts=found.attempts + 1, last_error_code=code,
                                                          last_failed_at=max(now, found.last_failed_at)), found.version)
            for message_ref, code in self.new_failures.items():
                failure_id = stable_id("mf", provider, mailbox, message_ref)
                found = uow.mailbox_sync.get_failure(failure_id)
                if found is None:
                    uow.mailbox_sync.add_failure(MailboxSyncFailure(
                        failure_id=failure_id, provider=provider, mailbox=mailbox, provider_message_id=message_ref,
                        last_error_code=code, first_failed_at=now, last_failed_at=now))
                elif found.status is MailboxSyncFailureStatus.OPEN:
                    uow.mailbox_sync.update_failure(_bump(found, attempts=found.attempts + 1, last_error_code=code,
                                                          last_failed_at=max(now, found.last_failed_at)), found.version)
                else:  # failed again after it once succeeded: reopen (it stays visible)
                    uow.mailbox_sync.update_failure(_bump(found, status=MailboxSyncFailureStatus.OPEN, resolved_at=None,
                                                          attempts=found.attempts + 1, last_error_code=code,
                                                          last_failed_at=max(now, found.last_failed_at)), found.version)
            current = uow.mailbox_sync.get_state(provider, mailbox)
            if current is not None and current.version == self.state.version:
                status = MailboxSyncStatus.RECOVERY_REQUIRED if expired else MailboxSyncStatus.ACTIVE
                uow.mailbox_sync.update_state(MailboxSyncState.model_validate(current.model_dump() | {
                    "cursor": next_position, "status": status, "last_synced_at": now,
                    "updated_at": max(now, current.updated_at), "version": current.version + 1}), current.version)
            # Otherwise a concurrent pass already advanced it: this pass's messages were handled
            # idempotently, and its failures are recorded above; the cursor is left to that pass.
            self.open_failures = len(uow.mailbox_sync.list_open_failures(provider, mailbox, 1_000_000))

    def _result(self, status: SyncStatus, reason: str | None, changes: int) -> MailboxSyncResult:
        return MailboxSyncResult(status=status, reason=reason, mailbox=self.state.mailbox, generation=self.state.generation,
                                 changes=changes, problems=tuple(self.problems), filtered_reasons=dict(self.filtered),
                                 open_failures=self.open_failures, **self.counts)


def _bump(failure: MailboxSyncFailure, **changes: object) -> MailboxSyncFailure:
    return MailboxSyncFailure.model_validate(failure.model_dump() | changes | {"version": failure.version + 1})


__all__ = [
    "FetchedMessage",
    "Handler",
    "MailboxChange",
    "MailboxChanges",
    "MailboxCursorExpired",
    "MailboxReadError",
    "MailboxReader",
    "MailboxSync",
    "MailboxSyncResult",
    "SyncItemProblem",
    "SyncStatus",
]
