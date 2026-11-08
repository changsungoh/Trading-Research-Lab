"""Read-only, reproducible research statistics. Returns are percentage points."""
import csv
import hashlib
import json
import math
import random
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median, stdev

KOREA = timezone(timedelta(hours=9))
HORIZONS = (5, 10, 30)


def timestamp(value):
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(KOREA).replace(tzinfo=None)
    return parsed


def finite(value):
    if value is None or isinstance(value, bool):
        raise ValueError('Expected a finite number')
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Expected a finite number')
    return number


def load_rows(db_path):
    """Never creates/upgrades a database or imports scanner/broker modules."""
    uri = Path(db_path).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('BEGIN')
        rows = [dict(row) for row in connection.execute(
            'SELECT * FROM canonical_signal_research ORDER BY entry_time, code, id')]
    return rows


def valid_observation(row, horizon):
    status = row.get(f'status_{horizon}m') or 'LEGACY_UNVERIFIED'
    if status != 'VALID':
        return None, status
    try:
        entry = timestamp(row['entry_time'])
        target = timestamp(row[f'target_timestamp_{horizon}m'])
        observed = timestamp(row[f'observed_timestamp_{horizon}m'])
        result = finite(row[f'ret_{horizon}m'])
        entry_price = finite(row['entry_price'])
        price = finite(row[f'price_{horizon}m'])
        if entry_price <= 0 or price <= 0:
            raise ValueError('Invalid price')
        if target != entry + timedelta(minutes=horizon):
            raise ValueError('Incorrect target')
        if not 0 <= (observed - target).total_seconds() <= 60:
            raise ValueError('Incorrect receipt time')
        if entry.date() != observed.date():
            raise ValueError('Session crossing')
        for point in (entry, target, observed):
            if point.weekday() >= 5 or not (9, 0) <= (point.hour, point.minute) < (15, 30):
                raise ValueError('Outside regular session')
        if abs(result - (price / entry_price - 1) * 100) > .005001:
            raise ValueError('Inconsistent price and rounded return')
        return (entry, observed, result), 'VALID'
    except (ValueError, TypeError, KeyError, OverflowError):
        return None, 'INVALID_METADATA'


def quantile(values, probability):
    """Linear interpolation at (n - 1) * p, including endpoints."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def describe(samples, *, seed=1729, resamples=2000, min_samples=30, min_days=20):
    """samples: (ISO date, return). Resample entire dates, not individual signals."""
    samples = sorted(samples)
    values = [finite(value) for _, value in samples]
    by_day = defaultdict(list)
    for day, value in samples:
        date.fromisoformat(day)
        by_day[day].append(finite(value))
    n, days = len(values), len(by_day)
    result = dict(n=n, days=days, mean=None, median=None, win_rate=None,
                  stddev=None, quantiles=None, mean_ci95=None,
                  ci_status='INSUFFICIENT_DATA', equal_day_mean=None)
    if not n:
        return result
    result.update(mean=mean(values), median=median(values),
                  win_rate=sum(value > 0 for value in values) / n,
                  stddev=stdev(values) if n > 1 else None,
                  quantiles={str(p): quantile(values, p) for p in (.05, .25, .75, .95)},
                  equal_day_mean=mean(mean(group) for group in by_day.values()))
    if n < min_samples or days < max(2, min_days):
        return result
    daily = [(sum(group), len(group)) for group in by_day.values()]
    rng = random.Random(seed)
    draws = []
    for _ in range(resamples):
        selected = rng.choices(daily, k=days)
        draws.append(sum(total for total, _ in selected) / sum(count for _, count in selected))
    result['mean_ci95'] = [quantile(draws, .025), quantile(draws, .975)]
    result['ci_status'] = 'DEGENERATE' if min(draws) == max(draws) else 'ESTIMATED'
    return result


def load_benchmark(path, name):
    """Explicit, exact paired intervals; no nearest-time matches or forward filling."""
    result = {}
    required = {'benchmark', 'entry_time', 'observed_time', 'return_pct'}
    with open(path, encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if not required.issubset(reader.fieldnames or []):
            raise ValueError('Benchmark CSV requires: ' + ', '.join(sorted(required)))
        for row in reader:
            if row['benchmark'] != name:
                continue
            start, end = timestamp(row['entry_time']), timestamp(row['observed_time'])
            value = finite(row['return_pct'])
            if end <= start or value <= -100:
                raise ValueError('Invalid benchmark interval or return')
            key = (start, end)
            if key in result:
                raise ValueError('Ambiguous duplicate benchmark interval')
            result[key] = value
    return result


def analyze(rows, *, benchmark=None, benchmark_name=None, holdout_start=None,
            setup=None, horizon=None, rule_version=None, seed=1729, resamples=2000,
            min_samples=30, min_days=20):
    if resamples < 100 or min_samples < 2 or min_days < 2:
        raise ValueError('Require resamples >= 100, min_samples >= 2 and min_days >= 2')
    if horizon is not None and horizon not in HORIZONS:
        raise ValueError('Horizon must be 5, 10 or 30')
    cutoff = date.fromisoformat(holdout_start) if holdout_start else None
    stats_options = dict(seed=seed, resamples=resamples, min_samples=min_samples, min_days=min_days)
    rows = list(rows)
    serialized = sorted(json.dumps(row, sort_keys=True, default=str) for row in rows)
    input_sha256 = hashlib.sha256(json.dumps(serialized).encode('utf-8')).hexdigest()
    benchmark_sha256 = None
    if benchmark is not None:
        benchmark_serialized = sorted((start.isoformat(), end.isoformat(), finite(value))
                                      for (start, end), value in benchmark.items())
        benchmark_sha256 = hashlib.sha256(json.dumps(benchmark_serialized).encode('utf-8')).hexdigest()
    selected = [row for row in rows if (setup is None or row.get('setup_quality') == setup)
                and (rule_version is None or row.get('rule_version') == rule_version)]
    # Canonical view already deduplicates DB input; protect programmatic callers too.
    unique = {}
    for row in selected:
        key = (row.get('code'), row.get('entry_time'), row.get('rule_version') or '')
        if key in unique:
            raise ValueError('Duplicate research identity in report input')
        unique[key] = row
    sections = []
    partitions = ('all', 'exploration', 'holdout') if cutoff else ('all',)
    for partition in partitions:
        partition_rows = []
        undated = 0
        for row in selected:
            try:
                day = timestamp(row['entry_time']).date()
            except (ValueError, TypeError, KeyError):
                undated += 1
                if partition == 'all':
                    partition_rows.append(row)
                continue
            if partition == 'all' or (partition == 'exploration' and day < cutoff) or (partition == 'holdout' and day >= cutoff):
                partition_rows.append(row)
        groups = [('overall', 'ALL', partition_rows)]
        for field in ('setup_quality', 'rule_version', 'entry_hour'):
            grouped = defaultdict(list)
            for row in partition_rows:
                if field == 'entry_hour':
                    try:
                        key = str(timestamp(row['entry_time']).hour)
                    except (ValueError, TypeError, KeyError):
                        key = 'UNKNOWN'
                else:
                    key = str(row.get(field) if row.get(field) is not None else 'UNKNOWN')
                grouped[key].append(row)
            groups += [(field, key, group) for key, group in sorted(grouped.items())]
        for field, key, group in groups:
            for minute in (horizon,) if horizon else HORIZONS:
                quality, samples, paired_stock, excess = Counter(), [], [], []
                daily = defaultdict(list)
                for row in group:
                    observation, status = valid_observation(row, minute)
                    quality[status] += 1
                    if observation is None:
                        continue
                    entry, observed, value = observation
                    day = entry.date().isoformat()
                    if partition == 'exploration' and observed.date() >= cutoff:
                        quality['VALID'] -= 1
                        quality['CROSSES_HOLDOUT'] += 1
                        continue
                    samples.append((day, value))
                    daily[day].append(value)
                    if benchmark is not None and (entry, observed) in benchmark:
                        paired_stock.append((day, value))
                        excess.append((day, value - benchmark[(entry, observed)]))
                sections.append(dict(partition=partition, group_by=field, group=key, horizon=minute,
                    total=len(group), quality=dict(sorted(quality.items())),
                    quality_rates={status: count / len(group) for status, count in sorted(quality.items())} if group else {},
                    excluded=len(group) - len(samples), coverage=len(samples) / len(group) if group else None,
                    stock=describe(samples, **stats_options),
                    daily={day: describe([(day, value) for value in values], **stats_options)
                           for day, values in sorted(daily.items())},
                    benchmark_status='NOT_PROVIDED' if benchmark is None else ('MATCHED' if excess else 'NO_EXACT_MATCHES'),
                    benchmark_matched=len(excess), benchmark_unmatched=len(samples) - len(excess),
                    paired_stock=describe(paired_stock, **stats_options),
                    excess=describe(excess, **stats_options)))
    return dict(schema_version=1, units='percentage_points; win_rate and coverage are fractions',
        input_sha256=input_sha256, benchmark_sha256=benchmark_sha256, input_rows=len(rows),
        selection=dict(setup=setup, horizon=horizon, rule_version=rule_version),
        holdout_start=holdout_start, benchmark=benchmark_name,
        inference='EXPLORATORY: no multiplicity adjustment or automatic edge verdict',
        method=dict(ci='95% percentile bootstrap of whole entry dates; observation-weighted mean',
                    seed=seed, resamples=resamples, min_samples=min_samples, min_days=min_days),
        selected_rows=len(selected), undated_rows=undated,
        limitations=[
            'Gross observed price changes, not executable or net trading profits.',
            'Receipt timestamps cannot identify upstream transport delays.',
            'Day resampling preserves within-day dependence, not dependence across days.',
            'Few days and degenerate intervals cannot establish an edge.',
            'Missing observations may be nonrandom; coverage is reported, not imputed.',
            'All grouped comparisons are exploratory; no corrected significance claim.',
            'Holdout is only untouched if its date and hypothesis were fixed before inspection.',
            'Exact benchmark interval differences are paired relative returns, not risk-adjusted alpha.'
        ], sections=sections)


def render_markdown(report):
    lines = ['# Quant research report', '', report['inference'], '',
             'Returns, dispersion and intervals: percentage points. Win rate: strictly positive returns / valid observations.',
             f"Selected rows: {report['selected_rows']}; undated: {report['undated_rows']}; holdout start: {report['holdout_start'] or 'not set'}.",
             f"Method: {report['method']}",
             f"Input research snapshot SHA256: {report['input_sha256']}",
             f"Benchmark: {report['benchmark'] or 'not provided'}; SHA256: {report['benchmark_sha256'] or 'N/A'}", '',
             '| Period | Group | Horizon | Valid/total | Days | Mean | Median | Win % | SD | 95% mean CI | CI status | Paired excess mean (n) |',
             '|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|---|']
    def fmt(value):
        return 'N/A' if value is None else f'{value:.4f}'
    def safe(value):
        return str(value).replace('|', '/').replace('\n', ' ')
    for section in report['sections']:
        stats = section['stock']
        interval = stats['mean_ci95']
        lines.append('| ' + ' | '.join(map(str, [section['partition'], safe(section['group_by'] + '=' + section['group']),
            section['horizon'], f"{stats['n']}/{section['total']}", stats['days'], fmt(stats['mean']), fmt(stats['median']),
            fmt(stats['win_rate'] * 100 if stats['win_rate'] is not None else None), fmt(stats['stddev']),
            'N/A' if interval is None else f'[{fmt(interval[0])}, {fmt(interval[1])}]', stats['ci_status'],
            f"{fmt(section['excess']['mean'])} ({section['benchmark_matched']})"])) + ' |')
    lines += ['', '## Quality, distribution and dates']
    for section in report['sections']:
        lines += ['', f"### {safe(section['partition'])}: {safe(section['group_by'])}={safe(section['group'])}, {section['horizon']}m",
                  f"Quality: {section['quality']}; rates: {section['quality_rates']}; excluded: {section['excluded']}; benchmark: {section['benchmark_status']}; unmatched valid: {section['benchmark_unmatched']}.",
                  f"Quantiles (5/25/75/95%): {section['stock']['quantiles']}; equal-day mean: {fmt(section['stock']['equal_day_mean'])}.",
                  f"Paired stock mean: {fmt(section['paired_stock']['mean'])}; excess CI: {section['excess']['mean_ci95']}; excess CI status: {section['excess']['ci_status']}."]
        for day, stats in section['daily'].items():
            lines.append(f"- {day}: n={stats['n']}, mean={fmt(stats['mean'])}, median={fmt(stats['median'])}")
    lines += ['', '## Limitations', ''] + ['- ' + item for item in report['limitations']]
    return '\n'.join(lines) + '\n'
