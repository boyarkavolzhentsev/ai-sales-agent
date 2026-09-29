"""Scripted fake email transport and reconciler. No sockets, no HTTP, no provider SDK.

The fake keeps its own "provider side" record of what it accepted, so tests can model
an acceptance whose response is lost and a later reconciliation that finds it.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from app.dispatch.transport import (
    NotSubmittedError,
    ReconciliationFinding,
    ReconciliationResult,
    TransportOutcome,
    TransportRequest,
    TransportResult,
)


class FakeBehavior(StrEnum):
    ACCEPT = "ACCEPT"  # accepted, response returned
    REJECT = "REJECT"  # confirmed non-acceptance
    FAIL_BEFORE_SUBMIT = "FAIL_BEFORE_SUBMIT"  # known not submitted (NotSubmittedError)
    TIMEOUT = "TIMEOUT"  # nothing accepted, but the caller cannot know (raises TimeoutError)
    ACCEPT_THEN_LOSE_RESPONSE = "ACCEPT_THEN_LOSE_RESPONSE"  # accepted, then the connection drops
    UNKNOWN_RESULT = "UNKNOWN_RESULT"  # returns an explicit UNKNOWN result


@dataclass(frozen=True)
class FakeStep:
    behavior: FakeBehavior
    retryable: bool = True
    # Runs inside submit() before the behavior, e.g. to simulate a crash (raise a
    # BaseException) or a concurrent unsubscribe while the request is in flight.
    before: Callable[[TransportRequest], None] | None = None
    # Runs after the provider side recorded its decision (accepted or rejected) but before
    # the response returns, e.g. to pause a worker whose result is still "in flight".
    after: Callable[[TransportRequest], None] | None = None


@dataclass
class FakeEmailTransport:
    steps: list[FakeStep] = field(default_factory=list)
    calls: list[TransportRequest] = field(default_factory=list)
    # Provider-side truth: rfc_message_id -> provider message id, and terminal rejections
    # (rfc_message_id -> retryable) of requests the provider saw and did not accept.
    accepted: dict[str, str] = field(default_factory=dict)
    rejected: dict[str, bool] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def script(self, *steps: FakeStep | FakeBehavior) -> "FakeEmailTransport":
        self.steps.extend(s if isinstance(s, FakeStep) else FakeStep(s) for s in steps)
        return self

    def submit(self, request: TransportRequest) -> TransportResult:
        with self._lock:
            self.calls.append(request)
            step = self.steps.pop(0) if self.steps else FakeStep(FakeBehavior.ACCEPT)
        if step.before is not None:
            step.before(request)
        behavior = step.behavior
        if behavior is FakeBehavior.FAIL_BEFORE_SUBMIT:
            raise NotSubmittedError("CONNECTION_REFUSED")
        if behavior is FakeBehavior.TIMEOUT:
            raise TimeoutError("fake timeout")
        if behavior is FakeBehavior.UNKNOWN_RESULT:
            return TransportResult(outcome=TransportOutcome.UNKNOWN, reason_code="PROVIDER_STATUS_UNKNOWN")
        if behavior is FakeBehavior.REJECT:
            with self._lock:
                self.rejected[request.rfc_message_id] = step.retryable
            if step.after is not None:
                step.after(request)
            return TransportResult(outcome=TransportOutcome.NOT_ACCEPTED, reason_code="PROVIDER_REJECTED", retryable=step.retryable)
        with self._lock:
            provider_id = f"fake-{len(self.accepted) + 1}"
            self.accepted[request.rfc_message_id] = provider_id
        if step.after is not None:
            step.after(request)
        if behavior is FakeBehavior.ACCEPT_THEN_LOSE_RESPONSE:
            raise ConnectionResetError("fake connection lost after acceptance")
        return TransportResult(outcome=TransportOutcome.ACCEPTED, reason_code="ACCEPTED", provider_message_id=provider_id)


@dataclass
class FakeReconciler:
    """Looks the request up in the fake transport's provider-side record: ACCEPTED when
    the provider accepted it, REJECTED_FINAL when the provider holds a terminal rejection
    of this exact request, and NOT_FOUND otherwise (never treated as non-acceptance)."""

    transport: FakeEmailTransport
    lookups: list[str] = field(default_factory=list)

    def lookup(self, request_id: str, rfc_message_id: str) -> ReconciliationResult:
        self.lookups.append(request_id)
        provider_id = self.transport.accepted.get(rfc_message_id)
        if provider_id is not None:
            return ReconciliationResult(
                finding=ReconciliationFinding.ACCEPTED, reason_code="RECONCILED_ACCEPTED", provider_message_id=provider_id
            )
        if rfc_message_id in self.transport.rejected:
            return ReconciliationResult(
                finding=ReconciliationFinding.REJECTED_FINAL, reason_code="RECONCILED_REJECTED",
                retryable=self.transport.rejected[rfc_message_id],
            )
        return ReconciliationResult(finding=ReconciliationFinding.NOT_FOUND, reason_code="RECONCILIATION_NOT_FOUND")
