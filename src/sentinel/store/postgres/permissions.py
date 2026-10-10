"""The intended privilege matrix: the single source for tests and the security report.

Tests compare this matrix with the privileges the database actually reports for every
role, table and privilege, so a migration that grants or revokes anything not listed here
fails the build.
"""

from __future__ import annotations

from typing import Final

__all__ = ["EXPECTED", "POLICY_TABLES", "PRIVILEGES", "TABLES", "expected"]

PRIVILEGES: Final = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")

POLICY_TABLES: Final = ("policy_versions", "policy_activations")
TABLES: Final = (
    *POLICY_TABLES,
    "decisions",
    "approvals",
    "shadow_trades",
    "audit_records",
    "audit_head",
    "market_candles",
    "spreads",
    "ingestion_runs",
    "market_series",
    "dataset_snapshots",
)

_R = frozenset({"SELECT"})
_RW = frozenset({"SELECT", "INSERT"})
_NONE: frozenset[str] = frozenset()

# role -> table -> privileges. Anything absent is denied. No role has UPDATE, DELETE,
# TRUNCATE, REFERENCES or TRIGGER on any table, and none owns a table. ``market_series`` is
# read-only for everyone: only an owner-controlled trigger registers a series. Only
# ``svc_market_data`` freezes ``dataset_snapshots`` (ADR 0014).
EXPECTED: Final[dict[str, dict[str, frozenset[str]]]] = {
    "human_admin": {
        "policy_versions": _RW,
        "policy_activations": _RW,
        "decisions": _R,
        "approvals": _R,
        "shadow_trades": _R,
        "audit_records": _R,
        "audit_head": _R,
        "market_candles": _R,
        "spreads": _R,
        "ingestion_runs": _R,
        "market_series": _R,
        "dataset_snapshots": _R,
    },
    "svc_risk": {
        "policy_versions": _R,
        "policy_activations": _R,
        "decisions": _RW,
        "approvals": _RW,
        "shadow_trades": _RW,
        "market_candles": _R,
        "spreads": _R,
        "ingestion_runs": _R,
        "market_series": _R,
        "dataset_snapshots": _R,
    },
    "svc_learning": {
        "policy_versions": _R,
        "policy_activations": _R,
        "decisions": _R,
        "approvals": _R,
        "shadow_trades": _R,
        "market_candles": _R,
        "spreads": _R,
        "ingestion_runs": _R,
        "market_series": _R,
        "dataset_snapshots": _R,
    },
    "svc_audit": {
        "policy_versions": _R,
        "policy_activations": _R,
        "audit_records": _RW,
        "audit_head": _R,
    },
    "svc_execution": {
        "policy_versions": _R,
        "policy_activations": _R,
        "decisions": _R,
        "approvals": _R,
        "shadow_trades": _R,
        "market_candles": _R,
        "spreads": _R,
        "ingestion_runs": _R,
        "market_series": _R,
        "dataset_snapshots": _R,
    },
    "svc_market_data": {
        "market_candles": _RW,
        "spreads": _RW,
        "ingestion_runs": _RW,
        "market_series": _R,
        "dataset_snapshots": _RW,
    },
}


def expected(role: str, table: str) -> frozenset[str]:
    return EXPECTED.get(role, {}).get(table, _NONE)
