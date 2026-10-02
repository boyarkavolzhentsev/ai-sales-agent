"""``deployment-check``: is this deployment ready to run its workload? (Stage 20)

Readiness, not liveness, and not connectivity. Safe by construction:
- no network: no provider is contacted (Gmail and Telegram are verified when the runtime
  starts; LLM and embeddings only by the explicit, billable ``llm-check`` /
  ``embeddings-check``), so no mail is read or sent, no Telegram update is consumed and no
  token is spent;
- no mutation: the database is opened read-only (``mode=ro``) and only if it exists; nothing
  is created, migrated, claimed or advanced.

Levels (all must hold for ``ready``):
- ``config_ready``: the ``SALES_AGENT_*`` configuration loads (and, in PRODUCTION mode, every
  production-required provider is CONFIGURED: email, LLM, operator channel, knowledge,
  embeddings);
- ``database_ready``: the database file exists (``init`` has run), it and its directory are
  writable by this process, and its schema is the current one;
- ``knowledge_index_ready``: approved, usable knowledge exists, and with an embeddings
  provider every usable chunk has a current vector in the configured space (else questions
  escalate with SEMANTIC_INDEX_INCOMPLETE until ``knowledge-index`` runs).
Operational counts (unresolved dispatch attempts, AI enrichment jobs, operator notifications,
mailbox recovery, open escalations) are reported for visibility and as warnings, never as
blockers: they are normal states with their own recovery paths. Paths, values and secrets
are never printed: codes and counts only.
"""

import os
import sqlite3
import stat
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from app.core.enums import EscalationStatus
from app.core.models.base import CoreModel
from app.embeddings import EmbeddingSpace
from app.integrations import IntegrationStatus
from app.knowledge.semantic import SemanticIndexError, all_versions, indexable_sources, searchable_vectors
from app.persistence import MEMORY, Database, PersistenceError
from app.persistence.migrations import current_version, latest_version
from app.runtime.config import RuntimeConfig, RuntimeMode
from app.runtime.env import inspect_integrations, load_config
from app.runtime.errors import ConfigError


class KnowledgeIndexReadiness(CoreModel):
    method: str  # SEMANTIC (an embeddings provider is configured) or LEXICAL
    eligible_chunks: int  # chunks of approved, usable source versions
    indexed_chunks: int | None = None  # of those, with a current vector (SEMANTIC only)
    ready: bool


class DeploymentReport(CoreModel):
    ready: bool
    mode: str | None
    config_ready: bool
    production_ready: bool
    database_ready: bool
    knowledge_index_ready: bool
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()  # configuration problems: variable names and codes only
    schema_version: int | None = None
    latest_schema_version: int
    knowledge_index: KnowledgeIndexReadiness | None = None
    operations: dict[str, int] = {}
    integrations: IntegrationStatus


def check_deployment(environ: Mapping[str, str], *, now: datetime) -> DeploymentReport:
    status = inspect_integrations(environ)
    base = {"latest_schema_version": latest_version(), "integrations": status, "production_ready": status.production_ready}
    try:
        config = load_config(environ, now=now)
    except ConfigError as exc:
        return DeploymentReport(ready=False, mode=None, config_ready=False, database_ready=False, knowledge_index_ready=False,
                                blockers=("CONFIG_INVALID",), problems=exc.problems, **base)
    blockers: list[str] = []
    warnings: list[str] = [w for p in status.providers for w in p.warnings]
    if config.mode is RuntimeMode.PRODUCTION and not status.production_ready:
        blockers += [f"PRODUCTION_NOT_READY:{b}" for b in status.production_blockers]
    if config.kill_switch.enabled:
        warnings.append("KILL_SWITCH_ON")  # sends are refused until it is switched off
    warnings += _token_permissions(config)
    config_ready = not blockers

    version, database_blockers = _database(config)
    blockers += database_blockers
    index: KnowledgeIndexReadiness | None = None
    operations: dict[str, int] = {}
    if not database_blockers:
        try:
            index, operations = _inspect(config, now)
        except SemanticIndexError:
            blockers.append("KNOWLEDGE_INDEX_CORRUPT")  # mixed sizes or an unreadable vector: re-run knowledge-index
        except PersistenceError:
            blockers.append("DATABASE_UNREADABLE")
    if index is not None and not index.ready:
        blockers.append("KNOWLEDGE_EMPTY" if index.eligible_chunks == 0 else "KNOWLEDGE_INDEX_INCOMPLETE")
    warnings += _operational_warnings(operations)
    knowledge_ready = index is not None and index.ready
    return DeploymentReport(
        ready=not blockers, mode=config.mode.value, config_ready=config_ready, database_ready=not database_blockers,
        knowledge_index_ready=knowledge_ready, blockers=tuple(dict.fromkeys(blockers)),
        warnings=tuple(dict.fromkeys(warnings)), schema_version=version, knowledge_index=index, operations=operations, **base,
    )


def _database(config: RuntimeConfig) -> tuple[int | None, list[str]]:
    if config.database_path == MEMORY:
        return None, ["DATABASE_NOT_PERSISTENT"]
    path = Path(config.database_path)
    if not os.access(path.parent, os.W_OK):
        return None, ["DATABASE_DIRECTORY_NOT_WRITABLE"]
    if not path.is_file():
        return None, ["DATABASE_MISSING"]  # run: python -m app.runtime init
    if not os.access(path, os.W_OK):
        return None, ["DATABASE_NOT_WRITABLE"]
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            version = current_version(connection)
        finally:
            connection.close()
    except sqlite3.Error:
        return None, ["DATABASE_UNREADABLE"]
    return version, ([] if version == latest_version() else ["SCHEMA_NOT_CURRENT"])


def _inspect(config: RuntimeConfig, now: datetime) -> tuple[KnowledgeIndexReadiness, dict[str, int]]:
    embeddings = config.integrations.embeddings
    with Database.read_only(config.database_path) as uow:
        sources = indexable_sources(all_versions(uow), now)
        chunks = uow.knowledge_index.list_chunks_for_sources({(s.source_id, s.version) for s in sources})
        if embeddings.provider.value == "NONE":
            index = KnowledgeIndexReadiness(method="LEXICAL", eligible_chunks=len(chunks), ready=bool(chunks))
        else:
            space = EmbeddingSpace(provider=embeddings.provider.value, model=embeddings.model or "-",
                                   dimensions=embeddings.dimensions)
            indexed = len(searchable_vectors(uow, chunks, space))
            index = KnowledgeIndexReadiness(method="SEMANTIC", eligible_chunks=len(chunks), indexed_chunks=indexed,
                                            ready=bool(chunks) and indexed == len(chunks))
        jobs = uow.enrichment_jobs.counts()
        notifications = uow.operator_channel.notification_counts()
        mailbox = config.integrations.email.address
        sync = uow.mailbox_sync.get_state("gmail", mailbox) if mailbox else None
        operations = {
            "unresolved_dispatch_attempts": len(uow.dispatch_attempts.list_unresolved()),
            "open_escalations": sum(len(uow.escalations.list_by_status(s))
                                    for s in (EscalationStatus.OPEN, EscalationStatus.ACKNOWLEDGED)),
            "ai_enrichment_retry_wait": jobs.get("RETRY_WAIT", 0),
            "ai_enrichment_failed_final": jobs.get("FAILED_FINAL", 0),
            "operator_notifications_unknown": notifications.get("UNKNOWN", 0),
            "operator_notifications_failed": notifications.get("FAILED", 0),
            "mailbox_recovery_required": int(sync is not None and sync.status.value == "RECOVERY_REQUIRED"),
            "embedding_claims": uow.knowledge_embeddings.count_claims(),
        }
    return index, operations


def _operational_warnings(operations: Mapping[str, int]) -> list[str]:
    names = {"unresolved_dispatch_attempts": "UNRESOLVED_DISPATCH_ATTEMPTS", "ai_enrichment_failed_final": "AI_ENRICHMENT_FAILED_FINAL",
             "operator_notifications_unknown": "OPERATOR_NOTIFICATIONS_UNKNOWN", "mailbox_recovery_required": "MAILBOX_RECOVERY_REQUIRED"}
    return [f"{code}:{operations[key]}" for key, code in names.items() if operations.get(key)]


def _token_permissions(config: RuntimeConfig) -> list[str]:
    """POSIX only: a Gmail token readable by group/others (credential files are already
    checked by the integration status)."""
    token = config.integrations.email.token_file
    if os.name != "posix" or token is None or not token.is_file():
        return []
    return ["GMAIL_TOKEN_FILE_PERMISSIONS_BROAD"] if stat.S_IMODE(token.stat().st_mode) & 0o077 else []
