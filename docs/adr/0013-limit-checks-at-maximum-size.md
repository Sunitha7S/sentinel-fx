# ADR 0013 — Limit rules judge a trade at the policy's maximum size

- **Status:** accepted (bug fix to the documented kernel property; flagged for review)
- **Date:** 2026-10-04

## Context
docs/04 §1 states, and INV-KERNEL-02 tests, that making any input worse never turns BLOCKED
into APPROVED. Under the CI property profile Hypothesis found a counterexample:

| | drawdown | throttle | trade risk | weekly loss incl. trade | outcome |
|---|---|---|---|---|---|
| base | 2.67 % | 1.00× | 0.500 % | ≈ 3.01 % (> 3 %) | BLOCKED by R-ACC-02 |
| worse | 4.00 % | 0.75× | 0.375 % | ≈ 2.89 % | **APPROVED** |

The loss, exposure, total-risk, gap and leverage rules were evaluated at the *final* size,
which the drawdown throttle, the LIVE_MICRO multiplier, costs and rounding all reduce. Any
of them could shrink a trade until it slipped under a limit it otherwise breached, so a
worse state could unlock a trade.

## Decision
Limit rules (R-ACC-01..03, R-PTF-01, R-PTF-02, R-PTF-04, R-TRD-05) evaluate the trade at the
**largest size the policy could ever allow it**:
- risk = equity × base risk per trade (before throttle and mode multiplier);
- units = that risk ÷ (stop distance × quote→account rate), with zero costs and no rounding.

The approved size is unchanged: still throttled, cost-adjusted and rounded down. Throttles
and multipliers now only decide *how much* is traded, never *whether*.

## Consequences
- Strictly tighter: some trades near a limit that were approved at reduced size are now
  blocked. No trade that was blocked becomes approved.
- LIVE_MICRO trades are checked against limits as if they were full size. This is
  conservative for a mode whose purpose is validation, not exposure.
- A named regression test reproduces the counterexample for drawdown 4 %, 8 % and LIVE_MICRO.
