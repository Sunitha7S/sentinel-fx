# Sentinel FX — A Capital-Preservation-First Multi-Agent Forex Decision System

> Working name. The system's job is to say **TRADE BLOCKED** most of the time, and to explain why.

This repository currently contains the **design**. Nothing here is investment advice. The system is meant to run on a broker **practice account** until the evaluation gates in [docs/06-monitoring-and-evaluation.md](docs/06-monitoring-and-evaluation.md) are passed.

## The design in one paragraph

A set of **deterministic services** watch prices, the economic calendar, sessions and the portfolio around the clock and write timestamped snapshots to PostgreSQL. When a bar closes, a **LangGraph decision graph** reads those snapshots, runs rule-based setup detectors, scores each candidate, and passes it through a **pure-function risk kernel**, which has the final say. Any error, stale input or missing approval ends in **BLOCKED** (fail-closed). Approved trades become **execution intents**. A separate execution service re-checks each one against fresh state before it sends a bracket order with a **broker-side stop**. LLMs (a primary provider, plus an independent second provider as a cross-check) do three narrow jobs. They classify untrusted news into a fixed schema, and that output can only make the system **more** cautious. They write the explanations. They narrate learning reports whose numbers come from code. **No LLM sits on the path that lets a trade through. No LLM can write risk rules.**

## Document map (the 16 required outputs)

| # | Required output | Where |
|---|---|---|
| — | **Critical review: weaknesses, challenged assumptions, safer alternatives** | [docs/00-critical-review.md](docs/00-critical-review.md) |
| 1 | Complete architecture (+ tech-stack rationale, per-agent specs) | [docs/01-architecture.md](docs/01-architecture.md) |
| 2 | Database schema | [docs/02-database-schema.md](docs/02-database-schema.md) |
| 3 | Agent communication flow | [docs/03-agent-flow-and-langgraph.md](docs/03-agent-flow-and-langgraph.md) §1 |
| 4 | LangGraph workflow | [docs/03-agent-flow-and-langgraph.md](docs/03-agent-flow-and-langgraph.md) §2 |
| 15 | Data flow diagrams | [docs/03-agent-flow-and-langgraph.md](docs/03-agent-flow-and-langgraph.md) §3 |
| 9 | Risk engine design | [docs/04-risk-engine.md](docs/04-risk-engine.md) |
| 10 | Learning engine design | [docs/05-learning-engine.md](docs/05-learning-engine.md) |
| 11 | Monitoring system | [docs/06-monitoring-and-evaluation.md](docs/06-monitoring-and-evaluation.md) §1 |
| 12 | Evaluation system | [docs/06-monitoring-and-evaluation.md](docs/06-monitoring-and-evaluation.md) §2 |
| 13 | Deployment architecture | [docs/07-deployment-and-api.md](docs/07-deployment-and-api.md) §1 |
| 14 | API design | [docs/07-deployment-and-api.md](docs/07-deployment-and-api.md) §2 |
| 5 | Folder structure | [docs/08-structure-and-roadmaps.md](docs/08-structure-and-roadmaps.md) §1 |
| 6 | Development roadmap | [docs/08-structure-and-roadmaps.md](docs/08-structure-and-roadmaps.md) §2 |
| 7 | MVP roadmap | [docs/08-structure-and-roadmaps.md](docs/08-structure-and-roadmaps.md) §3 |
| 8 | Production roadmap | [docs/08-structure-and-roadmaps.md](docs/08-structure-and-roadmaps.md) §4 |
| 16 | Implementation plan | [docs/09-implementation-plan.md](docs/09-implementation-plan.md) |

## Non-negotiable invariants

These are written as tests before any feature code. See [docs/09](docs/09-implementation-plan.md).

1. **Fail-closed.** Any exception, timeout, stale input or missing approval gives `BLOCKED`.
2. **Risk kernel supremacy.** No path reaches the broker without an `APPROVED` risk decision computed against state no older than its TTL, and the decision is re-checked inside the execution lock.
3. **LLM monotonicity.** LLM outputs can only raise risk scores and add blocks. They can never remove a block or raise position size.
4. **Risk-rule immutability.** Services other than the human-approval endpoint have no database grant to write risk rules. The learning engine can only *propose* changes.
5. **Broker-side protection.** Every order carries a stop-loss that lives at the broker. If the whole system dies, open risk is still bounded.
6. **Broker is the source of truth for positions.** Reconciliation mismatches trigger `NO_NEW_TRADES`.
7. **Every decision is explainable.** Each decision stores every rule evaluated, with the observed value, the threshold, the ruleset hash and the input snapshot IDs.
8. **Size rounds down.** If the computed size falls below the broker minimum, the trade is blocked, never rounded up.

## Development

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run poe check     # ruff, format check, mypy --strict, import layering, tests
uv run poe safety    # only the safety-invariant tests
uv run pytest --cov  # full suite with coverage gate
```

Architecture decisions that refine or deviate from the design are recorded in [docs/adr](docs/adr).
