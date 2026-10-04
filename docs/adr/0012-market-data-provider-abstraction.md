# ADR 0012 — Market data behind a read-only provider port

- **Status:** accepted
- **Date:** 2026-10-04

## Context
Historical bid/ask candles are needed from 2015 onwards. OANDA's practice API was the
assumed source, but its availability depends on the account's region, and a broker's data
API should not become an architectural dependency.

## Decision
- `perception.market_data.provider.MarketDataProvider` is the port: one method,
  `fetch_candles(symbol, timeframe, start, end)`, yielding validated `CandleBatch`es.
- **OANDA practice** (`OandaProvider`) is the primary implementation. **Dukascopy**
  (`DukascopyProvider`) is the declared fallback. It exists in the same port but is not
  implemented in M2; its terms of use must be confirmed first.
- The ingestion pipeline, schema, gap analysis and reports depend only on the port.
  Changing provider means writing one class.
- **Read-only by construction (INV-MD-READONLY).** The OANDA client has a request hook that
  refuses anything except `GET /v3/instruments/{INSTRUMENT}/candles` over HTTPS on the
  practice host. Order, trade, position, account and streaming endpoints, the live host and
  plain HTTP are rejected before a request leaves the process. The live environment is
  refused at construction.
- No credentials are committed. The token is read from `SENTINEL_OANDA_TOKEN`, never
  logged, and kept out of `repr` and error messages.
- **Spreads** are per-bar: ask − bid at each bar's open and close, the only simultaneous
  pairs a candle carries. Sampled spreads (median within a bar) need tick data and belong
  to the live spread sampler in a later milestone.
- **Completeness** for M1/M5/H1 is measured against an enumerated expected grid: Sunday
  17:00 to Friday 17:00 New York, excluding 25 December and 1 January. H4/D1 are aligned to
  New York and shift with DST, so they are measured per trading week.

## Consequences
- The holiday list is an assumption about the provider's calendar. Bars that appear where
  the calendar says "closed" are counted as *unexpected* in the report, so a wrong
  assumption is visible rather than hidden.
- OANDA omits M1 bars for minutes without ticks. Short gaps (≤ 5 bars) are therefore normal
  and reported separately from material gaps.
