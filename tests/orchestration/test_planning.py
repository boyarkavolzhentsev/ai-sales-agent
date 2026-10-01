"""The read-only planning phase: determinism, fingerprints, blockers, flags, references,
capability awareness, the kill switch, and one consistent snapshot per plan."""

import json
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest

from app.orchestration import ExecutionAction as A
from app.orchestration import ExecutionBlocker as B
from app.orchestration import ExecutionOwner as O
from app.orchestration import ExecutionQueue, OrchestrationNotFoundError
from app.orchestration.planner import _jsonable, state_material
from app.orchestration.snapshot import gather
from app.persistence import Database, FrozenClock
from tests.inbound.builders import NOW
from tests.orchestration.builders import (
    ANSWER,
    FULL,
    OFFLINE,
    approve_pending,
    enrolled,
    orchestrator,
    reject_pending,
    table_rows,
    world,
)
from tests.pipeline.builders import approve_qualification, qualifying_lead
from tests.runtime.builders import runtime_config


def test_same_durable_state_gives_the_same_plan_and_fingerprint(db: Database) -> None:
    lead_id = qualifying_lead(db)
    first = orchestrator(db).plan(lead_id)
    again = orchestrator(db, at=NOW + timedelta(seconds=30)).plan(lead_id)  # a fresh coordinator, later
    assert first.fingerprint == again.fingerprint and first.fingerprint.startswith("sxp-")
    assert first.model_dump(exclude={"observed_at"}) == again.model_dump(exclude={"observed_at"})
    assert first.observed_at != again.observed_at  # observation time is not part of the fingerprint


def test_a_meaningful_change_gives_a_different_fingerprint(db: Database) -> None:
    lead_id = qualifying_lead(db)
    seen = {orchestrator(db).plan(lead_id).fingerprint}
    reject_pending(db)
    seen.add(orchestrator(db).plan(lead_id).fingerprint)
    approve_qualification(db, lead_id)
    seen.add(orchestrator(db).plan(lead_id).fingerprint)
    assert len(seen) == 3


def test_fingerprint_material_and_plans_never_carry_message_text(db: Database) -> None:
    lead_id = qualifying_lead(db)
    cfg = runtime_config(db.path)
    with db.transaction() as uow:
        snapshot = gather(uow, lead_id, qualification=cfg.pipeline.profile, commercial=cfg.commercial.profile,
                          follow_up=cfg.follow_up_config(), dispatch=cfg.dispatch_config(), now=NOW)
        bodies = [m.body_final for m in uow.outbound.list_by_lead(lead_id)] + [
            m.body_text for t in uow.threads.list_by_lead(lead_id) for m in uow.messages.list_by_thread(t.thread_id)]
        subjects = [m.subject for m in uow.outbound.list_by_lead(lead_id)]
    assert snapshot is not None and bodies
    material = json.dumps(_jsonable(state_material(snapshot)))
    plan_json = orchestrator(db).plan(lead_id).model_dump_json()
    view_json = orchestrator(db).view(lead_id).model_dump_json()
    for text in [*bodies, *subjects, ANSWER, "Basic plan"]:
        assert text not in material and text not in plan_json and text not in view_json


def test_planning_writes_nothing(db: Database) -> None:
    lead_id = qualifying_lead(db)
    before = table_rows(db)
    service = orchestrator(db)
    service.plan(lead_id)
    service.view(lead_id)
    service.metrics()
    for which in ExecutionQueue:
        service.queue(which)
    assert table_rows(db) == before


def test_one_plan_reads_one_transactional_snapshot(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    lead_id = qualifying_lead(db)
    opened = []
    real = db.transaction

    @contextmanager
    def counting():  # noqa: ANN202
        opened.append(1)
        with real() as uow:
            yield uow

    monkeypatch.setattr(db, "transaction", counting)
    orchestrator(db).plan(lead_id)
    assert len(opened) == 1


def test_unknown_lead_is_reported(db: Database) -> None:
    with pytest.raises(OrchestrationNotFoundError):
        orchestrator(db).plan("ld_missing")


def test_executable_versus_operator_versus_customer_flags(db_path: Path) -> None:
    w = world(db_path)
    enrolled(w)
    auto = w.plan()
    assert auto.executable and not auto.requires_operator and not auto.requires_customer and auto.blockers == ()
    assert auto.operation == "campaign.schedule_member/claim/execute" and auto.operator_commands == ()
    w.execute()
    review = w.plan()
    assert not review.executable and review.requires_operator and review.operation is None
    assert [c.value for c in review.operator_commands] == ["APPROVE_DRAFT", "REJECT_DRAFT"]
    approve_pending(w)
    send = w.plan()
    assert send.executable and send.refs.outbound_ids and send.refs.member_id == w.member_id
    w.execute(dispatch=True)
    waiting = w.plan()
    assert waiting.requires_customer and not waiting.executable and waiting.blockers == (B.WAITING_FOR_CUSTOMER,)
    w.app.stop()


def test_entity_refs_and_expected_versions_point_at_the_current_entities(db_path: Path) -> None:
    w = world(db_path)
    enrolled(w)
    plan = w.plan()
    assert plan.refs.member_id == w.member_id and plan.refs.campaign_id == "camp-1" and plan.refs.job_id is None
    assert plan.expected_versions["lead"] == w.lead_row().version and "campaign_member" in plan.expected_versions
    assert plan.contact_id == w.lead_row().contact_id
    w.app.stop()


def test_kill_switch_blocks_outbound_automation_but_planning_still_works(db_path: Path) -> None:
    from app.policy import KillSwitchState
    w = world(db_path, kill_switch=KillSwitchState(enabled=True, reason="incident", changed_at=NOW, changed_by="ops"))
    enrolled(w)
    plan = w.plan()
    assert (plan.owner, plan.action) == (O.CAMPAIGN, A.PREPARE_CAMPAIGN_TOUCH)
    assert plan.blockers == (B.KILL_SWITCH_ACTIVE,) and not plan.executable
    w.app.stop()


def test_missing_capabilities_keep_the_logical_action_but_block_execution(db: Database) -> None:
    from tests.dispatch.builders import approved_reply
    outbound_id = approved_reply(db)
    with db.transaction() as uow:
        message = uow.outbound.get(outbound_id)
    assert message is not None
    online = orchestrator(db, capabilities=FULL).plan(message.lead_id)
    offline = orchestrator(db, capabilities=OFFLINE).plan(message.lead_id)
    assert online.action is offline.action is A.SEND_APPROVED_MESSAGE
    assert online.executable and not offline.executable
    assert offline.blockers == (B.PROVIDER_CAPABILITY_MISSING,) and "capability:DISPATCH" in offline.sources
    with db.transaction() as uow:
        assert uow.outbound.get(outbound_id) == message  # nothing changed because an adapter is absent


def test_customer_wrote_last_without_llm_reports_the_llm_capability(db: Database) -> None:
    lead_id = qualifying_lead(db, facts={"need": "Automate invoice matching"})
    reject_pending(db)
    plan = orchestrator(db, capabilities=OFFLINE).plan(lead_id)
    assert plan.action is A.QUALIFY_LEAD and B.LLM_CAPABILITY_MISSING in plan.blockers
    assert B.LLM_CAPABILITY_MISSING not in orchestrator(db, capabilities=FULL).plan(lead_id).blockers


def test_send_policy_refusal_is_reported_without_reimplementing_quota(db: Database) -> None:
    from tests.dispatch.builders import approved_reply
    outbound_id = approved_reply(db)
    with db.transaction() as uow:
        lead_id = uow.outbound.get(outbound_id).lead_id  # type: ignore[union-attr]
    saturday = NOW + timedelta(days=5)  # outside the Mon-Fri sending window
    plan = orchestrator(db, clock=FrozenClock(saturday)).plan(lead_id)
    assert plan.action is A.SEND_APPROVED_MESSAGE and not plan.executable
    assert plan.blockers == (B.SEND_BLOCKED_BY_POLICY,) and "send:OUTSIDE_SENDING_WINDOW" in plan.sources


def test_blockers_are_normalized_and_ordered(db: Database) -> None:
    lead_id = qualifying_lead(db)
    plan = orchestrator(db).plan(lead_id)
    order = list(B)
    assert list(plan.blockers) == sorted(plan.blockers, key=order.index)
    assert list(plan.conditions) == sorted(plan.conditions, key=order.index)
    assert all(isinstance(b, B) for b in (*plan.blockers, *plan.conditions))


def test_kill_switch_covers_sends_and_follow_ups_with_one_stable_blocker(db_path: Path) -> None:
    from tests.dispatch.builders import approved_reply
    with Database(db_path) as db:
        outbound_id = approved_reply(db)
        with db.transaction() as uow:
            lead_id = uow.outbound.get(outbound_id).lead_id  # type: ignore[union-attr]
        send = orchestrator(db, kill_switch=True).plan(lead_id)
    assert send.action is A.SEND_APPROVED_MESSAGE and send.blockers == (B.KILL_SWITCH_ACTIVE,)
    assert "send:KILL_SWITCH" not in send.sources  # reported once, as the stable blocker
    from tests.inbound.builders import envelope
    w = world(db_path, facts={})
    result = w.app.handle_inbound(envelope("p-9", sender="eve@epsilon.example"), correlation_id="corr-9")
    w.lead_id = result.lead_id
    approve_pending(w)
    w.execute(dispatch=True)
    assert w.plan().action is A.PROCESS_FOLLOW_UP and w.plan().executable
    killed = orchestrator(w.db, kill_switch=True).plan(w.lead)
    assert killed.action is A.PROCESS_FOLLOW_UP and killed.blockers == (B.KILL_SWITCH_ACTIVE,)
    w.app.stop()
