"""Command-line entry point.

M1 commands are read-only inspection tools:

* ``sentinel policy-hash PATH``   validate a policy file against code ceilings, print its hash
* ``sentinel audit-verify PATH``  verify a JSON-lines audit chain
* ``sentinel settings [--env E]`` show effective settings and whether execution is permitted
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from sentinel import __version__
from sentinel.audit.chain import AuditChainError, verify_chain
from sentinel.config.policy_loader import load_policy
from sentinel.config.settings import SettingsError, load_settings
from sentinel.execution.guard import execution_refusals
from sentinel.risk.policy import PolicyViolation
from sentinel.store.audit_jsonl import JsonlAuditSink


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sentinel", description="Sentinel FX command line")
    parser.add_argument("--version", action="version", version=f"sentinel {__version__}")
    sub = parser.add_subparsers(dest="command")

    policy = sub.add_parser("policy-hash", help="validate a risk policy file and print its hash")
    policy.add_argument("path", type=Path)

    audit = sub.add_parser("audit-verify", help="verify a JSON-lines audit chain")
    audit.add_argument("path", type=Path)

    settings = sub.add_parser("settings", help="show effective settings and execution status")
    settings.add_argument("--env", default=None)
    return parser


def _policy_hash(path: Path) -> int:
    try:
        policy = load_policy(path)
    except (OSError, PolicyViolation) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"{policy.policy_id} {policy.sha256}")
    return 0


def _audit_verify(path: Path) -> int:
    try:
        count = verify_chain(JsonlAuditSink(path))
    except (OSError, AuditChainError, ValueError, KeyError) as exc:
        print(f"BROKEN: {exc}", file=sys.stderr)
        return 1
    print(f"OK: {count} records")
    return 0


def _settings(env: str | None) -> int:
    try:
        s = load_settings(env)
    except SettingsError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print(s.model_dump_json(indent=2))
    refusals = execution_refusals(s.execution_environment())
    print("execution: " + ("PERMITTED" if not refusals else "REFUSED"))
    for reason in refusals:
        print(f"  - {reason}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "policy-hash":
        return _policy_hash(args.path)
    if args.command == "audit-verify":
        return _audit_verify(args.path)
    if args.command == "settings":
        return _settings(args.env)
    parser.print_help(sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
