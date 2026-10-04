# ADR 0006 — Approvals are bound to immutable state, expire, and are re-checked

- **Status:** accepted
- **Date:** 2026-10-04

## Decision
An `Approval` binds:
1. the decision id;
2. the account `state_version`, which changes on any balance, position or order change;
3. the **snapshot digest**, an order-independent SHA-256 over the content hashes of every
   snapshot the decision read (account, calendar, news, and every market in use);
4. a payload hash of (candidate, size, policy hash);
5. a validity window `[issued_at, issued_at + ttl)`, with ttl capped by the policy (≤ 10 min).

`risk.recheck.recheck_before_execution` is the only path from an approval to an order:
- it re-runs the full kernel on **fresh** inputs;
- it verifies the approvals against the **fresh** account state version (`R-APR-01..05`);
- it checks price drift from the signal entry (`R-EXE-01`), that the original decision
  approved this candidate (`R-EXE-02`), and that the policy is unchanged (`R-EXE-03`);
- the resulting size is `min(original, fresh)`.

"Markets in use" (`Context.required_symbols`) means the candidate's pair, every open
position's pair, and the pairs needed to convert those currencies to the account currency.
A stale quote for any of them blocks; an unrelated stale pair does not.

## Consequences
- Three authorities are required (VALIDATION, RISK, PORTFOLIO). The VALIDATION authority
  arrives with the signal-validation agent (M7), so nothing can execute end to end before
  then, independent of the execution guard (ADR 0007).
