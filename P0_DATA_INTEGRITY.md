# P0 research data integrity

> **Note:** This public repository contains the storage layer (`database.py`), the CSV export/import tools and the P1–P4 research code. The live quote collector that produces P0 observations is not part of this repository; this document describes the observation policy that the database schema and the research tools assume.


Research only: nothing in this repository places orders or reads credentials.

## Observation policy

- All research timestamps are Korea local time (UTC+09:00), independent of the host timezone, with microseconds. Existing second-resolution timestamps remain readable.
- `target_timestamp_5m/10m/30m` is entry time plus the stated horizon. `observed_timestamp_*` is receipt time captured at WebSocket callback entry, not processing time or exchange trade time. The feed currently supplies no validated exchange timestamp to research; transport-delayed quotes cannot be identified from receipt time alone.
- First quote at or after the target, up to and including 60 seconds late, is VALID. Earlier quotes never fill a horizon. Later quotes are STALE and retain diagnostic price/receipt time with NULL return. Expiration after the tolerance without a quote is MISSING, with NULL return and observation time. A timer may finalize MISSING before a subsequent late quote arrives; terminal statuses never change.
- OUT_OF_SESSION applies to targets outside weekday 09:00 <= time < 15:30 Korea local time, including next-day targets. This is a regular-session policy, not a complete exchange calendar. Set comma-separated ISO dates in RESEARCH_CLOSED_DATES for holidays/closures. Special session hours require a policy update. Extended sessions are deliberately excluded from research eligibility.
- A research-only worker expires missing horizons every five seconds, including disconnects. Restart restores active research from SQLite. Invalid/nonpositive/nonfinite prices and backward/duplicate receipt times are ignored.
- Extrema and TP/SL first touches only use received quotes inside the original 30-minute window and session. These remain sampled-path statistics, not continuous-path or executable-trade guarantees. Gaps can miss extrema/touches.

## Storage and recovery

- SQLite owns active state and completed rows. Legacy active JSON imports once and is renamed `.migrated`; malformed JSON raises visibly. Run only one scanner process per database; the in-process lock protects the callback and maintenance worker, not independent scanners.
- Completed rows commit before active state is removed. A crash between those steps replays safely. BEGIN IMMEDIATE serializes writers; a deterministic candidate ID, unique index and natural-key duplicate trigger protect `(code, entry_time, COALESCE(rule_version, ''))`. Replay keeps the first committed row and never overwrites it.
- Schema upgrades add columns/indexes/tables/view/trigger, preserving existing research rows. Existing duplicates remain physically intact. `canonical_signal_research` chooses the earliest row per natural key for the updated report and export. Older tools querying the raw table can still see historical duplicates.
- Historical rows have no verified observation status. The updated DB report labels them LEGACY_UNVERIFIED and excludes their horizon returns from verified averages. Historical returns are retained in storage for audit; older reports are not upgraded by this change.
- CSV is an atomic full export of canonical committed rows, with additional columns. A locked/unwritable CSV logs an error while retaining DB truth; run `python export_signal_research.py` to rebuild. Never import an old CSV to repair a newer DB. Export from a single process when other writers are stopped to avoid competing snapshot replacement. A pre-upgrade CSV is automatically preserved as `.csv.pre_p0` before replacement. CSV-only history must be explicitly imported before the first new export; back up DB, JSON and CSV before upgrade.
- CSV import uses the same transactional insertion function, retains leading-zero stock codes, imports observation metadata and trading-value buckets, and reports skipped duplicates.
- Research callback errors are logged separately from the collector's other processing. No collector, broker or authenticated API process is launched by the tests.

## Verification

`python -m unittest discover -s tests -v`

Covers all horizon timestamps, boundary tolerance, stale/missing/session observations, invalid/out-of-order quotes, restart recovery, storage/export failures, concurrent/null-rule idempotency, historical duplicate preservation, additive schema upgrade and report quality output. Tests use temporary databases and synthetic prices. If the Windows temporary directory is restricted, set TEMP and TMP to a writable workspace directory before running.

No live-market integration test was performed.
