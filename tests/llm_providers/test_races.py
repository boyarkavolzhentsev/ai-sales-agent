"""Two processes handle the same inbound email at once through the live-provider adapters:
Stage 6/12/13 idempotency stays authoritative, so the model's (possibly repeated) answers
produce exactly one logical result: one message, one draft, one set of facts."""

from pathlib import Path

import pytest

from app.persistence import Database, FrozenClock
from app.runtime import Adapters, SalesAgentRuntime, load_config
from tests.campaign.builders import PROSPECT
from tests.inbound.builders import NOW, envelope
from tests.llm_providers.builders import live, llm_values
from tests.llm_providers.fakes import Brain
from tests.llm_providers.test_end_to_end import FACTS_MESSAGE, GROUNDED_FACTS, first_touch
from tests.operator.builders import FakeAuthenticator
from tests.runtime.builders import env
from tests.telegram.builders import fake_connectors, telegram_values
from tests.telegram.test_races_and_cards import in_parallel


@pytest.mark.parametrize("attempt", range(5))
def test_concurrent_processing_of_one_email_has_one_result(tmp_path: Path, attempt: int) -> None:
    c, _ = live(tmp_path, "openai")
    first_touch(c)
    sent = [m for m in c.world.messages() if m.rfc_message_id is not None][-1]
    now = c.world.clock.now()
    c.app.stop()
    message = envelope("p-race", sender=PROSPECT, body=FACTS_MESSAGE, in_reply_to=sent.rfc_message_id, received_at=NOW)

    brains: list[Brain] = []

    def one_process():  # noqa: ANN202 - its own runtime, connection and provider session
        brain = Brain().script("QualificationCandidates", GROUNDED_FACTS)
        brains.append(brain)
        config = load_config(env(tmp_path / "agent.sqlite3", **(telegram_values() | llm_values("openai"))), now=NOW)
        app = SalesAgentRuntime(config, adapters=Adapters(authenticator=FakeAuthenticator()),
                                clock=FrozenClock(now), connectors=fake_connectors(llm_session=brain.session))
        app.start()
        try:
            return app.handle_inbound(message, correlation_id=f"race-{id(brain)}")
        finally:
            app.stop()

    results = in_parallel(one_process, one_process)
    assert not any(isinstance(r, BaseException) for r in results), results
    assert len({r.message_id for r in results}) == 1, [(r.reply_decision, r.escalation_reasons) for r in results]  # type: ignore[union-attr]
    assert all(r.reply_decision.value == "DRAFT_FOR_REVIEW" for r in results), [(r.reply_decision, r.escalation_reasons) for r in results]  # type: ignore[union-attr]
    with Database(tmp_path / "agent.sqlite3") as db, db.transaction() as uow:
        drafts = [m for m in uow.outbound.list_by_lead(c.world.lead) if m.kind.value != "FIRST_TOUCH"]
        qualification = uow.qualifications.get(c.world.lead)
        finals = uow._tx.fetch_all("SELECT COUNT(*) FROM idempotency_keys WHERE key LIKE 'inbound:final:%'")[0][0]  # noqa: SLF001
    assert len(drafts) == 1 and finals == 1  # one finalization, one reply draft
    assert qualification is not None and len(qualification.facts) == 4  # facts applied once
    # The enrichment job is claimed before the model is called: one extraction request in total.
    assert sum(b.calls.count("QualificationCandidates") for b in brains) == 1
