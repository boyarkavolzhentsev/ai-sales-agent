"""Load-bearing architecture checks for the inbound package.

- It never produces AUTO_REPLY (no reference in code; results reject it).
- It never imports sending, permits, quota reservation, providers or Telegram.
- Its app imports stay within core, persistence, knowledge, llm and the suppression evaluator.
"""

import ast
from pathlib import Path

INBOUND_DIR = Path(__file__).resolve().parents[2] / "app" / "inbound"
# app.policy.release only frees reservations of cancelled messages; it never reserves or sends.
ALLOWED_APP_MODULES = (
    "app.core", "app.persistence", "app.knowledge", "app.llm", "app.inbound", "app.policy.suppression", "app.policy.release",
)
FORBIDDEN_MODULES = (
    "smtplib", "imaplib", "poplib", "http", "socket", "ssl", "urllib.request", "requests", "httpx",
    "aiohttp", "openai", "anthropic", "telegram", "email.mime", "app.policy.reservation", "app.policy.quota",
)
FORBIDDEN_NAMES = {"AUTO_REPLY", "SendPermit", "reserve_quota", "send_permit", "SendGate"}


def sources() -> list[Path]:
    files = sorted(INBOUND_DIR.glob("*.py"))
    assert len(files) >= 10
    return files


def test_inbound_imports_only_allowed_modules() -> None:
    violations = []
    for path in sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
            )
            for name in names:
                if name.startswith("app") and not name.startswith(ALLOWED_APP_MODULES):
                    violations.append(f"{path.name}: {name}")
                if any(name == m or name.startswith(f"{m}.") for m in FORBIDDEN_MODULES):
                    violations.append(f"{path.name}: {name}")
    assert violations == []


def test_inbound_never_references_auto_reply_or_sending() -> None:
    found = []
    for path in sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
                found.append(f"{path.name}: .{node.attr}")
            if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
                found.append(f"{path.name}: {node.id}")
            if isinstance(node, ast.alias) and node.name in FORBIDDEN_NAMES:
                found.append(f"{path.name}: import {node.name}")
    assert found == []


def test_only_review_escalate_and_no_action_are_finalized() -> None:
    from app.core.enums import ReplyDecision
    from app.inbound import ALLOWED_DECISIONS

    assert ALLOWED_DECISIONS == {ReplyDecision.DRAFT_FOR_REVIEW, ReplyDecision.ESCALATE, ReplyDecision.NO_ACTION}
