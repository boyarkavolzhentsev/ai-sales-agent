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


def fake_connectors(telegram: FakeTelegramApi | None = None, gmail: FakeGmailApi | None = None,
                    llm_session: object = None, embeddings_session: object = None) -> ProviderConnectors:
    """Fakes beneath every provider adapter; ``llm_session`` is the HTTP session a selected
    LLM adapter posts through (Stage 18), ``embeddings_session`` the one a selected
    embeddings adapter posts through (Stage 19); fresh refusing ones by default."""
    telegram, gmail = telegram or FakeTelegramApi(), gmail or FakeGmailApi()
    if llm_session is None:
        from tests.llm_providers.fakes import FakeSession
        llm_session = FakeSession()
    if embeddings_session is None:
        from tests.llm_providers.fakes import FakeSession
        embeddings_session = FakeSession()
    return ProviderConnectors(gmail_api=lambda auth, timeout: gmail, telegram_api=lambda token, timeout: telegram,
                              llm_session=lambda: llm_session, embeddings_session=lambda: embeddings_session)


def telegram_values(**overrides: str | None) -> dict[str, str | None]:
    return {"OPERATOR_PROVIDER": "telegram", "TELEGRAM_BOT_TOKEN": BOT_TOKEN, "TELEGRAM_OPERATOR_CHAT_IDS": OPERATORS} | overrides


def knowledge_seeded(db: Database) -> bool:
    with db.transaction() as uow:
        return bool(uow._tx.fetch_all("SELECT 1 FROM knowledge_chunks LIMIT 1"))  # noqa: SLF001


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
            llm_session: object = None, gmail_api: FakeGmailApi | None = None, embeddings_session: object = None,
            **overrides: str | None) -> Console:
    telegram = telegram or FakeTelegramApi()
    gmail_api = gmail_api or FakeGmailApi()  # pass the previous one to restart over the same mailbox
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
    app = SalesAgentRuntime(config, adapters=adapters, clock=clock, connectors=fake_connectors(telegram, gmail_api, llm_session, embeddings_session))
    app.start()
    if not knowledge_seeded(app_db(app)):
        seed_knowledge(app_db(app))
    world = World(app=app, clock=clock, transport=transport, llm=llm, qualification=qualification, commercial=commercial)  # type: ignore[arg-type]
    return Console(world=world, telegram=telegram, gmail=gmail_api)
