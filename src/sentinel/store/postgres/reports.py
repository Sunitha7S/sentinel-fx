"""Markdown reports: security posture and data quality from a live database, and the
human-readable form of a strict quality-gate report (the canonical JSON is authoritative)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Engine, text

from sentinel.domain.dataset import QualityReport
from sentinel.domain.quality import APPROVED, FLAGS, INTEGRITY_GATES
from sentinel.perception.market_data.quality import analyse_fixed_grid, analyse_weekly
from sentinel.store.postgres.engine import OWNER_ROLE, SERVICE_ROLES
from sentinel.store.postgres.market_data_store import PostgresMarketDataStore
from sentinel.store.postgres.permissions import PRIVILEGES, TABLES, expected

__all__ = ["data_quality_report", "quality_gate_markdown", "security_report"]

_ABBR = {
    "SELECT": "S",
    "INSERT": "I",
    "UPDATE": "U",
    "DELETE": "D",
    "TRUNCATE": "T",
    "REFERENCES": "R",
    "TRIGGER": "Tg",
}


def security_report(engine: Engine) -> str:
    """Actual roles, privileges and triggers, checked against the intended matrix."""
    lines = ["# Database security report", ""]
    with engine.connect() as conn:
        roles = conn.execute(
            text(
                "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolcanlogin, "
                "rolbypassrls FROM pg_roles WHERE rolname = ANY(:n) ORDER BY rolname"
            ),
            {"n": [*SERVICE_ROLES, OWNER_ROLE]},
        ).all()
        lines += [
            "## Roles",
            "",
            "| Role | superuser | createrole | createdb | login | bypassrls |",
            "|---|---|---|---|---|---|",
        ]
        lines += [
            f"| `{r.rolname}` | {r.rolsuper} | {r.rolcreaterole} | {r.rolcreatedb} | "
            f"{r.rolcanlogin} | {r.rolbypassrls} |"
            for r in roles
        ]
        lines += [
            "",
            "## Table privileges (actual)",
            "",
            "S=SELECT I=INSERT; any U/D/T/R/Tg would be a finding.",
            "",
            "| Table | " + " | ".join(f"`{r}`" for r in SERVICE_ROLES) + " |",
            "|---|" + "---|" * len(SERVICE_ROLES),
        ]
        drift = []
        for table in TABLES:
            cells = []
            for role in SERVICE_ROLES:
                held = [
                    p
                    for p in PRIVILEGES
                    if conn.execute(
                        text("SELECT has_table_privilege(:r, :t, :p)"),
                        {"r": role, "t": f"sentinel.{table}", "p": p},
                    ).scalar_one()
                ]
                if set(held) != set(expected(role, table)):
                    drift.append(
                        f"{role} on {table}: has {held}, expected {sorted(expected(role, table))}"
                    )
                cells.append("".join(_ABBR[p] for p in held) or "-")
            lines.append(f"| `{table}` | " + " | ".join(cells) + " |")
        owners = (
            conn.execute(
                text(
                    "SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'sentinel'"
                )
            )
            .scalars()
            .all()
        )
        # pg_trigger, not information_schema.triggers: the latter hides triggers on tables
        # the current role cannot modify, and this report runs as a read-only role.
        triggers = conn.execute(
            text(
                "SELECT c.relname AS tbl, string_agg(tg.tgname, ', ' ORDER BY tg.tgname) AS names "
                "FROM pg_trigger tg JOIN pg_class c ON c.oid = tg.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'sentinel' AND NOT tg.tgisinternal "
                "GROUP BY c.relname ORDER BY 1"
            )
        ).all()
        truncate_guards = (
            conn.execute(
                text(
                    "SELECT c.relname FROM pg_trigger tg JOIN pg_class c ON c.oid = tg.tgrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'sentinel' "
                    "AND tg.tgname LIKE '%no_truncate' ORDER BY 1"
                )
            )
            .scalars()
            .all()
        )
    lines += ["", f"Matrix drift: **{'none' if not drift else len(drift)}**"]
    lines += [f"- {d}" for d in drift]
    lines += [
        "",
        f"Object owners in schema `sentinel`: {', '.join(sorted(owners))}",
        "",
        "## Triggers",
        "",
        "| Table | Triggers |",
        "|---|---|",
    ]
    lines += [f"| `{r.tbl}` | {r.names} |" for r in triggers]
    lines += [
        "",
        f"TRUNCATE guarded on: {', '.join(truncate_guards)}",
        "",
        "Limits: these protections stop application bugs and service-role misuse. A "
        "PostgreSQL superuser can bypass them (ADR 0011).",
        "",
    ]
    return "\n".join(lines)


def _fmt(ts: datetime | None) -> str:
    return ts.strftime("%Y-%m-%d %H:%M") if ts else "-"


def data_quality_report(
    engine: Engine,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    top_gaps: int = 15,
) -> str:
    store = PostgresMarketDataStore(engine)
    lines = ["# Data quality report", ""]
    summaries = store.summaries()
    if not summaries:
        return "\n".join([*lines, "No market data stored.", ""])
    lines += [
        "## Series",
        "",
        "| Symbol | TF | Bars | First | Last | Expected | Completeness | "
        "Gaps (short / material) | Missing bars | Unexpected bars |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    details = []
    for s in summaries:
        lo = start or s.first.replace(hour=0, minute=0, second=0, microsecond=0)
        hi = end or (s.last + s.timeframe.duration)
        series = store.timestamps(s.symbol, s.timeframe, lo, hi)
        if s.timeframe.fixed_utc_grid:
            q = analyse_fixed_grid(series, s.timeframe, lo, hi)
            lines.append(
                f"| {s.symbol} | {s.timeframe} | {s.count:,} | {_fmt(s.first)} | {_fmt(s.last)} | "
                f"{q.expected:,} | {q.completeness_pct}% | {q.short_gaps} / {len(q.material_gaps)} "
                f"| {q.missing:,} | {q.unexpected:,} |"
            )
            worst = sorted(q.material_gaps, key=lambda g: g.missing_bars, reverse=True)[:top_gaps]
            if worst:
                details += [
                    f"### {s.symbol} {s.timeframe}: largest material gaps",
                    "",
                    "| From (UTC) | To (UTC) | Missing bars |",
                    "|---|---|---|",
                ]
                details += [
                    f"| {_fmt(g.start)} | {_fmt(g.end)} | {g.missing_bars:,} |" for g in worst
                ]
                details.append("")
        else:
            w = analyse_weekly(series, s.timeframe)
            lines.append(
                f"| {s.symbol} | {s.timeframe} | {s.count:,} | {_fmt(s.first)} | {_fmt(s.last)} | "
                f"{w.weeks * w.expected_per_week:,} | {w.completeness_pct}% | weekly: "
                f"{len(w.short_weeks)} short weeks | - | - |"
            )
        stats = store.spread_stats(s.symbol, s.timeframe)
        details_spread = (
            f"- {s.symbol} {s.timeframe} close spread: median {stats['median']}, "
            f"p99 {stats['p99']}, max {stats['max']}"
        )
        details.insert(0, details_spread)
    runs = store.runs()
    lines += [
        "",
        "## Per-bar spreads (at bar close)",
        "",
        *[d for d in details if d.startswith("- ")],
        "",
        *[d for d in details if not d.startswith("- ")],
        "## Ingestion runs",
        "",
        "| Started (UTC) | Provider | Symbol | TF | Status | "
        "Received | Inserted | Rejected | Error |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {_fmt(r.started_at)} | {r.provider} | {r.symbol} | {r.timeframe} | {r.status} | "
        f"{r.candles_received:,} | {r.candles_inserted:,} | {r.candles_rejected:,} | "
        f"{(r.error or '')[:80]} |"
        for r in runs
    ]
    lines += [
        "",
        "Expected bars: Sunday 17:00 to Friday 17:00 New York, excluding 25 December and "
        "1 January. Short gaps (≤ 5 bars) are minutes without ticks; material gaps are "
        "listed above.",
        "",
    ]
    return "\n".join(lines)


def quality_gate_markdown(report: QualityReport) -> str:
    """Render a quality-gate report. Presentation only: the verdict and every number come
    from the report, whose canonical JSON (and its hash) is the authoritative form."""
    spec, cfg, r = report.spec, report.config, report.result
    m = r.metrics
    lines = [
        f"# Data-quality gate: {spec.symbol} {spec.timeframe} [{_fmt(spec.start)}, "
        f"{_fmt(spec.end)})",
        "",
        f"**Verdict: {r.verdict}**",
        "",
    ]
    if cfg.status != APPROVED:
        lines += [
            f"> Configuration {cfg.version} is **{cfg.status}**. This report is for review only;",
            "> no snapshot can be frozen against an unapproved configuration.",
            "",
        ]
    lines += [
        "| | |",
        "|---|---|",
        f"| Source | {spec.source} |",
        f"| Rows | {report.digest.rows:,} |",
        f"| Dataset SHA-256 | `{report.digest.sha256}` |",
        f"| Configuration | {cfg.version} ({cfg.status}) `{cfg.sha256}` |",
        f"| Engine / calendar | {cfg.engine} / {cfg.calendar} |",
        f"| Evaluated at (UTC) | {_fmt(report.as_of)} |",
        f"| Report SHA-256 | `{report.sha256}` |",
        "",
        "## Failures",
        "",
    ]
    if r.failures:
        lines += ["| Gate | Observed | Limit |", "|---|---|---|"]
        lines += [f"| {f.gate} | {f.observed} | {f.limit} |" for f in r.failures]
    else:
        lines.append("None.")
    lines += ["", "## Metrics", "", "| Metric | Value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in m.items()]
    lines += ["", "## Counts", "", "| Check | Kind | Count |", "|---|---|---|"]
    lines += [f"| {g} | FAIL if > 0 | {r.counts[g]:,} |" for g in INTEGRITY_GATES]
    lines += [f"| {g} | flag | {r.counts[g]:,} |" for g in FLAGS]
    lines.append(f"| unexpected_bar | series gate | {r.counts['unexpected_bar']:,} |")
    if r.largest_gaps:
        lines += ["", "## Largest gaps", "", "| From (UTC) | Missing bars |", "|---|---|"]
        lines += [f"| {_fmt(g.start)} | {g.missing_bars:,} |" for g in r.largest_gaps]
    for category, examples in r.examples.items():
        lines += ["", f"## Examples: {category}", "", "| Bar (UTC) | Detail |", "|---|---|"]
        lines += [f"| {_fmt(e.ts)} | {e.detail} |" for e in examples]
    lines.append("")
    return "\n".join(lines)
