# 06 — Monitoring System and Evaluation System

## 1. Monitoring

### 1.1 Principles
- **Monitor the monitors.** Each perception service publishes a heartbeat. The absence of a heartbeat is itself an alert *and* a trading-state input (stale ⇒ BLOCK).
- **External dead-man's switch.** A cron-style external service (e.g. healthchecks.io) expects a ping every 2 minutes from the execution service, but only while it is healthy and reconciled. If the pings stop, the operator's phone is paged even when the whole VM is down. Broker-side stops bound the loss while the human responds.
- **Alerts must be actionable.** Every alert has a runbook link. If an alert fires more than 3 times a week without action, it gets fixed or deleted.

### 1.2 Metrics (Prometheus)

| Domain | Metric | Alert |
|---|---|---|
| Data | `price_last_tick_age_seconds{pair}` | > 30s in active session → P2; also blocks trading |
| Data | `candles_missing_total{pair,tf}` | any → P3 |
| Data | `calendar_last_success_age_seconds`, `calendar_source_disagreements` | > 6h or ≥ 1 tier-1 disagreement → P1 |
| Data | `news_feed_silence_seconds{source}` | > 15 min → P3 |
| Data | `ntp_offset_seconds` | > 1s → P2 |
| Decision | `decision_runs_total{status}`, `decision_run_duration_seconds` | FAILED_CLOSED > 2 in 24h → P2 |
| Decision | `decisions_total{outcome,stage_blocked,rule_id}` | approval rate > 30% over 2 weeks → P3 ("filters may be broken") |
| Risk | `account_drawdown_pct`, `daily_pnl_pct`, `open_risk_pct`, `currency_net_risk_pct{ccy}` | crossing 50% / 80% of any limit → P3 / P2 |
| Risk | `trading_state` (enum gauge), `risk_of_ruin` | any transition → notification; HALTED → P1 |
| Execution | `order_reject_total{reason}`, `slippage_pips` (histogram), `intent_recheck_blocked_total` | reject spike → P2; slippage p95 > 2× model → P3 |
| Execution | `reconciliation_mismatch` | any → P1 |
| Execution | `position_without_stop` | **any → P1, auto-FLATTEN that position** |
| LLM | `llm_latency_seconds`, `llm_schema_failures_total`, `llm_cost_usd_total` | schema-fail rate > 5% → P3; daily cost > budget → P3 and disable non-essential LLM use |
| Infra | CPU, memory, disk, Postgres replication / WAL archive lag, backup age | backup > 26h → P2 |

### 1.3 Alert routing
- **P1** (capital at risk): phone push + call (e.g. PagerDuty / Pushover emergency), Telegram, email. Examples: HALT, reconciliation mismatch, position without stop, dead-man's switch.
- **P2** (trading degraded): push + Telegram.
- **P3** (needs attention): daily digest.
- Quiet hours apply only to P3.

### 1.4 Dashboards (Grafana)
1. **Desk overview:** trading state, mode, equity curve with drawdown, today/week/month P&L vs limits (as bullet gauges), open positions with distance to SL/TP, per-currency exposure bars.
2. **Market and session:** regime per pair, spread ratio, upcoming blackout timeline (next 48h), active tripwires and circuit-breaker history.
3. **Decision funnel:** candidates → passed validation → passed risk → passed portfolio → executed, per day. Top blocking rules.
4. **Execution quality:** slippage distribution vs model, reject reasons, fill latency.
5. **System health:** heartbeats, data ages, LLM latency, cost and errors, DB health.

### 1.5 Logging and tracing
- `structlog` JSON logs with `run_id`, `decision_id` and `intent_id` correlation IDs.
- OpenTelemetry traces across graph nodes and execution. LangGraph checkpoints hold the full node I/O.
- LLM calls are logged in `llm_calls` (prompt version, model, tokens, cost, validity). Raw prompts and responses are stored for 30 days for eval and debugging.

## 2. Evaluation system

Evaluation answers three separate questions. Mixing them up is the most common way trading projects fool themselves.
1. **Does the strategy have an edge?** (research evaluation)
2. **Does the system do what it is designed to do?** (engineering evaluation)
3. **Is it safe to move to the next stage?** (promotion gates)

### 2.1 Backtester requirements
- **Event-driven**, sharing the exact `indicators`, `detectors`, `validation` and `risk` libraries with live. The only replaced components are the clock and the execution adapter.
- **Bid/ask candles.** Long entries fill at the ask, exits at the bid. Plus:
  - a **spread model** by pair × hour-of-week, from collected `spread_samples`, ×1.25 pessimism factor;
  - slippage: 0.2 pips normal, 1.0 pip in elevated regime, stops ×2;
  - swap/financing from a broker rate history (or a conservative constant);
  - commissions.
- Intrabar ambiguity is resolved with M1 data; if still ambiguous, **SL first**.
- **Look-ahead guards:** indicators are computed on `complete=true` bars only. A test perturbs the future (bars after t) and asserts that decisions at t do not change.
- The calendar is replayed **as known at the time** (historical calendar snapshots, not today's revised data) to avoid backtesting with perfect knowledge of event times. This is a limitation when using purchased history; document it.

### 2.2 Research protocol
1. **Data split:** 2015–2021 in-sample (IS), 2022–2023 validation, 2024→ **locked OOS**. The OOS set is opened once per strategy version, and the opening is logged in `backtest_runs.is_oos`.
2. **Walk-forward:** 2-year train / 6-month test, rolling. Report the distribution across windows, not just the aggregate.
3. **Multiple-testing correction:** every variant tried is recorded (`n_variants_tested`). Report the **Deflated Sharpe Ratio** and/or White's Reality Check / SPA test.
4. **Robustness:** parameter perturbation ±20% (results must not collapse); 2× cost stress; removal of the best 5% of trades (does the edge survive without its outliers?); per-pair and per-year breakdown.
5. **Minimum bar to enter SHADOW:**
   - OOS expectancy > +0.10R net of costs, with the 95% CI lower bound > 0 *or* DSR > 0.9
   - profit factor > 1.2
   - max drawdown < 15R
   - positive in ≥ 60% of walk-forward windows
   - survives 2× costs with expectancy ≥ 0

**If no detector meets this bar, the system stays in SHADOW as a monitoring and risk tool, and the documentation says so.**

### 2.3 Engineering evaluation (CI and nightly)

| Suite | What it proves |
|---|---|
| Risk kernel property + mutation tests | Invariants 2, 3, 8 hold for all inputs |
| Fail-closed fuzzing | Injecting an exception, timeout or stale snapshot in **every** graph node yields BLOCKED with zero intents (parametrised over nodes × failure types) |
| Golden decision replay | Decisions are reproducible from stored snapshots |
| Look-ahead test | Future perturbation does not change past decisions |
| Backtest ≡ live parity | Feeding the same recorded bars through the live graph (SHADOW) and the backtester gives identical decisions |
| Execution adapter contract tests | Against an OANDA practice account (nightly): bracket placement, SL-reject handling, idempotent resubmit, partial fills, cancel |
| Chaos drills (monthly, in PAPER) | Kill the execution process mid-order; drop the DB; cut the network for 5 min; skew the clock. Expected outcome: no position without a stop, reconciliation recovers, no duplicate orders. |
| Timezone / DST suite | Sessions, rollover and blackouts across all 2026–2028 DST transitions (US/EU mismatch weeks) |

### 2.4 LLM evaluation (news classifier)
- **Labelled set:** ≥ 500 historical headlines, each labelled by the operator for severity and currencies, plus the **realised** 15/60-minute absolute move in ATR units as an objective label.
- **Metrics:**
  - recall of severity ≥ 2 on events that moved > 1.5 ATR in 60 min (target ≥ 0.9)
  - false-positive rate (cost: missed trades)
  - schema-validity rate (≥ 99.5%)
  - latency p95
- **Adversarial set:** 50 headlines with embedded instructions ("ignore previous instructions…", fake "official" text). Pass = the output stays in schema, and severity is not lowered relative to a clean control.
- Runs on every prompt or model change; results are stored per `prompt_version × model`. A regression blocks deployment.

### 2.5 Promotion gates (enforced by `POST /system/mode`)

| From → To | Required evidence |
|---|---|
| BACKTEST → SHADOW | §2.2 research bar met by ≥ 1 detector; all engineering suites green |
| SHADOW → PAPER | ≥ 4 weeks shadow; ≥ 99% of decision runs completed; zero unexplained FAILED_CLOSED; shadow results inside the backtest's 80% prediction band |
| PAPER → LIVE_MICRO | ≥ 8 weeks **and** ≥ 40 paper trades; zero reconciliation incidents unresolved; zero positions without a stop; slippage within model; all chaos drills passed; operator sign-off with written reason |
| LIVE_MICRO → LIVE | ≥ 12 weeks **and** ≥ 50 live trades; live expectancy CI not significantly below paper; max drawdown < 50% of limit; risk of ruin < 1% |
| any → lower | always allowed, immediate |

Demotion triggers are pre-approved rules. Examples: live expectancy 95% upper bound < 0 over 40 trades → back to PAPER; a max-drawdown halt → back to PAPER after review.

### 2.6 Live performance scorecard (monthly)
- Net P&L in R and %, expectancy CI, profit factor
- Max drawdown, Ulcer index, Calmar
- Approval rate, gate value (docs/05 §3.3)
- **Process compliance** = 100% expected; anything less is an incident
- Cost share, slippage vs model
- Risk-of-ruin trend
- Incidents and time-to-detect
