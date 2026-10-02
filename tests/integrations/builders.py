"""Fake provider configuration for Stage 15 tests. Every secret is an obvious fake, and
credential files live in pytest's temporary directory (outside the repository)."""

from pathlib import Path

from tests.inbound.builders import MAILBOX
from tests.runtime.builders import env

CLIENT_ID = "test-secret-do-not-use-client-id"
CLIENT_SECRET = "test-secret-do-not-use-client-secret"
REFRESH_TOKEN = "test-secret-do-not-use-refresh-token"
API_KEY = "test-secret-do-not-use-api-key"
BOT_TOKEN = "123456:test-secret-do-not-use-bot-token"
EMBEDDINGS_API_KEY = "test-secret-do-not-use-embeddings-api-key"
FAKE_SECRETS = (CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, API_KEY, BOT_TOKEN, EMBEDDINGS_API_KEY)
CREDENTIAL_CONTENT = '{"installed": {"client_secret": "test-secret-do-not-use-file-content"}}'


def credentials_file(tmp_path: Path, name: str = "gmail-oauth-client.json") -> Path:
    directory = tmp_path / "credentials"
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text(CREDENTIAL_CONTENT, encoding="utf-8")
    return path


def gmail(tmp_path: Path, *, with_file: bool = False, **overrides: str | None) -> dict[str, str | None]:
    """A structurally complete Gmail selection (client id/secret pair, or a client file)."""
    token_dir = tmp_path / "tokens"
    token_dir.mkdir(exist_ok=True)
    values: dict[str, str | None] = {"EMAIL_PROVIDER": "gmail", "GMAIL_ADDRESS": MAILBOX,
                                     "GMAIL_TOKEN_FILE": str(token_dir / "gmail-token.json")}
    if with_file:
        values["GMAIL_CREDENTIALS_FILE"] = str(credentials_file(tmp_path))
    else:
        values |= {"GMAIL_CLIENT_ID": CLIENT_ID, "GMAIL_CLIENT_SECRET": CLIENT_SECRET, "GMAIL_REFRESH_TOKEN": REFRESH_TOKEN}
    return values | overrides


def llm(provider: str = "openai", **overrides: str | None) -> dict[str, str | None]:
    return {"LLM_PROVIDER": provider, "LLM_MODEL": "model-under-test", "LLM_API_KEY": API_KEY} | overrides


def telegram(**overrides: str | None) -> dict[str, str | None]:
    return {"OPERATOR_PROVIDER": "telegram", "TELEGRAM_BOT_TOKEN": BOT_TOKEN, "TELEGRAM_OPERATOR_CHAT_IDS": "1001=op-alice,2002=op-bob"} | overrides


def embeddings(provider: str = "openai", **overrides: str | None) -> dict[str, str | None]:
    """Stage 19: semantic retrieval, required in production."""
    return {"EMBEDDINGS_PROVIDER": provider, "EMBEDDINGS_MODEL": "embed-model-under-test",
            "EMBEDDINGS_API_KEY": EMBEDDINGS_API_KEY} | overrides


NO_LLM: dict[str, str | None] = {"LLM_PROVIDER": None, "LLM_MODEL": None, "LLM_API_KEY": None}


def full_env(tmp_path: Path, **overrides: str | None) -> dict[str, str]:
    """Every provider selected with fake secrets, plus the core runtime variables."""
    return env(tmp_path / "agent.sqlite3", **(gmail(tmp_path) | llm() | telegram() | overrides))


def without_prefix(environ: dict[str, str]) -> dict[str, str]:
    return {key.removeprefix("SALES_AGENT_"): value for key, value in environ.items()}
