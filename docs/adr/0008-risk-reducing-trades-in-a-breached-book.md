# ADR 0008 — Risk-reducing trades remain possible in a breached currency book

- **Status:** accepted
- **Date:** 2026-10-04
- **Supersedes:** the M1 behaviour of R-PTF-01 (any currency above its limit after the trade → BLOCKED)

## Context
A book can exceed the per-currency net-risk limit without any new trade, for example when
equity falls and existing exposure grows as a share of it. Under the M1 rule every trade
touching that currency was blocked, including one that *reduces* the breach. Blocking a
hedge leaves the book more exposed than necessary.

## Decision
R-PTF-01 passes for each currency the trade touches if either:
- `|net after| <= limit`, or
- `|net after| < |net before|` (strictly closer to zero).

A trade that leaves a breached currency unchanged or worse is still blocked. Only the
currencies the candidate touches are judged; a breach in an unrelated currency is not this
trade's to fix, and it cannot be made worse by it.

## Consequences
- **Gross limits still apply.** R-PTF-02 (total stop-risk), R-PTF-04 (gap loss) and R-TRD-05
  (leverage, margin) are gross: a hedge adds to them. Under rs_v1 (1.0 % net, 1.5 % total,
  0.5 % per trade) a book over a currency limit is already above 1.0 % total risk, so the
  hedge also fails R-PTF-02. The new behaviour is reachable only if the gross limits leave
  room. Tests cover both cases. Whether hedges may exceed gross limits is an open policy
  decision.
- Property test: under relaxed gross limits, an approved trade never moves a touched
  currency further from zero while it is above the limit.
