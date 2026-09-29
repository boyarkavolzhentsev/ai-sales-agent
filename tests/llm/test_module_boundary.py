"""app.llm may depend only on app.core, its own modules, the standard library and pydantic.

It must never reach persistence (Database, UnitOfWork, repositories), policy (quota,
reservations, permits), knowledge ingestion/retrieval (which import persistence), or any
network/provider library.
"""

import ast
import subprocess
import sys
from pathlib import Path

LLM_DIR = Path(__file__).resolve().parents[2] / "app" / "llm"
ALLOWED_APP_PREFIXES = ("app.core", "app.llm")
FORBIDDEN_MODULES = (
    "sqlite3", "http", "socket", "ssl", "smtplib", "imaplib", "poplib", "ftplib", "urllib.request",
    "requests", "httpx", "aiohttp", "openai", "anthropic", "telegram", "email.mime",
)


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_llm_package_imports_only_allowed_modules() -> None:
    files = sorted(LLM_DIR.glob("*.py"))
    assert len(files) >= 10
    violations = []
    for path in files:
        for name in imported_modules(path):
            if name.startswith("app") and not name.startswith(ALLOWED_APP_PREFIXES):
                violations.append(f"{path.name}: {name}")
            if any(name == m or name.startswith(f"{m}.") for m in FORBIDDEN_MODULES):
                violations.append(f"{path.name}: {name}")
    assert violations == []


def test_importing_llm_loads_no_persistence_policy_or_network_modules() -> None:
    probe = (
        "import sys, app.llm; "
        "bad = [m for m in sys.modules if m.split('.')[0] in "
        "('sqlite3','http','socket','ssl','smtplib','requests','httpx','openai') "
        "or m.startswith(('app.persistence','app.policy','app.knowledge','urllib.request'))]; "
        "print(sorted(bad))"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"
