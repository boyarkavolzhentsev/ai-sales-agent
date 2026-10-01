"""One-shot local commands: ``python -m app.runtime <command>``.

  init              start once: create/migrate the database (the only command that may
                    create one), inspect recovery
  health            read-only: open the existing database read-only and report its schema;
                    never creates, migrates or writes anything
  tick              one runtime tick (reconciliation, campaign, follow-up)
                    [--dispatch-approved: also dispatch operator-approved messages]
  reconcile         one Stage 8 reconciliation pass
  campaign-tick     one campaign pass (drafts only)
  follow-up-tick    one conversation follow-up pass (drafts only)
  dispatch-tick     one pass over operator-approved messages (Stage 8 revalidates each)
  execution-plan    read-only: the Stage 14 execution plan of one lead (--lead-id)
  execution-queue   read-only: the plans of one execution queue (--queue, default OPERATOR)
  execution-metrics read-only: execution metrics over open leads
  execution-pass    one bounded Stage 14 pass: at most one automatic action per lead
                    [--dispatch-approved: may also dispatch operator-approved messages]
  provider-status   read-only: selected providers, configuration validity, implementation,
                    local authorization and capability availability, production readiness.
                    Needs no database, contacts no provider, never prints a secret value
  gmail-auth        the explicit, interactive Gmail authorization (installed-app OAuth in
                    the browser); stores the token file only if the authorized account is
                    the configured GMAIL_ADDRESS. Needs no database. Never runs implicitly
  operator-sync     one bounded operator-channel pass (Telegram): handle updates through the
                    Stage 7 commands, then send new review cards. Never a loop or daemon
  email-sync        one bounded inbound mailbox pass (the first pass only sets the cursor)
                    [--recover: explicitly re-establish an expired cursor; mail in the gap
                    is not ingested]

Every command runs once and exits; there is no loop or daemon. Configuration comes from
``SALES_AGENT_*`` environment variables. Output is JSON with IDs, counts and codes only;
the configuration and any secret are never printed. The CLI uses the offline adapters:
without a configured provider, dispatch and reconciliation report SKIPPED rather than
fabricating provider outcomes. Nothing is ever approved here.

Exit codes: 0 ok, 1 unexpected error, 2 invalid configuration or usage, 3 startup or
health failure, 4 a phase reported errors.
"""

import argparse
import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from pydantic import BaseModel

from app.integrations import ProviderConnectors
from app.orchestration import ExecutionOutcome, ExecutionPassResult, ExecutionQueue, OrchestrationNotFoundError
from app.persistence import MEMORY, SystemClock
from app.persistence.migrations import current_version, latest_version
from app.runtime.application import SalesAgentRuntime
from app.runtime.config import RuntimeConfig
from app.runtime.env import inspect_integrations, load_config
from app.runtime.errors import ConfigError, StartupError
from app.runtime.results import PhaseStatus, RuntimeTickResult

OK, UNEXPECTED, INVALID_CONFIG, UNHEALTHY, PHASE_ERRORS = 0, 1, 2, 3, 4
# Provider connectors for the runtime the CLI builds: None is the real providers. (A seam
# for tests, which substitute a fake Gmail API; never set in production code.)
CONNECTORS: ProviderConnectors | None = None
TICKS = ("tick", "reconcile", "campaign-tick", "follow-up-tick", "dispatch-tick")
EXECUTION = ("execution-plan", "execution-queue", "execution-metrics", "execution-pass")


def main(argv: Sequence[str], environ: Mapping[str, str], out: TextIO) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.runtime", description="AI sales agent one-shot runtime commands")
    parser.add_argument("command", choices=("init", "health", "provider-status", "gmail-auth", "email-sync", "operator-sync",
                                            *TICKS, *EXECUTION))
    parser.add_argument("--recover", action="store_true", help="email-sync only: re-establish an expired cursor")
    parser.add_argument("--dispatch-approved", action="store_true",
                        help="tick / execution-pass only: also dispatch approved messages")
    parser.add_argument("--lead-id", help="execution-plan: the lead to plan")
    parser.add_argument("--queue", choices=[q.value for q in ExecutionQueue], default=ExecutionQueue.OPERATOR.value,
                        help="execution-queue: which queue")
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:
        return INVALID_CONFIG if exc.code else OK
    clock = SystemClock()
    if args.command == "provider-status":
        return _provider_status(environ, clock, out)
    try:
        config = load_config(environ, now=clock.now())
    except ConfigError as exc:
        _emit(out, {"error": "INVALID_CONFIGURATION", "problems": list(exc.problems)})
        return INVALID_CONFIG
    if args.command == "health":
        return _health(config.database_path, out)
    if args.command == "gmail-auth":
        return _gmail_auth(config, out)
    if args.command == "execution-plan" and not args.lead_id:
        _emit(out, {"error": "LEAD_ID_REQUIRED"})
        return INVALID_CONFIG
    if args.command != "init" and (config.database_path == MEMORY or not Path(config.database_path).is_file()):
        # Only ``init`` may create a database: a mistyped path must not silently start empty.
        _emit(out, {"error": "DATABASE_MISSING", "hint": "run 'init' first"})
        return UNHEALTHY

    runtime = SalesAgentRuntime(config, clock=clock, connectors=CONNECTORS)
    try:
        startup = runtime.start()
    except StartupError as exc:
        _emit(out, {"error": "STARTUP_FAILED", "code": exc.code, "health": runtime.health().model_dump(mode="json")})
        return UNHEALTHY
    try:
        if args.command == "init":
            _emit(out, {"startup": startup.model_dump(mode="json"), "health": runtime.health().model_dump(mode="json")})
            return OK
        if args.command in EXECUTION:
            return _execution(runtime, args, out)
        if args.command == "operator-sync":
            result = runtime.operator_sync()
            _emit(out, result.model_dump(mode="json"))
            return PHASE_ERRORS if result.status.value == "ERROR" else OK
        if args.command == "email-sync":
            synced = runtime.email_sync(recover=args.recover)
            _emit(out, synced.model_dump(mode="json"))
            return PHASE_ERRORS if synced.status.value in ("ERROR", "RECOVERY_REQUIRED") else OK
        result: BaseModel = {
            "tick": lambda: runtime.tick(dispatch_approved=args.dispatch_approved),
            "reconcile": runtime.reconcile,
            "campaign-tick": runtime.campaign_tick,
            "follow-up-tick": runtime.follow_up_tick,
            "dispatch-tick": runtime.dispatch_tick,
        }[args.command]()
        _emit(out, result.model_dump(mode="json"))
        return PHASE_ERRORS if _has_errors(result) else OK
    finally:
        runtime.stop()


def _execution(runtime: SalesAgentRuntime, args: argparse.Namespace, out: TextIO) -> int:
    """Stage 14 commands. Only execution-pass may act, once and bounded; nothing is approved."""
    if args.command == "execution-plan":
        try:
            _emit(out, runtime.execution_plan(args.lead_id).model_dump(mode="json"))
        except OrchestrationNotFoundError:
            _emit(out, {"error": "LEAD_NOT_FOUND"})
            return INVALID_CONFIG
        return OK
    if args.command == "execution-queue":
        plans = runtime.execution_queue(ExecutionQueue(args.queue))
        _emit(out, {"queue": args.queue, "plans": [p.model_dump(mode="json") for p in plans]})
        return OK
    if args.command == "execution-metrics":
        _emit(out, runtime.execution_metrics().model_dump(mode="json"))
        return OK
    result: ExecutionPassResult = runtime.execution_pass(dispatch_approved=args.dispatch_approved)
    _emit(out, result.model_dump(mode="json"))
    return PHASE_ERRORS if result.count(ExecutionOutcome.ERROR) else OK


def _gmail_auth(config: RuntimeConfig, out: TextIO) -> int:
    """Only when Gmail is selected; prints the outcome code and the mailbox, never a token."""
    if config.integrations.email.provider.value != "GMAIL":
        _emit(out, {"error": "GMAIL_NOT_SELECTED"})
        return INVALID_CONFIG
    from app.integrations.gmail import auth as gmail_auth
    from app.integrations.gmail import provider as gmail_provider
    from app.integrations.gmail.errors import GmailError

    try:
        mailbox = gmail_provider.authorize_mailbox(config.integrations.email, config.secrets.gmail,
                                                   flow=gmail_auth.run_installed_app_flow, api_factory=gmail_provider.real_api)
    except GmailError as exc:
        _emit(out, {"error": "GMAIL_AUTHORIZATION_FAILED", "code": exc.code.value})
        return UNHEALTHY
    _emit(out, {"authorized": True, "mailbox": mailbox})
    return OK


def _provider_status(environ: Mapping[str, str], clock: SystemClock, out: TextIO) -> int:
    """Configuration health, not connectivity. Problems are variable names and codes."""
    status = inspect_integrations(environ)
    problems: tuple[str, ...] = ()
    mode: str | None = None
    try:
        mode = load_config(environ, now=clock.now()).mode.value
    except ConfigError as exc:
        problems = exc.problems
    _emit(out, {"configuration_valid": not problems, "problems": list(problems), "mode": mode,
                # Whether PRODUCTION mode could start with this configuration (any mode).
                "production_ready": not problems and status.production_ready,
                "integrations": status.model_dump(mode="json")})
    return OK if not problems else INVALID_CONFIG


def _health(database_path: str, out: TextIO) -> int:
    """Read-only: the database must already exist; it is opened with mode=ro."""
    report: dict[str, object] = {"latest_schema_version": latest_version()}
    if database_path == MEMORY or not Path(database_path).is_file():
        _emit(out, report | {"database_ok": False, "problem": "DATABASE_MISSING"})
        return UNHEALTHY
    try:
        connection = sqlite3.connect(f"{Path(database_path).resolve().as_uri()}?mode=ro", uri=True)
        try:
            version = current_version(connection)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        _emit(out, report | {"database_ok": False, "problem": f"DATABASE_UNAVAILABLE:{type(exc).__name__}"})
        return UNHEALTHY
    current = version == latest_version()
    _emit(out, report | {"database_ok": True, "schema_version": version, "schema_current": current})
    return OK if current else UNHEALTHY


def _has_errors(result: BaseModel) -> bool:
    if isinstance(result, RuntimeTickResult):
        return not result.ok
    return getattr(result, "status", None) is PhaseStatus.ERROR


def _emit(out: TextIO, payload: dict[str, object]) -> None:
    out.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
