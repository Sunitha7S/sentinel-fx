"""Registry of safety invariants.

Every invariant listed here must be enforced by at least one test decorated with
``@pytest.mark.invariant("<ID>")``. ``test_manifest.py`` fails the build when an
invariant has no test or a test references an unknown ID. Removing an entry from
this file is a deliberate, reviewable act; it should be accompanied by an ADR.
"""

from __future__ import annotations

REQUIRED_INVARIANTS: dict[str, str] = {
    "INV-ARCH-01": "Core packages import only the standard library and allowed internals.",
    "INV-ARCH-02": "Domain, fxmath, risk and audit load without any infrastructure library.",
    "INV-SIZE-01": "Position risk never exceeds equity x permitted risk fraction.",
    "INV-SIZE-02": "Size is non-negative, rounded down to the broker step, within min/max.",
    "INV-SIZE-03": "Invalid sizing inputs (incl. zero/negative stop distance) give zero size.",
    "INV-SIZE-04": "Wider stops, lower permitted risk, higher costs never give a larger size.",
    "INV-DEFAULT-01": "The system starts HALTED; missing data blocks.",
    "INV-DATA-01": "Stale or future-dated account, market, calendar or news data blocks.",
    "INV-DATA-02": "Disagreeing or insufficient calendar sources block.",
    "INV-LOSS-01": "Daily, weekly and monthly loss limits block new risk.",
    "INV-LOSS-02": "Drawdown at the halt level blocks; the throttle only reduces risk.",
    "INV-LOSS-03": "Consecutive-loss cooldown blocks and then releases; the halt does not.",
    "INV-PORT-01": "Correlated currency exposure and total open risk stay within limits.",
    "INV-KERNEL-01": "The kernel is total, exhaustive and deterministic.",
    "INV-KERNEL-02": "Worse inputs never turn BLOCKED into APPROVED.",
    "INV-APPROVAL-01": "Approvals expire.",
    "INV-APPROVAL-02": "Snapshot, state-version or payload mismatch invalidates approvals.",
    "INV-RECHECK-01": "Execution re-evaluates risk from fresh account and market state.",
    "INV-FAILCLOSED-01": "Any service failure produces BLOCKED.",
    "INV-AUDIT-01": "Every risk decision is in a tamper-evident audit chain.",
    "INV-AUDIT-IMMUTABLE": (
        "No historical decision can be modified without breaking verification "
        "(internal edits: chain alone; truncation and full re-hash: against the anchored head)."
    ),
    "INV-SHADOW-01": "Every candidate is persisted as a shadow trade.",
    "INV-LLM-01": "LLM output can only add caution; it cannot loosen any decision.",
    "INV-LLM-02": "LLM-facing code cannot reach trading state, policy or execution.",
    "INV-POLICY-01": (
        "Learning cannot modify protected risk policy (code capability and database grants)."
    ),
    "INV-POLICY-02": "Loosening needs human approval and cooling-off; code ceilings hold.",
    "INV-STATE-01": "Automatic transitions only tighten; every transition is audited.",
    "INV-POLICY-IMMUTABLE": (
        "Policy versions are immutable; activation is append-only and linear; decisions "
        "reference the exact policy hash; no service role can replace policy history."
    ),
    "INV-DB-ROLES": (
        "Database roles have no dangerous attributes, own nothing, and hold exactly the "
        "privileges in sentinel.store.postgres.permissions."
    ),
    "INV-DB-APPEND-ONLY": (
        "UPDATE, DELETE and TRUNCATE are refused on append-only tables even for the owner."
    ),
    "INV-MD-READONLY": (
        "Market-data providers can only issue GET requests for historical candles on the "
        "practice host; order, account and streaming endpoints are unreachable."
    ),
    "INV-MD-SINGLE-SOURCE": (
        "A market-data series has exactly one source: rows from any other source are refused "
        "by the database for every role, and only an owner-controlled trigger registers series."
    ),
    "INV-DATA-SNAPSHOT": (
        "Verified datasets are reproducible and tamper-evident: a snapshot's digest is a pure, "
        "machine-independent function of its rows, and any change to rows or record is "
        "refused on load."
    ),
    "INV-DATA-SEALED": (
        "Frozen ranges cannot change: no candle or spread row can be inserted inside a "
        "snapshot's range by any role, and updates, deletes and truncation are refused."
    ),
    "INV-DATA-VERIFIED-ONLY": (
        "Research code (backtest, indicators, strategy, learning) cannot reach raw market "
        "data; historical data reaches it only as a VerifiedDataset issued after verification."
    ),
    "INV-DATA-QUALITY-GATED": (
        "No research dataset may become a usable frozen snapshot unless the exact frozen rows "
        "deterministically PASS the exact hashed quality configuration, and that result is "
        "reproducible during verified load. That configuration's hash matches its content, it "
        "is APPROVED with exactly one matching approval record, and freeze and load obtain it "
        "only from the trusted registry loaded from the shipped directory (no registry, no "
        "freeze or load); a quality report's fields, canonical JSON and hash agree."
    ),
    "INV-EXEC-01": "Execution is disabled; live trading needs explicit, consistent enabling.",
}
