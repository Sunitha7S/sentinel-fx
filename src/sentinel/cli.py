"""Command-line entry point.

Inspection (M1):

* ``sentinel policy-hash PATH``   validate a policy file against code ceilings, print its hash
* ``sentinel quality-hash PATH``  validate a data-quality configuration, print its identity
* ``sentinel audit-verify PATH``  verify a JSON-lines audit chain
* ``sentinel settings [--env E]`` show effective settings and whether execution is permitted

Data layer (M2; database URL from ``SENTINEL_DATABASE_URL``):

* ``sentinel provider-check``  small fixed read-only probes; verdict and next step before any
  bulk ingestion. Needs only ``SENTINEL_OANDA_TOKEN``; never touches the database
* ``sentinel ingest --symbols EUR_USD,USD_JPY --timeframes M1,H1,H4,D1 --from 2015-01-01``
  read-only historical ingestion (OANDA practice; token from ``SENTINEL_OANDA_TOKEN``)
* ``sentinel data-report [--output FILE]``         completeness, gaps, spreads, runs
* ``sentinel data-gate --symbol EUR_USD --timeframe M1 --from 2015-01-01 --to 2016-01-01``
  strict quality-gate report for one range (read-only): Markdown to stdout or ``--output``,
  canonical JSON with ``--json``. Exit 0 PASS, 1 FAIL, 2 invalid configuration or
  environment, 3 evaluation error
* ``sentinel db-security-report [--output FILE]``  roles, grants, triggers vs intended matrix

Database sessions are pinned to the least-privileged role for the job (``--db-role``).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from sentinel import __version__
from sentinel.audit.chain import AuditChainError, verify_chain
from sentinel.config.policy_loader import load_policy
from sentinel.config.quality_loader import QUALITY_CONFIG_DIR, load_quality_config
from sentinel.config.settings import SettingsError, load_settings
from sentinel.domain.dataset import DatasetError, DatasetSpec
from sentinel.domain.market_data import Timeframe
from sentinel.domain.quality import QualityConfigError
from sentinel.execution.guard import execution_refusals
from sentinel.perception.market_data import connectivity, oanda
from sentinel.perception.market_data.connectivity import CheckStatus
from sentinel.perception.market_data.ingest import ingest
from sentinel.perception.market_data.provider import ProviderError
from sentinel.risk.policy import PolicyViolation
from sentinel.store.audit_jsonl import JsonlAuditSink
from sentinel.store.postgres.dataset_store import PostgresDatasetStore
from sentinel.store.postgres.engine import role_engine
from sentinel.store.postgres.market_data_store import PostgresMarketDataStore
from sentinel.store.postgres.reports import (
    data_quality_report,
    quality_gate_markdown,
    security_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sentinel", description="Sentinel FX command line")
    parser.add_argument("--version", action="version", version=f"sentinel {__version__}")
    sub = parser.add_subparsers(dest="command")

    policy = sub.add_parser("policy-hash", help="validate a risk policy file and print its hash")
    policy.add_argument("path", type=Path)

    quality = sub.add_parser(
        "quality-hash", help="validate a data-quality configuration and print its hash"
    )
    quality.add_argument("path", type=Path)

    audit = sub.add_parser("audit-verify", help="verify a JSON-lines audit chain")
    audit.add_argument("path", type=Path)

    settings = sub.add_parser("settings", help="show effective settings and execution status")
    settings.add_argument("--env", default=None)

    check = sub.add_parser("provider-check", help="verify read access to the history M2.1 needs")
    check.add_argument("--provider", choices=["oanda"], default="oanda")

    ingest = sub.add_parser("ingest", help="read-only historical candle ingestion")
    ingest.add_argument("--provider", choices=["oanda"], default="oanda")
    ingest.add_argument("--symbols", default="EUR_USD,USD_JPY")
    ingest.add_argument("--timeframes", default="M1,H1,H4,D1")
    ingest.add_argument("--from", dest="start", default="2015-01-01")
    ingest.add_argument("--to", dest="end", default=None, help="default: now")
    ingest.add_argument("--db-role", default="svc_market_data")

    gate = sub.add_parser("data-gate", help="strict data-quality gate report for one range")
    gate.add_argument("--symbol", required=True)
    gate.add_argument("--timeframe", required=True)
    gate.add_argument("--from", dest="start", required=True)
    gate.add_argument("--to", dest="end", required=True)
    gate.add_argument("--source", default=None, help="default: the series' registered source")
    gate.add_argument("--config", type=Path, default=QUALITY_CONFIG_DIR / "qc_v1.yaml")
    gate.add_argument("--json", type=Path, default=None, help="write the canonical JSON here")
    gate.add_argument("--output", type=Path, default=None, help="write the Markdown here")
    gate.add_argument("--db-role", default="svc_learning")

    for name, role, helptext in (
        ("data-report", "svc_learning", "data-quality report from the database"),
        ("db-security-report", "svc_learning", "database security report"),
    ):
        report = sub.add_parser(name, help=helptext)
        report.add_argument("--output", type=Path, default=None)
        report.add_argument("--db-role", default=role)
    return parser


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _database_url() -> str | None:
    url = os.environ.get("SENTINEL_DATABASE_URL")
    if not url:
        print("SENTINEL_DATABASE_URL is not set", file=sys.stderr)
    return url


_CHECK_EXIT = {CheckStatus.OK: 0, CheckStatus.ACCESS_DENIED: 3}


def _provider_check() -> int:
    token = os.environ.get("SENTINEL_OANDA_TOKEN", "")
    if not token.strip():
        print(
            "SENTINEL_OANDA_TOKEN is not set: create an OANDA *practice* API token and set it "
            "in this shell (never commit it). No request was made.",
            file=sys.stderr,
        )
        return 2
    provider = oanda.OandaProvider(token)
    try:
        result = connectivity.check_provider(provider, connectivity.DEFAULT_PROBES)
    finally:
        provider.close()
    print(connectivity.render_check(result))
    return _CHECK_EXIT.get(result.verdict, 1)


def _ingest(args: argparse.Namespace) -> int:

    url = _database_url()
    token = os.environ.get("SENTINEL_OANDA_TOKEN", "")
    if url is None:
        return 2
    try:
        provider = oanda.OandaProvider(token)
    except ProviderError as exc:
        print(f"provider unavailable: {exc}", file=sys.stderr)
        return 2
    engine = role_engine(url, args.db_role)
    store = PostgresMarketDataStore(engine)
    start = _utc(args.start)
    end = _utc(args.end) if args.end else datetime.now(UTC)
    failed = False
    try:
        for symbol in args.symbols.split(","):
            for tf in args.timeframes.split(","):
                run = ingest(
                    provider,
                    store,
                    symbol=symbol.strip(),
                    timeframe=Timeframe(tf.strip()),
                    start=start,
                    end=end,
                    clock=lambda: datetime.now(UTC),
                )
                failed = failed or run.status != "SUCCEEDED"
                print(
                    f"{run.status} {run.symbol} {run.timeframe}: received "
                    f"{run.candles_received:,}, inserted {run.candles_inserted:,}, "
                    f"rejected {run.candles_rejected:,}" + (f" ({run.error})" if run.error else "")
                )
    finally:
        provider.close()
        engine.dispose()
    return 1 if failed else 0


def _report(args: argparse.Namespace) -> int:

    url = _database_url()
    if url is None:
        return 2
    engine = role_engine(url, args.db_role)
    render = data_quality_report if args.command == "data-report" else security_report
    try:
        content = render(engine)
    finally:
        engine.dispose()
    if args.output:
        args.output.write_text(content, encoding="utf-8")
        print(f"written to {args.output}")
    else:
        print(content)
    return 0


GATE_PASS, GATE_FAIL, GATE_INVALID, GATE_ERROR = 0, 1, 2, 3


def _data_gate(args: argparse.Namespace) -> int:
    try:
        config = load_quality_config(args.config)
        timeframe = Timeframe(args.timeframe)
    except (QualityConfigError, ValueError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return GATE_INVALID
    url = _database_url()
    if url is None:
        return GATE_INVALID
    engine = role_engine(url, args.db_role)
    try:
        source = args.source or PostgresMarketDataStore(engine).registered_source(
            args.symbol, timeframe
        )
        if source is None:
            raise DatasetError(f"series {args.symbol} {timeframe} is not registered")
        spec = DatasetSpec(args.symbol, timeframe, source, _utc(args.start), _utc(args.end))
        report = PostgresDatasetStore(engine).judge(spec, config)
    except Exception as exc:  # noqa: BLE001 - any evaluation failure is a non-PASS exit
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return GATE_ERROR
    finally:
        engine.dispose()
    markdown = quality_gate_markdown(report)
    if args.json:
        args.json.write_bytes(report.canonical_json.encode("ascii"))
    if args.output:
        args.output.write_text(markdown, encoding="utf-8")
    else:
        print(markdown)
    print(f"{report.verdict} {report.sha256}", file=sys.stderr)
    return GATE_PASS if report.passed else GATE_FAIL


def _policy_hash(path: Path) -> int:
    try:
        policy = load_policy(path)
    except (OSError, PolicyViolation) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"{policy.policy_id} {policy.sha256}")
    return 0


def _quality_hash(path: Path) -> int:
    try:
        config = load_quality_config(path)
    except QualityConfigError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"{config.version} {config.status} {config.sha256}")
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
    handlers: dict[str, Callable[[], int]] = {
        "policy-hash": lambda: _policy_hash(args.path),
        "quality-hash": lambda: _quality_hash(args.path),
        "audit-verify": lambda: _audit_verify(args.path),
        "settings": lambda: _settings(args.env),
        "provider-check": _provider_check,
        "ingest": lambda: _ingest(args),
        "data-report": lambda: _report(args),
        "data-gate": lambda: _data_gate(args),
        "db-security-report": lambda: _report(args),
    }
    handler = handlers.get(args.command or "")
    if handler is None:
        parser.print_help(sys.stdout)
        return 0
    return handler()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
