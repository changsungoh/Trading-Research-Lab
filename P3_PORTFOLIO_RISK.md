# P3 allocation and portfolio concentration research

`portfolio_risk_report.py` compares explicit allocation rules across P2 execution scenarios using P0 SQLite observations. It uses P1's read-only loader, P2's cash chronology, whole-share fills, unresolved-position handling and realized-risk stops. It never imports a broker, reads credentials, fetches prices or sends orders. No third-party dependency or DB migration is added.

## Run

```powershell
python portfolio_risk_report.py --db trading_engine.db --config simulation/allocation.example.json --horizon 5 --output portfolio_report.md --json-output portfolio_report.json
```

The example has three allocation policies and two fictional execution scenarios. Values are **illustrative research assumptions**, not recommended allocations or current fees/taxes. `ILLUSTRATIVE_GROUP_A/B` are demonstration group labels, not verified sector classifications. Replace them with your explicit mapping before interpreting sector concentration.

Config has exactly five required objects/arrays: `portfolio`, `scenarios`, `allocations`, `sectors`, `stresses`. The shared `portfolio` fields are P2's initial cash, common per-position fraction cap, maximum positions, filled entries per day, daily realized loss limit and daily consecutive-loss limit. All P2 execution scenario fields remain required. Arrays must be nonempty, names unique within each array, unexpected/missing fields rejected. Sector mapping is `{ "stock_code": "sector_label" }`; retain leading-zero codes. A stress sector absent from the mapping fails visibly to catch misspellings.

Optional `--setup`, `--rule-version`, `--start-date` (inclusive), `--end-date` (exclusive), and `--seed` fix the experiment. Dates are Korea local. Each filtered comparison restarts with fresh cash, not positions inherited from earlier dates. Fix the choices before inspecting holdout results. A missing/unupgraded DB fails without creating/migrating it; outputs cannot overwrite inputs or share a path.

## Allocation rules

Each allocation object supplies all fields, including parameters unused by that mode so configuration has one explicit schema:

- `fixed_cash`: request `fixed_cash` monetary units per entry.
- `fixed_fraction`: request current cost-basis equity times `fraction`.
- `volatility_target`: request current cost-basis equity times `target_volatility_pct / max(estimated_volatility_pct, volatility_floor_pct)`.

Cash and prices must use the same monetary unit. For example, target 0.1 percentage points divided by estimated standard deviation 1 percentage point requests 10% of equity before caps. This is an inverse-volatility sizing **heuristic for the selected signal horizon**, not a calibrated portfolio volatility target, Kelly fraction, forecast of loss or stop-distance risk budget.

Requested budgets are capped by available cash, P2's shared `position_fraction` cap, `max_symbol_fraction`, remaining `max_sector_fraction` and remaining `max_gross_fraction`. Limits use **entry cash outlay including buy fees**, not reference notional. Whole shares and partial fills can leave unused budget. P2 still blocks a symbol already open; no pyramiding/leverage/shorting is added. No-fill attempts consume no exposure.

Unknown sector behavior is explicit: `unknown_sector_policy = "block"` rejects the entry, while `"bucket"` places every unknown symbol into the same `UNKNOWN` concentration bucket. Themes are never interpreted as sectors. Supplied static sector labels have no automatically verified historical effective dates; the supplier must avoid hindsight in classification.

Caps govern additional entries. If realized losses reduce the equity denominator, previously opened positions may exceed a configured fraction; they are not forcibly rebalanced. Post-exit snapshots expose such breaches. Realized loss limits cannot guarantee intratrade drawdown bounds.

## Past-only volatility

History is canonical, same-symbol, same-horizon **verified signal returns**, filtered by the selected setup/rule. It is not a series of daily stock returns. Each entry considers only outcomes with actual observed timestamp **strictly earlier** than that entry. Even observations at exactly the entry timestamp are excluded conservatively. No current trade's eventual outcome or later observation chooses its size.

Use the most recent `lookback` eligible observations. Sample standard deviation (`n - 1`) is measured in percentage points; require at least `min_observations` (>=2) and `min_days` distinct entry dates. Missing/stale/unverified/invalid outcomes are not imputed. Insufficient history blocks volatility sizing with `INSUFFICIENT_PAST_VOLATILITY`, rather than falling back to a favorable constant. A positive `volatility_floor_pct` prevents zero-history-dispersion leverage.

Date selection limits **trades**, not warmup history. Earlier eligible rows remain available for sizing, but rows whose outcomes have not completed by the decision time cannot enter its estimate. History/input/config fingerprints, counts and each decision's sample count, date count, last history timestamp and volatility are recorded. History can be selectively missing, overlapping and strategy-dependent; inverse-volatility sizing does not remove these biases.

## Concentration and simultaneous stress

After every filled entry and priced exit, record cash, cost-basis equity, total gross exposure fraction, symbol/sector fractions and sector HHI (sector shares within invested outlay). JSON includes the snapshots, decisions, trades and open positions. Peak symbol, sector and gross fractions are reported across these snapshots. This is cost-basis co-exposure, not an empirically estimated return-correlation matrix.

Each explicit stress defines `market_return_pct` and `sector_returns_pct`, with downside returns between -100 and 0%. Apply to all simultaneously open positions at each snapshot:

`stressed reference price = entry reference price * (1 + market shock/100) * (1 + sector shock/100)`

Market and sector shocks compound: -10% market and -20% sector means -28% reference-price movement, not -30%. Apply the execution scenario's adverse sell price, sell fee and sell tax to full hypothetical liquidation. A -100% shock yields zero proceeds without inventing a positive price. Compare cash plus stressed proceeds with the same snapshot's cost-basis equity; retain the worst loss fraction, timestamp and monetary loss by sector per stress.

Stress liquidation is a **counterfactual assumption**, independent of fill probabilities, not a claim that liquidation is possible. Prices are shocked from entry references because validated contemporaneous market marks are unavailable. This is not marked-to-market stress, VaR, expected shortfall, ruin probability or a calibrated worst-case bound. Sector losses also include execution friction and can be positive even for a flat shock. Market impact, tax/fee rounding, tick sizes and liquidity remain P2 limitations.

Unresolved holdings remain unpriced in the main simulation. Stresses can analyze their entry exposure, but never replace missing exits, release reserved cash or fill in terminal equity/return. Closed-trade distributions remain conditional on known exits. Realized risk policy and cost-basis drawdown retain P2's conventions.

## Output and interpretation

Markdown compares allocation x execution scenario outcomes, completeness, realized net P&L, terminal return when all holdings are priced, exposure peaks, decision counts and worst simultaneous stress losses. JSON adds all assumptions, configurations and audit detail. No winner is ranked or selected automatically. Results do not establish strategy profitability, and changing policies after inspecting a holdout invalidates its untouched status.

Only offline research files and P2's optional allocation interface change. With no allocator, P2's defaults/results retain their original behavior. No actual research DB is shipped or analyzed in this repository; automated verification uses synthetic inputs.

## Verify

```powershell
python -m unittest discover -s tests -v
```

Tests cover analytical sizing differences, history cutoffs and lookback, warmup selection, future-outcome independence, history sparsity/floors, symbol/sector/gross and shared caps, unknown sectors, unresolved positions, compounded/total-loss/cost-inclusive stresses, P2 default compatibility, read-only source bytes, CLI/config validation and P0/P1/P2 regressions. No live account or scanner process runs. If Windows restricts temporary storage, set TEMP/TMP to a writable workspace directory.
