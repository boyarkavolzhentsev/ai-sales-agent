"""Schema DDL, one tuple of statements per migration.

Storage pattern: each aggregate row stores the canonical JSON of its core model in
``data`` (the source for reconstruction). Identity, foreign-key, uniqueness and query
fields are projected into real columns from the same model on every write, so SQL
constraints and indexes apply to them. Projected timestamps are fixed-width UTC text.

Mutable aggregates have an INTEGER ``version`` column for optimistic concurrency,
constrained to equal the version inside ``data``.

Append-only tables (audit, provenance, do-not-contact) are guarded by triggers that
abort any UPDATE or DELETE.
"""

_JSON_DATA = "data TEXT NOT NULL CHECK (json_valid(data))"
# Mutable aggregates carry an optimistic-concurrency version. The projected column must
# always equal the version inside the stored model JSON.
_VERSIONED_DATA = (
    "version INTEGER NOT NULL CHECK (version >= 1),\n"
    f"        {_JSON_DATA},\n"
    "        CHECK (version = json_extract(data, '$.version'))"
)


def _append_only(table: str) -> tuple[str, str]:
    # ``table`` is always a literal from this module, never external input.
    return (
        f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END",
        f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END",
    )


V1_INITIAL_SCHEMA: tuple[str, ...] = (
    f"""CREATE TABLE companies (
        company_id TEXT PRIMARY KEY,
        domain TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        {_VERSIONED_DATA}
    ) STRICT""",
    f"""CREATE TABLE contacts (
        contact_id TEXT PRIMARY KEY,
        company_id TEXT NOT NULL REFERENCES companies (company_id),
        email TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        {_VERSIONED_DATA}
    ) STRICT""",
    "CREATE INDEX contacts_company_idx ON contacts (company_id)",
    f"""CREATE TABLE campaigns (
        campaign_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        config_version INTEGER NOT NULL CHECK (config_version >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        {_VERSIONED_DATA}
    ) STRICT""",
    "CREATE INDEX campaigns_status_idx ON campaigns (status)",
    f"""CREATE TABLE leads (
        lead_id TEXT PRIMARY KEY,
        contact_id TEXT NOT NULL REFERENCES contacts (contact_id),
        company_id TEXT NOT NULL REFERENCES companies (company_id),
        campaign_id TEXT REFERENCES campaigns (campaign_id),
        stage TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        {_VERSIONED_DATA}
    ) STRICT""",
    "CREATE INDEX leads_contact_idx ON leads (contact_id)",
    "CREATE INDEX leads_campaign_idx ON leads (campaign_id)",
    f"""CREATE TABLE email_threads (
        thread_id TEXT PRIMARY KEY,
        mailbox TEXT NOT NULL,
        lead_id TEXT REFERENCES leads (lead_id),
        {_VERSIONED_DATA}
    ) STRICT""",
    "CREATE INDEX email_threads_lead_idx ON email_threads (lead_id)",
    f"""CREATE TABLE email_messages (
        message_id TEXT PRIMARY KEY,
        rfc_message_id TEXT NOT NULL UNIQUE,
        thread_id TEXT NOT NULL REFERENCES email_threads (thread_id),
        direction TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        {_JSON_DATA}
    ) STRICT""",
    "CREATE INDEX email_messages_thread_idx ON email_messages (thread_id, occurred_at)",
    f"""CREATE TABLE outbound_messages (
        outbound_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        lead_id TEXT NOT NULL REFERENCES leads (lead_id),
        contact_id TEXT NOT NULL REFERENCES contacts (contact_id),
        campaign_id TEXT REFERENCES campaigns (campaign_id),
        thread_id TEXT REFERENCES email_threads (thread_id),
        created_at TEXT NOT NULL,
        sent_at TEXT,
        {_VERSIONED_DATA}
    ) STRICT""",
    "CREATE INDEX outbound_messages_lead_idx ON outbound_messages (lead_id)",
    # Status and sent_at back the later send-ledger counts for limits and statistics.
    "CREATE INDEX outbound_messages_status_idx ON outbound_messages (status)",
    "CREATE INDEX outbound_messages_sent_at_idx ON outbound_messages (sent_at)",
    f"""CREATE TABLE follow_up_plans (
        plan_id TEXT PRIMARY KEY,
        lead_id TEXT NOT NULL REFERENCES leads (lead_id),
        campaign_id TEXT NOT NULL REFERENCES campaigns (campaign_id),
        anchor_outbound_id TEXT NOT NULL REFERENCES outbound_messages (outbound_id),
        status TEXT NOT NULL,
        next_due_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        {_VERSIONED_DATA}
    ) STRICT""",
    # Stage 0 invariant: at most one open (ACTIVE or PAUSED) plan per lead.
    """CREATE UNIQUE INDEX follow_up_plans_one_open_per_lead
        ON follow_up_plans (lead_id) WHERE status IN ('ACTIVE', 'PAUSED')""",
    "CREATE INDEX follow_up_plans_due_idx ON follow_up_plans (status, next_due_at)",
    f"""CREATE TABLE do_not_contact (
        entry_id TEXT PRIMARY KEY,
        scope TEXT NOT NULL,
        value TEXT NOT NULL CHECK (value <> ''),
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT,
        {_JSON_DATA}
    ) STRICT""",
    "CREATE INDEX do_not_contact_value_idx ON do_not_contact (scope, value)",
    *_append_only("do_not_contact"),
    f"""CREATE TABLE escalations (
        escalation_id TEXT PRIMARY KEY,
        lead_id TEXT NOT NULL REFERENCES leads (lead_id),
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        {_VERSIONED_DATA}
    ) STRICT""",
    "CREATE INDEX escalations_lead_idx ON escalations (lead_id)",
    "CREATE INDEX escalations_status_idx ON escalations (status)",
    f"""CREATE TABLE operator_commands (
        command_id TEXT PRIMARY KEY,
        telegram_update_id INTEGER NOT NULL UNIQUE,
        operator_user_id INTEGER NOT NULL,
        received_at TEXT NOT NULL,
        {_JSON_DATA}
    ) STRICT""",
    f"""CREATE TABLE operator_responses (
        response_id INTEGER PRIMARY KEY,
        command_id TEXT NOT NULL REFERENCES operator_commands (command_id),
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        {_JSON_DATA}
    ) STRICT""",
    "CREATE INDEX operator_responses_command_idx ON operator_responses (command_id)",
    f"""CREATE TABLE audit_events (
        event_id TEXT PRIMARY KEY,
        occurred_at TEXT NOT NULL,
        event_type TEXT NOT NULL,
        correlation_id TEXT NOT NULL,
        actor_type TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        {_JSON_DATA}
    ) STRICT""",
    "CREATE INDEX audit_events_correlation_idx ON audit_events (correlation_id)",
    """CREATE TABLE audit_event_subjects (
        event_id TEXT NOT NULL REFERENCES audit_events (event_id),
        subject_kind TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        PRIMARY KEY (event_id, subject_kind, subject_id)
    ) STRICT""",
    "CREATE INDEX audit_event_subjects_subject_idx ON audit_event_subjects (subject_kind, subject_id)",
    *_append_only("audit_events"),
    *_append_only("audit_event_subjects"),
    f"""CREATE TABLE provenance_records (
        record_id INTEGER PRIMARY KEY,
        artifact_kind TEXT NOT NULL,
        artifact_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        {_JSON_DATA}
    ) STRICT""",
    "CREATE INDEX provenance_records_artifact_idx ON provenance_records (artifact_kind, artifact_id)",
    *_append_only("provenance_records"),
    f"""CREATE TABLE knowledge_sources_meta (
        source_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        domain TEXT NOT NULL,
        approval_status TEXT NOT NULL,
        external_use TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        {_JSON_DATA},
        PRIMARY KEY (source_id, version)
    ) STRICT""",
    "CREATE INDEX knowledge_sources_meta_domain_idx ON knowledge_sources_meta (domain)",
    """CREATE TABLE idempotency_keys (
        key TEXT PRIMARY KEY CHECK (key <> ''),
        operation TEXT NOT NULL,
        created_at TEXT NOT NULL
    ) STRICT""",
)

# v2: quota reservations and the ledger timestamp used for quota counting.
# ``outbound_messages.sending_at`` is added and backfilled by the v2 migration function,
# because existing rows need their timestamp projected from the stored model JSON.
V2_ADD_OUTBOUND_SENDING_AT = "ALTER TABLE outbound_messages ADD COLUMN sending_at TEXT"

V2_QUOTA_SCHEMA: tuple[str, ...] = (
    # Daily quota counts filter the ledger by dispatch time; follow-up caps by contact.
    "CREATE INDEX outbound_messages_sending_at_idx ON outbound_messages (sending_at)",
    "CREATE INDEX outbound_messages_contact_idx ON outbound_messages (contact_id)",
    f"""CREATE TABLE quota_reservations (
        reservation_id TEXT PRIMARY KEY,
        outbound_id TEXT NOT NULL REFERENCES outbound_messages (outbound_id),
        kind TEXT NOT NULL,
        policy_date TEXT NOT NULL,
        mailbox TEXT NOT NULL,
        campaign_id TEXT REFERENCES campaigns (campaign_id),
        contact_id TEXT NOT NULL REFERENCES contacts (contact_id),
        state TEXT NOT NULL CHECK (state IN ('ACTIVE', 'CONSUMED', 'RELEASED')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        {_VERSIONED_DATA}
    ) STRICT""",
    # One live (ACTIVE or CONSUMED) reservation per outbound message; released slots
    # may be reserved again.
    """CREATE UNIQUE INDEX quota_reservations_one_live_per_outbound
        ON quota_reservations (outbound_id) WHERE state IN ('ACTIVE', 'CONSUMED')""",
    "CREATE INDEX quota_reservations_date_idx ON quota_reservations (policy_date, state)",
    "CREATE INDEX quota_reservations_contact_idx ON quota_reservations (contact_id, state)",
)

# v3: knowledge index. Knowledge tables never reference operational tables. Chunks and
# facts are derived from approved source files and can be rebuilt into a fresh database;
# the FTS table is a derived search index over knowledge_chunks and may be rebuilt at any
# time. Ingested knowledge versions are immutable: triggers reject UPDATE and DELETE on
# source metadata, chunks and facts.
V3_KNOWLEDGE_INDEX_SCHEMA: tuple[str, ...] = (
    f"""CREATE TABLE knowledge_chunks (
        chunk_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        source_version INTEGER NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        content_hash TEXT NOT NULL,
        {_JSON_DATA},
        UNIQUE (source_id, source_version, ordinal),
        FOREIGN KEY (source_id, source_version)
            REFERENCES knowledge_sources_meta (source_id, version)
    ) STRICT""",
    """CREATE TABLE knowledge_facts (
        source_id TEXT NOT NULL,
        source_version INTEGER NOT NULL,
        fact_key TEXT NOT NULL,
        value TEXT NOT NULL,
        unit TEXT,
        chunk_id TEXT NOT NULL REFERENCES knowledge_chunks (chunk_id),
        PRIMARY KEY (source_id, source_version, fact_key),
        FOREIGN KEY (source_id, source_version)
            REFERENCES knowledge_sources_meta (source_id, version)
    ) STRICT""",
    "CREATE INDEX knowledge_facts_key_idx ON knowledge_facts (fact_key)",
    """CREATE VIRTUAL TABLE knowledge_chunks_fts USING fts5(
        chunk_id UNINDEXED,
        text,
        tokenize = 'unicode61 remove_diacritics 2'
    )""",
    *_append_only("knowledge_sources_meta"),
    *_append_only("knowledge_chunks"),
    *_append_only("knowledge_facts"),
)
