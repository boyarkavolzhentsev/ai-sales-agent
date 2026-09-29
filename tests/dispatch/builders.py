"""Builders for dispatch tests: a real Stage 6 draft, a real Stage 7 approval, the fake
transport, FrozenClock and a file-backed temporary SQLite database."""

from dataclasses import dataclass

from app.core.enums import OutboundStatus
from app.core.models import OutboundMessage
from app.dispatch import (
    DispatchConfig,
    DispatchReconciler,
    DispatchRequest,
    DispatchResult,
    DispatchService,
    FakeEmailTransport,
)
from app.llm import SenderIdentity
from app.persistence import Database, DispatchAttempt, FrozenClock, QuotaReservation
from app.policy import KillSwitchState, LimitPolicy
from tests.inbound.builders import MAILBOX, NOW, envelope, happy_transport, process
from tests.operator.builders import AS_ALICE, approve_command
from tests.operator.builders import operator as operator_service
from tests.policy import builders as policy


def config(**overrides: object) -> DispatchConfig:
    base: dict[str, object] = {
        "sender_mailboxes": (MAILBOX,),
        "sender": SenderIdentity(sender_name="Alex Seller", company_name="Samplewidget Co"),
        "limits": policy.limits(sends=10, new_contacts=10),
        "window": policy.window(),  # Mon-Fri 09:00-18:00 Europe/Kyiv; NOW is Monday 15:00 there
        "kill_switch": KillSwitchState(enabled=False, changed_at=NOW, changed_by="ops"),
    }
    return DispatchConfig.model_validate(base | overrides)


def one_slot() -> LimitPolicy:
    return policy.limits(sends=1, new_contacts=10)


def dispatcher(
    db: Database,
    transport: FakeEmailTransport | None = None,
    *,
    reconciler: DispatchReconciler | None = None,
    clock: FrozenClock | None = None,
    **config_overrides: object,
) -> DispatchService:
    return DispatchService(db, clock or FrozenClock(NOW), config(**config_overrides), transport or FakeEmailTransport(), reconciler)


def drafted(db: Database, provider_message_id: str = "p-1", **envelope_overrides: object) -> str:
    result = process(db, happy_transport(), envelope(provider_message_id, **envelope_overrides))
    assert result.outbound_id is not None, result
    return result.outbound_id


def approve(db: Database, outbound_id: str, command_id: str = "cmd-approve") -> None:
    service = operator_service(db)
    service.approve_draft(AS_ALICE, approve_command(service.get_draft(AS_ALICE, outbound_id), command_id))


def approved_reply(db: Database, provider_message_id: str = "p-1", **envelope_overrides: object) -> str:
    outbound_id = drafted(db, provider_message_id, **envelope_overrides)
    approve(db, outbound_id, f"cmd-approve-{provider_message_id}")
    return outbound_id


def request(outbound_id: str, correlation_id: str = "corr-dispatch") -> DispatchRequest:
    return DispatchRequest(outbound_id=outbound_id, correlation_id=correlation_id)


def send(service: DispatchService, outbound_id: str, correlation_id: str = "corr-dispatch") -> DispatchResult:
    return service.dispatch(request(outbound_id, correlation_id))


@dataclass(frozen=True)
class State:
    outbound: OutboundMessage
    attempts: list[DispatchAttempt]
    reservations: list[QuotaReservation]


def state(db: Database, outbound_id: str) -> State:
    with db.transaction() as uow:
        outbound = uow.outbound.get(outbound_id)
        assert outbound is not None
        rows = uow._tx.fetch_all("SELECT reservation_id FROM quota_reservations WHERE outbound_id = ? ORDER BY 1", (outbound_id,))  # noqa: SLF001
        reservations = [r for r in (uow.quota_reservations.get(row[0]) for row in rows) if r is not None]
        return State(outbound, uow.dispatch_attempts.list_for_outbound(outbound_id), reservations)


def status(db: Database, outbound_id: str) -> OutboundStatus:
    return state(db, outbound_id).outbound.status


WRITABLE_TABLES = (
    "outbound_messages", "dispatch_attempts", "quota_reservations", "leads", "email_messages", "email_threads",
    "idempotency_keys", "do_not_contact",
)


def snapshot(db: Database) -> tuple[object, ...]:
    """Every operational row dispatch could touch (audit excluded: blocked requests are audited)."""
    with db.transaction() as uow:
        return tuple(
            tuple(tuple(row) for row in uow._tx.fetch_all(f"SELECT * FROM {table} ORDER BY 1"))  # noqa: SLF001
            for table in WRITABLE_TABLES
        )
