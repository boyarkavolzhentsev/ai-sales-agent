"""Builders for Stage 19 (embeddings + semantic retrieval) tests: fictional knowledge trees,
real adapters over the fake vendor session, indexers, retrievers and full runtimes with a
live-provider LLM (fake vendor API) plus Telegram/Gmail fakes. No network."""

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from app.core.enums import KnowledgeDomain, KnowledgePurpose, LeadIntent
from app.core.models import KnowledgeQuery
from app.integrations.config import EmbeddingsProviderConfig
from app.integrations.embeddings.base import HttpEmbeddingTransport
from app.integrations.embeddings.provider import build_embeddings
from app.integrations.providers import EmbeddingsProviderId
from app.integrations.secrets import EmbeddingsSecrets
from app.knowledge import KnowledgeIndexer, SemanticRetriever, ingest_directory
from app.persistence import Database, FrozenClock
from tests.inbound.builders import NOW, classification
from tests.knowledge.sources import FIXTURE_ROOT, fact, meta, write, yaml_doc
from tests.llm_providers.builders import llm_values
from tests.llm_providers.fakes import Brain
from tests.rag.fakes import EMBEDDINGS_KEY, Vendor
from tests.telegram.builders import Console, console


def model_of(provider: str) -> str:
    return f"{provider}-embed-under-test"


def emb_values(provider: str = "openai", **overrides: str | None) -> dict[str, str | None]:
    return {"EMBEDDINGS_PROVIDER": provider, "EMBEDDINGS_MODEL": model_of(provider),
            "EMBEDDINGS_API_KEY": EMBEDDINGS_KEY} | overrides


def transport(provider: str, vendor: Vendor, *, dims: int | None = None, timeout: int = 30,
              model: str | None = None) -> HttpEmbeddingTransport:
    config = EmbeddingsProviderConfig(provider=EmbeddingsProviderId(provider.upper()), model=model or model_of(provider),
                                      dimensions=dims, timeout_seconds=timeout)
    return build_embeddings(config, EmbeddingsSecrets(api_key=SecretStr(EMBEDDINGS_KEY)), session=vendor.session)


# ---- Knowledge ----------------------------------------------------------------------------------------


def price_list(price: str = "79", *, version: int = 2, approval: str = "APPROVED", body: str | None = None,
               **metadata: object) -> str:
    approved = {} if approval == "APPROVED" else {"approved_by": None, "approved_at": None}
    return yaml_doc(
        meta(source_id="sample-price-list", domain="PRICING_COMMERCIAL", title="Sample Widget price list (fictional)",
             version=version, approval_status=approval, tags=["pricing"], **approved, **metadata),
        facts=[fact("plan.basic.monthly_price", price, "EUR", f"The Basic plan of the Sample Widget costs {price} EUR per month.")],
        body=body if body is not None else "## Billing\nInvoices are issued monthly in EUR.\n",
    )


def knowledge_dir(root: Path, *, price: str = "79", version: int = 2, extra: dict[str, tuple[str, str]] | None = None) -> Path:
    """A copy of the fictional fixture tree with our own price list (and ``extra`` files:
    name -> (domain, text))."""
    kb = root / "kb"
    if kb.exists():
        shutil.rmtree(kb)
    shutil.copytree(FIXTURE_ROOT, kb)
    (kb / "pricing" / "price_list.yaml").unlink()
    write(kb, "PRICING_COMMERCIAL", f"price_list_v{version}.yaml", price_list(price, version=version))
    for name, (domain, text) in (extra or {}).items():
        write(kb, domain, name, text)
    return kb


def seeded_db(path: Path, kb: Path) -> Path:
    with Database(path) as db:
        db.initialize_schema(FrozenClock(NOW))
        with db.transaction() as uow:
            ingest_directory(uow, kb, now=NOW)
    return path


def ingest(db: Database, kb: Path) -> None:
    with db.transaction() as uow:
        ingest_directory(uow, kb, now=NOW)


def indexer(db: Database, vendor: Vendor, provider: str = "openai", *, dims: int | None = None, batch_size: int = 32,
            clock: FrozenClock | None = None, model: str | None = None) -> KnowledgeIndexer:
    return KnowledgeIndexer(db, clock or FrozenClock(NOW), transport(provider, vendor, dims=dims, model=model),
                            batch_size=batch_size)


def retriever(db: Database, vendor: Vendor, provider: str = "openai", *, min_similarity: float = 0.30,
              dims: int | None = None, model: str | None = None) -> SemanticRetriever:
    return SemanticRetriever(db, transport(provider, vendor, dims=dims, model=model), min_similarity=min_similarity)


def query(*questions: str, domains: tuple[KnowledgeDomain, ...] = tuple(KnowledgeDomain),
          required: tuple[KnowledgeDomain, ...] = (), top_k: int = 5, query_id: str = "kq-rag") -> KnowledgeQuery:
    return KnowledgeQuery(query_id=query_id, purpose=KnowledgePurpose.INBOUND_REPLY, questions=questions,
                          allowed_domains=domains, required_domains=required, locale="en", top_k=top_k,
                          correlation_id="corr-rag")


def chunk_ids(db: Database, sql: str = "SELECT chunk_id FROM knowledge_embeddings") -> list[str]:
    with db.transaction() as uow:
        return [r[0] for r in uow._tx.fetch_all(sql)]  # noqa: SLF001


# ---- Live runtimes ----------------------------------------------------------------------------------------


def intent(question: str, kind: str = "PRICING_REQUEST") -> dict[str, Any]:
    return json.loads(classification(LeadIntent(kind), question).text)


def grounded_draft(price: str, body: str | None = None) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """A model that cites the supplied evidence stating the price (none if there is none)."""
    def answer(data: dict[str, Any]) -> dict[str, Any]:
        cited = [e["evidence_id"] for e in data["evidence"] if f"{price} EUR" in e["excerpt"]]
        return {"subject": "Re: Pricing question", "body": body or f"Hi, the Basic plan costs {price} EUR per month.",
                "evidence_ids_used": cited, "proposed_next_step": "ANSWER_QUESTIONS"}
    return answer


def rag_console(tmp_path: Path, *, llm: str = "openai", emb: str | None = "openai", price: str = "79",
                brain: Brain | None = None, vendor: Vendor | None = None, kb: Path | None = None, gmail: bool = False,
                index: bool = True, **overrides: str | None) -> tuple[Console, Brain, Vendor]:
    """A started runtime (live LLM + embeddings adapters over fakes, Telegram, Gmail or the
    fake transport) whose knowledge is ``kb`` (pre-ingested, so no other fixture is seeded),
    indexed once unless ``index`` is False."""
    kb = kb or knowledge_dir(tmp_path, price=price)
    seeded_db(tmp_path / "agent.sqlite3", kb)
    brain = brain or Brain()
    brain.script("ReplyDraftProposal", *(grounded_draft(price),) * 8)
    vendor = vendor or Vendor()
    values = llm_values(llm) | (emb_values(emb) if emb else {}) | {"KNOWLEDGE_DIR": str(kb)} | overrides
    c = console(tmp_path, gmail=gmail, llm_session=brain.session, embeddings_session=vendor.session, **values)
    if index and emb:
        result = c.app.knowledge_index()
        assert result.status.value == "OK", result
    return c, brain, vendor
