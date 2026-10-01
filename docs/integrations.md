# Integration configuration

Stage 15 added the configuration foundation real providers plug into; Stage 16 added the
first real provider, **Gmail**. Selecting a provider that is not implemented yet validates
its configuration and reports `NOT_IMPLEMENTED`; its capability stays unavailable and
nothing contacts it.

## Provider categories

| Category | Variable | Provider IDs | Implemented |
|---|---|---|---|
| Email | `SALES_AGENT_EMAIL_PROVIDER` | `NONE`, `GMAIL` | `GMAIL` (Stage 16) |
| LLM | `SALES_AGENT_LLM_PROVIDER` | `NONE`, `OPENAI`, `ANTHROPIC`, `GEMINI` | none |
| Operator channel | `SALES_AGENT_OPERATOR_PROVIDER` | `NONE`, `TELEGRAM` | none |
| Knowledge | `SALES_AGENT_KNOWLEDGE_PROVIDER` | `LOCAL` | `LOCAL` (the local approved-knowledge index) |
| Embeddings | `SALES_AGENT_EMBEDDINGS_PROVIDER` | `NONE` | not applicable |

Provider-specific variables (`NONE` needs none; a setting or secret for a provider that
is not selected is rejected with `PROVIDER_NOT_SELECTED`):

- **GMAIL**: `GMAIL_ADDRESS` (one of `SALES_AGENT_MAILBOXES`), `GMAIL_TOKEN_FILE` (its
  directory must exist; `gmail-auth` writes the file), and the OAuth client from exactly
  one source: `GMAIL_CREDENTIALS_FILE` *or* the secrets `GMAIL_CLIENT_ID` +
  `GMAIL_CLIENT_SECRET`. Optional: `GMAIL_REFRESH_TOKEN` (secret; bootstraps a token file
  on headless machines), `GMAIL_POLL_INTERVAL_SECONDS` (15-3600, default 60),
  `GMAIL_TIMEOUT_SECONDS` (bound of every Gmail API call, 1-120, default 30).
- **OPENAI / ANTHROPIC / GEMINI**: `LLM_MODEL`, the secret `LLM_API_KEY`; optional
  `LLM_TIMEOUT_SECONDS` (1-300, default 30).
- **TELEGRAM**: the secret `TELEGRAM_BOT_TOKEN` (shape `<digits>:<token>`),
  `TELEGRAM_OPERATOR_CHAT_IDS` (comma-separated, distinct, non-zero integers).
- **LOCAL** knowledge: optional `KNOWLEDGE_DIR` (must be a directory).

All names carry the `SALES_AGENT_` prefix. Unknown `SALES_AGENT_*` variables are rejected.

## Sources and precedence

The process environment is the only source: documented defaults, overridden by explicit
`SALES_AGENT_*` variables. No `.env` file is read automatically, and there is no config
file or CLI override. To use a `.env` file, load it with your shell or process manager
(`.env` is gitignored; `.env.example` lists every variable with placeholders only).

Programmatic composition (e.g. tests) may inject adapters directly; a selected provider's
adapters (built at startup) replace the injected ones of its category. The provider
factory never substitutes a fake.

## Secrets

- Secrets are held as `SecretStr` in `ProviderSecrets`, separate from ordinary settings.
  Their repr, str, JSON and error output are masked. Errors, health, startup reports,
  sync reports and `provider-status` show variable names and codes only, never values.
- Secrets, OAuth tokens and credential file contents are never stored in the database
  and never audited.
- **Local secrets location:** `.local/` at the repository root (for example
  `.local/credentials/` for the OAuth client and token files). The whole directory is
  gitignored. A credential or token file inside the code tree anywhere else is rejected
  (`SECRET_SOURCE_INVALID`) so it cannot be committed by accident. Files elsewhere on the
  machine are fine.
- Configuration checks look at credential-file metadata only (exists, is a file,
  readable; on POSIX a group/world-readable file produces a warning). The OAuth client
  file is read only by `gmail-auth` and when the runtime builds the Gmail adapters.
- Never commit a real credential. Tests use obvious fake values only and cannot reach the
  network (the test suite blocks sockets).

## States

`provider-status` (`python -m app.runtime provider-status`) prints, without a database and
without contacting anything:

- `DISABLED`: provider `NONE`.
- `INVALID`: missing, malformed or contradictory settings/secrets (codes:
  `UNKNOWN_PROVIDER`, `PROVIDER_NOT_SELECTED`, `MISSING_SETTING`, `MISSING_SECRET`,
  `INVALID_PROVIDER_CONFIG`, `SECRET_SOURCE_INVALID`, `CREDENTIAL_FILE_MISSING`,
  `CREDENTIAL_FILE_UNREADABLE`).
- `NOT_IMPLEMENTED`: valid configuration, no adapter yet; the capability is unavailable.
- `AUTH_REQUIRED`: valid and implemented, but no usable local authorization (Gmail: no
  token file / refresh secret, or a token without a refresh token or the needed scopes);
  `authorization` says `AUTH_REQUIRED` or `AUTH_INVALID`. Run `gmail-auth`.
- `CONFIGURED`: valid, implemented and (locally) authorized.

This is configuration health, not connectivity: the token is not refreshed and Google is
not contacted. A configuration fingerprint (`cfg-...`) identifies the non-secret provider
configuration; rotating a secret does not change it.

## Modes

- `local`, `test`: an `INVALID` provider stops loading/startup; a Gmail selection that is
  `AUTH_REQUIRED` stops startup with `EMAIL_AUTH_REQUIRED` (before the database is
  touched); `NOT_IMPLEMENTED` only leaves the capability unavailable.
- `production`: startup is refused (`PRODUCTION_NOT_READY`) unless email, LLM, operator
  channel and knowledge are all `CONFIGURED`. LLM and Telegram are not implemented yet,
  so production still fails closed.

## Gmail (Stage 16)

**What it does.** Gmail is infrastructure only: it implements Stage 8's `EmailTransport`
and `DispatchReconciler` and a provider-neutral `MailboxReader` for inbound sync. Every
business decision (approval, policy, DNC, quota, threading identity, lead/campaign/
commercial state) stays where it was. Exactly one Gmail account per process.

**OAuth.** Installed-app OAuth for one user mailbox. Scopes (least privilege):
`https://www.googleapis.com/auth/gmail.send` (send) and
`https://www.googleapis.com/auth/gmail.readonly` (inbound sync, reconciliation against our
sent mail, the account profile). No modify/label scope: the agent never archives,
labels, marks read, stars or deletes anything. The token file holds Google's
"authorized user" JSON; refreshed tokens are written atomically (owner-only permissions on
POSIX); a failed refresh keeps the previous file. The interactive flow runs **only** from
`python -m app.runtime gmail-auth`, which stores the token only if the authorized account
is `GMAIL_ADDRESS`.

**Startup.** The runtime loads (and if needed refreshes) the token and reads the account
profile once: a different account fails closed (`EMAIL_PROVIDER_UNAVAILABLE:
MAILBOX_MISMATCH`). No browser is ever opened by `init`, `tick`, `health`,
`provider-status`, `email-sync` or any other command.

**Sending.** One Stage 8 submission is exactly one `users.messages.send` request: no
client-library retries, no automatic refresh-and-retry, bounded timeouts. The message is
plain text UTF-8 from the authorized account (display name from the sender identity),
with Stage 8's per-attempt Message-ID and the In-Reply-To/References of the conversation;
a reply is placed in the Gmail thread of the message it answers when that can be found.
Outcomes: Gmail returned a message id → ACCEPTED; nothing left the process → not
submitted; Gmail answered 4xx → NOT_ACCEPTED (retryable only for rate limits/expired
authorization, by Stage 8's rules); 5xx, timeouts after sending, lost responses → UNKNOWN
(reconcile, never resend).

**Reconciliation.** Searches our sent mail for the attempt's exact Message-ID: a SENT
message with that header is ACCEPTED. Gmail cannot prove a rejection, so no match is
NOT_FOUND (the attempt stays unresolved) and read errors are UNKNOWN. No subject/body
matching.

**Inbound sync** (`python -m app.runtime email-sync`, one bounded pass, no daemon):
- The first pass only records the mailbox's current Gmail history id: existing mail is
  never ingested (no mailbox replay).
- Each pass first retries recorded failures, then reads at most the worker batch limit of
  new messages after the cursor and hands each to the same path as `handle_inbound`
  (Stage 6, then the pipeline and commercial hooks). Drafts, spam, trash, chat, our own
  sent mail and messages deleted meanwhile are filtered and counted.
- The cursor advances, together with any failure records, only after every message of the
  batch was handled, filtered or recorded as a failure. A crash before that replays the
  batch; Stage 6 processes each Gmail message once.
- A message that fails is recorded (Gmail message id and error code only), retried by
  every later pass, reported, and never deleted; it does not block later mail.
- If Gmail no longer has history since the cursor (history expiry), the sync stops with
  `RECOVERY_REQUIRED`; only `email-sync --recover` sets a new cursor (mail in the gap is
  not ingested). There is no silent reset.
- Until an LLM provider exists, inbound processing is unavailable: the first pass still
  sets the cursor, later passes report `SKIPPED` and read nothing past it.
- Bodies: text/plain preferred; HTML-only mail reduced to text (scripts/styles dropped,
  nothing fetched); attachments never used as the customer's words.

**Persistence.** Schema v10 (`email_provider_sync`): `mailbox_sync_states` (cursor,
status, generation) and `mailbox_sync_failures` (message id, attempts, error code). No
token, secret, body or subject.

### Setup and a manual live smoke test

Nothing below runs in the test suite; it touches a real mailbox only when you run it.

1. In Google Cloud, enable the Gmail API and create an OAuth client of type *Desktop app*.
   Save its JSON as `.local/credentials/gmail-oauth-client.json` (gitignored).
2. Set `SALES_AGENT_EMAIL_PROVIDER=GMAIL`, `SALES_AGENT_GMAIL_ADDRESS=<the mailbox>` (also
   listed in `SALES_AGENT_MAILBOXES`), `SALES_AGENT_GMAIL_CREDENTIALS_FILE=.local/credentials/gmail-oauth-client.json`,
   `SALES_AGENT_GMAIL_TOKEN_FILE=.local/credentials/gmail-token.json`.
3. `python -m app.runtime gmail-auth` — a browser opens; sign in to that mailbox; the
   command prints `{"authorized": true, "mailbox": ...}`.
4. `python -m app.runtime provider-status` — EMAIL should be `CONFIGURED`/`AUTHORIZED`.
5. `python -m app.runtime init`, then `python -m app.runtime email-sync` — the first pass
   reports `INITIALIZED` (checkpoint only).
6. Optional outbound check, through the normal workflow only: an operator approves a draft
   for a test recipient you control (Stage 7), then `python -m app.runtime dispatch-tick`
   (or `tick --dispatch-approved`) sends it through Stage 8. There is no command that
   sends arbitrary email, and approval is never bypassed.

## Deployments

Each business/operator runs the same code with its own environment and its own local
secrets: provider selection, accounts, keys and knowledge directory are configuration
only. Nothing provider-related is global or shared between runtime instances.
