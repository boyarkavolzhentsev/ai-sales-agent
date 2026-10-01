"""Telegram-selected runtimes over the fake Bot API (and the fake Gmail API when Gmail is
selected too). No network: tests/conftest.py blocks sockets."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.commercial.fake import FakeCommercialExtractor
from app.dispatch import FakeEmailTransport, FakeReconciler
from app.integrations import ProviderConnectors
from app.persistence import Database, FrozenClock
from app.pipeline.fake import FakeQualificationExtractor
from app.runtime import Adapters, SalesAgentRuntime, load_config
from tests.gmail.fakes import FakeGmailApi
from tests.inbound.builders import NOW
from tests.inbound.conftest import seed_knowledge
from tests.operator.builders import FakeAuthenticator
from tests.orchestration.builders import World, answering_llm
from tests.pipeline.builders import REQUIRED, extraction
from tests.runtime.builders import app_db, env
from tests.telegram.fakes import ALICE_CHAT, BOB_CHAT, FakeTelegramApi

BOT_TOKEN = "654321:test-secret-do-not-use-telegram-bot"  # deliberately not a real token shape
OPERATORS = f"{ALICE_CHAT}=op-alice,{BOB_CHAT}=op-bob"


def fake_connectors(telegram: FakeTelegramApi | None = None, gmail: FakeGmailApi | None = None) -> ProviderConnectors:
    telegram, gmail = telegram or FakeTelegramApi(), gmail or FakeGmailApi()
    return ProviderConnectors(gmail_api=lambda auth, timeout: gmail, telegram_api=lambda token, timeout: telegram)


def telegram_values(**overrides: str | None) -> dict[str, str | None]:
    return {"OPERATOR_PROVIDER": "telegram", "TELEGRAM_BOT_TOKEN": BOT_TOKEN, "TELEGRAM_OPERATOR_CHAT_IDS": OPERATORS} | overrides


@dataclass
class Console:
    """A started runtime with Telegram (and optionally Gmail) over fakes, plus a fake LLM
    and extractors so Stage 6 can process mail."""

    world: World
    telegram: FakeTelegramApi
    gmail: FakeGmailApi = field(default_factory=FakeGmailApi)

    @property
    def app(self) -> SalesAgentRuntime:
        return self.world.app

    @property
    def db(self) -> Database:
        return self.world.db

    def sync(self, limit: int | None = None):  # noqa: ANN201
        return self.app.operator_sync(limit=limit)


def console(tmp_path: Path, *, gmail: bool = False, at: datetime = NOW, telegram: FakeTelegramApi | None = None,
            **overrides: str | None) -> Console:
    telegram = telegram or FakeTelegramApi()
    gmail_api = FakeGmailApi()
    clock = FrozenClock(at)
    values = telegram_values(**overrides)
    if gmail:
        from tests.gmail.builders import gmail_values
        values = gmail_values(tmp_path) | values
    config = load_config(env(tmp_path / "agent.sqlite3", **values), now=at)
    llm = answering_llm()
    qualification, commercial = FakeQualificationExtractor(default=extraction(**REQUIRED)), FakeCommercialExtractor()
    transport = None if gmail else FakeEmailTransport()  # Telegram-only worlds dispatch through the fake transport
    adapters = Adapters(llm_transport=llm, authenticator=FakeAuthenticator(), qualification_extractor=qualification,
                        commercial_extractor=commercial, email_transport=transport,
                        reconciler=FakeReconciler(transport) if transport else None)
    app = SalesAgentRuntime(config, adapters=adapters, clock=clock, connectors=fake_connectors(telegram, gmail_api))
    app.start()
    seed_knowledge(app_db(app))
    world = World(app=app, clock=clock, transport=transport, llm=llm, qualification=qualification, commercial=commercial)  # type: ignore[arg-type]
    return Console(world=world, telegram=telegram, gmail=gmail_api)
