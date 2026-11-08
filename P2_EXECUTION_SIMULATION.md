# P2 execution and risk scenarios

`execution_simulation_report.py` adds an offline long-only scenario simulator. It reads P0's canonical SQLite view through P1's **read-only loader**, and never imports the scanner, broker, order functions, credentials or account configuration. P0/P1 behavior and the live trading path are unchanged. No new third-party dependencies.

## Run

```powershell
python execution_simulation_report.py --db trading_engine.db --config simulation/scenarios.example.json --horizon 5 --output execution_report.md --json-output execution_report.json
```

The example file contains a zero-cost reference and a fictional friction scenario. **Every example rate/probability/amount is illustrative; none is a statement of current brokerage fees, statutory tax rates, liquidity, or investment advice.** Supply explicit assumptions appropriate to the experiment. All fields are required; unknown/missing fields, duplicate scenario names and invalid/nonfinite settings fail visibly. JSON numbers must be numbers, not quoted numeric strings.

Choose one of 5/10/30 minutes per simulation so the same candidate is not counted as three simultaneous trades. Optional `--setup`, `--rule-version`, `--start-date` (inclusive Korea-local date), `--end-date` (exclusive) select a fixed experiment. Example holdout selection:

```powershell
python execution_simulation_report.py --db trading_engine.db --config simulation/scenarios.example.json --horizon 10 --start-date 2026-12-01 --setup YOUR_SETUP --output execution_report.md
```

Dates/setup/horizon/scenarios should be fixed before examining the holdout. Each filtered run starts with fresh initial cash and no inherited positions: it is not a continuation of a previous run. A missing or pre-P0 database fails without being created/upgraded. Reports cannot use the DB/config path or each other's output path. Explicit report outputs may replace older reports. There is no production research DB in this repository; verification uses synthetic data only.

## Explicit execution arithmetic

One basis point (bp) = 0.01%. `spread_bps` is a full spread, with half charged adversely on each side; `slippage_bps` is additional adverse movement **per side**. With entry reference E, exit reference X, quantity q, and a = (spread_bps / 2 + slippage_bps) / 10,000:

- Assumed buy price = E * (1 + a); assumed sell price = X * (1 - a).
- Requested whole shares = floor(budget / (buy price * (1 + buy_fee_bps / 10,000))). Fees therefore cannot cause the entry to overspend its budget.
- The entry fills with configured `fill_probability`. On success, q = floor(requested shares * `fill_fraction`); the remainder cancels immediately. Zero-share partials are not positions. A no-fill uses no cash/fees and does not consume the filled-trade daily limit.
- Entry outlay = q * buy price plus buy fee. Exit proceeds = q * sell price minus sell fee and sell tax, each calculated on sell notional. No minimum fee, account rounding, tax treatment, tick-size rounding or additional latency is inferred.
- Net P&L = exit proceeds minus entry outlay. Net return = net P&L / entry outlay * 100. The gross comparator uses **unrounded reference prices** for the same closed position quantity, not an executable-return claim or separately sized zero-cost portfolio.
- For each closed trade, gross reference P&L minus adverse-price cost minus explicit fees/tax equals net P&L. Aggregate closed cost drag and known explicit fees/tax (including entry fees of unresolved positions) are reported separately.
- Exit fills use `exit_fill_probability`. A failure means the entire position remains unresolved; no partial exit, retries or later invented liquidation is performed.

Fill draws are seeded from stable candidate identity; entry and exit use separate draws. Changing future returns does not change an entry fill draw. These are independent **assumptions**, not estimates from the market. Outcomes may depend on size/liquidity in reality; the model has no such evidence. Running several seeds explores sensitivity, not a calibrated probability distribution.

## Cash chronology and risk controls

Candidates sort by entry receipt time, code, rule version and identity. At a shared timestamp all known exits settle before new entries; simultaneous candidate priority is artificial and documented rather than optimized after seeing returns.

Entry budget = min(available cash, cost-basis equity * `position_fraction`). Cost-basis equity = available cash plus entry outlays of open positions; it is not current liquidation value. Cash is reserved on entry and cannot fund overlapping entries. No leverage, shorting, pyramiding or fractional shares. A symbol already open cannot be re-entered. `max_positions` and `max_trades_per_day` limit filled positions/entries.

`daily_loss_limit_pct` compares completed-trade net P&L with the day's opening cost-basis equity. Entry fees are included in that completed-trade P&L on exit, not treated as a separate risk-stop event at entry. `max_consecutive_losses` counts consecutive negative completed trades within the date; a positive result resets it, a zero result leaves it unchanged. Once either loss limit triggers, new entries remain blocked for the rest of that date even if an already-open position subsequently recovers losses. Counters/latches reset on a new entry date; existing positions and reserved cash do not. Limits do not forcibly close positions or promise a cap on intratrade/daily losses.

No future return, TP/SL label, extrema or exit-quality status chooses whether to enter. Valid entry metadata is required, but a missing/stale/invalid/legacy exit outcome does **not** disqualify that entry in advance. If filled, it remains open with reserved cash indefinitely through this simulation. A later candidate can consequently be blocked by this unknown position. This is deliberately conservative bookkeeping, not a claim that real holdings never exit.

## Reading the output

Markdown gives per-scenario gross/net closed-trade distributions, cost drag, fill/skip counts, dates, realized net P&L, remaining cash/reserved outlay and risk diagnostics. JSON additionally contains every decision, closed position, unpriced position and exit ledger. Input/config fingerprints, seed and all scenario/policy assumptions are included for reproduction.

- `terminal_equity` and `portfolio_return_pct` are **null** when any filled position has no priced exit. Unpriced positions are not assigned a zero return, last-price mark or zero value. Known cash and realized P&L alone are not the total portfolio result.
- `max_cost_basis_drawdown_pct` uses the exit-event ledger of cash plus reserved entry outlay, from initial capital. It is a realized cost-basis diagnostic; actual marked-to-market drawdown, intratrade losses, market impact and liquidation risk remain unknown.
- Net-return summary statistics use only completed scenario trades. P1's date-cluster bootstrap conventions/default sample thresholds are reused for those conditional returns, not as an interval for terminal equity or a guarantee of prospective risk. Capital and risk decisions can couple observations across dates.
- `NO_ELIGIBLE_ENTRIES`, `NO_FILLED_POSITIONS`, `ALL_FILLED_POSITIONS_CLOSED` and `INCOMPLETE_UNPRICED_POSITIONS` distinguish empty/no-fill/completed/unpriced outcomes. A zero no-trade return is not evidence of a strategy.
- Quality counts and invalid-entry exclusions are separate. Candidate exit quality still follows P0/P1's verified horizon rules; weekend/regular-session checks do not replace the configured holiday calendar.

No automatic profitability verdict is produced. Actual spread/depth, queue priority, correlated fills, cancellations, halts, variable latency, tick schedules and broker fee/tax rounding need better market/account data. P2 provides explicit sensitivity scenarios and cash/risk accounting, not a validated execution backtest. It does not collect live data or alter orders.

## Verification

```powershell
python -m unittest discover -s tests -v
```

Tests check hand-calculated costs and P&L conservation, whole-share/partial/nonfills, exit failure, chronological cash reservation, same-time settlement, symbol/position/daily limits, loss-limit latching/reset, entry decisions independent of future outcomes, unresolved positions across dates, deterministic seeds/order, metadata rejection, CLI/date filters, safe output paths, required config fields, unchanged source bytes, missing DB behavior, and P0/P1 regressions. No live account/scanner/broker process is executed. If the Windows temporary directory is restricted, set TEMP and TMP to a writable workspace directory.
