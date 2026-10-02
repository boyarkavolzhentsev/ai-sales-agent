"""Stage 20 builders: a production-mode environment over fakes beneath every provider (Gmail,
Telegram, LLM, embeddings), the CLI driven exactly as an operator/scheduler would run it, and
restartable runtimes over one database. No network: tests/conftest.py blocks sockets."""

import hashlib
import io
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.runtime import cli
from tests.gmail.fakes import FakeGmailApi
from tests.integrations.builders import full_env
from tests.llm_providers.fakes import Brain
from tests.rag.builders import emb_values, grounded_draft, knowledge_dir
from tests.rag.fakes import Vendor
from tests.telegram.builders import fake_connectors
from tests.telegram.fakes import FakeTelegramApi


@dataclass
class Fakes:
    gmail: FakeGmailApi = field(default_factory=FakeGmailApi)
    telegram: FakeTelegramApi = field(default_factory=FakeTelegramApi)
    brain: Brain = field(default_factory=Brain)
    vendor: Vendor = field(default_factory=Vendor)

    def connectors(self):  # noqa: ANN201
        return fake_connectors(self.telegram, self.gmail, self.brain.session, self.vendor.session)

    def network_calls(self) -> int:
        return (len(self.gmail.calls) + len(self.telegram.calls) + len(self.brain.session.posts)
                + len(self.vendor.session.posts))


def production_env(tmp_path: Path, **overrides: str | None) -> dict[str, str]:
    """Every production-required provider selected (Gmail with a refresh secret, Telegram, an
    LLM, LOCAL knowledge with a knowledge directory, embeddings), kill switch off."""
    kb = tmp_path / "kb" if (tmp_path / "kb").exists() else knowledge_dir(tmp_path)
    return full_env(tmp_path, MODE="production", KNOWLEDGE_DIR=str(kb), **(emb_values("openai") | overrides))


def run(command: list[str], environ: dict[str, str], fakes: Fakes, monkeypatch: pytest.MonkeyPatch) -> tuple[int, dict]:
    monkeypatch.setattr(cli, "CONNECTORS", fakes.connectors())
    out = io.StringIO()
    code = cli.main(command, environ, out)
    return code, json.loads(out.getvalue().splitlines()[-1])


def fingerprint(path: Path) -> tuple[str, int]:
    """Content hash and modification time: proves a command did not write the file."""
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def scripted(brain: Brain, price: str = "79") -> Brain:
    brain.script("ReplyDraftProposal", *(grounded_draft(price),) * 8)
    return brain
