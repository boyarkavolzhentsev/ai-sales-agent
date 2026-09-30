"""Builders for Stage 11 runtime tests: explicit configuration, deterministic fakes,
FrozenClock and file-backed temporary SQLite databases."""

from datetime import datetime
from pathlib import Path

from app.dispatch import FakeEmailTransport, FakeReconciler
from app.llm import FakeLLMTransport, SenderIdentity
from app.operator import ActivateCampaign
from app.persistence import Database, FrozenClock
from app.policy import KillSwitchState
from app.runtime import Adapters, CampaignSettings, RuntimeConfig, RuntimeMode, SalesAgentRuntime
from tests.campaign.builders import CAMPAIGN_ID, VALUE_QUESTION
from tests.inbound.builders import MAILBOX, NOW
from tests.operator.builders import AS_ALICE, FakeAuthenticator
from tests.policy import builders as policy


def runtime_config(database_path: Path | str, **overrides: object) -> RuntimeConfig:
    base: dict[str, object] = {
        "mode": RuntimeMode.LOCAL,
        "database_path": str(database_path),
        "mailboxes": (MAILBOX,),
        "sender": SenderIdentity(sender_name="Alex Seller", company_name="Samplewidget Co"),
        "limits": policy.limits(sends=20, new_contacts=20, follow_ups=20),
        "window": policy.window(),
        "kill_switch": KillSwitchState(enabled=False, changed_at=NOW, changed_by="ops"),
        "operator_ids": ("op-alice", "op-bob"),
        "campaign": CampaignSettings(value_question=VALUE_QUESTION),
    }
    return RuntimeConfig.model_validate(base | overrides)


def env(database_path: Path | str, **overrides: str | None) -> dict[str, str]:
    values = {
        "MODE": "local", "DATABASE_PATH": str(database_path), "SENDER_NAME": "Alex Seller", "COMPANY_NAME": "Samplewidget Co",
        "MAILBOXES": MAILBOX, "TIMEZONE": "Europe/Kyiv", "WINDOW_DAYS": "MON,TUE,WED,THU,FRI", "WINDOW_START": "09:00",
        "WINDOW_END": "18:00", "MAX_SENDS_PER_DAY": "20", "MAX_NEW_CONTACTS_PER_DAY": "20", "MAX_FOLLOW_UPS_PER_DAY": "20",
        "MAX_FOLLOW_UPS_PER_CONTACT": "3", "MIN_FOLLOW_UP_INTERVAL_HOURS": "48", "KILL_SWITCH": "false",
        "OPERATOR_IDS": "op-alice,op-bob",
    } | overrides
    return {f"SALES_AGENT_{key}": value for key, value in values.items() if value is not None}


def fake_adapters(
    transport: FakeEmailTransport | None = None, *, reconciler: bool = True, llm: FakeLLMTransport | None = None
) -> Adapters:
    transport = transport or FakeEmailTransport()
    return Adapters(
        email_transport=transport, reconciler=FakeReconciler(transport) if reconciler else None,
        llm_transport=llm or FakeLLMTransport(), authenticator=FakeAuthenticator(),
    )


def runtime(database_path: Path | str, *, at: datetime = NOW, adapters: Adapters | None = None,
            clock: FrozenClock | None = None, **config: object) -> SalesAgentRuntime:
    return SalesAgentRuntime(runtime_config(database_path, **config), adapters=adapters or fake_adapters(),
                             clock=clock or FrozenClock(at))


def activate_campaign(app: SalesAgentRuntime, campaign_id: str = CAMPAIGN_ID) -> None:
    with app_db(app).transaction() as uow:
        campaign = uow.campaigns.get(campaign_id)
    assert campaign is not None
    app.services.operator.activate_campaign(AS_ALICE, ActivateCampaign(
        command_id=f"cmd-activate-{campaign_id}", correlation_id="c", campaign_id=campaign_id, expected_campaign_version=campaign.version))


def app_db(app: SalesAgentRuntime) -> Database:
    """The runtime's own open Database (tests read durable state through it)."""
    return app._db  # noqa: SLF001
