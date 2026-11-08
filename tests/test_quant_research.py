import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout, redirect_stderr
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import database
from quant_research_report import main
from research.statistical_analysis import (
    analyze, describe, load_benchmark, load_rows, render_markdown, timestamp, valid_observation,
)


def signal(day='2026-11-09', code='005930', value=1.0, setup='B', status='VALID'):
    entry = datetime.fromisoformat(day + ' 10:00:00')
    row = dict(code=code, entry_time=str(entry), entry_price=100.0,
               setup_quality=setup, rule_version='test')
    for horizon in (5, 10, 30):
        target = entry + timedelta(minutes=horizon)
        row.update({f'target_timestamp_{horizon}m': str(target),
                    f'observed_timestamp_{horizon}m': str(target + timedelta(seconds=20)),
                    f'price_{horizon}m': 100 + value,
                    f'ret_{horizon}m': value, f'status_{horizon}m': status})
    return row


def overall(report, horizon=5, partition='all'):
    return next(section for section in report['sections'] if section['group_by'] == 'overall'
                and section['horizon'] == horizon and section['partition'] == partition)


class StatisticalTests(unittest.TestCase):
    def test_known_distribution_and_break_even_win_definition(self):
        result = describe([('2026-11-09', x) for x in (-2, 0, 2, 4)])
        self.assertEqual(result['mean'], 1)
        self.assertEqual(result['median'], 1)
        self.assertEqual(result['win_rate'], .5)
        self.assertAlmostEqual(result['stddev'], (20 / 3) ** .5)
        self.assertAlmostEqual(result['quantiles']['0.05'], -1.7)
        self.assertEqual(result['quantiles']['0.25'], -.5)
        self.assertIsNone(result['mean_ci95'])

    def test_zero_and_one_samples_do_not_invent_uncertainty(self):
        for samples in ([], [('2026-11-09', 2)]):
            result = describe(samples)
            self.assertIsNone(result['stddev'])
            self.assertIsNone(result['mean_ci95'])
            self.assertEqual(result['ci_status'], 'INSUFFICIENT_DATA')
        self.assertIsNone(describe([])['mean'])

    def test_cluster_bootstrap_reproducible_and_preserves_day_dependence(self):
        samples = [('2026-11-09', 1)] * 30 + [('2026-11-10', 3)] * 30
        options = dict(seed=23, resamples=500, min_days=2)
        result = describe(samples, **options)
        self.assertEqual(result, describe(list(reversed(samples)), **options))
        self.assertEqual(result['mean_ci95'], [1, 3])
        self.assertEqual(result['days'], 2)
        # 60 same-day signals still do not meet even a two-day threshold.
        self.assertIsNone(describe(samples[:30] * 2, **options)['mean_ci95'])

    def test_degenerate_interval_is_explicit(self):
        result = describe([('2026-11-09', 1), ('2026-11-10', 1)],
                          resamples=100, min_samples=2, min_days=2)
        self.assertEqual(result['ci_status'], 'DEGENERATE')
        self.assertEqual(result['mean_ci95'], [1, 1])

    def test_observation_weighted_and_equal_day_means_differ(self):
        result = describe([('2026-11-09', 0)] * 9 + [('2026-11-10', 10)])
        self.assertEqual(result['mean'], 1)
        self.assertEqual(result['equal_day_mean'], 5)

    def test_nonfinite_direct_statistics_rejected(self):
        for value in (float('nan'), float('inf'), None, True):
            with self.assertRaises(ValueError):
                describe([('2026-11-09', value)])


class ResearchReportTests(unittest.TestCase):
    def test_horizons_use_independent_quality_denominators(self):
        first = signal()
        first['status_10m'] = 'MISSING'
        second = signal(code='000660', value=-2)
        second['status_5m'] = 'STALE'
        third = signal(code='035420', status=None, value=50)
        report = analyze([first, second, third])
        self.assertEqual(overall(report)['stock']['mean'], 1)
        self.assertEqual(overall(report, 10)['stock']['mean'], -2)
        self.assertEqual(overall(report, 30)['stock']['mean'], -.5)
        self.assertEqual(overall(report)['total'], 3)
        self.assertEqual(overall(report)['quality']['LEGACY_UNVERIFIED'], 1)
        self.assertEqual(overall(report)['excluded'], 2)
        self.assertEqual(overall(report)['quality_rates']['STALE'], 1 / 3)

    def test_claimed_valid_requires_metadata_consistency(self):
        for field, value in [('ret_5m', float('nan')), ('price_5m', 500),
                             ('observed_timestamp_5m', '2026-11-09 10:04:59'),
                             ('target_timestamp_5m', '2026-11-09 10:06:00'),
                             ('observed_timestamp_5m', '2026-11-09 10:06:01'),
                             ('entry_price', 0)]:
            with self.subTest(field=field, value=value):
                row = signal()
                row[field] = value
                self.assertEqual(valid_observation(row, 5), (None, 'INVALID_METADATA'))

    def test_cutoff_keeps_complete_dates_separate(self):
        rows = [signal('2026-11-09', value=1), signal('2026-11-10', value=9)]
        report = analyze(rows, holdout_start='2026-11-10')
        self.assertEqual(overall(report, partition='exploration')['stock']['mean'], 1)
        self.assertEqual(overall(report, partition='holdout')['stock']['mean'], 9)
        self.assertEqual(overall(report)['stock']['mean'], 5)
        self.assertIn('EXPLORATORY', report['inference'])

    def test_hypothesis_filter_applies_before_holdout_analysis(self):
        rows = [signal(setup='A'), signal(code='000660', setup='B')]
        report = analyze(rows, setup='A', horizon=10, rule_version='test')
        self.assertEqual(report['selected_rows'], 1)
        self.assertTrue(all(section['horizon'] == 10 for section in report['sections']))
        self.assertEqual(analyze(rows, setup='none')['selected_rows'], 0)

    def test_duplicate_programmatic_inputs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            analyze([signal(), signal()])

    def test_invalid_dates_retained_in_all_quality_but_not_partitions(self):
        row = signal()
        row['entry_time'] = 'bad'
        report = analyze([row], holdout_start='2026-11-10')
        self.assertEqual(report['undated_rows'], 1)
        self.assertEqual(overall(report)['quality'], {'INVALID_METADATA': 1})
        self.assertEqual(overall(report, partition='holdout')['total'], 0)

    def test_benchmark_exact_pairing_and_matched_subset_mean(self):
        first = signal(value=2)
        second = signal(code='000660', value=8)
        second['entry_time'] = '2026-11-09 11:00:00'
        for horizon in (5, 10, 30):
            for field in ('target_timestamp', 'observed_timestamp'):
                second[f'{field}_{horizon}m'] = str(timestamp(second[f'{field}_{horizon}m']) + timedelta(hours=1))
        key = (timestamp(first['entry_time']), timestamp(first['observed_timestamp_5m']))
        report = analyze([first, second], benchmark={key: 3}, benchmark_name='KOSPI')
        section = overall(report)
        self.assertEqual(section['stock']['mean'], 5)
        self.assertEqual(section['paired_stock']['mean'], 2)
        self.assertEqual(section['excess']['mean'], -1)
        self.assertEqual(section['benchmark_unmatched'], 1)
        changed = analyze([first, second], benchmark={key: 4}, benchmark_name='KOSPI')
        self.assertNotEqual(report['benchmark_sha256'], changed['benchmark_sha256'])
        self.assertEqual(overall(report, 10)['benchmark_status'], 'NO_EXACT_MATCHES')
        self.assertEqual(overall(analyze([first]))['benchmark_status'], 'NOT_PROVIDED')

    def test_default_thresholds_and_json_have_no_nan(self):
        report = analyze([signal()])
        self.assertEqual(report['method']['min_days'], 20)
        self.assertIsNone(overall(report)['stock']['mean_ci95'])
        json.dumps(report, allow_nan=False)
        markdown = render_markdown(report)
        self.assertIn('INSUFFICIENT_DATA', markdown)
        self.assertIn('Quantiles', markdown)
        self.assertIn('2026-11-09', markdown)

    def test_input_fingerprint_is_order_independent_and_changes_with_data(self):
        rows = [signal(), signal(code='000660')]
        first = analyze(rows)['input_sha256']
        self.assertEqual(first, analyze(list(reversed(rows)))['input_sha256'])
        rows[0]['ret_5m'] = 10
        self.assertNotEqual(first, analyze(rows)['input_sha256'])

    def test_invalid_options_fail(self):
        for options in ({'resamples': 99}, {'min_days': 1}, {'min_samples': 1},
                        {'horizon': 7}, {'holdout_start': 'bad'}):
            with self.assertRaises(ValueError):
                analyze([], **options)


class StorageAndCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'data.db'
        with patch.object(database, 'DB_FILE', str(self.db)):
            database.init_db()
            database.insert_signal_research(signal())

    def test_read_only_loader_and_cli_do_not_modify_input(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        output, json_output = self.root / 'report.md', self.root / 'report.json'
        report = main(['--db', str(self.db), '--output', str(output), '--json-output', str(json_output)])
        self.assertEqual(report, json.loads(json_output.read_text(encoding='utf-8')))
        self.assertIn('Quant research report', output.read_text(encoding='utf-8'))
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())

    def test_cli_runs_as_a_standalone_process(self):
        entrypoint = Path(__file__).resolve().parents[1] / 'quant_research_report.py'
        completed = subprocess.run([sys.executable, str(entrypoint), '--db', str(self.db), '--horizon', '5'],
                                   cwd=self.root, text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('Quant research report', completed.stdout)
        self.assertNotIn('Traceback', completed.stderr)

    def test_missing_db_never_created_and_old_schema_not_upgraded(self):
        missing = self.root / 'missing.db'
        with self.assertRaises(sqlite3.OperationalError):
            load_rows(missing)
        self.assertFalse(missing.exists())
        legacy = self.root / 'legacy.db'
        with closing(sqlite3.connect(legacy)) as conn:
            conn.execute('CREATE TABLE signal_research (code TEXT)')
            conn.commit()
        before = legacy.read_bytes()
        with self.assertRaises(sqlite3.OperationalError):
            load_rows(legacy)
        self.assertEqual(before, legacy.read_bytes())

    def test_historical_duplicates_use_canonical_view(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('DROP TRIGGER research_no_duplicate')
            columns = [row[1] for row in conn.execute('PRAGMA table_info(signal_research)')
                       if row[1] not in ('id', 'candidate_id')]
            names = ','.join(columns)
            conn.execute(f'INSERT INTO signal_research({names}) SELECT {names} FROM signal_research')
            conn.commit()
        self.assertEqual(len(load_rows(self.db)), 1)

    def test_cli_cannot_overwrite_sources_or_share_outputs(self):
        for args in (['--output', str(self.db)],
                     ['--output', str(self.root / 'report'), '--json-output', str(self.root / 'report')],
                     ['--benchmark-name', 'KOSPI']):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(['--db', str(self.db)] + args)

    def test_benchmark_timezone_normalization_duplicates_and_invalid_values(self):
        benchmark = self.root / 'benchmark.csv'
        header = 'benchmark,entry_time,observed_time,return_pct\n'
        line = 'KOSPI,2026-11-09T01:00:00+00:00,2026-11-09T01:05:20+00:00,1\n'
        benchmark.write_text(header + line, encoding='utf-8')
        values = load_benchmark(benchmark, 'KOSPI')
        self.assertEqual(next(iter(values))[0], datetime(2026, 11, 9, 10))
        benchmark.write_text(header + line * 2, encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            load_benchmark(benchmark, 'KOSPI')
        for text in (header + line.replace(',1\n', ',nan\n'), 'bad,header\n'):
            benchmark.write_text(text, encoding='utf-8')
            with self.assertRaises(ValueError):
                load_benchmark(benchmark, 'KOSPI')


if __name__ == '__main__':
    unittest.main()
