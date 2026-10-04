# ADR 0007 — Execution is hard-disabled in code until M10

- **Status:** accepted
- **Date:** 2026-10-04

## Decision
- `execution.guard.EXECUTION_HARD_DISABLED = True` is a code constant. No configuration,
  environment variable or API can enable order submission. Lifting it requires a reviewed
  commit in M10, together with the broker adapter and its contract and chaos tests.
- The guard also enforces environment consistency, so these rules are tested before the
  switch is ever flipped:
  - execution must be enabled explicitly;
  - SHADOW and BACKTEST never send orders;
  - PAPER requires a PRACTICE broker;
  - LIVE_MICRO and LIVE require a LIVE broker **and** `live_trading_enabled`.
- Refusals are audited.
- Environments: `shadow` (default), `paper` (broker practice account, which is what the
  request called "paper/practice"), and `live`. Every shipped file sets execution and live
  trading to `false`. `live_trading_enabled` is rejected outside the `live` environment, and
  boolean overrides accept only the exact strings `true` or `false`.
- The only broker adapter is `DisabledBrokerAdapter`, which goes through the guard.

## Consequences
M10 will change one constant. Every guard test except the "hard-disabled" ones keeps
passing unchanged.
