# ADR 0004 — Fail closed, start HALTED, audit everything

- **Status:** accepted
- **Date:** 2026-10-04

## Decision
1. **Default state.** `initial_system_state()` is `HALTED`. Trading needs two separate human
   steps (HALTED → NO_NEW_TRADES → ACTIVE). Automatic actors can only tighten.
2. **Kernel totality.** `risk.kernel.evaluate` never raises. Missing inputs fail the rules that
   need them (`R-SYS-00` lists them), a rule that raises fails itself, and anything else
   produces a minimal BLOCKED decision (`R-SYS-99`). All 33 rules are reported on every decision.
3. **Approval invariants in the type.** `RiskDecision.__post_init__` refuses an APPROVED value
   with a failing hard rule, zero units, or a missing binding (candidate, policy hash,
   account state version, snapshot digest). An invalid approval cannot be constructed.
4. **Gate ordering** (`decision.risk_gate.RiskGate`): assemble → evaluate → audit → shadow.
   If the audit write fails, the approval is downgraded to BLOCKED; an approval that is not
   on the record does not exist. If the shadow write fails, it is also downgraded, so every
   approved candidate stays measurable.
5. **Audit chain.** Records are SHA-256 hash-chained (sequence, time, kind, subject, canonical
   payload, previous hash). The JSONL sink fsyncs each record and re-verifies the whole chain
   on open; a tampered file refuses to load. Trading-state changes are audited *before* they
   take effect, and refused transitions are audited too.

## Consequences
- A full disk or a broken audit store stops trading. This is intended.
- Superseded decisions appear twice in the chain (APPROVED, then the BLOCKED downgrade).
  Consumers must treat the latest record for a decision id as authoritative.
