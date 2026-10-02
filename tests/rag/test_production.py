"""Stage 19 production rule: semantic retrieval is mandatory in PRODUCTION (embeddings are a
production-required category); LOCAL/TEST keep the lexical path with EMBEDDINGS=NONE.

Why: the lexical path has a demonstrated false-positive sufficiency path ("Do you support
SSO?" is covered by an unrelated "Support hours" chunk), and the claim check cannot always
reject a qualitative capability claim. Semantic retrieval with its threshold returns no
evidence for it, so the message escalates."""

import io
import json
from pathlib import Path

import pytest

from app.core.enums import EscalationReason, KnowledgeDecision, KnowledgeDomain
from app.knowledge import LexicalRetriever, SemanticRetriever
from app.persistence import Database
from app.runtime import SalesAgentRuntime, StartupError, cli, load_config
from app.runtime.results import RuntimeState
from tests.campaign.builders import PROSPECT, campaign_messages
from tests.inbound.builders import NOW
from tests.integrations.builders import full_env
from tests.llm_providers.fakes import Brain
from tests.llm_providers.test_end_to_end import first_touch, sent
from tests.rag.builders import emb_values, indexer, intent, knowledge_dir, query, rag_console, seeded_db
from tests.rag.fakes import Vendor
from tests.rag.test_end_to_end import knowledge_events, reply_drafts
from tests.telegram.builders import Console, fake_connectors

SSO = "Do you support SSO?"
SSO_CLAIM = {"subject": "Re: SSO", "body": "Yes, we support SSO with Okta and SAML.", "evidence_ids_used": [],
             "proposed_next_step": "ANSWER_QUESTIONS"}


def production(tmp_path: Path, **values: str | None) -> dict[str, str]:
    return full_env(tmp_path, MODE="production", **values)


# ---- Readiness ------------------------------------------------------------------------------------------------


def test_production_without_embeddings_fails_before_touching_the_database(tmp_path: Path) -> None:
    vendor, brain = Vendor(), Brain()
    app = SalesAgentRuntime(load_config(production(tmp_path), now=NOW),
                            connectors=fake_connectors(llm_session=brain.session, embeddings_session=vendor.session))
    with pytest.raises(StartupError) as error:
        app.start()
    assert error.value.code == "PRODUCTION_NOT_READY" and str(error.value) == "PRODUCTION_NOT_READY: EMBEDDINGS:DISABLED"
    assert app.state is RuntimeState.FAILED and not (tmp_path / "agent.sqlite3").exists()
    assert vendor.session.posts == [] and brain.session.posts == []


def test_provider_status_reports_the_production_blocker_without_a_request(tmp_path: Path,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    vendor = Vendor()
    monkeypatch.setattr(cli, "CONNECTORS", fake_connectors(embeddings_session=vendor.session))
    out = io.StringIO()
    assert cli.main(["provider-status"], production(tmp_path), out) == 0
    report = json.loads(out.getvalue())
    assert report["production_ready"] is False
    assert report["integrations"]["production_blockers"] == ["EMBEDDINGS:DISABLED"]
    out = io.StringIO()
    assert cli.main(["provider-status"], production(tmp_path, **emb_values("gemini")), out) == 0
    assert json.loads(out.getvalue())["production_ready"] is True and vendor.session.posts == []


def test_local_mode_without_embeddings_starts_with_lexical_retrieval(tmp_path: Path) -> None:
    app = SalesAgentRuntime(load_config(full_env(tmp_path), now=NOW), connectors=fake_connectors(llm_session=Brain().session))
    report = app.start()
    assert app.state is RuntimeState.READY and not report.capabilities.semantic_retrieval
    assert isinstance(app.services.inbound._knowledge, LexicalRetriever)  # noqa: SLF001
    assert not report.integrations.production_ready  # usable locally, never production-ready
    app.stop()


@pytest.mark.parametrize("llm,emb", [("openai", "openai"), ("gemini", "gemini"), ("anthropic", "openai"),
                                     ("anthropic", "gemini")])
def test_production_starts_with_any_valid_embeddings_provider(tmp_path: Path, llm: str, emb: str) -> None:
    from tests.llm_providers.builders import llm_values
    vendor = Vendor()
    app = SalesAgentRuntime(load_config(production(tmp_path, **(llm_values(llm) | emb_values(emb))), now=NOW),
                            connectors=fake_connectors(llm_session=Brain().session, embeddings_session=vendor.session))
    report = app.start()
    assert app.state is RuntimeState.READY and report.integrations.production_blockers == ()
    assert report.capabilities.semantic_retrieval and isinstance(app.services.inbound._knowledge, SemanticRetriever)  # noqa: SLF001
    assert vendor.session.posts == []  # still no billable request at startup
    app.stop()


# ---- The production path never drafts an unsupported capability claim ----------------------------------------


def production_console(tmp_path: Path, *, index: bool) -> tuple[Console, Brain, Vendor]:
    c, brain, vendor = rag_console(tmp_path, gmail=True, index=index, MODE="production")
    assert c.app.state is RuntimeState.READY and c.app.health().capabilities.semantic_retrieval
    assert c.app.email_sync().status.value == "INITIALIZED"
    first_touch(c)
    return c, brain, vendor


def ask(c: Console, brain: Brain, question: str, kind: str, message_id: str) -> None:
    from tests.gmail.fakes import customer_email
    [first] = campaign_messages(c.db, c.world.lead)
    brain.script("IntentClassificationProposal", intent(question, kind))
    c.gmail.deliver(customer_email(sender=PROSPECT, body=f"Hello, {question}", message_id=message_id,
                                   in_reply_to=first.rfc_message_id))
    assert c.app.email_sync().processed == 1


def escalation_reasons(c: Console) -> set[str]:
    with c.db.transaction() as uow:
        rows = uow._tx.fetch_all("SELECT data FROM escalations")  # noqa: SLF001
    return {reason for r in rows for reason in json.loads(r[0])["reasons"]}


def test_production_sso_question_without_approved_evidence_is_escalated_never_drafted_or_sent(tmp_path: Path) -> None:
    c, brain, vendor = production_console(tmp_path, index=True)
    brain.scripts["ReplyDraftProposal"].clear()
    brain.script("ReplyDraftProposal", SSO_CLAIM)  # the model would claim SSO if it were asked
    before = sent(c)
    ask(c, brain, SSO, "INFO_REQUEST", "<sso-1@acme-prospect.example>")
    [assessed] = knowledge_events(c)
    assert assessed["retrieval"]["method"] == "SEMANTIC" and assessed["evidence"] == []  # type: ignore[index]
    assert assessed["deterministic_assessment"]["decision"] == KnowledgeDecision.INSUFFICIENT.value  # type: ignore[index]
    assert EscalationReason.KNOWLEDGE_INSUFFICIENT.value in escalation_reasons(c)
    assert "ReplyDraftProposal" not in brain.calls and reply_drafts(c) == []
    c.sync()
    c.app.dispatch_tick()
    assert sent(c) == before
    assert not any(b"SSO" in m.raw for m in c.gmail.messages.values() if "SENT" in m.labels)


@pytest.mark.parametrize("question,kind", [(SSO, "INFO_REQUEST"),
                                           ("What does the Basic plan cost per month?", "PRICING_REQUEST")])
def test_production_with_an_empty_index_escalates_without_lexical_fallback(tmp_path: Path, question: str, kind: str) -> None:
    c, brain, vendor = production_console(tmp_path, index=False)
    before = sent(c)
    ask(c, brain, question, kind, "<empty-1@acme-prospect.example>")
    [assessed] = knowledge_events(c)
    assert assessed["retrieval"]["method"] == "SEMANTIC" and assessed["evidence"] == []  # type: ignore[index]
    assert "SEMANTIC_INDEX_INCOMPLETE" in assessed["deterministic_assessment"]["deterministic_flags"]  # type: ignore[index]
    assert vendor.session.posts == []  # nothing indexed: no query embedding, no billable call
    assert "ReplyDraftProposal" not in brain.calls and reply_drafts(c) == []
    c.app.dispatch_tick()
    assert sent(c) == before


def test_the_known_lexical_weakness_is_confined_to_local_mode(tmp_path: Path) -> None:
    """Documented limitation: lexical retrieval (EMBEDDINGS=NONE, local/test only) treats an
    unrelated "Support hours" chunk as covering the SSO question. Production cannot run that
    path; semantic retrieval finds no evidence."""
    domains = (KnowledgeDomain.PRODUCTS_SERVICES, KnowledgeDomain.COMPANY, KnowledgeDomain.FAQ,
               KnowledgeDomain.MEETING_GUIDANCE)
    with Database(seeded_db(tmp_path / "x.sqlite3", knowledge_dir(tmp_path))) as db:
        indexer(db, Vendor()).run()
        from tests.rag.builders import retriever
        lexical = LexicalRetriever(db).evaluate(query(SSO, domains=domains), NOW)
        semantic = retriever(db, Vendor()).evaluate(query(SSO, domains=domains), NOW)
    assert lexical.assessment.decision is KnowledgeDecision.SUFFICIENT  # the local-only weakness
    assert semantic.evidence == () and semantic.assessment.decision is KnowledgeDecision.INSUFFICIENT


def test_production_answers_a_supported_question_after_indexing(tmp_path: Path) -> None:
    c, brain, _ = production_console(tmp_path, index=True)
    before = sent(c)
    ask(c, brain, "How much is Basic?", "PRICING_REQUEST", "<ok-1@acme-prospect.example>")
    [draft] = reply_drafts(c)
    assert "79 EUR" in draft.body_final  # type: ignore[attr-defined]
    c.app.dispatch_tick()
    assert sent(c) == before  # a draft waits for operator review; nothing is sent automatically
