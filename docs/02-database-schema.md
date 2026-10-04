# 02 — Database Schema

PostgreSQL 16 with the TimescaleDB, pgvector and pgcrypto extensions. Migrations are managed by Alembic. The DDL below is the target schema; the MVP subset is marked **[MVP]**.

## Design principles
- **Append-only for anything auditable.** Decisions, rule results, orders, fills and ruleset versions are never `UPDATE`d. State changes are new rows. Triggers reject `UPDATE`/`DELETE` on audit tables.
- **Every decision references the exact snapshot IDs it read**, which is how replay is possible.
- **Hash-chained decision log**: `decisions.hash = sha256(prev_hash || canonical_json(row))`. Tampering or silent edits are detectable.
- **Money as `NUMERIC`**, prices as `NUMERIC(18,8)`. Never floats in persisted financial values.
- **All timestamps `TIMESTAMPTZ` in UTC.**
- **Role separation enforced by grants** (§9). The learning and LLM roles physically cannot write risk rules.

## 1. Reference data

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE trade_side AS ENUM ('LONG','SHORT');
CREATE TYPE decision_outcome AS ENUM ('APPROVED','BLOCKED');
CREATE TYPE system_mode AS ENUM ('BACKTEST','SHADOW','PAPER','LIVE_MICRO','LIVE');
CREATE TYPE trading_state AS ENUM ('ACTIVE','NO_NEW_TRADES','FLATTEN','HALTED');

-- [MVP]
CREATE TABLE instruments (
  symbol          TEXT PRIMARY KEY,            -- 'EUR_USD'
  base_ccy        CHAR(3) NOT NULL,
  quote_ccy       CHAR(3) NOT NULL,
  pip_size        NUMERIC(10,8) NOT NULL,      -- 0.0001 / 0.01 for JPY
  min_units       NUMERIC NOT NULL,
  unit_step       NUMERIC NOT NULL,
  max_spread_pips NUMERIC NOT NULL,            -- absolute hard cap
  active          BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE trading_holidays (
  day DATE NOT NULL, market TEXT NOT NULL, kind TEXT NOT NULL,   -- 'closed' | 'thin'
  PRIMARY KEY (day, market)
);
```

## 2. Market data

```sql
-- [MVP]
CREATE TABLE candles (
  symbol    TEXT NOT NULL REFERENCES instruments,
  timeframe TEXT NOT NULL,                     -- M1,M5,H1,H4,D1
  ts        TIMESTAMPTZ NOT NULL,              -- bar open time
  o_bid NUMERIC(18,8), h_bid NUMERIC(18,8), l_bid NUMERIC(18,8), c_bid NUMERIC(18,8),
  o_ask NUMERIC(18,8), h_ask NUMERIC(18,8), l_ask NUMERIC(18,8), c_ask NUMERIC(18,8),
  tick_volume INT,
  complete  BOOLEAN NOT NULL,                  -- never compute signals on incomplete bars
  source    TEXT NOT NULL,
  PRIMARY KEY (symbol, timeframe, ts)
);
SELECT create_hypertable('candles','ts', chunk_time_interval => INTERVAL '30 days');

CREATE TABLE spread_samples (                  -- 1/min summary, not raw ticks
  symbol TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL,
  spread_min NUMERIC, spread_med NUMERIC, spread_max NUMERIC, ticks INT,
  PRIMARY KEY (symbol, ts)
);
SELECT create_hypertable('spread_samples','ts');

-- [MVP] Agent 1 output
CREATE TABLE market_health_reports (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  symbol      TEXT NOT NULL REFERENCES instruments,
  as_of       TIMESTAMPTZ NOT NULL,
  regime      TEXT NOT NULL,                   -- low|normal|elevated|extreme
  trend_state JSONB NOT NULL,                  -- per timeframe: direction, ema alignment, adx
  structure   JSONB NOT NULL,                  -- swings, HH/HL state
  levels      JSONB NOT NULL,                  -- [{price, kind, touches, age_bars}]
  atr         JSONB NOT NULL,                  -- {H1:..., H4:..., D1:..., pctile:...}
  spread_ratio NUMERIC NOT NULL,
  liquidity_score NUMERIC NOT NULL,
  data_quality TEXT NOT NULL,                  -- OK|DEGRADED|STALE
  healthy     BOOLEAN NOT NULL,
  reasons     JSONB NOT NULL,
  code_version TEXT NOT NULL
);
CREATE INDEX ON market_health_reports (symbol, as_of DESC);

CREATE TABLE circuit_breaker_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ts TIMESTAMPTZ NOT NULL, symbol TEXT, trigger TEXT NOT NULL,   -- sigma_move|spread_blowout|tick_gap|multi_pair_jump
  observed NUMERIC, threshold NUMERIC, action trading_state NOT NULL,
  resolved_at TIMESTAMPTZ, resolved_by TEXT
);
```

## 3. Calendar, news and sessions

```sql
-- [MVP]
CREATE TABLE economic_events (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source        TEXT NOT NULL,
  source_event_id TEXT NOT NULL,
  currency      CHAR(3) NOT NULL,
  title         TEXT NOT NULL,
  category      TEXT NOT NULL,                 -- CPI|NFP|RATE_DECISION|GDP|PMI|...
  tier          SMALLINT NOT NULL CHECK (tier IN (1,2,3)),
  scheduled_at  TIMESTAMPTZ NOT NULL,
  forecast TEXT, previous TEXT, actual TEXT,
  fetched_at    TIMESTAMPTZ NOT NULL,
  UNIQUE (source, source_event_id, fetched_at)
);
CREATE INDEX ON economic_events (scheduled_at);

CREATE TABLE risk_calendar_snapshots (         -- Agent 2 output
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  as_of TIMESTAMPTZ NOT NULL,
  blackout_windows JSONB NOT NULL,             -- [{currency,start,end,tier,event_ids,reason}]
  source_agreement BOOLEAN NOT NULL,
  feed_fresh BOOLEAN NOT NULL,
  discrepancies JSONB NOT NULL
);

-- [Phase 2]
CREATE TABLE news_items (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source       TEXT NOT NULL,
  source_item_id TEXT NOT NULL,
  published_at TIMESTAMPTZ NOT NULL,
  received_at  TIMESTAMPTZ NOT NULL,
  headline     TEXT NOT NULL,
  body         TEXT,
  url          TEXT,
  embedding    vector(1024),                   -- dedup / novelty
  UNIQUE (source, source_item_id)
);
CREATE INDEX ON news_items USING hnsw (embedding vector_cosine_ops);

CREATE TABLE news_classifications (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  news_item_id UUID NOT NULL REFERENCES news_items,
  classifier   TEXT NOT NULL,                  -- 'tripwire@1.0' | 'primary:<model>@prompt_v3' | 'secondary:<model>@prompt_v3'
  output       JSONB NOT NULL,                 -- validated NewsClassification
  severity     SMALLINT NOT NULL,
  latency_ms   INT, cost_usd NUMERIC(10,6),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE news_risk_snapshots (             -- Agent 3 output
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  as_of TIMESTAMPTZ NOT NULL,
  per_currency JSONB NOT NULL,                 -- {USD:{score, tripwires[], top_items[]}, ...}
  feed_fresh BOOLEAN NOT NULL
);

CREATE TABLE session_plans (                   -- Agent 4 output
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  trading_date DATE NOT NULL,
  as_of TIMESTAMPTZ NOT NULL,
  per_session JSONB NOT NULL,
  best_session TEXT, avoid_sessions TEXT[] NOT NULL,
  reasons JSONB NOT NULL
);
```

## 4. Risk governance (immutable, human-controlled)

```sql
-- [MVP]
CREATE TABLE risk_rule_sets (
  id           TEXT PRIMARY KEY,               -- 'rs_v7'
  content      JSONB NOT NULL,                 -- full ruleset (from YAML), canonicalised
  sha256       TEXT NOT NULL UNIQUE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by   TEXT NOT NULL,                  -- human identity only
  parent_id    TEXT REFERENCES risk_rule_sets
);

CREATE TABLE risk_rule_change_requests (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  proposed_ruleset_id TEXT NOT NULL REFERENCES risk_rule_sets,
  origin        TEXT NOT NULL,                 -- 'human' | 'learning_proposal:<uuid>'
  diff          JSONB NOT NULL,
  direction     TEXT NOT NULL CHECK (direction IN ('TIGHTEN','LOOSEN','MIXED')),
  evidence      JSONB,
  status        TEXT NOT NULL CHECK (status IN ('PENDING','APPROVED','REJECTED','ACTIVE','SUPERSEDED')),
  approved_by   TEXT, approved_at TIMESTAMPTZ,
  effective_at  TIMESTAMPTZ,                   -- LOOSEN ⇒ ≥ approved_at + 24h, and next trading-day boundary
  CHECK (status <> 'APPROVED' OR approved_by IS NOT NULL)
);

CREATE TABLE active_ruleset (                  -- exactly one row
  singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
  ruleset_id TEXT NOT NULL REFERENCES risk_rule_sets,
  sha256 TEXT NOT NULL,
  activated_at TIMESTAMPTZ NOT NULL,
  change_request_id UUID REFERENCES risk_rule_change_requests
);

-- [MVP]
CREATE TABLE system_state_log (                -- append-only; current = latest row
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  mode system_mode NOT NULL,
  trading_state trading_state NOT NULL,
  reason TEXT NOT NULL,
  actor TEXT NOT NULL,                         -- 'human:<id>' | 'auto:<rule_id>'
  halt_requires_human BOOLEAN NOT NULL DEFAULT false
);
```

## 5. Decisions (the audit core)

```sql
-- [MVP]
CREATE TABLE decision_runs (                   -- one LangGraph run per bar close
  id UUID PRIMARY KEY,
  trigger TEXT NOT NULL,                       -- 'bar_close:H1:2026-10-05T13:00Z'
  as_of TIMESTAMPTZ NOT NULL,
  mode system_mode NOT NULL,
  state_version BIGINT NOT NULL,               -- monotonic portfolio/account version read
  input_snapshot_ids JSONB NOT NULL,           -- {market:[...], calendar:..., news:..., session:..., portfolio:...}
  ruleset_id TEXT NOT NULL, ruleset_sha256 TEXT NOT NULL,
  code_version TEXT NOT NULL,                  -- git sha
  langgraph_thread_id TEXT NOT NULL,
  status TEXT NOT NULL,                        -- COMPLETED|FAILED_CLOSED
  errors JSONB NOT NULL DEFAULT '[]'
);

CREATE TABLE signal_candidates (
  id UUID PRIMARY KEY,
  run_id UUID NOT NULL REFERENCES decision_runs,
  symbol TEXT NOT NULL, side trade_side NOT NULL,
  detector TEXT NOT NULL,                      -- 'trend_pullback@1.2.0'
  entry_type TEXT NOT NULL,
  entry NUMERIC(18,8) NOT NULL, stop_loss NUMERIC(18,8) NOT NULL, take_profit NUMERIC(18,8) NOT NULL,
  rr_gross NUMERIC NOT NULL, rr_net NUMERIC NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  features JSONB NOT NULL                      -- decision-time feature vector for learning
);

CREATE TABLE decisions (                       -- final verdict per candidate (or run-level block)
  id UUID PRIMARY KEY,
  run_id UUID NOT NULL REFERENCES decision_runs,
  candidate_id UUID REFERENCES signal_candidates,
  outcome decision_outcome NOT NULL,
  stage_blocked TEXT,                          -- PRE_GATE|VALIDATION|RISK|PORTFOLIO|SYSTEM_ERROR
  quality_score NUMERIC, calibrated_exp_r NUMERIC,
  approved_units NUMERIC, risk_pct NUMERIC,
  explanation_canonical TEXT NOT NULL,
  explanation_prose TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL UNIQUE
);

CREATE TABLE rule_results (                    -- every rule, every decision — no short-circuit
  decision_id UUID NOT NULL REFERENCES decisions,
  stage TEXT NOT NULL,                         -- VALIDATION|RISK|PORTFOLIO|EXECUTION_RECHECK
  rule_id TEXT NOT NULL,                       -- 'R-ACC-04'
  passed BOOLEAN NOT NULL,
  severity TEXT NOT NULL,                      -- HARD|SOFT
  observed JSONB, threshold JSONB,
  message TEXT NOT NULL,
  PRIMARY KEY (decision_id, stage, rule_id)
);

CREATE TABLE approvals (                       -- the three keys Execution requires
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  decision_id UUID NOT NULL REFERENCES decisions,
  authority TEXT NOT NULL CHECK (authority IN ('VALIDATION','RISK','PORTFOLIO')),
  state_version BIGINT NOT NULL,
  issued_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  payload_sha256 TEXT NOT NULL,                -- hash of (candidate, size, ruleset_sha)
  UNIQUE (decision_id, authority)
);
```

## 6. Execution, positions and account

```sql
-- [MVP]
CREATE TABLE execution_intents (               -- outbox consumed by Execution Service
  id UUID PRIMARY KEY,                         -- also broker client_order_id
  decision_id UUID NOT NULL UNIQUE REFERENCES decisions,
  status TEXT NOT NULL,                        -- RECEIVED|VERIFYING|BLOCKED|SUBMITTING|SUBMITTED|FILLED|PARTIAL|REJECTED|EXPIRED|CLOSED
  status_history JSONB NOT NULL DEFAULT '[]',
  recheck_rule_results JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE orders (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  intent_id UUID NOT NULL REFERENCES execution_intents,
  broker_order_id TEXT,
  kind TEXT NOT NULL,                          -- ENTRY|SL|TP|CLOSE|MODIFY_SL
  symbol TEXT NOT NULL, side trade_side NOT NULL, units NUMERIC NOT NULL,
  price NUMERIC(18,8), status TEXT NOT NULL,
  request JSONB NOT NULL, response JSONB,
  ts TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE fills (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id UUID NOT NULL REFERENCES orders,
  broker_fill_id TEXT UNIQUE,
  ts TIMESTAMPTZ NOT NULL, price NUMERIC(18,8) NOT NULL, units NUMERIC NOT NULL,
  commission NUMERIC NOT NULL DEFAULT 0, financing NUMERIC NOT NULL DEFAULT 0,
  slippage_pips NUMERIC
);

CREATE TABLE trades (                          -- one row per round-trip position
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  intent_id UUID NOT NULL UNIQUE REFERENCES execution_intents,
  symbol TEXT NOT NULL, side trade_side NOT NULL,
  opened_at TIMESTAMPTZ NOT NULL, closed_at TIMESTAMPTZ,
  entry_price NUMERIC(18,8) NOT NULL, exit_price NUMERIC(18,8),
  initial_stop NUMERIC(18,8) NOT NULL, units NUMERIC NOT NULL,
  risk_amount NUMERIC NOT NULL,                -- account ccy at entry
  pnl NUMERIC, r_multiple NUMERIC, mae_r NUMERIC, mfe_r NUMERIC,
  exit_reason TEXT                             -- SL|TP|TIME|BREAKEVEN|MANUAL|FLATTEN|WEEKEND|EVENT
);

CREATE TABLE account_snapshots (
  ts TIMESTAMPTZ NOT NULL, mode system_mode NOT NULL,
  balance NUMERIC NOT NULL, equity NUMERIC NOT NULL, margin_used NUMERIC NOT NULL,
  unrealized_pnl NUMERIC NOT NULL, peak_equity NUMERIC NOT NULL, drawdown_pct NUMERIC NOT NULL,
  state_version BIGINT NOT NULL,
  PRIMARY KEY (mode, ts)
);
SELECT create_hypertable('account_snapshots','ts');

CREATE TABLE portfolio_health_snapshots (     -- Agent 9 output
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  as_of TIMESTAMPTZ NOT NULL, state_version BIGINT NOT NULL,
  status TEXT NOT NULL,                        -- HEALTHY|CAUTION|CRITICAL
  currency_exposure JSONB NOT NULL,            -- {USD:{risk_pct:-0.8, notional:...}, ...}
  open_risk_pct NUMERIC NOT NULL, var_99_1d_pct NUMERIC, gap_risk_pct NUMERIC,
  corr_matrix JSONB, risk_of_ruin NUMERIC, reasons JSONB NOT NULL
);

CREATE TABLE reconciliation_runs (
  id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  matched BOOLEAN NOT NULL, discrepancies JSONB NOT NULL, action_taken TEXT
);
```

## 7. Learning

```sql
-- [MVP] — shadow trades are cheap and are the main data source for learning
CREATE TABLE shadow_trades (
  candidate_id UUID PRIMARY KEY REFERENCES signal_candidates,
  decision_outcome decision_outcome NOT NULL,
  blocking_rules TEXT[] NOT NULL,              -- empty if approved
  simulated_entry_at TIMESTAMPTZ, simulated_exit_at TIMESTAMPTZ,
  filled BOOLEAN, r_multiple NUMERIC, mae_r NUMERIC, mfe_r NUMERIC, exit_reason TEXT,
  cost_model_version TEXT NOT NULL,
  resolved_at TIMESTAMPTZ
);

CREATE TABLE trade_journal (                   -- one per live/paper trade, enriched post-close
  trade_id UUID PRIMARY KEY REFERENCES trades,
  features JSONB NOT NULL,                     -- copied from candidate at decision time
  session TEXT NOT NULL, regime TEXT NOT NULL, news_score_max NUMERIC,
  process_grade TEXT,                          -- A: rules followed; B: deviation; C: system fault
  notes TEXT,
  embedding vector(1024)
);

CREATE TABLE lesson_reports (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  period TEXT NOT NULL,                        -- 'weekly' | 'monthly'
  period_start DATE NOT NULL, period_end DATE NOT NULL,
  stats JSONB NOT NULL,                        -- computed by code; source of truth
  narrative TEXT,                              -- LLM; numbers validated against stats
  narrative_validation JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  embedding vector(1024),
  UNIQUE (period, period_start)
);

CREATE TABLE learning_proposals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  kind TEXT NOT NULL CHECK (kind IN ('STRATEGY_PARAM','FILTER','DETECTOR_DISABLE','RISK_RULE')),
  target TEXT NOT NULL,                        -- e.g. 'range_breakout@1.0.3' or 'R-SES-02'
  proposal JSONB NOT NULL,
  evidence JSONB NOT NULL,                     -- n, effect, CI, q-value, replay backtest id
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','ACCEPTED','REJECTED','EXPIRED')),
  reviewed_by TEXT, reviewed_at TIMESTAMPTZ,
  change_request_id UUID REFERENCES risk_rule_change_requests   -- only via human action
);

CREATE TABLE backtest_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  config JSONB NOT NULL, code_version TEXT NOT NULL, data_range TSTZRANGE NOT NULL,
  is_oos BOOLEAN NOT NULL, n_variants_tested INT NOT NULL,
  metrics JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## 8. Operations

```sql
CREATE TABLE llm_calls (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  purpose TEXT NOT NULL,                       -- NEWS_CLASSIFY|NEWS_XCHECK|EXPLAIN|LESSON
  provider TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
  input_tokens INT, output_tokens INT, latency_ms INT, cost_usd NUMERIC(10,6),
  schema_valid BOOLEAN NOT NULL, retries SMALLINT NOT NULL DEFAULT 0, error TEXT
);

CREATE TABLE incidents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  opened_at TIMESTAMPTZ NOT NULL DEFAULT now(), closed_at TIMESTAMPTZ,
  severity TEXT NOT NULL, component TEXT NOT NULL, summary TEXT NOT NULL,
  trading_state_action trading_state, postmortem TEXT
);
```

## 9. Roles and grants (immutability enforced in the database)

```sql
CREATE ROLE svc_perception;   -- market, calendar, news, session services
CREATE ROLE svc_decision;     -- LangGraph worker
CREATE ROLE svc_execution;    -- execution + reconciler
CREATE ROLE svc_learning;     -- learning jobs, LLM narration
CREATE ROLE svc_api;          -- FastAPI (read + narrow writes)
CREATE ROLE human_risk_admin; -- used only by the re-authenticated approval endpoint

REVOKE ALL ON risk_rule_sets, risk_rule_change_requests, active_ruleset FROM PUBLIC;
GRANT SELECT ON risk_rule_sets, active_ruleset TO svc_decision, svc_execution, svc_learning, svc_api;
GRANT INSERT ON risk_rule_change_requests TO svc_api;            -- humans file requests via API
GRANT INSERT, SELECT ON risk_rule_sets TO human_risk_admin;
GRANT UPDATE (status, approved_by, approved_at, effective_at) ON risk_rule_change_requests TO human_risk_admin;
GRANT UPDATE ON active_ruleset TO human_risk_admin;
-- svc_learning: INSERT on learning_proposals only; no access path to rule tables beyond SELECT.

-- Append-only enforcement
CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS
$$ BEGIN RAISE EXCEPTION 'append-only table %', TG_TABLE_NAME; END $$;
CREATE TRIGGER decisions_immutable BEFORE UPDATE OR DELETE ON decisions
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
-- same for rule_results, approvals, risk_rule_sets, fills, system_state_log
```

## 10. Retention
- Raw ticks are **not stored**; only per-minute spread summaries and M1 bars.
- `candles`: compress chunks older than 7 days, keep indefinitely (the backtest corpus).
- Decisions, rule results, orders, fills and journal: retain indefinitely (audit).
- `llm_calls`: 1 year.
