# ADR 0010 — Plain PostgreSQL for M2; TimescaleDB deferred

- **Status:** accepted
- **Date:** 2026-10-04
- **Deviates from:** docs/02, which created `candles`, `spread_samples` and `account_snapshots`
  as TimescaleDB hypertables.

## Context
M2 stores M1/H1/H4/D1 bid/ask candles for EURUSD and USDJPY from 2015 onwards. At roughly
370,000 M1 bars per pair per year, that is about 8–10 million rows, plus per-bar spreads.

## Decision
Use plain PostgreSQL 16:
- `market_candles` and `spreads` keyed by `(symbol, timeframe, ts)`, which is the access
  path for every query in M2–M4 (one symbol, one timeframe, a time range);
- a BRIN index on `ts` for cross-symbol scans, which costs almost nothing on append-only,
  time-ordered data;
- no compression, continuous aggregates or retention policies yet.

## Why this is sufficient
- The primary-key B-tree serves range scans for a single series directly.
- Ten million narrow rows is a small table for PostgreSQL; a full EURUSD M1 history fits
  in memory on the planned VM.
- The tables are append-only, so there is no update or vacuum churn to manage.
- One fewer extension keeps CI (the stock `postgres:16` image) and local development identical.

## Revisit when
- tick or sub-minute data is stored (the M9 spread sampler), or
- more than ~10 symbols, or
- range queries over M1 history exceed ~1 s in the backtester.

Moving to hypertables is then a migration of these two tables only; their keys already
lead with time-series dimensions.
