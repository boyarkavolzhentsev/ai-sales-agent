# Security

## Security model

AI Sales Agent is designed to fail closed around external providers and model output.

Key controls:

- secrets are loaded from environment variables or local credential files and are not committed;
- API keys and tokens are represented as secret values and redacted from status/error output;
- provider-specific raw responses do not cross domain boundaries;
- outbound email requires explicit operator approval;
- dispatch re-checks kill switch, quotas, send window and DNC state;
- unsupported knowledge claims are escalated instead of invented;
- ambiguous email submission is reconciled instead of blindly retried;
- production requires semantic retrieval over approved knowledge;
- tests block unintended network access.

## Secret handling

Never commit:

- `.env`;
- Gmail OAuth client secrets or token files;
- Telegram bot tokens;
- LLM API keys;
- embeddings API keys;
- local SQLite databases;
- logs containing production data.

The repository's `.gitignore` and `.dockerignore` exclude local credentials and runtime data.

## Deployment guidance

Use platform secret storage for environment values.

Persist the Gmail token file and SQLite database outside the container image. Keep the kill switch enabled during first deployment until `deployment-check` and the controlled smoke test succeed.

See [docs/deployment.md](docs/deployment.md).

## Reporting a vulnerability

If you discover a security issue, do not publish credentials, customer data or exploit details in a public issue.

Contact the repository owner privately through GitHub before public disclosure when the issue may expose secrets, customer information or unsafe outbound behavior.
