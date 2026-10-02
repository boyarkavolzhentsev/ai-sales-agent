# Integration configuration

Stage 15 added the configuration foundation real providers plug into; Stage 16 added the
first real provider, **Gmail**; Stage 17 added **Telegram** as the operator channel; Stage 18
added the live **LLM** providers (OpenAI, Anthropic, Gemini); Stage 19 added **embeddings**
(OpenAI, Gemini) for semantic retrieval over the approved local knowledge. Selecting a provider that is not implemented yet validates
its configuration and reports `NOT_IMPLEMENTED`; its capability stays unavailable and
nothing contacts it.

## Provider categories

| Category | Variable | Provider IDs | Implemented |
|---|---|---|---|
| Email | `SALES_AGENT_EMAIL_PROVIDER` | `NONE`, `GMAIL` | `GMAIL` (Stage 16) |
| LLM | `SALES_AGENT_LLM_PROVIDER` | `NONE`, `OPENAI`, `ANTHROPIC`, `GEMINI` | all three (Stage 18) |
| Operator channel | `SALES_AGENT_OPERATOR_PROVIDER` | `NONE`, `TELEGRAM` | `TELEGRAM` (Stage 17) |
| Knowledge | `SALES_AGENT_KNOWLEDGE_PROVIDER` | `LOCAL` | `LOCAL` (the local approved-knowledge index) |
| Embeddings | `SALES_AGENT_EMBEDDINGS_PROVIDER` | `NONE`, `OPENAI`, `GEMINI` | both (Stage 19; required in production) |

Provider-specific variables (`NONE` needs none; a setting or secret for a provider that
is not selected is rejected with `PROVIDER_NOT_SELECTED`):

- **GMAIL**: `GMAIL_ADDRESS` (one of `SALES_AGENT_MAILBOXES`), `GMAIL_TOKEN_FILE` (its
  directory must exist; `gmail-auth` writes the file), and the OAuth client from exactly
  one source: `GMAIL_CREDENTIALS_FILE` *or* the secrets `GMAIL_CLIENT_ID` +
  `GMAIL_CLIENT_SECRET`. Optional: `GMAIL_REFRESH_TOKEN` (secret; bootstraps a token file
  on headless machines), `GMAIL_POLL_INTERVAL_SECONDS` (15-3600, default 60),
  `GMAIL_TIMEOUT_SECONDS` (bound of every Gmail API call, 1-120, default 30).
- **OPENAI / ANTHROPIC / GEMINI**: `LLM_MODEL` (the provider's model id, e.g.
  `gpt-4.1-mini`, `claude-sonnet-4-5`, `gemini-2.5-flash`; letters, digits and `._:@-` only),
  the secret `LLM_API_KEY`; optional `LLM_TIMEOUT_SECONDS` (1-300, default 30) and
  `LLM_MAX_OUTPUT_TOKENS` (256-32768, default 4096).
- **TELEGRAM**: the secret `TELEGRAM_BOT_TOKEN` (shape `<digits>:<token>`),
  `TELEGRAM_OPERATOR_CHAT_IDS` (comma-separated `<telegram user id>=<operator id>` pairs:
  private chats only, where the chat id equals the user id; each operator id must be one
  of `SALES_AGENT_OPERATOR_IDS`; one chat per operator), optional
  `TELEGRAM_TIMEOUT_SECONDS` (bound of every Bot API call, 1-60, default 20).
- **LOCAL** knowledge: optional `KNOWLEDGE_DIR` (must be a directory; `knowledge-index`
  ingests it).
- **OPENAI / GEMINI** embeddings: `EMBEDDINGS_MODEL` (the provider's embedding model id, e.g.
  `text-embedding-3-small`, `gemini-embedding-001`; same character rules as `LLM_MODEL`), the
  secret `EMBEDDINGS_API_KEY` (its own key: the LLM key is never reused); optional
  `EMBEDDINGS_DIMENSIONS` (1-8192; a reduced output size for models that support it; default:
  the model's native size), `EMBEDDINGS_TIMEOUT_SECONDS` (1-120, default 30) and
  `EMBEDDINGS_MIN_SIMILARITY` (a decimal between 0 and 1, default `0.30`).

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
- `CONFIGURED`: valid, implemented and (locally) authorized. Telegram shows
  `authorization: TOKEN_PRESENT`: the token is structurally valid but not verified here;
  the runtime verifies it with one `getMe` at startup, and only then does the runtime's
  capability report list `operator_channel`.

This is configuration health, not connectivity: the token is not refreshed and neither
Google nor Telegram is contacted. A configuration fingerprint (`cfg-...`) identifies the non-secret provider
configuration; rotating a secret does not change it.

## Modes

- `local`, `test`: an `INVALID` provider stops loading/startup; a Gmail selection that is
  `AUTH_REQUIRED` stops startup with `EMAIL_AUTH_REQUIRED` (before the database is
  touched); a Telegram token that `getMe` refuses stops startup with
  `OPERATOR_CHANNEL_PROVIDER_UNAVAILABLE`; `NOT_IMPLEMENTED` only leaves the capability
  unavailable.
- `production`: startup is refused (`PRODUCTION_NOT_READY: <category>:<state>, ...`, before
  the database is touched) unless email, LLM, operator channel, knowledge and (since Stage 19)
  embeddings are all `CONFIGURED`. A deployment with Gmail, Telegram, an LLM, LOCAL knowledge
  and an embeddings provider all configured starts in production (Gmail and Telegram are still
  verified at startup and fail it if unusable); with `EMBEDDINGS_PROVIDER=NONE` it is refused
  with `EMBEDDINGS:DISABLED`. This removes no safety gate: every outbound message
  still needs an operator's Stage 7 approval (auto-reply stays disabled), operators
  authenticate only through Telegram, and the kill switch, quotas and send window apply.

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

## Telegram operator channel (Stage 17)

**What it is.** A human operator interface only. Telegram is never the source of truth:
leads, qualification, opportunities, proposals, dispatch, do-not-contact, campaigns,
conversations and WON/LOST all stay in the database and change only through the existing
Stage 7 operator commands, re-validated by the services that own them. Telegram never
sends email: an approved draft is sent only by the normal dispatch step (Stage 14/8).

**Bot API.** Direct HTTPS (`requests`), no bot framework, no webhook, no daemon. Only
`getMe`, `getUpdates`, `sendMessage`, `editMessageText` and `answerCallbackQuery`; one
request per call, bounded by `TELEGRAM_TIMEOUT_SECONDS`, never retried behind the caller.
Messages are plain text (no parse mode); customer text is shown as an excerpt marked
"untrusted", with control/bidi characters removed and long text truncated.

**Who may act.** `TELEGRAM_OPERATOR_CHAT_IDS` maps numeric Telegram user ids to Stage 7
operator ids (`<user id>=<operator id>`). Only private chats where the chat id is that user's
id count; every update is then checked against `SALES_AGENT_OPERATOR_IDS` by Stage 7
itself. Usernames and display names are never used. Anyone else gets "Not authorized."
and no data; groups and channels get nothing.

**`python -m app.runtime operator-sync`** (one bounded pass, no loop):
1. reads at most the worker batch limit of updates after the stored cursor (short poll);
2. handles each in order: text commands `/start`, `/help`, `/status`, `/queue`; button
   presses become Stage 7 commands; everything else is ignored;
3. moves the cursor past each update after it was handled. An update that fails is
   recorded (update id, kind, error code: never its content) and skipped;
4. sends new review cards from the Stage 14 operator queue (and recovery items that need a
   human): one card per operator chat and plan version, never repeated; a rate limit ends
   the pass and the next pass continues.
Exit code 4 when Telegram could not be read. Run it from a scheduler as often as you like;
concurrent passes are safe.

**Card delivery** is claimed durably, never by a process-local lock. A pass first claims a
card (one row per chat, lead, action and plan version, created or taken over by a
compare-and-set in an IMMEDIATE transaction), then commits `SUBMITTING` before calling
`sendMessage`, then records `SENT`, `FAILED` or `UNKNOWN`; only the current claim holder
can move the row. So two passes never both send one card. `FAILED` means Telegram refused
it or was never reached, and a later pass retries it; a claim abandoned before submission
is taken over when its 2-minute lease expires. `UNKNOWN` (read timeout, dropped connection,
5xx, unreadable success) and an abandoned `SUBMITTING` may already have produced the
card, so they are never resent automatically: no duplicate card, and `/queue` still shows
the item. A new plan version is a new card.

**Buttons.** Only actions the current plan allows are shown. Callback data holds an action
code, an opaque id and the version seen (at most 64 bytes, no text or prices). Every press
re-reads the current state: if it changed, nothing runs ("changed since the card was
sent"). Each press has a deterministic Stage 7 command id, so a replayed update or a double
tap never executes twice; a failed Telegram acknowledgement never retries or undoes the
committed command.

**WON, LOST, do-not-contact** are never one click: the first press creates a confirmation
bound to the operator, the chat, the lead and the lead version, valid for 5 minutes; only
that operator confirming it in that chat executes the command. Confirming a customer's
acceptance is not WON; WON is offered only once Stage 14 plans `MARK_WON`.

**Persistence.** Schema v11 (`operator_channel_sync`): the update cursor (keyed by the
bot's numeric id), failed-update records, card delivery records (status, claim, Telegram
message id) and pending confirmations.
No token, raw update or customer text.

### Setup and a manual live smoke test

Nothing below runs in the test suite; it contacts Telegram only when you run it.

1. Create a bot with @BotFather and keep its token **only** in your environment or in a
   gitignored file under `.local/` (never in the repository).
2. Find your numeric Telegram user id (for example from @userinfobot) and start a private
   chat with your bot (press *Start*).
3. Set `SALES_AGENT_OPERATOR_PROVIDER=TELEGRAM`, `SALES_AGENT_TELEGRAM_BOT_TOKEN=<token>`,
   `SALES_AGENT_TELEGRAM_OPERATOR_CHAT_IDS=<your user id>=<an operator id>` (that operator id
   must be listed in `SALES_AGENT_OPERATOR_IDS`).
4. `python -m app.runtime provider-status` - OPERATOR_CHANNEL should be `CONFIGURED` (no
   network call).
5. `python -m app.runtime init` (runs `getMe` once), then send `/start` to the bot and run
   `python -m app.runtime operator-sync`: the bot answers "You are authorized...".
6. Send `/queue` and run `operator-sync` again. With a pending test draft (e.g. after
   `execution-pass` drafted a campaign touch to a test address you own) a review card
   arrives; press Approve (or Reject) and run `operator-sync`. Approve records the decision
   only - the email is sent by `dispatch-tick`/`execution-pass --dispatch-approved`.
7. Verify the Stage 7 state changed exactly once: `execution-plan --lead-id <lead>` no longer
   shows the review, and pressing the old button again (then `operator-sync`) answers
   "Already handled." or "changed since the card was sent" without a second change.
8. From another Telegram account (or a group) the bot must answer "Not authorized." or
   nothing at all.

## LLM providers (Stage 18)

**What it is.** A text-generation/extraction service behind the existing `LLMTransport`
contract. The model classifies, extracts, drafts and suggests; it never decides. Every
output is a typed proposal validated by deterministic code, and every business change
still goes through the existing services (Stage 6/7/8/12/13/14): the model cannot approve,
send, mark WON/LOST, add do-not-contact, set a term or move a lead.

**Adapters.** OpenAI (Responses API), Anthropic (Messages API) and Gemini
(`generateContent`), called directly over HTTPS with `requests` (no vendor SDK, no
framework): one request per call, no tools, no web search/grounding, no streaming, no
provider-side storage (`store: false` on OpenAI). The configured provider and model are
always used: there is no fallback to another provider or model.

**Requests.** The versioned prompt (in code: `app/llm/prompts.py`, `app/ai/prompts.py`) is
the system prompt, followed by the output JSON schema; data sections (customer text marked
UNTRUSTED_DATA, approved knowledge as TRUSTED_EVIDENCE) form one user message. Inputs are
bounded (customer text 8000 characters for extraction, 60000 characters per request);
outputs by `LLM_MAX_OUTPUT_TOKENS`. Temperature: 0 for classification/extraction/advice,
0.3 for reply drafts, 0.2 for summaries (not sent to OpenAI, whose reasoning models refuse
it).

**Validation.** Strict JSON parsing (only an exact outer ```` ```json ```` fence is removed),
strict schema validation (unknown fields, missing fields, unknown enum values and wrong
types are rejected, never repaired), then the existing contract checks (cited evidence
must be evidence that was supplied; the draft claim check; allowed next steps).
Extraction (qualification facts, commercial requests, objections, acceptance/decline) must
quote the customer's own words: a quote that is not in the message, or a number/amount/
currency the customer did not write, refuses the whole output. Customer requests (e.g.
"Can you do 999 EUR?") are recorded as requests for an operator, never as terms.

**Failures.** Stable codes: `AUTH_INVALID`, `RATE_LIMITED`, `QUOTA_EXCEEDED`,
`MODEL_NOT_FOUND`, `BAD_REQUEST`, `INPUT_TOO_LARGE`, `CONTENT_BLOCKED`,
`TEMPORARY_PROVIDER_ERROR`, `TIMEOUT`, `NETWORK_ERROR`, `INVALID_RESPONSE`,
`OUTPUT_TRUNCATED`, `SCHEMA_VALIDATION_FAILED`, `CONTRACT_VIOLATION`. At most one extra
attempt, only when the first certainly produced nothing (connection never established, or
503/529 "overloaded"); timeouts, 429s and other errors are not retried within a call. Every failure fails
closed with the existing semantics: Stage 6 records the inbound message and escalates it
to an operator (no draft), a failed qualification/commercial extraction changes nothing
and is retried later by its durable job if the cause was transient (below). No fabricated fallback.

**Durable enrichment (qualification and commercial extraction).** Each customer message
with a lead gets one job per configured extraction (`ai_enrichment_jobs`, schema v12,
identified by task + message). Inbound processing creates the jobs and makes one inline
attempt; every attempt first claims the job (a compare-and-set with a 15-minute lease), so
concurrent processes never call the model twice for one job. The claim holder runs the
existing Stage 12/13 hook (which validates and applies through the existing rules and
idempotency) and settles the job:
- `COMPLETED`: applied, already applied, or skipped by the hook; never run again;
- `RETRY_WAIT`: `RATE_LIMITED`, `TEMPORARY_PROVIDER_ERROR`, `TIMEOUT` or `NETWORK_ERROR`;
  claimable again after 5 min, 30 min, 2 h, then 6 h;
- `FAILED_FINAL`: any other code (`AUTH_INVALID`, `MODEL_NOT_FOUND`, `BAD_REQUEST`,
  `CONTENT_BLOCKED`, `SCHEMA_VALIDATION_FAILED`, `CONTRACT_VIOLATION`, ...) or the 5th failed
  attempt; nothing is applied or invented, and Telegram `/status` shows the count.

`python -m app.runtime ai-recovery-tick` (one bounded pass, worker batch limit, no daemon)
runs the due jobs from the stored Stage 6 result: no mailbox redelivery, cursor rewind or
re-sent envelope is needed, and jobs survive restarts. A claim abandoned by a crash is taken
over when its lease expires. Run it from the same scheduler as the other ticks. Jobs hold
ids, codes and times only. Stage 6 itself keeps its semantics: a failed classification or
draft still escalates the message to an operator (no automatic retry of drafting).

**Startup and status.** No request is made at startup or by `provider-status` (no tokens
spent): a valid selection is `CONFIGURED`; the first real request proves the key (a refused
key then fails that request with `AUTH_INVALID`).

**Logging and storage.** Logs carry provider, model, task, outcome code, latency, attempt,
the provider's request id and token counts; never prompts, customer text, outputs or the
key. Nothing new is stored: prompts and raw responses are not persisted; the existing
provenance records keep the prompt id/version, the model reported by the provider and
input/output hashes.

### Manual live smoke test (LLM)

Nothing below runs in the test suite. It makes billable requests; use a mailbox and a
recipient you control, never a real prospect.

1. Choose one provider; set `SALES_AGENT_LLM_PROVIDER`, `SALES_AGENT_LLM_MODEL` and
   `SALES_AGENT_LLM_API_KEY` (environment or a gitignored file under `.local/`).
2. `python -m app.runtime provider-status` - LLM `CONFIGURED` (no request is made).
3. `python -m app.runtime init` (Gmail and Telegram as in their sections).
4. From an address you control, send one email to the configured mailbox (e.g. a
   question your approved knowledge answers), then `python -m app.runtime email-sync`.
5. `python -m app.runtime operator-sync`: the review card shows the drafted reply.
6. Approve it in Telegram, run `operator-sync`, then `python -m app.runtime dispatch-tick`
   (or `execution-pass --dispatch-approved`): Gmail sends it.

## Embeddings and semantic retrieval (Stage 19)

**What it is.** A knowledge-*selection* mechanism over the approved LOCAL knowledge index:
it decides which approved chunks are relevant to a customer's questions and in which order.
It decides nothing else: not sufficiency on its own, not sending, approval, lead stage,
WON/LOST, DNC, campaigns, commercial terms, dispatch, quota or policy. The knowledge
provider stays `LOCAL` (the authority); the embeddings provider only ranks it. Embeddings and
LLM providers are independent (e.g. `LLM_PROVIDER=ANTHROPIC` with `EMBEDDINGS_PROVIDER=OPENAI`).
There is no fallback from one embeddings provider to another, and Anthropic is not offered
(it has no first-party embeddings API).

**What is embedded.** Only chunks of source versions that are usable now (approved,
`EXTERNAL_OK`, effective and not past `review_by`, the newest usable version, not superseded
or withdrawn; for any locale). Drafts, retired, internal-only, stale, not-yet-effective and
superseded content is never indexed. The embedding input is exactly the stored chunk text (it
begins with its heading path or document title); nothing else is appended, and a chunk over
6000 characters is reported as failed, never truncated. The existing chunking is reused.

**Storage (schema v13, `knowledge_embeddings`).** One row per chunk, embedding space and
input: identity = chunk id + provider + exact model + requested dimensions + SHA-256 of the
embedded text. A changed text, model, provider or dimensionality never reuses a vector.
Vectors are validated (finite numbers, non-empty, the expected and one consistent
dimensionality, the right count; never repaired) and stored L2-normalized as little-endian
float32. No key, chunk text or provider response is stored. SQLite only: no external vector
database; the search is a linear scan, fine for small/medium knowledge bases (an ANN index or
vector database is a future scaling option behind the same retrieval contract).

**Indexing.** `python -m app.runtime knowledge-index` runs once and exits: it ingests
`KNOWLEDGE_DIR` if configured (validated as a whole, one transaction, identical versions are
no-ops, nothing is approved), then deletes stale vectors (no longer usable sources, other
spaces, other text) and embeds only chunks without a current vector, in batches of 32 (one
request each). Unchanged chunks cost zero requests. Concurrent runs claim each batch first
(a lease in `knowledge_embedding_claims`), so a chunk is embedded once; a provider failure
stores nothing for that batch, releases its claims and stops the run (the next run retries);
a crash between the provider's answer and the commit only re-embeds after the lease. Output
is counts and codes only (`scanned`, `unchanged`, `embedded`, `removed`, `skipped`,
`failed`, `requests`, `error_code`); exit code 4 when anything failed. Run it after every
knowledge change and periodically (a scheduled source becomes indexable only once effective).
Without an embeddings provider it only ingests.

**Retrieval.** Stage 6 builds the query exactly as before (the classifier's extracted,
normalized and bounded questions; no mailbox history). With an embeddings provider: if the
configured space has no vectors at all, no query is embedded (no billable call); otherwise
the questions are embedded in one request, outside any database transaction. Candidates are
the current vectors of the usable sources (re-selected at query time, so a withdrawn source
is never returned, even before re-indexing). Similarity is cosine (a dot product of unit
vectors, rounded to 6 decimals); candidates below `EMBEDDINGS_MIN_SIMILARITY` are dropped;
the best `top_k` (5) per question are kept, merged, ordered by (score desc, chunk id) and
bounded to 24,000 characters of evidence. Evidence IDs are the same IDs the LLM contracts
and the claim check accept. The deterministic knowledge gate (lexical coverage, required
domains, diagnostics, fact conflicts) still decides sufficiency over that evidence: a high
similarity never makes an answer sufficient by itself, and anything not SUFFICIENT is
escalated to an operator without a draft. Usable chunks without a current vector add the flag
`SEMANTIC_INDEX_INCOMPLETE`; until `knowledge-index` has run after selecting a provider (or
changing its model), questions therefore escalate rather than silently using the lexical
path. Approved knowledge reaches the model only as TRUSTED_EVIDENCE data
in the user message, never as instructions. Retrieval is used only where a contract needs
evidence (Stage 6 sufficiency and reply drafting); qualification/commercial extraction and the
advisor receive no knowledge. Outbound campaign touches keep the lexical path.

**Failures.** Codes: `AUTH_INVALID`, `RATE_LIMITED`, `QUOTA_EXCEEDED`, `MODEL_NOT_FOUND`,
`BAD_REQUEST`, `INPUT_TOO_LARGE`, `TEMPORARY_PROVIDER_ERROR`, `TIMEOUT`, `NETWORK_ERROR`,
`INVALID_RESPONSE`, `DIMENSION_MISMATCH`. Same retry rule as the LLM (one extra attempt only
when nothing was processed: no connection, or 503). A failed query embedding (or an index of
mixed dimensionality) fails closed: the inbound message stays stored and is escalated with
`KNOWLEDGE_RETRIEVAL_FAILURE`; no draft is made and the lexical path is not silently used
instead. Duplicate concurrent retrievals may embed the same question twice (a cost, never a
state change).

**Production.** Semantic retrieval is mandatory in production from Stage 19 onward (`EMBEDDINGS`
is a production-required category; NONE blocks startup with `EMBEDDINGS:DISABLED`). Embeddings
stay optional in `local` and `test` mode, where `EMBEDDINGS_PROVIDER=NONE` keeps the Stage 4
lexical retrieval (no key, model, index or provider code needed). Why: lexical retrieval is a
compatibility/local path and is not strong enough to be the sole grounding for capability
questions. A single shared word can make an unrelated chunk "cover" a question (e.g. "Do you
support SSO?" is covered by a "Support hours" FAQ chunk), and the claim check cannot always
reject a qualitative claim such as "we support SSO"; semantic retrieval with its similarity
threshold returns no evidence there, so the question escalates. After configuring an embeddings
provider or changing its model, run `knowledge-index`: until vectors exist, retrieval fails closed
(`SEMANTIC_INDEX_INCOMPLETE`, escalation, no lexical fallback). Startup still makes no
embeddings request; a valid configuration is `CONFIGURED` in `provider-status` (no request either)
and the capability report shows `semantic_retrieval`.

**Limitations.** Local/test mode with `EMBEDDINGS_PROVIDER=NONE` keeps the lexical weakness
above (production cannot run that path). No translation: queries and knowledge are embedded in their own language
(cross-language matching depends on the model). The similarity threshold is model-dependent:
tune it per deployment. Linear scan in SQLite.

**Logging.** Provider, model, purpose, input count, outcome code, latency, attempt, request id,
token count and dimensions; for retrieval the query id and hit count; for indexing the counts.
Never knowledge text, customer text, vectors or the key.

### Manual live smoke test (RAG)

Billable; use a mailbox and a sender you control, never a real prospect.

1. Set `SALES_AGENT_EMBEDDINGS_PROVIDER` (`OPENAI` or `GEMINI`), `SALES_AGENT_EMBEDDINGS_MODEL`
   and `SALES_AGENT_EMBEDDINGS_API_KEY` (plus the LLM, Gmail and Telegram as above).
2. `python -m app.runtime provider-status`: EMBEDDINGS `CONFIGURED` (no request is made).
3. Put an approved test source in `SALES_AGENT_KNOWLEDGE_DIR` (e.g. a price list stating the
   Basic plan price) and run `python -m app.runtime init`.
4. `python -m app.runtime knowledge-index`: `sources_ingested` and `embeddings.embedded` are
   above 0 and `failed` is 0. Run it again: `embedded` 0 and `requests` 0.
5. From an address you control, ask the question the source answers; run `email-sync`.
6. `operator-sync`: the review card shows a draft whose figures come from that source.
7. Approve in Telegram, `operator-sync`, then `dispatch-tick`: Gmail sends it.
8. Ask something the knowledge does not cover (e.g. SSO): the message is escalated, no draft.

## Deployments

Each business/operator runs the same code with its own environment and its own local
secrets: provider selection, accounts, keys and knowledge directory are configuration
only. Nothing provider-related is global or shared between runtime instances.
