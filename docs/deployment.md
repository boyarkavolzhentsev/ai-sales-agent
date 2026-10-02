# Deployment

How to run one AI sales agent deployment in production. Provider details (variables, states,
error codes, failure semantics) are in [integrations.md](integrations.md); this document is the
operational runbook. Every command below is `python -m app.runtime <command>` (in the container
image the entrypoint is already `python -m app.runtime`, so pass only `<command>`).

## 1. Architecture

One deployment = one company = one process image, run as **one-shot commands** started by a
scheduler. There is no daemon, no web server and no background thread.

- SQLite database (one file) on a persistent volume.
- One Gmail mailbox (send + inbound sync), one Telegram bot (operators, private chats only),
  one LLM provider, one embeddings provider, LOCAL approved knowledge, one operator set.
- Every outbound email still needs an operator's approval in Telegram (auto-reply is
  disabled); Stage 8 dispatch re-checks kill switch, quotas, send window and do-not-contact on
  every send.

**Single instance.** Run exactly one deployment (one scheduler) against a database file. The
durable claims and compare-and-set updates make overlapping commands of *one* deployment safe
(e.g. a slow `service-tick` overlapping the next one), but V1 is not designed or tested for
several hosts/containers sharing one SQLite file: do not scale horizontally.

**Python.** 3.13 (the image and CI use `python:3.13-slim` / Python 3.13; the suite also passes on
3.14). Python 3.12 or older is not supported.

## 2. Prerequisites

- A Linux host or container platform (any Docker-compatible host, VPS, Render, Railway, Fly.io,
  ...) with a persistent volume and a scheduler (cron, a platform cron job, systemd timers).
- A Gmail account for the sales mailbox and a Google OAuth client (installed app) - see 5.
- A Telegram bot token and the numeric user ids of the operators - see 6.
- An LLM API key (OpenAI, Anthropic or Gemini) and an embeddings API key (OpenAI or Gemini).
- The company's approved knowledge files (`knowledge_base/README.md` describes the format).

## 3. Configuration

All configuration is `SALES_AGENT_*` environment variables (`.env.example` lists every one,
grouped: core, sending policy, providers, email, LLM, Telegram, knowledge, embeddings, worker).
No `.env` file is read by the application: load it with your platform's secret/env settings.
Unknown `SALES_AGENT_*` variables are rejected; values are never echoed in errors.

Set `SALES_AGENT_MODE=production`. Production refuses to start (`PRODUCTION_NOT_READY`, before
the database is touched) unless email, LLM, operator channel, knowledge **and embeddings** are all
`CONFIGURED`.

The same image serves another company by changing only environment variables, credential
files, the approved knowledge directory and the operator ids: no source change.

### Persistent paths

| What | Variable | Example (container) | Notes |
|---|---|---|---|
| SQLite database | `SALES_AGENT_DATABASE_PATH` | `/data/db/agent.sqlite3` | must survive restarts; directory writable |
| Gmail token | `SALES_AGENT_GMAIL_TOKEN_FILE` | `/data/credentials/gmail-token.json` | written by `gmail-auth` / refreshed by the runtime |
| Gmail OAuth client (optional) | `SALES_AGENT_GMAIL_CREDENTIALS_FILE` | `/data/credentials/gmail-oauth-client.json` | or the secrets `GMAIL_CLIENT_ID` + `GMAIL_CLIENT_SECRET` |
| Approved knowledge | `SALES_AGENT_KNOWLEDGE_DIR` | `/data/knowledge` | ingested by `knowledge-index` |

Credential and token files must not live inside the code tree (only under `.local/` there);
the image keeps code in `/app` and data in `/data`. The image runs as the unprivileged user
`agent` (uid 10001), which owns `/data`. Secrets (API keys, bot token, OAuth secrets) are
environment variables only; nothing secret is in the image (`.dockerignore` allowlists `app/`
and `requirements.txt`).

### Timezone, send window, quotas, kill switch

- `SALES_AGENT_TIMEZONE` (IANA name) is the timezone of the send window and the daily quotas;
  the host timezone is never used. The window is `WINDOW_DAYS` + `WINDOW_START`-`WINDOW_END`.
- Start with conservative quotas, e.g. `MAX_SENDS_PER_DAY=20`, `MAX_NEW_CONTACTS_PER_DAY=10`,
  `MAX_FOLLOW_UPS_PER_DAY=10`, `MAX_FOLLOW_UPS_PER_CONTACT=2`, `MIN_FOLLOW_UP_INTERVAL_HOURS=72`,
  and raise them deliberately.
- **Kill switch:** `SALES_AGENT_KILL_SWITCH=true` (with `KILL_SWITCH_REASON`) makes every send
  refuse at the Stage 8 gate; drafts, approvals and inbound processing continue and nothing is
  lost or corrupted. Keep it `true` until the deployment check and the smoke test pass, and use
  it as the emergency stop (set it and let the next command pick it up; `deployment-check`
  reports `KILL_SWITCH_ON`).

## 4. First deployment

```
python -m app.runtime provider-status      # configuration only: no network, no database
python -m app.runtime gmail-auth           # once, interactive (see 5); not needed with a refresh token
python -m app.runtime init                 # create/migrate the database (schema v13)
python -m app.runtime knowledge-index      # ingest KNOWLEDGE_DIR and embed it (billable)
python -m app.runtime deployment-check     # must report "ready": true (exit 0)
python -m app.runtime llm-check            # optional, billable: one tiny request
python -m app.runtime embeddings-check     # optional, billable: one tiny request
```

Then enable the scheduler (8). With an empty or incomplete semantic index customer questions
are escalated to an operator (`SEMANTIC_INDEX_INCOMPLETE`); there is no lexical fallback in
production, so run `knowledge-index` before inbound operation.

### What each step contacts

| Command | Database | Network | Billable |
|---|---|---|---|
| `provider-status` | none | none | no |
| `health` | read-only | none | no |
| `deployment-check` | read-only | none | no |
| `init` (and every workload command at startup) | creates/migrates | Gmail account profile (1 read, token refresh if needed), Telegram `getMe` | no |
| `knowledge-index` | writes the index | embeddings provider, only for chunks without a current vector | **yes** (0 requests when nothing changed) |
| `llm-check` | none | one LLM request | **yes** |
| `embeddings-check` | none | one embeddings request | **yes** |
| `email-sync`, `service-tick` | writes | Gmail; the LLM (classification, drafts, extraction) and embeddings (one query per answerable message) only when there is new customer mail | **yes, per new message** |
| `ai-recovery-tick` | writes | the LLM, only for due enrichment jobs | **yes, per due job** |

`provider-status`, `health`, `deployment-check` and startup never call the LLM or embeddings.

## 5. Gmail setup

1. In Google Cloud, create an OAuth client of type *Desktop app* and enable the Gmail API.
   Scopes used: `gmail.send` and `gmail.readonly`.
2. Provide the client either as a file (`SALES_AGENT_GMAIL_CREDENTIALS_FILE`) or as the secrets
   `SALES_AGENT_GMAIL_CLIENT_ID` + `SALES_AGENT_GMAIL_CLIENT_SECRET`.
3. Set `SALES_AGENT_GMAIL_ADDRESS` (must be one of `SALES_AGENT_MAILBOXES`) and
   `SALES_AGENT_GMAIL_TOKEN_FILE` (its directory must exist and be persistent).
4. Authorize once: `python -m app.runtime gmail-auth` opens the browser flow and stores the token
   only if the authorized account is `GMAIL_ADDRESS`.
   **Headless hosts:** run `gmail-auth` on a machine with a browser and copy the token file to
   the volume, or set the secret `SALES_AGENT_GMAIL_REFRESH_TOKEN` (bootstraps the token file on
   first start).
5. Every runtime start verifies the account (profile read) and refuses a mismatched mailbox.
   The first `email-sync` only sets the cursor (no mailbox replay).

Never commit the token or client file (`.gitignore` covers `.local/`, `token*.json`,
`credentials*.json`, `client_secret*.json`).

## 6. Telegram setup

1. Create a bot with @BotFather; set `SALES_AGENT_TELEGRAM_BOT_TOKEN`.
2. Each operator opens a **private** chat with the bot. Map their numeric Telegram user id to
   an operator id: `SALES_AGENT_TELEGRAM_OPERATOR_CHAT_IDS=<user id>=<operator id>,...`; every
   operator id must be in `SALES_AGENT_OPERATOR_IDS`. No usernames, no groups: anything else is
   answered "Not authorized." or ignored.
3. `operator-sync` handles operator commands/buttons and sends new review cards; `/status` in
   the chat shows a safe summary.

## 7. LLM and embeddings

- **LLM:** `SALES_AGENT_LLM_PROVIDER` (`OPENAI`, `ANTHROPIC`, `GEMINI`), `SALES_AGENT_LLM_MODEL`,
  the secret `SALES_AGENT_LLM_API_KEY`; optional `SALES_AGENT_LLM_TIMEOUT_SECONDS`,
  `SALES_AGENT_LLM_MAX_OUTPUT_TOKENS`. A model change needs no migration and takes effect on the
  next command; there is no fallback to another model or provider. Verify a change with
  `llm-check`.
- **Embeddings:** `SALES_AGENT_EMBEDDINGS_PROVIDER` (`OPENAI`, `GEMINI`; independent of the LLM),
  `SALES_AGENT_EMBEDDINGS_MODEL`, the secret `SALES_AGENT_EMBEDDINGS_API_KEY` (its own key);
  optional `SALES_AGENT_EMBEDDINGS_DIMENSIONS`, `SALES_AGENT_EMBEDDINGS_TIMEOUT_SECONDS`,
  `SALES_AGENT_EMBEDDINGS_MIN_SIMILARITY` (default 0.30; model-dependent). **Changing the
  embeddings provider, model or dimensions requires `knowledge-index`**; until it has run,
  questions escalate (fail closed).

## 8. Scheduling

Production work is a set of one-shot commands, each bounded by `SALES_AGENT_BATCH_LIMIT`.
Simplest: one scheduler entry every minute

```
python -m app.runtime service-tick --dispatch-approved
```

which runs, once and in this order: `email-sync` (new customer mail through Stage 6),
`ai-recovery-tick` (due AI enrichment retries), `operator-sync` (Telegram commands such as
approvals, then new review cards), then `tick --dispatch-approved` (Stage 8 reconciliation,
campaign and follow-up drafts, dispatch of operator-approved messages). A failing phase is reported and the
others still run. Equivalent separate entries (same commands, e.g. different cadences):

```
* * * * *   python -m app.runtime email-sync
* * * * *   python -m app.runtime operator-sync
* * * * *   python -m app.runtime tick --dispatch-approved
*/5 * * * * python -m app.runtime ai-recovery-tick
```

`execution-pass --dispatch-approved` (Stage 14: at most one action per lead) is an alternative
to `tick --dispatch-approved`; run one of the two, not both. Run `knowledge-index` after every
knowledge change and, for scheduled knowledge (`effective_from` in the future), daily.
Overlapping runs are safe (durable claims and leases), but keep one scheduler per deployment.
Each command start verifies Gmail and Telegram (two cheap, non-billable requests).

## 9. Readiness, liveness and observability

- **Liveness / schema:** `python -m app.runtime health` (read-only; exit 0 only when the schema
  is current).
- **Readiness:** `python -m app.runtime deployment-check` prints JSON with `ready`,
  `config_ready`, `database_ready`, `knowledge_index_ready`, `blockers`, `warnings`,
  `knowledge_index` (`eligible_chunks`, `indexed_chunks`) and `operations` (unresolved dispatch
  attempts, open escalations, AI enrichment retry/final-failure counts, operator notifications in
  UNKNOWN/FAILED, mailbox recovery, embedding claims). Exit codes: `0` ready, `2` invalid
  configuration, `3` a blocker (e.g. `PRODUCTION_NOT_READY:EMBEDDINGS:DISABLED`,
  `DATABASE_MISSING`, `SCHEMA_NOT_CURRENT`, `DATABASE_DIRECTORY_NOT_WRITABLE`,
  `KNOWLEDGE_EMPTY`, `KNOWLEDGE_INDEX_INCOMPLETE`). It contacts nothing and changes nothing.
- **Connectivity:** Gmail and Telegram are verified at every command start (`STARTUP_FAILED`
  with `EMAIL_PROVIDER_UNAVAILABLE` / `OPERATOR_CHANNEL_PROVIDER_UNAVAILABLE` otherwise); LLM and
  embeddings by `llm-check` / `embeddings-check` (billable).
- **Logs:** stderr, one plain line per event: the command (`command name=... exit=...
  duration_ms=...`), provider calls (provider, model, outcome code, latency, attempt, request
  id, token counts), retrieval and indexing counts. Never message bodies, prompts, knowledge
  text, vectors, tokens or keys. Results are JSON on stdout. All exit codes: `0` ok, `1`
  unexpected error, `2` invalid configuration/usage, `3` startup/health/readiness failure, `4`
  a phase reported errors.

## 10. Knowledge updates

1. Edit/add approved files in the knowledge source (versions are immutable: a change is a new
   `version`; approval, external use, effective dates and `review_by` are authored in the files).
2. Sync them to `SALES_AGENT_KNOWLEDGE_DIR`.
3. `python -m app.runtime knowledge-index` - check `sources_ingested`, `embeddings.embedded`,
   `embeddings.failed` (must be 0) and `error_code`.
4. `python -m app.runtime deployment-check` - `knowledge_index_ready: true`.

No restart is needed: every command reads the database afresh. A withdrawn or superseded
source stops being used at once; its vectors are deleted by the next `knowledge-index`. If
`knowledge-index` fails (e.g. a provider error), fix the cause and run it again: valid vectors
are kept and only the missing ones are embedded.

## 11. Updates and rollback

1. Stop the scheduler (or set the kill switch) and let running commands finish.
2. Back up the database (11) and the credentials.
3. Deploy the new image/code.
4. `python -m app.runtime init` (applies migrations; idempotent).
5. `python -m app.runtime provider-status` and `python -m app.runtime deployment-check`.
6. `python -m app.runtime knowledge-index` if knowledge or the embeddings settings changed.
7. Resume the scheduler; watch `deployment-check` and the logs.

**Rollback:** migrations are forward-only. Older code refuses a newer schema
(`SCHEMA_NEWER_THAN_SUPPORTED`). To roll back across a migration, restore the pre-update
database backup together with the old image; never run old code against a migrated file.

## 12. Backup and restore

- Back up while no command is writing (stop the scheduler), or online with SQLite's backup API
  (a consistent copy; never copy the raw file while a command runs), e.g. inside the container:
  `python -c "import sqlite3; sqlite3.connect('/data/db/agent.sqlite3').backup(sqlite3.connect('/data/db/backup.sqlite3'))"`,
  then move the copy off the host. Always back up before an update.
- Back up the Gmail token/client files separately (they are secrets), and the knowledge source
  if it is not version-controlled.
- Restore: stop the scheduler, put the file back at `SALES_AGENT_DATABASE_PATH`, run `init`
  and `deployment-check`. Use the same knowledge and embeddings settings as when the backup was
  taken (or re-run `knowledge-index`). Dispatch attempts that were UNKNOWN in the backup are
  resolved by reconciliation, never by resending.

## 13. Credential rotation

No code change and no migration: replace the value and the next command uses it.
- LLM / embeddings key, Telegram bot token: update the secret.
- Gmail: rotate the OAuth client secret or re-run `gmail-auth` to replace the token file.
The configuration fingerprint (`cfg-...`) does not change when only a secret changes.

## 14. Troubleshooting

| Symptom | Meaning | Action |
|---|---|---|
| `STARTUP_FAILED` `EMAIL_AUTH_REQUIRED` / `EMAIL_PROVIDER_UNAVAILABLE` | Gmail token missing/revoked or another account | re-run `gmail-auth`; check `GMAIL_ADDRESS` |
| `OPERATOR_CHANNEL_PROVIDER_UNAVAILABLE` | Telegram refused the bot token | fix `TELEGRAM_BOT_TOKEN` |
| Escalations with `CLASSIFIER_FAILURE`/`COMPOSER_FAILURE`, `llm-check` `AUTH_INVALID` | LLM key/model refused | fix the key/model; `llm-check` |
| `KNOWLEDGE_RETRIEVAL_FAILURE` escalations, `embeddings-check` `AUTH_INVALID` | embeddings key/model refused | fix it; `embeddings-check`; `knowledge-index` |
| `RATE_LIMITED` (LLM or embeddings) | provider throttling | nothing to do: failed messages are escalated to operators, enrichment jobs retry with backoff, a later `knowledge-index` continues |
| `email-sync` status `RECOVERY_REQUIRED` | Gmail history expired (sync paused too long) | `python -m app.runtime email-sync --recover` (mail in the gap is not ingested: check the mailbox manually) |
| `UNRESOLVED_DISPATCH_ATTEMPTS` / a send in UNKNOWN | Gmail may or may not have accepted it | never resend by hand; `tick` reconciles it against the Sent mailbox and records the result |
| `OPERATOR_NOTIFICATIONS_UNKNOWN` | a Telegram card may not have arrived | it is never resent automatically; operators can list the queue with `/queue` |
| `AI_ENRICHMENT_FAILED_FINAL` | an extraction failed permanently (or 5 times) | nothing was applied; review the lead manually (`/status` shows the count) |
| `KNOWLEDGE_INDEX_INCOMPLETE` / `SEMANTIC_INDEX_INCOMPLETE` | chunks without a current vector | `knowledge-index` |

## 15. Live smoke test (controlled)

Billable and real: use only a mailbox, sender and Telegram account you control, synthetic data,
low quotas, a send window that is open now, and the kill switch off only for the test.

1. Configure the environment (all providers; `MODE=production`).
2. `gmail-auth` (or the refresh token), then `provider-status`: every category `CONFIGURED`.
3. Put one approved synthetic source in `KNOWLEDGE_DIR`, e.g. a price list for "Example
   Company" stating "The Basic plan costs 79 EUR per month." (no SSO knowledge).
4. `init`, `knowledge-index` (`embedded` > 0, `failed` 0), `deployment-check` (`ready: true`),
   `llm-check`, `embeddings-check`.
5. From your test sender, email the sales mailbox: "How much is the Basic plan?".
6. `service-tick` (without `--dispatch-approved`): the message is synced, a draft is created
   and its review card is sent to Telegram.
7. In Telegram: the card shows the draft citing 79 EUR. Approve it.
8. `service-tick --dispatch-approved`: the approval is applied, then Gmail sends the reply.
   Check the Sent folder: exactly one copy; run it again: nothing more is sent.
9. `deployment-check`: `unresolved_dispatch_attempts` is 0; the logs show no message text.
10. From the same sender ask "Do you support SSO?"; `service-tick`: the message is escalated
    (knowledge insufficient), no draft card appears and nothing is sent.
11. Optional: send qualification facts (they reach operator review, nothing qualifies
    automatically) and "We accept the proposal" (Telegram asks for confirmation; WON is never
    automatic).
12. Set the kill switch back on (or keep low quotas) after the test.

Rate limits and UNKNOWN sends are covered by the automated suite; do not provoke them live.

## 16. Release checklist

- [ ] Configuration complete; `provider-status` all `CONFIGURED`; `MODE=production`
- [ ] Secrets only in the platform's secret store; nothing secret in the image or repository
- [ ] Persistent volume for the database, token and knowledge; database backed up
- [ ] `init` done (schema v13)
- [ ] Gmail authorized for the configured address
- [ ] Telegram operator mapping (private chats, ids in `OPERATOR_IDS`)
- [ ] Approved knowledge in place; `knowledge-index` with `failed: 0`
- [ ] `llm-check` and `embeddings-check` OK
- [ ] Kill switch set deliberately; conservative quotas; send window and timezone correct
- [ ] `deployment-check` exit 0
- [ ] Controlled smoke test (15) passed, including the SSO escalation
- [ ] One scheduler entry (8) enabled
