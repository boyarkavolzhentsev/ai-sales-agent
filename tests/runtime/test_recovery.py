"""Crash/restart recovery through the runtime: work resumes in its owning subsystem, and
provider uncertainty is never resolved by a restart."""

from datetime import timedelta
from pathlib import Path

from app.core.enums import OutboundStatus
from app.dispatch import FakeBehavior, FakeEmailTransport
from app.persistence import FrozenClock
from app.runtime import SalesAgentRuntime
from tests.campaign.builders import CAMPAIGN_ID
from tests.conversation.builders import FIRST_DUE
from tests.inbound.builders import NOW, envelope, happy_transport
from tests.operator.builders import AS_ALICE
from tests.runtime.builders import app_db, fake_adapters, runtime
from tests.runtime.test_ticks import approve_all, with_campaign

LATER = FIRST_DUE + timedelta(minutes=1)


def drafts(app: SalesAgentRuntime) -> int:
    return len(app.services.operator.list_pending_drafts(AS_ALICE))


def test_expired_campaign_claim_is_recovered_once(db_path: Path) -> None:
    crashed = runtime(db_path)
    crashed.start()
    with_campaign(crashed)
    crashed.services.campaign_scheduler.schedule(CAMPAIGN_ID, correlation_id="c")
    [claim] = crashed.services.campaign_scheduler.claim_due("dead-worker", correlation_id="c")
    crashed.stop()  # the process died between claim and execution

    restarted = runtime(db_path, at=claim.lease_expires_at)
    report = restarted.start()
    assert report.recovery.expired_campaign_claims == 1
    assert restarted.campaign_tick().drafted == 1 and restarted.campaign_tick().drafted == 0
    assert drafts(restarted) == 1
    restarted.stop()


def test_expired_follow_up_claim_is_recovered_once(db_path: Path) -> None:
    clock = FrozenClock(NOW)
    crashed = runtime(db_path, clock=clock, adapters=fake_adapters(llm=happy_transport()))
    crashed.start()
    crashed.handle_inbound(envelope("p-1"), correlation_id="c")
    approve_all(crashed)
    crashed.dispatch_tick()
    crashed.follow_up_tick()
    clock.set(LATER)
    [claim] = crashed.services.follow_up_scheduler.claim_due("dead-worker", correlation_id="c")
    crashed.stop()

    restarted = runtime(db_path, at=claim.lease_expires_at)
    assert restarted.start().recovery.expired_follow_up_claims == 1
    assert restarted.follow_up_tick().drafted == 1 and restarted.follow_up_tick().drafted == 0
    assert drafts(restarted) == 1
    restarted.stop()


def test_unknown_dispatch_is_never_resent_after_restart(db_path: Path) -> None:
    first_transport = FakeEmailTransport().script(FakeBehavior.TIMEOUT)
    crashed = runtime(db_path, adapters=fake_adapters(first_transport))
    crashed.start()
    with_campaign(crashed)
    crashed.campaign_tick()
    [outbound_id] = approve_all(crashed)
    assert crashed.dispatch_tick().unknown == 1
    crashed.stop()

    new_transport = FakeEmailTransport()  # a fresh process has a fresh provider client
    restarted = runtime(db_path, adapters=fake_adapters(new_transport))
    report = restarted.start()
    assert report.recovery.unresolved_dispatch_attempts == 1 and report.recovery.sending_without_attempt == 0
    tick = restarted.tick(dispatch_approved=True)
    assert tick.ok and tick.reconciliation.unresolved == 1 and tick.dispatch is not None and tick.dispatch.processed == 0
    assert new_transport.calls == [] and len(first_transport.calls) == 1
    with app_db(restarted).transaction() as uow:
        message = uow.outbound.get(outbound_id)
    assert message is not None and message.status is OutboundStatus.SENDING
    restarted.stop()


def test_restart_does_not_duplicate_logical_work(db_path: Path) -> None:
    first = runtime(db_path)
    first.start()
    with_campaign(first)
    assert first.tick().campaign.drafted == 1
    first.stop()
    second = runtime(db_path)
    second.start()
    assert second.tick().campaign.drafted == 0 and drafts(second) == 1
    second.stop()
