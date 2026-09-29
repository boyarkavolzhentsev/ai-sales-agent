"""Controlled dispatch of operator-approved inbound replies (V1: REPLY messages only).

Transitions of the outbound message (Stage 0 lifecycle):
  OPERATOR_APPROVED --claim--> APPROVED --(same transaction)--> SENDING
  FAILED (retryable, attempts left) --claim--> APPROVED --> SENDING
  SENDING --confirmed acceptance--> SENT   (provider accepted it; not proof of delivery)
  SENDING --confirmed non-acceptance--> FAILED
  SENDING stays SENDING while an attempt is unresolved (Stage 0: a message stuck in
  SENDING is reconciled with the provider, never blindly resent, and counts against
  limits). The attempt row says CLAIMED or UNKNOWN; SENDING never claims success.

Transaction boundaries:
1. CLAIM (one IMMEDIATE transaction): re-check status, attempts, approval provenance,
   recipient/sender binding, the shared Stage 7 gates and the Stage 3 outbound policy at
   the injected ``now``; then issue a single-use SendPermit, move to APPROVED, reserve
   quota (Stage 3 ``reserve_quota``) on a first attempt, consume the permit, move to
   SENDING, mark the reservation CONSUMED (the ledger now counts the message), and add the
   CLAIMED attempt plus audit. A blocked request rolls all of it back: no permit, no
   reservation, no attempt, no transport call; only a DISPATCH_BLOCKED audit record.
   SQL allows one unresolved attempt per message, so two workers cannot both claim.
2. SUBMIT: no transaction is open. The request is built from the claim's persisted data.
3. RECORD (one IMMEDIATE transaction): the attempt outcome and the message status. If
   this fails, a separate transaction marks the attempt UNKNOWN (FINALIZATION_FAILED);
   if that fails too the attempt stays CLAIMED. Either way it is unresolved and is never
   submitted again; only reconciliation or an operator resolves it.

Quota (Stage 3 message-based semantics, unchanged): the reservation is consumed at the
claim. SENDING, SENT and FAILED are counted by the ledger exactly as in Stage 3 (FAILED
stays counted conservatively; a retry takes the same message back to SENDING, so it is
counted once). Unknown outcomes stay counted. A retry is checked against the limits of
the day it runs. The ledger has one row per message, dated by its latest ``sending_at``:
a retry on a later day moves the message's attribution to that day, so historical ledger
snapshots are not immutable send statistics. ``dispatch_attempts`` keeps every attempt
with its own claim time. A cancellation before the claim needs no release here: nothing
was reserved (Stage 6/7 cancellation releases any ACTIVE reservation of an undispatched
message).

Late acceptance evidence: if an attempt recorded as NOT_ACCEPTED is later reported as
accepted while another attempt is accepted or unresolved, the evidence is stored on that
attempt; the claim gate consults it and refuses every further attempt of the message.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime

from pydantic import JsonValue

from app.core.enums import ActorType, EmailDirection, OutboundDecision, OutboundKind, OutboundStatus, RefKind
from app.core.models import Actor, AuditEvent, EmailMessage, EmailThread, EntityRef, OutboundMessage, SendPermit
from app.core.models.types import JsonObject
from app.dispatch.errors import DispatchNotFoundError, DispatchStateError
from app.dispatch.gates import Binding, approval_codes, bind, evaluate_policy
from app.dispatch.models import (
    AttemptView,
    DispatchCode,
    DispatchConfig,
    DispatchOutcome,
    DispatchRequest,
    DispatchResult,
    DispatchStatusView,
)
from app.dispatch.transport import (
    DispatchReconciler,
    EmailTransport,
    NotSubmittedError,
    TransportOutcome,
    TransportRequest,
    TransportResult,
)
from app.inbound import stable_id
from app.inbound.records import ref
from app.operator.review import load_draft_context, reply_gate_blockers
from app.persistence import (
    UNRESOLVED_ATTEMPT_STATES,
    Clock,
    Database,
    DispatchAttempt,
    DispatchAttemptState,
    QuotaReservation,
    QuotaReservationState,
    UnitOfWork,
)
from app.persistence.serialization import dumps_json
from app.policy import QuotaExceededError, consume_reservation, reserve_quota

DISPATCH_ACTOR = Actor(type=ActorType.SYSTEM, id="dispatch_service")
CHECKS_PASSED = (
    "OPERATOR_APPROVAL", "CONTENT_INTEGRITY", "RECIPIENT_BINDING", "REPLY_GATES", "OUTBOUND_POLICY", "QUOTA",
)
_OUTCOME_OF_STATE = {
    DispatchAttemptState.ACCEPTED: DispatchOutcome.ACCEPTED,
    DispatchAttemptState.NOT_ACCEPTED: DispatchOutcome.NOT_ACCEPTED,
    DispatchAttemptState.CLAIMED: DispatchOutcome.UNKNOWN,
    DispatchAttemptState.UNKNOWN: DispatchOutcome.UNKNOWN,
}
_STATE_OF_OUTCOME = {
    TransportOutcome.ACCEPTED: DispatchAttemptState.ACCEPTED,
    TransportOutcome.NOT_ACCEPTED: DispatchAttemptState.NOT_ACCEPTED,
    TransportOutcome.UNKNOWN: DispatchAttemptState.UNKNOWN,
}


class _Blocked(Exception):
    def __init__(self, codes: list[str], status: OutboundStatus) -> None:
        self.codes = tuple(dict.fromkeys(codes))
        self.status = status


@dataclass(frozen=True)
class _Claim:
    attempt: DispatchAttempt
    request: TransportRequest


class DispatchService:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        config: DispatchConfig,
        transport: EmailTransport,
        reconciler: DispatchReconciler | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._config = config
        self._transport = transport
        self._reconciler = reconciler

    # ---- Dispatch ---------------------------------------------------------------------------

    def dispatch(self, request: DispatchRequest) -> DispatchResult:
        try:
            claim = self._claim(request)
        except _Blocked as blocked:
            return self._blocked(request, blocked)
        if isinstance(claim, DispatchResult):
            return claim
        try:
            result = self._transport.submit(claim.request)
        except NotSubmittedError as exc:
            result = TransportResult(outcome=TransportOutcome.NOT_ACCEPTED, reason_code=exc.reason_code, retryable=True)
        except Exception as exc:  # noqa: BLE001 - any other failure: acceptance is unknown
            result = TransportResult(
                outcome=TransportOutcome.UNKNOWN, reason_code=f"{DispatchCode.TRANSPORT_EXCEPTION}:{type(exc).__name__}"
            )
        return self._record(claim.attempt, result, request.correlation_id, reconciled=False, transport_called=True)

    def reconcile(self, request: DispatchRequest) -> DispatchResult:
        """Resolve an unresolved attempt through the read-only reconciler, if one exists.
        Never submits. Idempotent: a resolved attempt returns its recorded outcome."""
        now = self._clock.now()
        with self._db.transaction() as uow:
            outbound = self._reply(uow, request.outbound_id)
            attempts = uow.dispatch_attempts.list_for_outbound(outbound.outbound_id)
            unresolved = next((a for a in attempts if a.state in UNRESOLVED_ATTEMPT_STATES), None)
            if unresolved is None:
                latest = attempts[-1] if attempts else None
                return self._result(outbound, latest, request.correlation_id, now, replayed=True)
        if self._reconciler is None:
            unavailable = TransportResult(outcome=TransportOutcome.UNKNOWN, reason_code=DispatchCode.RECONCILIATION_UNAVAILABLE)
            return self._record(unresolved, unavailable, request.correlation_id, reconciled=True, transport_called=False)
        try:
            found = self._reconciler.lookup(unresolved.attempt_id, unresolved.rfc_message_id).as_transport_result()
        except Exception as exc:  # noqa: BLE001 - an inconclusive lookup keeps the attempt unresolved
            found = TransportResult(outcome=TransportOutcome.UNKNOWN, reason_code=f"RECONCILIATION_ERROR:{type(exc).__name__}")
        return self._record(unresolved, found, request.correlation_id, reconciled=True, transport_called=False)

    def list_unresolved(self) -> tuple[AttemptView, ...]:
        """Attempts awaiting reconciliation or operator review (never resent automatically)."""
        with self._db.transaction() as uow:
            attempts = uow.dispatch_attempts.list_unresolved()
        return tuple(_attempt_view(a) for a in attempts)

    def inspect(self, outbound_id: str) -> DispatchStatusView:
        """Typed read of one message's dispatch state, including acceptance conflicts."""
        with self._db.transaction() as uow:
            outbound = self._reply(uow, outbound_id)
            attempts = uow.dispatch_attempts.list_for_outbound(outbound_id)
        return DispatchStatusView(
            outbound_id=outbound_id, outbound_status=outbound.status, attempts=tuple(_attempt_view(a) for a in attempts),
            unresolved=any(a.state in UNRESOLVED_ATTEMPT_STATES for a in attempts),
            acceptance_conflict=any(a.late_acceptance_provider_message_id is not None for a in attempts),
        )

    # ---- Claim ------------------------------------------------------------------------------

    def _claim(self, request: DispatchRequest) -> _Claim | DispatchResult:
        now = self._clock.now()
        with self._db.transaction() as uow:
            outbound = self._reply(uow, request.outbound_id)
            attempts = uow.dispatch_attempts.list_for_outbound(outbound.outbound_id)
            accepted = next((a for a in attempts if a.state is DispatchAttemptState.ACCEPTED), None)
            if accepted is not None:
                return self._result(outbound, accepted, request.correlation_id, now, replayed=True,
                                    codes=(DispatchCode.ALREADY_ACCEPTED,))
            unresolved = next((a for a in attempts if a.state in UNRESOLVED_ATTEMPT_STATES), None)
            if unresolved is not None:
                return self._result(outbound, unresolved, request.correlation_id, now, replayed=True,
                                    codes=(DispatchCode.ATTEMPT_UNRESOLVED,))
            # Durable positive evidence that an earlier attempt was accepted: never attempt again.
            if any(a.late_acceptance_provider_message_id is not None for a in attempts):
                raise _Blocked([DispatchCode.ACCEPTANCE_EVIDENCE_CONFLICT], outbound.status)

            self._check_status(outbound, attempts)
            binding, codes = self._check_gates(uow, outbound, first_attempt=not attempts, now=now)
            if codes or binding is None:
                raise _Blocked(codes, outbound.status)
            return self._write_claim(uow, outbound, attempts, binding, request.correlation_id, now)

    def _check_status(self, outbound: OutboundMessage, attempts: list[DispatchAttempt]) -> None:
        if outbound.status is OutboundStatus.CANCELLED:
            raise _Blocked([DispatchCode.ARTIFACT_CANCELLED], outbound.status)
        if outbound.status is OutboundStatus.FAILED:
            last = attempts[-1] if attempts else None
            if last is None or last.state is not DispatchAttemptState.NOT_ACCEPTED or not last.retryable:
                raise _Blocked([DispatchCode.RETRY_NOT_PERMITTED], outbound.status)
            if len(attempts) >= self._config.max_attempts:
                raise _Blocked([DispatchCode.RETRY_LIMIT_REACHED], outbound.status)
            return
        if outbound.status is not OutboundStatus.OPERATOR_APPROVED:
            raise _Blocked([DispatchCode.NOT_OPERATOR_APPROVED], outbound.status)

    def _check_gates(
        self, uow: UnitOfWork, outbound: OutboundMessage, *, first_attempt: bool, now: datetime
    ) -> tuple[Binding | None, list[str]]:
        codes: list[str] = list(approval_codes(uow, outbound, first_attempt=first_attempt))
        binding, binding_codes = bind(uow, outbound, self._config)
        codes += binding_codes
        codes += [code.value for code in reply_gate_blockers(uow, outbound, self._config.sender, now)]
        if binding is not None:
            policy = evaluate_policy(uow, outbound, binding, self._config, now)
            if policy.decision is not OutboundDecision.SEND:
                codes += [reason.value for reason in policy.reasons]
        return binding, codes

    def _write_claim(
        self, uow: UnitOfWork, outbound: OutboundMessage, attempts: list[DispatchAttempt], binding: Binding,
        correlation_id: str, now: datetime,
    ) -> _Claim:
        attempt_no = len(attempts) + 1
        attempt_id = stable_id("da", outbound.outbound_id, str(attempt_no))
        permit = SendPermit(
            permit_id=stable_id("sp", attempt_id), outbound_id=outbound.outbound_id, content_hash=outbound.content_hash,
            checks_passed=CHECKS_PASSED, policy_config_version=self._config.limits.policy_version,
            issued_at=now, expires_at=now + self._config.permit_ttl,
        )
        approved = OutboundMessage.model_validate(
            outbound.model_dump()
            | {"status": OutboundStatus.APPROVED, "decision": OutboundDecision.SEND, "send_permit_id": permit.permit_id,
               "failure_reason": None, "sending_at": None, "version": outbound.version + 1}
        )
        uow.outbound.update(approved, outbound.version)
        reservation = self._account(uow, approved, attempt_id, now)
        consumed_permit = SendPermit.model_validate(permit.model_dump() | {"consumed_at": now})
        sending = OutboundMessage.model_validate(
            approved.model_dump() | {"status": OutboundStatus.SENDING, "sending_at": now, "version": approved.version + 1}
        )
        uow.outbound.update(sending, approved.version)
        domain = binding.sender_mailbox.split("@", 1)[1]
        attempt = DispatchAttempt(
            attempt_id=attempt_id, outbound_id=outbound.outbound_id, attempt_no=attempt_no, permit=consumed_permit,
            reservation_id=reservation.reservation_id, recipient=binding.recipient, sender_mailbox=binding.sender_mailbox,
            content_hash=outbound.content_hash, rfc_message_id=f"<{attempt_id}@{domain}>", correlation_id=correlation_id,
            claimed_at=now,
        )
        uow.dispatch_attempts.add(attempt)
        self._audit(uow, "DISPATCH_CLAIMED", attempt, correlation_id, now, {
            "attempt_no": attempt_no,
            "permit_id": permit.permit_id,
            "permit_expires_at": permit.expires_at.isoformat(),
            "checks_passed": list(CHECKS_PASSED),
            "policy_config_version": permit.policy_config_version,
            "reservation_id": reservation.reservation_id,
            "reservation_state": reservation.state.value,
            "outbound_versions": {"expected": outbound.version, "resulting": sending.version},
            "previous_status": outbound.status.value,
        })
        trigger = binding.trigger
        request = TransportRequest(
            request_id=attempt.attempt_id, rfc_message_id=attempt.rfc_message_id, sender_mailbox=attempt.sender_mailbox,
            sender_name=self._config.sender.sender_name, recipient=attempt.recipient, subject=sending.subject,
            body=sending.body_final, in_reply_to=trigger.rfc_message_id,
            references=tuple(dict.fromkeys((*trigger.references, trigger.rfc_message_id))),
        )
        return _Claim(attempt=attempt, request=request)

    def _account(self, uow: UnitOfWork, approved: OutboundMessage, attempt_id: str, now: datetime) -> QuotaReservation:
        """First attempt: reserve (Stage 3) and consume in this transaction. Retry: the
        message's CONSUMED reservation from its earlier attempt still accounts for it."""
        live = uow.quota_reservations.get_live_for_outbound(approved.outbound_id)
        if live is not None and live.state is QuotaReservationState.CONSUMED:
            return live
        try:
            reservation = reserve_quota(uow, self._config.limits, approved, reservation_id=stable_id("qr", attempt_id), now=now)
        except QuotaExceededError as exc:
            raise _Blocked([check.reason.value for check in exc.checks], approved.status) from None
        return consume_reservation(uow, reservation, now)

    def _blocked(self, request: DispatchRequest, blocked: _Blocked) -> DispatchResult:
        now = self._clock.now()
        with self._db.transaction() as uow:
            event_id = stable_id("ae", "dispatch-blocked", request.outbound_id, request.correlation_id)
            if uow.audit.get(event_id) is None:
                after: JsonObject = {"reason_codes": list(blocked.codes), "status": blocked.status.value}
                uow.audit.append(_event(event_id, "DISPATCH_BLOCKED", (ref(RefKind.OUTBOUND_MESSAGE, request.outbound_id),),
                                        after, request.correlation_id, now))
        return DispatchResult(
            outbound_id=request.outbound_id, outcome=DispatchOutcome.BLOCKED, outbound_status=blocked.status,
            reason_codes=blocked.codes, correlation_id=request.correlation_id, occurred_at=now,
        )

    # ---- Outcome ----------------------------------------------------------------------------

    def _record(
        self, attempt: DispatchAttempt, result: TransportResult, correlation_id: str, *, reconciled: bool, transport_called: bool
    ) -> DispatchResult:
        try:
            return self._finalize(attempt.attempt_id, result, correlation_id, reconciled=reconciled, transport_called=transport_called)
        except Exception:  # noqa: BLE001 - never resubmit; keep the attempt unresolved
            try:
                unknown = TransportResult(outcome=TransportOutcome.UNKNOWN, reason_code=DispatchCode.FINALIZATION_FAILED)
                self._finalize(attempt.attempt_id, unknown, correlation_id, reconciled=reconciled, transport_called=transport_called)
            except Exception:  # noqa: BLE001 - the attempt stays CLAIMED, which is unresolved too
                pass
            return DispatchResult(
                outbound_id=attempt.outbound_id, outcome=DispatchOutcome.UNKNOWN, outbound_status=OutboundStatus.SENDING,
                reason_codes=(DispatchCode.FINALIZATION_FAILED,), attempt_id=attempt.attempt_id, attempt_no=attempt.attempt_no,
                permit_id=attempt.permit.permit_id, reservation_id=attempt.reservation_id, recipient=attempt.recipient,
                correlation_id=correlation_id, occurred_at=self._clock.now(), reconciled=reconciled,
                transport_called=transport_called,
            )

    def _finalize(
        self, attempt_id: str, result: TransportResult, correlation_id: str, *, reconciled: bool, transport_called: bool
    ) -> DispatchResult:
        now = self._clock.now()
        with self._db.transaction() as uow:
            attempt = uow.dispatch_attempts.get(attempt_id)
            if attempt is None:
                raise DispatchNotFoundError(f"attempt {attempt_id} not found")
            outbound = self._reply(uow, attempt.outbound_id)
            if attempt.state not in UNRESOLVED_ATTEMPT_STATES:
                if result.outcome is TransportOutcome.ACCEPTED and attempt.state is DispatchAttemptState.NOT_ACCEPTED:
                    return self._late_acceptance(uow, outbound, attempt, result, correlation_id, now,
                                                 reconciled=reconciled, transport_called=transport_called)
                # Resolved attempts are terminal: nothing (least of all a negative) overwrites them.
                return self._result(outbound, attempt, correlation_id, now, replayed=True, reconciled=reconciled,
                                    transport_called=transport_called)
            state = _STATE_OF_OUTCOME[result.outcome]
            if state is DispatchAttemptState.UNKNOWN and attempt.state is DispatchAttemptState.UNKNOWN:
                return self._result(outbound, attempt, correlation_id, now, codes=(result.reason_code,),
                                    reconciled=reconciled, transport_called=transport_called)
            resolved = state is not DispatchAttemptState.UNKNOWN
            updated = DispatchAttempt.model_validate(
                attempt.model_dump()
                | {"state": state, "reason_code": result.reason_code, "retryable": result.retryable,
                   "provider_message_id": result.provider_message_id, "resolved_at": now if resolved else None,
                   "version": attempt.version + 1}
            )
            uow.dispatch_attempts.update(updated, attempt.version)
            if resolved and outbound.status is not OutboundStatus.SENDING:
                raise DispatchStateError(f"message {outbound.outbound_id} is {outbound.status}, not SENDING")
            if state is DispatchAttemptState.ACCEPTED:
                outbound = self._mark_sent(uow, outbound, updated, now)
            elif state is DispatchAttemptState.NOT_ACCEPTED:
                failed = OutboundMessage.model_validate(
                    outbound.model_dump() | {"status": OutboundStatus.FAILED, "failure_reason": result.reason_code, "version": outbound.version + 1}
                )
                uow.outbound.update(failed, outbound.version)
                outbound = failed
            self._audit(uow, f"DISPATCH_{state.value}", updated, correlation_id, now, {
                "reason_code": result.reason_code,
                "retryable": result.retryable,
                "provider_message_id": result.provider_message_id,
                "reconciled": reconciled,
                "outbound_status": outbound.status.value,
                "reservation_id": updated.reservation_id,
            })
            return self._result(outbound, updated, correlation_id, now, codes=(result.reason_code,), reconciled=reconciled,
                                transport_called=transport_called)

    def _late_acceptance(
        self, uow: UnitOfWork, outbound: OutboundMessage, attempt: DispatchAttempt, result: TransportResult,
        correlation_id: str, now: datetime, *, reconciled: bool, transport_called: bool,
    ) -> DispatchResult:
        """Provider acceptance for an attempt recorded as NOT_ACCEPTED (the source that
        reported non-acceptance was wrong). Acceptance is positive proof the email left,
        so it is never dropped: it is always audited. If no other attempt of this message
        is accepted or unresolved, the attempt is corrected to ACCEPTED and the message to
        SENT, which also stops any retry. If a newer attempt exists, its history stands
        and the conflict is left for an operator."""
        others = [a for a in uow.dispatch_attempts.list_for_outbound(outbound.outbound_id) if a.attempt_id != attempt.attempt_id]
        newer_active = any(a.state in UNRESOLVED_ATTEMPT_STATES or a.state is DispatchAttemptState.ACCEPTED for a in others)
        promote = not newer_active and outbound.status is OutboundStatus.FAILED
        self._audit(uow, "DISPATCH_RESULT_CONFLICT", attempt, correlation_id, now, {
            "recorded_state": attempt.state.value,
            "late_outcome": result.outcome.value,
            "late_reason_code": result.reason_code,
            "late_provider_message_id": result.provider_message_id,
            "corrected_to_accepted": promote,
            "reconciled": reconciled,
        })
        if not promote:
            recorded = attempt
            if attempt.late_acceptance_provider_message_id is None:
                recorded = DispatchAttempt.model_validate(
                    attempt.model_dump()
                    | {"late_acceptance_provider_message_id": result.provider_message_id, "late_acceptance_at": now,
                       "version": attempt.version + 1}
                )
                uow.dispatch_attempts.update(recorded, attempt.version)
            return self._result(outbound, recorded, correlation_id, now, codes=(DispatchCode.LATE_RESULT_CONFLICT,),
                                reconciled=reconciled, transport_called=transport_called)
        corrected = DispatchAttempt.model_validate(
            attempt.model_dump()
            | {"state": DispatchAttemptState.ACCEPTED, "reason_code": DispatchCode.LATE_RESULT_CONFLICT, "retryable": False,
               "provider_message_id": result.provider_message_id, "resolved_at": now, "version": attempt.version + 1}
        )
        uow.dispatch_attempts.update(corrected, attempt.version)
        sent = self._mark_sent(uow, outbound, corrected, now)
        return self._result(sent, corrected, correlation_id, now, codes=(DispatchCode.LATE_RESULT_CONFLICT,),
                            reconciled=reconciled, transport_called=transport_called)

    def _mark_sent(self, uow: UnitOfWork, outbound: OutboundMessage, attempt: DispatchAttempt, now: datetime) -> OutboundMessage:
        sent = OutboundMessage.model_validate(
            outbound.model_dump()
            | {"status": OutboundStatus.SENT, "sent_at": now, "provider_message_id": attempt.provider_message_id,
               "rfc_message_id": attempt.rfc_message_id, "failure_reason": None, "version": outbound.version + 1}
        )
        uow.outbound.update(sent, outbound.version)
        # Record our message in its thread so the customer's reply to it threads normally.
        thread = uow.threads.get(outbound.thread_id or "")
        if thread is not None:
            trigger_refs = self._trigger_refs(uow, outbound)
            message = EmailMessage(
                message_id=stable_id("em", "dispatch", attempt.attempt_id), rfc_message_id=attempt.rfc_message_id,
                thread_id=thread.thread_id, direction=EmailDirection.OUTBOUND, mailbox=attempt.sender_mailbox,
                from_address=attempt.sender_mailbox, to_addresses=(attempt.recipient,), subject=outbound.subject,
                body_text=outbound.body_final, raw_ref=f"dispatch:{attempt.attempt_id}", raw_hash=attempt.content_hash,
                in_reply_to=trigger_refs[-1] if trigger_refs else None, references=trigger_refs, sent_at=now,
            )
            uow.messages.add(message)
            uow.threads.update(
                EmailThread.model_validate(
                    thread.model_dump()
                    | {"message_ids": (*thread.message_ids, message.message_id), "last_outbound_at": now, "version": thread.version + 1}
                ),
                thread.version,
            )
        return sent

    @staticmethod
    def _trigger_refs(uow: UnitOfWork, outbound: OutboundMessage) -> tuple[str, ...]:
        context = load_draft_context(uow, outbound)
        trigger = uow.messages.get(context.message_id) if context else None
        if trigger is None:
            return ()
        return tuple(dict.fromkeys((*trigger.references, trigger.rfc_message_id)))

    # ---- Helpers ----------------------------------------------------------------------------

    @staticmethod
    def _reply(uow: UnitOfWork, outbound_id: str) -> OutboundMessage:
        outbound = uow.outbound.get(outbound_id)
        if outbound is None or outbound.kind is not OutboundKind.REPLY:
            raise DispatchNotFoundError(f"reply {outbound_id} not found")
        return outbound

    @staticmethod
    def _result(
        outbound: OutboundMessage, attempt: DispatchAttempt | None, correlation_id: str, now: datetime, *,
        codes: tuple[str, ...] = (), replayed: bool = False, reconciled: bool = False, transport_called: bool = False,
    ) -> DispatchResult:
        outcome = _OUTCOME_OF_STATE[attempt.state] if attempt is not None else DispatchOutcome.BLOCKED
        return DispatchResult(
            outbound_id=outbound.outbound_id, outcome=outcome, outbound_status=outbound.status,
            reason_codes=codes or ((attempt.reason_code,) if attempt is not None and attempt.reason_code else ()),
            attempt_id=attempt.attempt_id if attempt else None, attempt_no=attempt.attempt_no if attempt else None,
            permit_id=attempt.permit.permit_id if attempt else None, reservation_id=attempt.reservation_id if attempt else None,
            provider_message_id=attempt.provider_message_id if attempt else None, recipient=attempt.recipient if attempt else None,
            correlation_id=correlation_id, occurred_at=now, replayed=replayed, reconciled=reconciled,
            transport_called=transport_called,
        )

    @staticmethod
    def _audit(uow: UnitOfWork, event_type: str, attempt: DispatchAttempt, correlation_id: str, now: datetime, details: JsonObject) -> None:
        """IDs, states, codes and accounting only; never email bodies, credentials or raw errors."""
        event_id = stable_id("ae", "dispatch", attempt.attempt_id, event_type)
        if uow.audit.get(event_id) is not None:
            return
        after: dict[str, JsonValue] = {
            "attempt_id": attempt.attempt_id, "attempt_no": attempt.attempt_no, "state": attempt.state.value,
            "outbound_id": attempt.outbound_id, "content_hash": attempt.content_hash,
            "rfc_message_id": attempt.rfc_message_id, **details,
        }
        subjects = (ref(RefKind.OUTBOUND_MESSAGE, attempt.outbound_id), ref(RefKind.SEND_PERMIT, attempt.permit.permit_id))
        uow.audit.append(_event(event_id, event_type, subjects, after, correlation_id, now))


def _attempt_view(attempt: DispatchAttempt) -> AttemptView:
    return AttemptView(
        attempt_id=attempt.attempt_id, outbound_id=attempt.outbound_id, attempt_no=attempt.attempt_no,
        state=attempt.state.value, reason_code=attempt.reason_code, claimed_at=attempt.claimed_at,
        provider_message_id=attempt.provider_message_id,
        late_acceptance_provider_message_id=attempt.late_acceptance_provider_message_id,
    )


def _event(event_id: str, event_type: str, subjects: tuple[EntityRef, ...], after: JsonObject, correlation_id: str, now: datetime) -> AuditEvent:
    payload: dict[str, JsonValue] = {"event_type": event_type, "before": None, "after": after}
    return AuditEvent(
        event_id=event_id, occurred_at=now, actor=DISPATCH_ACTOR, event_type=event_type, subject_refs=subjects,
        after=after, correlation_id=correlation_id, payload_hash=hashlib.sha256(dumps_json(payload).encode("utf-8")).hexdigest(),
    )
