# Integration configuration

Stage 15 adds the configuration foundation that real providers will plug into. No real
provider is implemented yet: selecting one validates its configuration and reports
`NOT_IMPLEMENTED`; the matching capability stays unavailable and nothing contacts the
provider.

## Provider categories

| Category | Variable | Provider IDs | Implemented |
|---|---|---|---|
| Email | `SALES_AGENT_EMAIL_PROVIDER` | `NONE`, `GMAIL` | none |
| LLM | `SALES_AGENT_LLM_PROVIDER` | `NONE`, `OPENAI`, `ANTHROPIC`, `GEMINI` | none |
| Operator channel | `SALES_AGENT_OPERATOR_PROVIDER` | `NONE`, `TELEGRAM` | none |
| Knowledge | `SALES_AGENT_KNOWLEDGE_PROVIDER` | `LOCAL` | `LOCAL` (the local approved-knowledge index) |
| Embeddings | `SALES_AGENT_EMBEDDINGS_PROVIDER` | `NONE` | not applicable |

Provider-specific variables (`NONE` needs none; a setting or secret for a provider that
is not selected is rejected with `PROVIDER_NOT_SELECTED`):

- **GMAIL**: `GMAIL_ADDRESS` (one of `SALES_AGENT_MAILBOXES`), `GMAIL_TOKEN_FILE` (its
  directory must exist; the file is written by a future OAuth flow), and the OAuth client
  from exactly one source: `GMAIL_CREDENTIALS_FILE` *or* the secrets `GMAIL_CLIENT_ID` +
  `GMAIL_CLIENT_SECRET`. Optional: `GMAIL_REFRESH_TOKEN` (secret),
  `GMAIL_POLL_INTERVAL_SECONDS` (15-3600, default 60).
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

Programmatic composition (e.g. tests) may inject adapters directly; the configured
providers are used otherwise. The provider factory never substitutes a fake.

## Secrets

- Secrets are held as `SecretStr` in `ProviderSecrets`, separate from ordinary settings.
  Their repr, str, JSON and error output are masked. Errors, health, startup reports and
  `provider-status` show variable names and codes only, never values.
- Secrets are never stored in the database and never audited.
- **Local secrets location:** `.local/` at the repository root (for example
  `.local/credentials/` for OAuth client and token files). The whole directory is
  gitignored. A credential or token file inside the code tree anywhere else is rejected
  (`SECRET_SOURCE_INVALID`) so it cannot be committed by accident. Files elsewhere on the
  machine are fine.
- Credential files are checked by metadata only (exists, is a file, readable; on POSIX a
  group/world-readable file produces a warning). Their contents are never read before the
  provider implementation exists.
- Never commit a real credential. Tests use obvious fake values only.

## States

`provider-status` (`python -m app.runtime provider-status`) prints, without a database and
without contacting anything:

- `DISABLED`: provider `NONE`.
- `INVALID`: missing, malformed or contradictory settings/secrets (codes:
  `UNKNOWN_PROVIDER`, `PROVIDER_NOT_SELECTED`, `MISSING_SETTING`, `MISSING_SECRET`,
  `INVALID_PROVIDER_CONFIG`, `SECRET_SOURCE_INVALID`, `CREDENTIAL_FILE_MISSING`,
  `CREDENTIAL_FILE_UNREADABLE`).
- `NOT_IMPLEMENTED`: valid configuration, no adapter yet; the capability is unavailable.
- `CONFIGURED`: valid and implemented.

This is configuration health, not connectivity. A configuration fingerprint (`cfg-...`)
identifies the non-secret provider configuration; rotating a secret does not change it.

## Modes

- `local`, `test`: an `INVALID` provider stops loading/startup; `NOT_IMPLEMENTED` only
  leaves the capability unavailable.
- `production`: startup is refused (`PRODUCTION_NOT_READY`) unless email, LLM, operator
  channel and knowledge are all `CONFIGURED`. Until the provider stages land, production
  therefore fails closed.

## Deployments

Each business/operator runs the same code with its own environment and its own local
secrets: provider selection, accounts, keys and knowledge directory are configuration
only. Nothing provider-related is global or shared between runtime instances.
