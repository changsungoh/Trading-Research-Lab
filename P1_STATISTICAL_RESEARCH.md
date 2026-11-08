# P1 statistical research engine

`quant_research_report.py` reads a **P0-upgraded SQLite database in read-only mode** and produces Markdown and/or JSON. It does not initialize/migrate the database, import trading modules, contact any service, collect benchmark data, place orders or alter scanner behavior. It uses only the Python standard library. Existing report entry points are unchanged.

## Run

From the repository directory:

```powershell
python quant_research_report.py --db trading_engine.db
python quant_research_report.py --db trading_engine.db --output quant_report.md --json-output quant_report.json
```

The DB must exist and contain P0's `canonical_signal_research` view and observation columns. Missing/unupgraded databases fail visibly and are never created or upgraded by the report. Raw historical duplicates remain untouched; the P0 canonical view chooses their earliest committed representative.

No actual research database is included in the repository. Tests use synthetic inputs; implementing this report does not establish a strategy edge.

## What the report measures

For 5/10/30-minute horizons, overall and by setup, rule version and Korea-local entry hour:

- Total candidates, per-status counts and fractions, excluded observations, and valid coverage. Each horizon uses its own denominator. Missing/stale/legacy outcomes are never zero-filled.
- Observation count, distinct entry dates, mean, median, strictly-positive win rate, sample standard deviation (`n-1`), and linearly interpolated 5/25/75/95% quantiles.
- Equal-weight-per-date mean alongside the primary observation-weighted mean, plus date-by-date distributions. A busy day therefore remains visible rather than silently becoming many independent experiments.
- A seeded 95% percentile bootstrap interval for the observation-weighted mean, resampling entire entry-date clusters with replacement. Each sampled date brings all its observations. Default: 2,000 replications, seed 1729, at least 30 valid observations across at least 20 dates. These thresholds are a reporting policy, not proof of statistical adequacy. Fewer samples/dates yield `INSUFFICIENT_DATA` and a null interval; constant bootstrap results yield `DEGENERATE`, not evidence of certainty.
- A content fingerprint of all input research rows (independent of their order), a separate normalized benchmark fingerprint, selection options and resampling configuration, enabling reproduction with the same input and Python runtime. JSON uses null rather than NaN/Infinity. Source data paths are not included in the report.

Only `VALID` observations enter statistics. The report additionally checks finite positive entry/observed prices, finite returns consistent with P0's two-decimal rounding, exact target = entry + horizon, receipt within [target, target + 60 seconds], and same-day weekday regular-session bounds. A claimed VALID row failing these checks is `INVALID_METADATA`. Legacy rows remain `LEGACY_UNVERIFIED` even if they contain historical returns. Session holiday validity still relies on P0's collection policy and configured closure dates.

Returns, standard deviations, quantiles, excess returns and CI bounds use **percentage points** (1.0 means +1%, not +100%). JSON win rates, quality rates and coverage are fractions. Zero return is not a win. No annualization, portfolio sizing or trading-cost assumption is applied.

## Exploration and holdout

Fix a hypothesis (setup, rule version, horizon, benchmark and split date) **before** examining the holdout. For example, replacing the illustrative selection/date with your predeclared choices:

```powershell
python quant_research_report.py --db trading_engine.db --setup YOUR_SETUP --rule-version YOUR_RULE --horizon 10 --holdout-start 2026-12-01 --output quant_report.md
```

The cutoff is a Korea-local calendar date. Exploration includes entry dates strictly before it; holdout includes dates on/after it. Observations crossing into the holdout are excluded from exploration. Valid P0 regular-session horizons normally cannot cross midnight. Invalid/undated candidates remain in the combined quality counts and are excluded from dated partitions. The `all` partition mixes dates and is descriptive only. Without a cutoff, the report has only the exploratory `all` partition.

Every output is explicitly **exploratory**. The CLI does not seal a holdout, preserve a preregistration, correct multiple comparisons, select a winning configuration, calculate p-values or declare an edge. Looking at a holdout and then changing a setup invalidates its untouched status. Seeing many setup/hour/horizon intervals does not make any one of them a corrected significance test.

## Optional paired market benchmark

There is no verified index feed in the current engine. The report therefore defaults to `NOT_PROVIDED`; it does not fabricate market returns or subtract a daily index return from an intraday signal. To supply an independently prepared interval-return CSV:

```csv
benchmark,entry_time,observed_time,return_pct
KOSPI,2026-11-09 10:00:00,2026-11-09 10:05:20,0.4
```

```powershell
python quant_research_report.py --db trading_engine.db --benchmark-csv benchmark_intervals.csv --benchmark-name KOSPI --output quant_report.md
```

Match is exact on the signal's entry and **actual observed** timestamps, not nominal target timestamps. Naive times are Korea local; explicit offsets normalize to Korea local, preserving microseconds. Both endpoints must match. No nearest-neighbor matching, interpolation, forward filling or substitution is performed. A benchmark return must be finite and greater than -100%, its end later than its start; duplicate intervals for the selected benchmark fail rather than arbitrarily choosing one. Other named benchmarks are ignored.

For matched observations, excess = stock return minus benchmark return, in percentage points. Both the matched-subset stock distribution and paired-excess distribution use the same samples. The report gives matched/unmatched counts and `NO_EXACT_MATCHES` when supplied data cannot align. Benchmark data provenance, endpoint price validity and index suitability remain the supplier's responsibility; declaring matching timestamps alone does not prove measurement accuracy. Exact microsecond coverage may be sparse. This difference is relative performance, not beta-adjusted alpha. Benchmark collection/integration remains future work.

## Limits and operation

Day clustering accounts for within-day dependence, including overlapping signals, but not persistent dependence across trading dates. Percentile bootstrap coverage can be poor with few clusters, nonstationarity or selection bias. Missing quotes may depend on liquidity or price movement. Small samples, wide intervals and degenerate intervals cannot establish an edge. Gross receipt-price changes are not executable returns, realized trades or returns after fees/slippage; those belong to P2.

P1 adds new offline files only. It changes neither P0 database schema nor live-order/secret files. Input paths cannot be used as report output paths. JSON/Markdown output files may overwrite prior reports when explicitly selected; generated reports and benchmark CSVs are ignored by Git.

## Verification

```powershell
python -m unittest discover -s tests -v
```

Tests cover known analytical distributions, day-cluster intervals, reproducibility, sparse/degenerate data, independent horizon quality, invalid metadata, canonical duplicates, holdout splits and filters, exact benchmark matching, timezone/duplicate validation, CLI artifacts, source-overwrite rejection, read-only database hashes, absent/old databases, and all P0 regressions. They never execute a live scanner or broker. On Windows with a restricted temporary directory, set TEMP/TMP to a writable workspace directory.
