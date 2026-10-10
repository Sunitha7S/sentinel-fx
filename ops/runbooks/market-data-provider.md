# Runbook: market-data provider check and fallback decision (M2.1 step 0)

Run this before any bulk historical ingestion, and again whenever the token or the account
changes. The check is read-only, makes a few small fixed requests, and never touches the
database.

## 1. Prepare
- Create an OANDA **practice** (demo) account and generate a personal API token for it.
  Never use a live-account token: the provider refuses the live host anyway.
- Set the token only in the shell that runs the check. Never commit it or paste it into
  files under the repository (`.env` is git-ignored if you prefer a file).

```powershell
$env:SENTINEL_OANDA_TOKEN = "<practice token>"
uv run --no-sync sentinel provider-check
```

## 2. Read the verdict

| Exit | Verdict | Meaning | Next step |
|---|---|---|---|
| 0 | `OK` | Both pairs, recent and 2015 M1 history, D1 aligned to 17:00 New York | Ingest into a **disposable** database first (§3) |
| 1 | `INSUFFICIENT_DATA` | Reachable, but history is missing or short | Do not ingest. Decide (§4) |
| 1 | `UNEXPECTED_DATA` | Misaligned bars, failed validation, or incomplete bars in a closed window | Do not ingest. Report the output; needs a reviewed fix with a regression test |
| 1 | `ERROR` | Network, server or provider error | Retry later; investigate if it persists |
| 2 | — | No token set; nothing was requested | Set the token |
| 3 | `ACCESS_DENIED` | HTTP 401: token invalid or revoked. HTTP 403: account may not use the API (regional restriction) | 401: regenerate the token. 403: do not retry repeatedly; go to §4 |

## 3. After `OK`
1. Point `SENTINEL_DATABASE_URL` at a **disposable** database (the tables are append-only
   even for the owner, so a bad load cannot be cleaned up in place).
2. Ingest, then run the strict data-quality report.
3. Only after the report passes and has been reviewed, repeat into the real database.

## 4. Fallback decision path (requires human approval)
Taken only on `ACCESS_DENIED` (403) or an `INSUFFICIENT_DATA` that a shorter history cannot
absorb.

1. **Record the evidence:** the full check output (it never contains the token).
2. **Option A — shorter history from OANDA.** Acceptable only if M1 history still covers
   enough distinct volatility regimes for the M4 walk-forward. Decision by the operator.
3. **Option B — Dukascopy (declared fallback, ADR 0012).** Before any code is written, the
   operator confirms that Dukascopy's current terms of use permit downloading and storing
   historical data for private research, and records the decision. Then the provider is
   implemented behind the same port with its own tests.
4. **Never mix providers within one series.** A series is registered to the provider that
   first wrote it; the database refuses rows from another source.
5. **No workaround** that hides the account's location, shares someone else's token, or
   scrapes a site whose terms forbid it.
