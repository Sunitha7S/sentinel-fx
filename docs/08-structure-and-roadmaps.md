# 08 — Folder Structure and Roadmaps

## 1. Folder structure

```
sentinel-fx/
├── README.md
├── pyproject.toml / uv.lock
├── docker/
│   ├── Dockerfile                   # one Python image, many entrypoints
│   ├── Dockerfile.web
│   └── compose.yaml                 # + compose.paper.yaml, compose.live.yaml overrides
├── config/
│   ├── rulesets/rs_v1.yaml          # human-owned; activated only via DB approval flow
│   ├── instruments.yaml
│   ├── sessions.yaml                # exchange-local session definitions
│   ├── calendar_tier_map.yaml       # event → tier, currencies
│   ├── known_schedules/2026.yaml    # FOMC/ECB/BoE/BoJ/RBA/BoC dates, NFP
│   ├── tripwires.yaml
│   └── settings.example.toml        # no secrets
├── alembic.ini
├── migrations/                      # Alembic: versions/0001-0005 (roles, policy, decisions, audit, market data)
├── src/sentinel/
│   ├── domain/                      # stdlib dataclasses: the contracts. No I/O, no Pydantic (ADR 0002)
│   │   ├── market.py  calendar.py  news.py  session.py  signals.py
│   │   ├── risk.py  portfolio.py  execution.py  learning.py  system.py
│   ├── fxmath/                      # pip values, conversion, sizing (Decimal). No I/O.
│   ├── indicators/                  # ATR, EMA, ADX, swings, levels, regime — shared live/backtest
│   ├── risk/                        # ★ risk kernel: pure, stdlib + domain + fxmath only
│   │   ├── kernel.py  rules/  ruleset.py  sizing.py  exposure.py  ruin.py  state_machine.py
│   ├── perception/
│   │   ├── market_data/             # broker streaming, bar builder, spread sampler
│   │   ├── market_observer.py       # Agent 1
│   │   ├── calendar/                # Agent 2: adapters/, tiering.py, blackout.py, crosscheck.py
│   │   ├── news/                    # Agent 3: adapters/, tripwires.py, classifier.py, novelty.py
│   │   ├── sessions.py              # Agent 4
│   │   ├── portfolio.py             # Agent 9 (continuous)
│   │   └── circuit_breaker.py
│   ├── strategy/
│   │   ├── detectors/               # Agent 5: trend_pullback.py, structure_break_retest.py, ...
│   │   └── validation.py            # Agent 6: gates + score + calibration
│   ├── decision/                    # LangGraph
│   │   ├── state.py  graph.py  nodes/  fail_closed.py  aggregate.py  replay.py
│   ├── explain/                     # canonical templates + optional LLM prose + number validator
│   ├── execution/                   # Agent 10
│   │   ├── service.py  state_machine.py  recheck.py  position_manager.py  reconciler.py
│   │   └── adapters/ (base.py, oanda.py, simulated.py, shadow.py)
│   ├── learning/                    # Agent 8
│   │   ├── shadow_sim.py  segments.py  shrinkage.py  gate_value.py  drift.py
│   │   ├── loss_mining.py  calibration.py  proposals.py  lessons.py
│   ├── backtest/                    # engine.py, cost_model.py, walkforward.py, dsr.py, reports.py
│   ├── llm/                         # LLMClient interface, PydanticAI agents, prompts/ (versioned), budget.py
│   ├── audit/                       # hash-chained audit records (pure)
│   ├── schemas/                     # Pydantic boundary schemas (messages, LLM output)
│   ├── config/                      # settings and policy-file loading
│   ├── store/                       # JSONL sinks; postgres/ (engine, stores, permissions, reports)
│   ├── bus/                         # Redis Streams / PG notify abstraction
│   ├── observability/               # metrics.py, logging.py, tracing.py, heartbeat.py
│   ├── api/                         # FastAPI app, routers/, auth.py, stepup.py
│   └── cli.py                       # `sentinel perception|decide|execute|learn|backtest|replay`
├── web/                             # Next.js operator console (Phase 3)
├── ops/
│   ├── grafana/dashboards/  prometheus/  alertmanager/
│   ├── runbooks/                    # one per P1/P2 alert
│   └── scripts/ (bootstrap.sh, deploy.sh, restore_drill.sh)
├── research/                        # notebooks; may import sentinel.*, never imported by it
├── tests/
│   ├── unit/  property/  golden/  fail_closed/  lookahead/  parity/
│   ├── contract/                    # broker practice-account tests (nightly)
│   ├── llm_eval/                    # labelled + adversarial headline sets
│   └── fixtures/
└── docs/                            # this design
```

**Dependency rule** (enforced with `import-linter`):

```
domain ← fxmath ← indicators ← risk
domain ← strategy, perception, decision, execution, learning, backtest
```

`risk` imports **only** `domain` and `fxmath`. Nothing imports `research/`. `llm` can be imported only by `perception.news`, `explain` and `learning.lessons`.

## 2. Development roadmap (overall)

| Phase | Duration (1 dev, part-time ≈ ×1.7) | Theme | Exit criterion |
|---|---|---|---|
| **P0 Foundations** | 1 wk | Repo, CI, domain models, DB, fxmath | CI green; sizing tests for all 5 pairs |
| **P1 Data + Research** | 3 wks | Historical bid/ask data, indicators, backtester with cost model, detectors | Research report per detector (docs/06 §2.2). **Go/no-go on edge.** |
| **P2 Risk + Decision core** | 3 wks | Risk kernel, calendar agent, session agent, market observer, validation, LangGraph graph, explainer (template) | Fail-closed + property + golden suites green; SHADOW running |
| **P3 Execution + Paper** | 3 wks | OANDA adapter, execution service, reconciler, position manager, portfolio agent, monitoring, alerts | Chaos drills pass; PAPER running |
| **P4 Learning + News** | 3 wks | Shadow simulator, journal, segment stats, gate value, lesson reports; news tripwires → LLM classifier + eval | Weekly lessons generated; news eval meets targets |
| **P5 Console + Governance** | 2 wks | Next.js console, risk change-request workflow, step-up auth, promotion checks | Rule change end-to-end with cooling-off works |
| **P6 Hardening → Live-micro** | ≥ 8 wks of paper (calendar time) | Ops drills, DR restore, cost tuning, docs | Promotion gate PAPER → LIVE_MICRO |

The critical path is **P1's go/no-go**. Everything in P2–P5 is still worth building even without an edge (as a risk/monitoring desk), but **capital is never committed without passing P1 and the promotion gates.**

## 3. MVP roadmap: "Shadow desk" (~6–7 weeks)

The MVP proves the **safety architecture** end to end with live data and **no orders**. Scope is cut hard.

| In MVP | Deferred |
|---|---|
| 2 pairs: EURUSD, USDJPY (different quote currencies exercise the conversion logic) | other 3 pairs (config-only addition later) |
| Market Observer (ATR, trend, swings, levels, spread ratio, regime) | liquidity tick-rate model |
| Calendar: 1 licensed API + known-schedule file + blackout | second calendar source (P3) |
| News: **tripwires only** on central-bank RSS feeds | LLM classifier, cross-check, pgvector |
| Sessions: deterministic windows + rollover + Friday + holidays | statistical session expectancy |
| 2 detectors: `trend_pullback`, `structure_break_retest` | breakout, continuation |
| Validation: hard gates; score = simple prior (uncalibrated, labelled as such) | calibration |
| **Full risk kernel** (all rules, sizing, exposure, state machine) | — (*never* defer risk) |
| LangGraph decision graph, fail-closed, Postgres checkpointer | — |
| Template explanations (TRADE ALLOWED / TRADE BLOCKED) | LLM prose |
| SHADOW execution adapter + shadow-trade simulator | OANDA order adapter |
| Postgres (+Timescale) schema [MVP] tables; PG LISTEN/NOTIFY | Redis |
| Prometheus + Grafana desk dashboard; Telegram alerts | Next.js console |
| CLI: `decide`, `backtest`, `replay`, `kill-switch` | full API |

**MVP acceptance tests:**
1. 4 consecutive weeks of shadow operation with ≥ 99% of H1 runs completed.
2. Each decision is reproducible via `replay`.
3. Every tier-1 event in the period produced a blackout, verified against the actual schedule.
4. The fail-closed suite passes.
5. The approval rate is low and every block has a rule ID. Manual audit of 50 random decisions finds no wrong verdicts.

## 4. Production roadmap

| Stage | Adds | Gate |
|---|---|---|
| **Prod-1 Paper** | OANDA practice adapter, execution service, reconciler, position manager, dead-man's switch, P1/P2 alerting, backups + restore drill, 5 pairs, 2nd calendar source | docs/06 §2.5 SHADOW → PAPER |
| **Prod-2 Intelligence** | News LLM classifier (primary provider) + secondary-provider cross-check + adversarial eval; pgvector novelty; learning engine (segments, gate value, drift, lessons); score calibration once n ≥ 200 | LLM eval targets; 4 weekly lesson reports reviewed |
| **Prod-3 Governance & console** | FastAPI full surface, Next.js console, step-up auth, risk change-request workflow with replay + cooling-off, promotion-check endpoint | End-to-end rule change test; pen-test checklist on auth |
| **Prod-4 Live-micro** | Live token, LIVE_MICRO mode (0.1% risk), slippage model refit on real fills | docs/06 §2.5 PAPER → LIVE_MICRO |
| **Prod-5 Live** | Ruleset risk (≤ 0.5%), monthly scorecard, quarterly chaos + DR drills, annual strategy re-validation on fresh OOS | docs/06 §2.5 LIVE_MICRO → LIVE |
| **Later (only if justified by data)** | third LLM provider (if eval gain), additional detectors, second broker adapter (failover), warm-standby VM | each one goes through the research protocol / eval |

**Things explicitly not on the roadmap:** auto-applied learning, LLM-generated signals, discretionary trade buttons, scalping, martingale/grid/averaging-down (pre-rejected by `R-PTF-03 max_positions_per_pair = 1`).
