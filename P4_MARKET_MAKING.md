# P4 market-making research laboratory

`market_making_report.py` adds a **separate offline, single-asset market-making simulation**. It never launches the scanner, contacts a broker, reads account credentials, places an order or changes P0/P1/P2/P3. Only Python standard-library modules are required.

P0's sparse 5/10/30-minute observations cannot reconstruct an order book or taker flow. P4 therefore uses an explicit synthetic model, or a supplied event tape with the same documented semantics. Neither source is automatically a validated execution backtest. There is no live data collection or exchange connector in this change.

## Run

```powershell
python market_making_report.py --config simulation/market_making.example.json --output market_making_report.md --json-output market_making_report.json
```

The illustrative example compares symmetric and inventory-skewed quotes on identical synthetic tapes for seeds 11, 29 and 47. Rates, volatility, spread, flow probabilities, capital and inventory limits are fictional assumptions, not current fees, exchange specifications or recommended quoting settings. Policies are not ranked or selected automatically. Output paths cannot overwrite inputs or each other; explicitly selected report outputs can replace older reports.

Config requires exactly `market`, `policies`, `seeds`, with all dataclass fields supplied. Unknown/missing fields, invalid/nonfinite JSON numbers, duplicate policy names/seeds, boolean quantities, nonpositive initial values and inconsistent initial midpoint fail visibly. Policy initial midpoint must match the market's initial midpoint. Numeric strings are not accepted. One abstract instrument is simulated independently per run; there is no multistock allocation or order routing.

## Causal event sequence

1. Estimate fair value from **previously observed mids**. Initially this is the explicitly supplied `initial_mid`; after each step, update `fair = alpha * observed_mid + (1-alpha) * prior_fair`.
2. Place one bid and one ask based on that estimate and current inventory/cash. The step's incoming flow and new midpoint are unavailable to this decision.
3. Reveal this step's taker side, limit price and quantity. A taker buy crosses an ask when its limit >= ask; a taker sell crosses a bid when its limit <= bid. Fill at the maker's quoted price, up to the smaller of incoming quantity and quoted size. The remaining incoming flow is ignored, not carried forward.
4. Observe this step's new midpoint, mark inventory, update the fair-value estimate and risk status. The next event gets fresh quotes; old quotes do not persist across events.
5. Compute future markout diagnostics **after the full decision loop**, never as policy inputs.

This deliberately exposes stale-quote/information risk over one abstract event interval. There is no elapsed-time calibration, exchange priority, queue position, price-time matching, competing maker, tick schedule, market impact, persistent order life, cancel acknowledgement or real fill-probability estimate. Crossing flow fills deterministically in this toy model; it is not a claim of actual execution.

## Inventory-sensitive quotes and constraints

Let q be signed inventory, Q maximum absolute inventory, f the causal fair-value estimate, and s configured inventory skew in basis points:

`reservation center = f * (1 - s / 10,000 * q / Q)`

Bid/ask are center minus/plus `f * half_spread_bps / 10,000`. Bid rounds down and ask up to the configured constant tick; ask remains strictly above bid and bid stays positive. Positive inventory shifts both quotes down, encouraging sales and discouraging further purchases. Negative inventory shifts both up. This is an explicit heuristic, not an optimal-control or calibrated fair-pricing model.

Whole-share quoted quantities obey quote size, maximum absolute inventory, nonnegative purchase cash including fee, and a prequote inventory-notional cap based on current marked equity times `max_notional_fraction`. A disabled side has size zero. The synthetic account **permits bounded short inventory**; short sale cash is offset by the negative inventory mark, not counted as profit. Securities lending availability, borrow costs, interest, taxes, account margin and short-sale rules are outside this model. No actual short order is sent.

At each step equity = cash + signed inventory * newly observed midpoint. A drawdown from running equity peak reaching `max_drawdown_pct`, or nonpositive equity, halts all subsequent quoting permanently for that run. Existing inventory remains and keeps being marked; the halt does not invent a liquidation or guarantee a maximum loss. Price jumps can breach notional/drawdown limits before another decision is possible. Limits are quote-time controls, not a model of broker liquidation.

## Synthetic market and supplied tapes

The generated midpoint follows positive multiplicative shocks:

`new_mid = previous_mid * exp((drift_bps + Normal(0, volatility_bps)) / 10,000)`

At each step an arrival occurs with `arrival_probability`. An informed arrival uses this step's already drawn price-shock direction and new midpoint to select its side and willingness to pay; an uninformed arrival chooses a random side around the previous midpoint. Limit-price reach is `taker_reach_bps`; whole-share size is uniform from 1 to `max_taker_quantity`. Thus informed flow can hit a stale quote before the maker observes the shock. It sees no later shocks. These are simulation assumptions, not estimates about actual informed trading.

Generate each seed's tape once, then replay it for every policy. Changing a maker policy does not alter the tape; this supports paired comparisons but assumes no maker impact or flow response. Repeatability means same configuration, seed and Python runtime; different seeds do not establish calibrated confidence intervals.

For an explicit tape:

```powershell
python market_making_report.py --config simulation/market_making.example.json --tape supplied_tape.json --output market_making_report.md
```

The JSON array uses contiguous integer `step` values starting at 1. Each midpoint is the **post-quote event observation**, not an input available before that step's quote:

```json
[
  {"step": 1, "mid": 99.8, "taker_side": "sell", "limit_price": 99.5, "quantity": 5},
  {"step": 2, "mid": 100.0, "taker_side": "none", "limit_price": 100.0, "quantity": 0}
]
```

Sides are `buy`, `sell`, `none`; no-order quantity must be zero. Mid/limit must be finite positive numbers and quantity a nonnegative integer. Tapes have abstract event steps, not automatically parsed exchange timestamps. Supply a separate consistent initial midpoint through the config; replay cannot determine whether your event construction itself used hindsight. Empty/malformed/reordered/skipped-step tapes fail. A supplied deterministic tape runs once per policy with seed null; configured synthetic seeds do not create fake independent replay samples.

## Output and interpretation

Markdown compares fills, residual inventory, marked P&L, fees, marked drawdown and markouts. JSON also stores configuration, tape/config fingerprints, every quote, fill and equity observation. The initial cash is the initial equity; all fees are charged on absolute fill notional, with no rounding/minimums/rebates.

- `marked_equity = ending_cash + ending_inventory * final_mid`. This is final-mid marking, **not liquidation proceeds or realized profit** when inventory remains. `terminal_flat` identifies zero residual inventory. Short liabilities are included.
- `quoted_edge_total` sums signed quantity * (fair at quote - fill price), with buy quantity positive and sell quantity negative. It is an estimate-relative edge, not guaranteed captured spread. A positive edge can coexist with a loss.
- `fair_revaluation_pnl` is the exact residual so `quoted_edge_total + fair_revaluation_pnl - total_fees = marked_pnl`. It includes subsequent price/estimate changes and is not a pure estimate of adverse selection or realized inventory profit.
- Immediate markout = signed quantity * (this event's observed midpoint - fill price). Future markout uses the midpoint `markout_steps` later; `future_mid_move` measures the signed midpoint change after the fill event. Negative values show an adverse price move in that synthetic/replayed sample, not proof of its cause.
- Future markouts unavailable near tape end remain null; fills remain in inventory/accounting. Mean future markout is per **fill record** in monetary units, quantity-weighted within each record, excludes fees and is not a per-share or annualized statistic. Counts are shown. Overlapping markouts are not independent and their sum is not portfolio P&L.
- `max_marked_notional` is the maximum observed absolute inventory midpoint value, which may exceed its quote-time cap after jumps. A halted account can continue losing value.

There is no true fundamental-price oracle exposed to the policy, no parameter fitting, holdout sealing, statistical significance claim, automatic winner selection or production deployment. Freeze hypotheses/settings and use unseen tapes/seeds rather than repeatedly choosing the best-looking run. Actual market-making validation requires trustworthy synchronized quotes/trades, order-lifecycle/queue data, venue rules and realistic cost/latency assumptions.

## Verification

```powershell
python -m unittest discover -s tests -v
```

Tests include analytical spread/fee round trips, cash and short-liability accounting, tick rounding, inventory-skew direction, inventory/cash/notional controls, noncrossing flow, adverse markouts and missing tail diagnostics, current/future information exclusion, prefix invariance, irreversible quote halts with residual risk, P&L identities, seeded/shared tapes, deterministic supplied replay, CLI artifacts/read-only sources/config safety, and all P0-P3 regressions. No live scanner, broker or external API is executed. If Windows restricts temporary storage, set TEMP/TMP to a writable workspace directory.
