from contextlib import closing, redirect_stderr
from dataclasses import asdict, replace
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import database
from portfolio_risk_report import compare, load_config, main
from simulation.execution_simulator import Scenario
from simulation.risk_simulator import RiskPolicy, simulate
from simulation.portfolio_allocation import AllocationPolicy, Stress, PastSignalHistory, Allocator

ZERO = Scenario('zero', 0, 0, 0, 0, 0, 1, 1, 1)
RISK = RiskPolicy(1000, 1, 10, 20, 100, 20)
ALLOCATION = AllocationPolicy('fraction', 'fixed_fraction', 300, .5, 1, .5, 20, 2, 2, 1, 1, 1, 'bucket')
STRESS = Stress('market', -10, {})


def row(code='005930', when='2026-11-09 10:00:00', value=1, status='VALID'):
    start = datetime.fromisoformat(when)
    return dict(code=code, entry_time=when, rule_version='test', setup_quality='B', entry_price=100,
                ret_5m=value, price_5m=100 + value, status_5m=status,
                target_timestamp_5m=str(start + timedelta(minutes=5)),
                observed_timestamp_5m=str(start + timedelta(minutes=5)))


def run(rows, allocation=ALLOCATION, *, sectors=None, stresses=None, risk=RISK):
    report = compare(rows, risk, [ZERO], [allocation], sectors or {}, stresses or [STRESS], horizon=5)
    return report['results'][0]


class HistoryTests(unittest.TestCase):
    def test_only_completed_observations_strictly_before_entry(self):
        rows = [row(when='2026-11-09 10:00:00', value=1), row(when='2026-11-10 10:00:00', value=3),
                row(when='2026-11-11 10:00:00', value=99)]
        history = PastSignalHistory(rows, 5)
        exact = datetime(2026, 11, 10, 10, 5)
        self.assertEqual(history.estimate('005930', exact, ALLOCATION)['history_n'], 1)
        estimate = history.estimate('005930', datetime(2026, 11, 11, 10), ALLOCATION)
        self.assertEqual(estimate['history_n'], 2)
        self.assertEqual(estimate['history_days'], 2)
        self.assertAlmostEqual(estimate['volatility_pct'], 2 ** .5)
        self.assertTrue(estimate['history_ready'])

    def test_lookback_and_quality_and_symbol_isolation(self):
        rows = [row(when='2026-11-09 10:00:00'), row(when='2026-11-10 10:00:00', status='STALE'),
                row('000660', '2026-11-11 10:00:00'), row(when='2026-11-12 10:00:00'),
                row(when='2026-11-13 10:00:00')]
        history = PastSignalHistory(rows, 5)
        estimate = history.estimate('005930', datetime(2026, 11, 16), replace(ALLOCATION, lookback=2))
        self.assertEqual(estimate['history_n'], 2)
        self.assertEqual(estimate['latest_history_observed'], '2026-11-13 10:05:00')
        self.assertFalse(history.estimate('000660', datetime(2026, 11, 16), ALLOCATION)['history_ready'])

    def test_warmup_dates_available_but_not_traded(self):
        rows = [row(when='2026-11-09 10:00:00', value=1), row(when='2026-11-10 10:00:00', value=3),
                row(when='2026-11-11 10:00:00')]
        allocation = replace(ALLOCATION, mode='volatility_target')
        report = compare(rows, RISK, [ZERO], [allocation], {}, [STRESS], horizon=5, start_date='2026-11-11')
        decision = report['results'][0]['decisions'][0]
        self.assertEqual(report['candidate_rows'], 1)
        self.assertEqual(report['history_rows'], 3)
        self.assertEqual(decision['history_n'], 2)
        self.assertAlmostEqual(decision['requested_budget'], 1000 / 2 ** .5)

    def test_future_returns_do_not_change_current_sizing(self):
        rows = [row(when='2026-11-09 10:00:00', value=1), row(when='2026-11-10 10:00:00', value=3),
                row(when='2026-11-11 10:00:00', value=99)]
        allocation = replace(ALLOCATION, mode='volatility_target')
        kwargs = dict(horizon=5, start_date='2026-11-11')
        first = compare(rows, RISK, [ZERO], [allocation], {}, [STRESS], **kwargs)['results'][0]
        rows[-1] = row(when='2026-11-11 10:00:00', value=-90)
        second = compare(rows, RISK, [ZERO], [allocation], {}, [STRESS], **kwargs)['results'][0]
        self.assertEqual(first['decisions'][0]['allocated_budget'], second['decisions'][0]['allocated_budget'])
        self.assertEqual(first['decisions'][0]['quantity'], second['decisions'][0]['quantity'])


class AllocationTests(unittest.TestCase):
    def test_fixed_cash_and_fraction_differ(self):
        first = run([row()], replace(ALLOCATION, mode='fixed_cash'))
        second = run([row()])
        self.assertEqual(first['trades'][0]['quantity'], 3)
        self.assertEqual(second['trades'][0]['quantity'], 5)

    def test_volatility_floor_and_insufficient_history(self):
        allocation = replace(ALLOCATION, mode='volatility_target', volatility_floor_pct=2)
        result = run([row()], allocation)
        self.assertEqual(result['decision_counts']['INSUFFICIENT_PAST_VOLATILITY'], 1)
        past = [row(when='2026-11-09 10:00:00'), row(when='2026-11-10 10:00:00')]
        manager = Allocator(allocation, {}, PastSignalHistory(past, 5), [STRESS])
        budget, details = manager.budget(row(), datetime(2026, 11, 11, 10), 1000, {}, 1000)
        self.assertEqual(budget, 500)
        self.assertEqual(details['volatility_pct'], 0)

    def test_sector_symbol_and_gross_caps_enforce_at_entry(self):
        rows = [row(), row('000660'), row('035420')]
        allocation = replace(ALLOCATION, fraction=.8, max_symbol_fraction=.25, max_sector_fraction=.4, max_gross_fraction=.5)
        sectors = {'005930': 'A', '000660': 'A', '035420': 'B'}
        result = run(rows, allocation, sectors=sectors)
        analysis = result['allocation_analysis']
        self.assertAlmostEqual(analysis['peak_gross_fraction'], .5)
        self.assertLessEqual(analysis['peak_symbol_fraction'], .25)
        self.assertAlmostEqual(analysis['peak_sector_fraction'], .4)
        self.assertEqual(result, run(list(reversed(rows)), allocation, sectors=sectors))

    def test_common_p2_position_cap_is_preserved(self):
        result = run([row()], risk=replace(RISK, position_fraction=.1))
        self.assertEqual(result['trades'][0]['quantity'], 1)
        self.assertEqual(result['decisions'][0]['allocated_budget'], 100)

    def test_unknown_sector_block_or_shared_bucket(self):
        result = run([row()], replace(ALLOCATION, unknown_sector_policy='block'))
        self.assertEqual(result['decision_counts']['UNKNOWN_SECTOR'], 1)
        allocation = replace(ALLOCATION, fraction=1, max_sector_fraction=.2)
        result = run([row(), row('000660', '2026-11-09 10:01:00')], allocation)
        self.assertEqual(result['decision_counts']['CONCENTRATION_OR_CASH_LIMIT'], 1)
        self.assertEqual(result['allocation_analysis']['peak_sector_fraction'], .2)

    def test_p2_default_result_unchanged_without_allocator(self):
        result = simulate([row()], ZERO, replace(RISK, position_fraction=.5), horizon=5)
        allocation_result = run([row()])
        for field in ('ending_cash', 'realized_net_pnl', 'filled', 'closed', 'unresolved', 'portfolio_return_pct'):
            self.assertEqual(result[field], allocation_result[field])
        self.assertNotIn('sector', result['trades'][0])

    def test_missing_exit_still_open_and_concentration_still_reserved(self):
        rows = [row(status='MISSING'), row('000660', '2026-11-10 10:00:00')]
        allocation = replace(ALLOCATION, fraction=1, max_sector_fraction=.5)
        result = run(rows, allocation)
        self.assertEqual(result['unresolved'], 1)
        self.assertEqual(result['decision_counts']['CONCENTRATION_OR_CASH_LIMIT'], 1)
        self.assertIsNone(result['terminal_equity'])

    def test_exposure_breach_after_loss_is_visible_and_blocks_new_allocation(self):
        rows = [row(when='2026-11-09 10:00:00', value=-50),
                row('000660', '2026-11-09 10:01:00', status='MISSING'),
                row('035420', '2026-11-09 10:06:00')]
        allocation = replace(ALLOCATION, fraction=.5, max_sector_fraction=.5)
        sectors = {'005930': 'A', '000660': 'B', '035420': 'B'}
        result = run(rows, allocation, sectors=sectors)
        # After A realizes a loss, B's paid outlay exceeds 50% of remaining book equity.
        self.assertGreater(result['allocation_analysis']['peak_sector_fraction'], .5)
        self.assertEqual(result['decision_counts']['CONCENTRATION_OR_CASH_LIMIT'], 1)
        self.assertEqual(result['unresolved'], 1)
        self.assertIsNone(result['portfolio_return_pct'])

    def test_invalid_allocation_parameters(self):
        for kwargs in ({'mode': 'kelly'}, {'fraction': 2}, {'fixed_cash': '100'}, {'max_gross_fraction': 0},
                       {'volatility_floor_pct': float('nan')}, {'lookback': 1}, {'min_observations': 1},
                       {'unknown_sector_policy': 'invent'}, {'min_days': 30}, {'lookback': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                replace(ALLOCATION, **kwargs)


class StressTests(unittest.TestCase):
    def test_compounded_market_and_sector_loss_matches_arithmetic(self):
        stress = Stress('combined', -10, {'A': -20})
        result = run([row()], sectors={'005930': 'A'}, stresses=[stress])
        worst = result['allocation_analysis']['stress_worst']['combined']
        self.assertAlmostEqual(worst['loss_pct'], 14)
        self.assertAlmostEqual(worst['liquidation_equity'], 860)
        self.assertAlmostEqual(worst['sector_loss']['A'], 140)

    def test_total_loss_is_supported_and_does_not_override_unpriced_equity(self):
        result = run([row(status='MISSING')], stresses=[Stress('zero', -100, {})])
        self.assertIsNone(result['terminal_equity'])
        worst = result['allocation_analysis']['stress_worst']['zero']
        self.assertEqual(worst['loss_pct'], 50)
        self.assertEqual(worst['liquidation_equity'], 500)

    def test_stress_includes_adverse_prices_and_exit_costs(self):
        scenario = replace(ZERO, buy_fee_bps=10, sell_fee_bps=10, sell_tax_bps=20, spread_bps=20)
        report = compare([row()], RISK, [scenario], [ALLOCATION], {}, [Stress('flat', 0, {})], horizon=5)
        worst = report['results'][0]['allocation_analysis']['stress_worst']['flat']
        self.assertGreater(worst['loss_pct'], 0)
        self.assertLess(worst['liquidation_equity'], 1000)

    def test_invalid_stresses_rejected(self):
        for kwargs in ({'market_return_pct': -101}, {'market_return_pct': 1}, {'sector_returns_pct': {'A': float('inf')}},
                       {'sector_returns_pct': []}, {'name': ''}):
            with self.assertRaises(ValueError):
                replace(STRESS, **kwargs)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db, self.config = self.root / 'data.db', self.root / 'config.json'
        with patch.object(database, 'DB_FILE', str(self.db)):
            database.init_db()
            database.insert_signal_research(row())
        self.config.write_text(json.dumps(dict(portfolio=asdict(RISK), scenarios=[asdict(ZERO)],
            allocations=[asdict(ALLOCATION)], sectors={}, stresses=[asdict(STRESS)])))
        self.md, self.js = self.root / 'out.md', self.root / 'out.json'

    def args(self):
        return ['--db', str(self.db), '--config', str(self.config), '--horizon', '5',
                '--output', str(self.md), '--json-output', str(self.js)]

    def test_finite_artifacts_and_read_only_sources(self):
        before = self.db.read_bytes(), self.config.read_bytes()
        report = main(self.args())
        self.assertEqual(report, json.loads(self.js.read_text(encoding='utf-8')))
        self.assertEqual(before, (self.db.read_bytes(), self.config.read_bytes()))
        self.assertIn('P3 allocation', self.md.read_text(encoding='utf-8'))
        json.dumps(report, allow_nan=False)

    def test_standalone_cli(self):
        script = Path(__file__).resolve().parents[1] / 'portfolio_risk_report.py'
        completed = subprocess.run([sys.executable, str(script)] + self.args(), cwd=self.root, text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_safe_paths_dates_and_missing_database(self):
        missing = self.root / 'missing.db'
        for additional in (['--output', str(self.db)], ['--output', str(self.config)], ['--output', str(self.js)],
                           ['--start-date', 'bad'], ['--start-date', '2026-12-01', '--end-date', '2026-11-01'],
                           ['--db', str(missing)]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(self.args() + additional)
        self.assertFalse(missing.exists())

    def test_example_config_comparison_grid_and_selection(self):
        example = Path(__file__).resolve().parents[1] / 'simulation/allocation.example.json'
        risk, scenarios, allocations, sectors, stresses = load_config(example)
        report = compare([row()], risk, scenarios, allocations, sectors, stresses, horizon=5)
        self.assertEqual(len(report['results']), 6)
        self.assertTrue(any(result['decision_counts'].get('INSUFFICIENT_PAST_VOLATILITY') for result in report['results']))
        self.assertTrue(all(len(result['allocation_analysis']['stress_worst']) == 2 for result in report['results']))
        selected = compare([row()], risk, scenarios, allocations, sectors, stresses, horizon=5, setup='A')
        self.assertEqual(selected['candidate_rows'], 0)
        self.assertEqual(selected['history_rows'], 0)
        self.assertTrue(all(result['completeness'] == 'NO_ELIGIBLE_ENTRIES' for result in selected['results']))
        json.dumps(report, allow_nan=False)

    def test_config_rejects_unknown_sectors_duplicates_and_fields(self):
        base = json.loads(self.config.read_text())
        variants = [dict(base, unexpected=True), dict(base, allocations=[]),
                    dict(base, allocations=[asdict(ALLOCATION)] * 2),
                    dict(base, sectors={'005930': ''}), dict(base, stresses=[asdict(Stress('x', 0, {'TYPO': -10}))])]
        for value in variants:
            self.config.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                load_config(self.config)


if __name__ == '__main__':
    unittest.main()
