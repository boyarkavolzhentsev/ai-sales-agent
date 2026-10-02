"""Repository hygiene (.gitignore, .env.example, docs) and the integration layer's
boundaries: no network or provider SDK, no persistence, no business-logic dependency."""

import ast
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.integrations import INTEGRATION_VARIABLES, SECRET_NAMES
from app.persistence.migrations import MIGRATIONS, latest_version
from app.runtime.env import OPTIONAL, REQUIRED

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
INTEGRATIONS = APP / "integrations"
SENSITIVE = (".env", ".env.*", ".local/", "*.pem", "*.key", "*.p12", "client_secret*.json", "credentials*.json",
             "service-account*.json", "token*.json", "*.sqlite3", "*.db", "*.log", "logs/", "__pycache__/", ".pytest_cache/")
NETWORK = ("socket", "ssl", "http", "urllib", "requests", "httpx", "aiohttp", "smtplib", "imaplib", "poplib", "email",
           "googleapiclient", "google", "google_auth_oauthlib", "telegram", "aiogram", "openai", "anthropic", "asyncio",
           "threading", "subprocess")
# The provider-neutral mailbox sync (Stage 16) also uses persistence and the inbound envelope.
MAY_IMPORT = ("app.core", "app.integrations", "app.dispatch", "app.llm", "app.operator", "app.persistence",
              "app.inbound.models", "app.embeddings")


def imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def matches(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == p or name.startswith(f"{p}.") for p in prefixes)


def test_gitignore_protects_secrets_databases_and_caches() -> None:
    lines = {line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert set(SENSITIVE) <= lines and "!.env.example" in lines


@pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs the git checkout")
def test_ignore_rules_hit_secrets_but_no_tracked_file() -> None:
    tracked_ignored = subprocess.run(["git", "ls-files", "-ci", "--exclude-standard"], cwd=ROOT, capture_output=True,
                                     text=True, check=True).stdout.split()
    assert tracked_ignored == []
    probes = [".env", ".env.production", ".local/credentials/gmail-token.json", "client_secret_123.json",
              "service-account.json", "keys/private.pem", "data/agent.sqlite3", "logs/run.log"]
    ignored = subprocess.run(["git", "check-ignore", "--no-index", *probes], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert sorted(ignored) == sorted(probes)
    kept = subprocess.run(["git", "check-ignore", "--no-index", ".env.example", "docs/integrations.md"], cwd=ROOT,
                          capture_output=True, text=True).stdout.split()
    assert kept == []


def test_env_example_lists_known_variables_and_no_secret_values() -> None:
    known = set(REQUIRED) | set(OPTIONAL)
    names: list[str] = []
    for raw in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.lstrip("# ").strip()
        match = re.fullmatch(r"SALES_AGENT_([A-Z0-9_]+)=(.*)", line)
        if not match:
            continue
        name, value = match.groups()
        names.append(name)
        assert name in known, name
        if name in SECRET_NAMES:
            assert value == "", f"{name} must stay empty in .env.example"
    assert set(REQUIRED) <= set(names) and SECRET_NAMES <= set(names)
    assert not re.search(r"(sk-|AIza|xox[bp]-|\d{6,}:[A-Za-z0-9_-]{30,})", (ROOT / ".env.example").read_text(encoding="utf-8"))


def test_every_integration_variable_is_documented() -> None:
    docs = (ROOT / "docs" / "integrations.md").read_text(encoding="utf-8")
    assert all(name in docs for name in INTEGRATION_VARIABLES)


def test_integration_layer_has_no_network_sdk_persistence_or_business_dependency() -> None:
    problems = [f"{p.name}: {n}" for p in sorted(INTEGRATIONS.glob("*.py")) for n in imports(p)
                if matches(n, NETWORK) or (n.startswith("app") and not matches(n, MAY_IMPORT))]
    assert problems == []
    for path in INTEGRATIONS.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert ".open(" not in source and "read_text" not in source and "read_bytes" not in source, path.name
        assert not any(isinstance(n, ast.While) for n in ast.walk(ast.parse(source))), path.name


def test_only_the_runtime_depends_on_the_integration_layer() -> None:
    offenders = [f"{p.relative_to(APP)}: {n}" for p in APP.rglob("*.py")
                 if not p.is_relative_to(APP / "runtime") and not p.is_relative_to(INTEGRATIONS)
                 for n in imports(p) if matches(n, ("app.integrations",))]
    assert offenders == []


def test_no_schema_change() -> None:
    # Stage 15 changed no schema; v10..v13 are Stages 16/17/18/19 (mailbox sync, operator channel, AI enrichment
    # jobs, knowledge embeddings).
    assert MIGRATIONS[8].name == "commercial_decisioning" and latest_version() == len(MIGRATIONS) == 13


def test_no_real_secret_shapes_in_tracked_text() -> None:
    pattern = re.compile(r"(sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY|ya29\.[0-9A-Za-z_-]{20,}"
                         r"|xox[bp]-[0-9A-Za-z-]{10,}|\b\d{8,10}:AA[0-9A-Za-z_-]{30,})")
    hits = [str(p.relative_to(ROOT)) for p in [*ROOT.glob("*.*"), *(ROOT / "docs").rglob("*"), *APP.rglob("*.py"),
                                               *(ROOT / "tests").rglob("*.py")]
            if p.is_file() and pattern.search(p.read_text(encoding="utf-8", errors="ignore"))]
    assert hits == []
