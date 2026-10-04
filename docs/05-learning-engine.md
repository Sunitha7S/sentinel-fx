# 05 — Learning Engine Design (Agent 8)

## 1. Purpose and hard boundary
The learning engine answers four questions:
- **Which sessions perform best?**
- **Which pairs perform best?**
- **Which setups perform best?**
- **Which conditions produce losses?**

It also answers a fifth, which is the most important for a capital-preservation system: **are our filters actually saving money?**

**Boundary:** it reads everything and writes only `lesson_reports`, `learning_proposals` and `trade_journal` enrichments. It has no runtime authority, no write access to rules, and no write access to detector configs. Proposals become changes only through a human action.

## 2. Data sources, ranked by usefulness

| Source | Volume / year (est.) | Bias | Use |
|---|---|---|---|
| **Shadow trades** (all candidates, approved or blocked, simulated) | 500–2,000 | Simulated fills; the cost model must be pessimistic | Main source. Gate evaluation. Setup / session statistics. |
| Paper trades | 50–150 | Practice-account fills are better than live | Execution-quality calibration |
| Live trades | 50–150 | Real; small n | Ground truth for slippage and costs; final verdicts |
| Backtest OOS | thousands | Model risk, survivorship of ideas | Priors for shrinkage; risk-of-ruin when n is small |

Shadow simulation uses the **same** bracket logic and cost model as the backtester, M1 bars for intrabar SL/TP ordering, and the **pessimistic assumption that the SL is hit first** when SL and TP fall inside the same M1 bar.

### Features captured at decision time (`signal_candidates.features`)
pair, side, detector@version, session, minutes since session open, H4/D1 trend state, ADX, ATR percentile, volatility regime, spread ratio, distance to nearest opposing level (ATR units), level touches, news score (per leg), minutes to next tier-1/tier-2 event, day of week, rr_net, quality score and its components, open positions count, currency exposure before the trade.

Capturing these **at decision time** prevents look-ahead in later analysis.

## 3. Analytics pipeline

```mermaid
flowchart LR
  A[Resolve shadow trades<br/>nightly 22:15 UTC] --> B[Journal enrichment<br/>MAE/MFE, exit reason, process grade]
  B --> C[Segment statistics<br/>with hierarchical shrinkage]
  C --> D[Gate value analysis<br/>counterfactual]
  C --> E[Drift detection<br/>CUSUM on R]
  D & E & C --> F[Candidate findings]
  F --> G[Multiple-testing control<br/>Benjamini–Hochberg q ≤ 0.10]
  G --> H[Replay backtest of each proposed change]
  H --> I[Proposals w/ evidence]
  C & D & E --> J[Lesson report stats JSON]
  J --> K[LLM narration → numeric validator]
  I & K --> L[(DB) → Console for human review]
```

### 3.1 Metrics (per segment, rolling-60 and full history, by volatility regime)
- n, win rate, **expectancy in R** (mean R), median R
- profit factor
- max drawdown in R, longest losing streak
- average MAE/MFE (shows whether stops are too tight or targets too far)
- **cost share** = costs / gross P&L

### 3.2 Hierarchical shrinkage
Segments form a tree: `global → detector → detector×session → detector×session×pair`. For expectancy, use an empirical-Bayes normal–normal model:

```
θ̂_cell = w·x̄_cell + (1−w)·θ̂_parent,     w = n / (n + k),    k = σ²_within / τ²_between
```

`k` is estimated from the data with a floor of 20. A cell with 6 trades is therefore mostly its parent's estimate. That is the correct behaviour, and it is what stops the system from "learning" that Tuesday-Tokyo-AUDUSD breakouts are magic.

Win rate uses a Beta-Binomial prior centred on the parent. Reports always show **posterior mean and 80%/95% credible intervals**, never a bare point estimate.

### 3.3 Gate value analysis (counterfactual): are the filters worth it?
For each rule `r` and each blocked candidate whose **only** blocking rule was `r` ("sole-blocker set"):
- `E[R | blocked solely by r]` vs `E[R | approved]`, with a bootstrap CI.
- **Saved loss** = −Σ R of the sole-blocked set. **Opportunity cost** = Σ R of the winners in it.

| Finding | Interpretation | Allowed proposal |
|---|---|---|
| Blocked set expectancy significantly < approved | Gate works | none; report as "gate earning its keep" |
| No significant difference, n ≥ 100 | Gate is neutral (costs opportunities) | `RISK_RULE` review (human), flagged *low priority* |
| Blocked set expectancy significantly > approved | Gate may be harmful | `RISK_RULE` review, **but** the default recommendation is to keep it if the rule is a tail-risk protection (blackout, circuit breaker, loss limits). Their value lives in rare events that shadow statistics underweight. |

**Tail-risk rules are exempt from removal proposals.** Rare-event protections cannot be judged on a few hundred samples. The learning engine reports their opportunity cost for transparency and never proposes loosening them.

### 3.4 Drift detection
- One-sided CUSUM on standardised R per detector. An alarm produces an alert and a `DETECTOR_DISABLE` proposal.
- The **pre-approved** ruleset rule (human-authored) disables a detector automatically when its rolling-40 expectancy 95% upper bound < 0. That is executing a rule, not rewriting one.
- Population-stability index on the feature distributions (ATR percentile, spread ratio) flags regime shifts that make historical stats less relevant.

### 3.5 Loss-condition mining ("which conditions produce losses")
- Fit a **shallow, interpretable** model (depth-3 decision tree or L1-logistic regression) predicting `R < 0` from the decision-time features, on shadow + live data, with time-series cross-validation.
- Only rules (tree leaves) with n ≥ 50, lift ≥ 1.3, stability across ≥ 3 of 4 time folds, and q ≤ 0.10 become `FILTER` proposals, e.g. "block `range_breakout` when ATR percentile < 20".
- **No black-box models.** Gradient boosting or neural nets may be used *offline* to look for signal. A finding from them must be re-expressed as an explicit rule before it can be proposed.

## 4. Proposals

```json
{
  "kind": "FILTER",
  "target": "range_breakout@1.0.3",
  "proposal": {"add_gate": "atr_pctile_h1 >= 20"},
  "evidence": {
    "n_affected": 87, "exp_r_affected": -0.31, "ci95": [-0.52, -0.09],
    "exp_r_rest": 0.12, "q_value": 0.04, "fold_stability": "4/4",
    "replay_backtest_id": "bt_…", "replay_delta": {"exp_r": +0.07, "max_dd_r": -2.1, "trades": -18%}
  }
}
```

| Proposal kind | Who can apply | Path |
|---|---|---|
| `STRATEGY_PARAM`, `FILTER`, `DETECTOR_DISABLE` | Human, in console | New detector version (`semver` bump) → backtest OOS → shadow ≥ 2 weeks → enable |
| `RISK_RULE` | Human with step-up re-auth (`human_admin` DB role) | Change request → replay → approval → cooling-off if loosening (docs/04 §7) |

Proposals expire after 30 days if not reviewed. Rejected proposals are kept, with the reason, and the same proposal is not raised again for 90 days unless the evidence gets ≥ 2× stronger.

## 5. Score calibration (makes the "Quality Score" honest)
- Once ≥ 200 resolved candidates have passed all gates: fit **isotonic regression** from raw score to realised R (and to P(win)). Refit monthly on an expanding window.
- Report a **reliability diagram** and Brier score. If calibration is poor (slope of realised vs predicted < 0.5), the dashboard displays "score uncalibrated — ranking only".
- The validation threshold is then expressed as **calibrated E[R] ≥ +0.10R** rather than "score ≥ 65". Changing the threshold is a `STRATEGY_PARAM` proposal that a human applies.

## 6. Lesson reports
**Weekly** (Sat 08:00 UTC) and **monthly** (1st, 08:00 UTC).

1. Code computes `stats` JSON:
   - performance by segment, gate value, drift alarms, execution quality (slippage vs model), rule-trigger frequency
   - "near misses": approved trades that hit SL within 3 bars, and blocked trades that ran 3R
   - process grade: were all trades rule-compliant? Were there system faults?
2. An LLM (primary provider, stronger model) writes a narrative with a fixed structure:
   - **What happened**
   - **What worked**
   - **What hurt**
   - **What the data cannot yet tell us** (mandatory section listing segments with insufficient n)
   - **Open proposals**
   - **Questions for the operator**
3. **Numeric validator:** every number in the narrative must match a value in `stats` (with tolerance for rounding). Any unmatched number causes a regeneration (max 2 attempts); after that the canonical stats-only report is published. Statements of causality ("X caused losses") are prompted against. The narrative must use "associated with" language and cite the n.
4. Lessons are embedded (pgvector), so the console can answer questions like "show past lessons about breakouts in low volatility".

Example lesson line (generated from stats):
> `trend_pullback` in the London–NY overlap: 41 shadow + 9 live trades, expectancy +0.22R (80% CI +0.05 to +0.39). This is the only segment whose interval excludes zero. Tokyo-session `range_breakout`: n = 14, too few to judge; it is shrunk to the detector average of −0.04R.

## 7. What the learning engine must never do
- Change any runtime config, detector, threshold or rule.
- Train on data that includes the period it is evaluated on (strict time-ordered folds only).
- Report a segment result without n and an interval.
- Use LLM-generated numbers.
- Propose loosening a tail-risk rule (blackouts, circuit breakers, loss limits, drawdown halt, broker-side stops).
