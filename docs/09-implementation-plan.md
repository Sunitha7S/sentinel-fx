# 09 — Implementation Plan

How the system is built so that safety invariants are **encoded as tests before features**, and every milestone has a bounded, verifiable goal.

## 1. Working method
1. **One milestone at a time, each ending with green CI and its own commit.** A milestone does not start until the previous one has been reviewed.
2. **Tests first for anything under `risk/`, `fxmath/`, `execution/` and the fail-closed wrapper.** Invariant tests are written in M0–M1 and never weakened; changing one requires an explicit note in the commit message and an ADR.
3. **Design review before code** for M5 (risk kernel), M8 (decision graph) and M10 (execution).
4. **Verification is by running things**, not by reading code: `pytest`, `sentinel backtest`, `sentinel replay`, the shadow run in Grafana.
5. **No live broker credentials on development machines.** Practice tokens only, from a git-ignored `.env`. The live token is installed manually on the server.

## 2. Engineering invariants (tests in `tests/safety` and `tests/property` — never weaken them)
1. Fail-closed: any exception, timeout or stale input in the decision path gives BLOCKED and zero intents.
2. `risk/` is pure: no I/O, no `datetime.now()`, no randomness; imports only `domain` and `fxmath`.
3. LLM output may only raise risk scores (merged with `max`); it is never used to create or approve signals.
4. No code path writes protected risk policy except the governance module, using a human capability.
5. Every order has a broker-side stop; an SL rejection cancels the entry.
6. Position size rounds down; below the broker minimum the trade is blocked.
7. Money and prices are `Decimal`; timestamps are UTC-aware; sessions use `zoneinfo`.
8. Indicators use complete bars only; backtest and live share indicators, detectors, validation and risk.

## 3. Milestones

Each milestone lists the **scope**, the **deliverables**, and the **done-when** check, which must be observed by running it, not asserted.

### M0 — Skeleton & CI (½ day)
- **Scope:** uv project, src layout, ruff, mypy strict, pytest + hypothesis, import-linter contracts from docs/08, GitHub Actions, docker compose with Postgres, contributor conventions (§2).
- **Done when:** `uv run pytest` passes (placeholder test), `lint-imports` passes, `docker compose up postgres` is healthy.

### M1 — Domain models + fxmath + invariant tests (1–2 days)
- Pydantic models for every agent output in docs/01 §3.
- `fxmath`: pip size, quote→account conversion, `size_position()` with Decimal and round-down.
- **Write the invariant test files now** (most marked `xfail` until their modules exist):
  - `tests/property/test_risk_invariants.py`
  - `tests/fail_closed/test_graph_fail_closed.py`
  - `tests/lookahead/test_no_lookahead.py`
- **Done when:** the sizing tests reproduce the docs/04 §4 worked examples exactly; a hypothesis test shows `risk ≤ cap` for 10k random cases.

### M2 — Data layer (2–3 days)
- Alembic migrations for the [MVP] tables in docs/02, including roles/grants and append-only triggers.
- Historical bid/ask candle loader (OANDA practice API, M1/H1/H4/D1, 2015→) into `candles`. Build a spread sampler.
- **Done when:**
  - a test proves `svc_learning` gets *permission denied* inserting into `risk_rule_sets`;
  - a test proves `UPDATE decisions` raises;
  - candles have been loaded for EURUSD and USDJPY with a gap report.

### M3 — Indicators + Market Observer (2–3 days)
- ATR, EMA, ADX, fractal swings, S/R clustering, regime percentiles, spread ratio. `MarketHealthReport` builder.
- **Done when:** the look-ahead test passes (perturbing future bars changes nothing at t), and values match a reference implementation on fixtures to 1e-9.

### M4 — Backtester + cost model + detectors (5–7 days) → **edge go/no-go**
- Event-driven engine with simulated adapter; bid/ask fills; spread model; SL-first ambiguity rule; swaps.
- `trend_pullback`, `structure_break_retest` (+ later the other two).
- Walk-forward, DSR, cost stress, parameter perturbation reports into `backtest_runs`.
- **Done when:** the research report for each detector is produced per docs/06 §2.2, with `n_variants_tested` recorded honestly. **Human decision:** proceed / revise / stop.

### M5 — Risk kernel (4–5 days)
- `risk/` per docs/04: ruleset loader with code ceilings + sha256, every rule R-SYS…R-PTF, exposure decomposition, gap scenario, drawdown throttle, state machine, risk of ruin.
- **Done when:**
  - all rule table tests and property tests pass;
  - `mutmut` kills ≥ 90% of mutants;
  - `mypy --strict` passes;
  - there are no imports outside domain/fxmath.

### M6 — Calendar + Session agents (3–4 days)
- Calendar adapter (one licensed API), known-schedule file, tier map, blackout builder, staleness and disagreement → state change.
- Session engine with zoneinfo, rollover, Friday cutoff, holidays; daily `SessionPlan` at 21:30 UTC.
- **Done when:** the DST suite covers 2026–2028 transitions, including the US/EU mismatch weeks, and a replay of a historical month generates blackouts for every tier-1 event.

### M7 — Validation + circuit breaker + news tripwires (2–3 days)
- Hard gates and the prior score; price-based circuit breaker; central-bank RSS adapters + tripwires.
- **Done when:** gate table tests pass, and a replay of 15-Jan-2015 EURCHF data (as a fixture) trips the breaker within one M1 bar.

### M8 — LangGraph decision graph (3–4 days)
- `state.py`, `fail_closed.py`, nodes, aggregation with sequential portfolio re-check, `persist_and_emit` (single transaction + outbox), PostgresSaver checkpointer, template explainer, `replay`.
- **Done when:**
  - the fail-closed suite (every node × {exception, timeout, stale input}) yields BLOCKED with 0 intents;
  - golden replay is deterministic;
  - backtest ≡ live parity holds on a recorded week.

### M9 — SHADOW mode + shadow simulator + monitoring (3–4 days) → **MVP complete**
- Live perception services, decision worker on `bar.closed`, shadow adapter, shadow-trade resolution, Prometheus metrics, Grafana desk dashboard, Telegram alerts, heartbeat + external dead-man's switch.
- **Done when:** MVP acceptance tests (docs/08 §3) begin. They run for 4 weeks of calendar time, while the next milestones proceed.

### M10 — Execution service + reconciler (5–6 days)
- `BrokerAdapter` (OANDA practice): bracket orders, idempotent `client_order_id`, SL-reject ⇒ cancel; execution state machine; recheck with the risk kernel inside the advisory lock; position manager; reconciler; startup sequence (docs/07 §1.3).
- **Done when:**
  - nightly contract tests pass against the practice account;
  - chaos drills pass: kill mid-order, DB down, network cut, duplicate intent;
  - `position_without_stop` never fires.

### M11 — Portfolio agent + risk of ruin (2 days)
- EWMA covariance, VaR, currency exposure, Monte Carlo ruin, `PortfolioHealth`.
- **Done when:** exposure tests (three short-USD trades ⇒ the third is blocked) pass, and the ruin simulation is reproducible with a seed.

### M12 — Learning engine (5–6 days)
- Journal enrichment, segments with shrinkage, gate-value analysis, CUSUM drift, loss mining (shallow tree), calibration, proposals, weekly/monthly lessons (code stats + LLM narration + numeric validator).
- **Done when:**
  - a synthetic-data test plants a known losing condition and the engine proposes the right filter with q ≤ 0.10;
  - a planted noise-only condition produces **no** proposal;
  - the narrative validator rejects an injected wrong number.

### M13 — News LLM classifier (3–4 days)
- PydanticAI agent (primary provider) with an untrusted-content wrapper, secondary-provider cross-check for severity ≥ 2, monotonic merge, pgvector novelty, budget guard; labelled + adversarial eval sets.
- **Done when:** the eval meets docs/06 §2.4 targets and the adversarial set never lowers severity.

### M14 — API + governance + console (6–8 days)
- FastAPI surface (docs/07 §2), OIDC + WebAuthn step-up, risk change-request workflow with replay + cooling-off, promotion-check endpoint, Next.js console (decision explorer, proposal review, ruleset diff/approve).
- **Done when:**
  - an end-to-end test files a LOOSEN request, approves it, and observes `effective_at ≥ +24h`;
  - loosening while in drawdown > 5% is refused;
  - a learning-role token cannot reach governance endpoints.

### M15 — Hardening and promotion
- Backups + restore drill script, deploy script with NO_NEW_TRADES handshake, runbooks for every P1/P2 alert, the 8-week paper period, monthly scorecard.
- **Done when:** `/evaluation/promotion-check?to=LIVE_MICRO` passes on real evidence, and the operator signs off.

## 4. Definition of done (every milestone)
- [ ] Tests added first for new rules or contracts; all suites green locally and in CI
- [ ] `mypy --strict` (core) and `lint-imports` pass
- [ ] Docs updated if a contract, rule ID or table changed
- [ ] For risk/execution changes: adversarial review listing every path to an order without a fresh APPROVED decision or a broker-side stop; findings resolved
- [ ] Commit message states which invariants were touched (ideally none)
