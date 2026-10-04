# ADR 0005 — Continuous learning is research; the production policy is versioned and frozen

- **Status:** accepted
- **Date:** 2026-10-04

## Context
"The system must continuously learn" and "the system must never rewrite its own risk rules"
only fit together if learning and changing behaviour are separate activities.

## Decision
1. **Learning discovers; it does not apply.** The learning layer analyses shadow, paper and
   live outcomes and produces *proposals with evidence* (`learning.proposals.Proposal`).
   It receives only a read-only `PolicyReader`.
2. **The production policy is a frozen, content-addressed version.** Every decision records
   the policy id and SHA-256 it ran under; `R-SYS-02` blocks if the policy in use differs
   from the approved active one.
3. **Promotion path for any change:** backtest → out-of-sample evaluation → paper trading →
   human approval through `risk.governance` → activation. Tightening activates immediately;
   anything that loosens, or mixes tightening and loosening, waits 24 h. Loosening is refused
   while HALTED or in a drawdown above 5 %. Fields with no clear safer direction count as
   loosening.
4. **Automatic adaptation only tightens, and only as written into the approved policy.**
   The drawdown throttle (multipliers ≤ 1, non-increasing) and automatic trading-state
   transitions (tighten-only) are rules a human approved. Executing them is not rewriting them.
5. **Shadow trades are first-class.** Every candidate, approved or blocked, is persisted with
   its blocking rules, policy hash and snapshot digest. This is what makes it possible to
   measure the incremental value of each filter later (docs/05 §3.3), instead of assuming
   that every conservative rule helps.

## Enforcement in M1
- import-linter: `sentinel.learning` cannot import `risk.governance`, `risk.state_machine`,
  `store`, `execution`, `decision` or `config`. An AST test checks the same.
- `HumanRiskAdmin` capability: it can only be issued by the governance module after
  step-up verification, and forging one raises.
- **M2 adds the database-level guarantee:** the learning role gets no INSERT/UPDATE grant on
  policy tables (docs/02 §9). Until then the guarantee is code-level only.
