from contextlib import closing, redirect_stderr
from dataclasses import asdict, replace
from datetime import datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import database
from execution_simulation_report import load_config, main, render_markdown
from simulation.execution_simulator import Scenario, entry_order, close_order
from simulation.risk_simulator import RiskPolicy, simulate


ZERO = Scenario('zero', 0, 0, 0, 0, 0, 1, 1, 1)
POLICY = RiskPolicy(1000, 1, 4, 20, 100, 20)


def candidate(code='005930', entry='2026-11-09 10:00:00', value=1, status='VALID', horizon=5):
    start = datetime.fromisoformat(entry)
    return dict(code=code, entry_time=entry, rule_version='test', setup_quality='B', entry_price=100,
                **{f'ret_{horizon}m': value, f'price_{horizon}m': 100 + value,
                   f'status_{horizon}m': status,
                   f'target_timestamp_{horizon}m': str(start + timedelta(minutes=horizon)),
                   f'observed_timestamp_{horizon}m': str(start + timedelta(minutes=horizon))})


class ExecutionTests(unittest.TestCase):
    def test_zero_cost_matches_exact_price_return(self):
        order = entry_order(100, 1000, ZERO, 'a', 1)
        self.assertEqual(order['quantity'], 10)
        self.assertEqual(order['entry_outlay'], 1000)
        closed = close_order(order, 110, ZERO)
        self.assertEqual(closed['net_pnl'], 100)
        self.assertEqual(closed['net_return_pct'], 10)

    def test_costs_use_fill_notionals_and_fee_inclusive_budget(self):
        costs = Scenario('cost', 10, 20, 30, 20, 10, 1, 1, 1)
        order = entry_order(100, 1000, costs, 'a', 1)
        buy = 100 * 1.002
        self.assertEqual(order['quantity'], 9)
        self.assertAlmostEqual(order['entry_fill_price'], buy)
        self.assertAlmostEqual(order['entry_fee'], buy * 9 * .001)
        closed = close_order(order, 110, costs)
        sell = 110 * .998
        expected = sell * 9 * .995 - buy * 9 * 1.001
        self.assertAlmostEqual(closed['net_pnl'], expected)
        self.assertAlmostEqual(closed['exit_tax'], sell * 9 * .003)
        self.assertLess(closed['net_return_pct'], 10)
        self.assertAlmostEqual(closed['gross_reference_pnl'] - closed['explicit_cost'] - closed['adverse_price_cost'], closed['net_pnl'])
        self.assertAlmostEqual(closed['total_cost_drag'], closed['explicit_cost'] + closed['adverse_price_cost'])
        self.assertLessEqual(order['entry_outlay'], 1000)

    def test_zero_move_loses_after_friction(self):
        costs = replace(ZERO, buy_fee_bps=10, sell_fee_bps=10, spread_bps=20)
        self.assertLess(close_order(entry_order(100, 1000, costs, 'a', 1), 100, costs)['net_pnl'], 0)

    def test_partial_and_nonfills_use_whole_shares(self):
        partial = entry_order(100, 1000, replace(ZERO, fill_fraction=.35), 'a', 1)
        self.assertEqual(partial['requested_quantity'], 10)
        self.assertEqual(partial['quantity'], 3)
        self.assertEqual(partial['entry_outlay'], 300)
        self.assertEqual(entry_order(100, 1000, replace(ZERO, fill_probability=0), 'a', 1)['status'], 'NO_FILL')
        self.assertEqual(entry_order(100, 100, replace(ZERO, fill_fraction=.5), 'a', 1)['status'], 'PARTIAL_FILL_ZERO')
        self.assertEqual(entry_order(100, 99, ZERO, 'a', 1)['status'], 'INSUFFICIENT_CASH')

    def test_invalid_parameters_rejected(self):
        for options in ({'spread_bps': -1}, {'buy_fee_bps': float('nan')}, {'fill_probability': 2},
                        {'fill_fraction': 0}, {'sell_fee_bps': 5000, 'sell_tax_bps': 5000},
                        {'slippage_bps': 9999, 'spread_bps': 3}, {'buy_fee_bps': '2'},
                        {'exit_fill_probability': float('inf')}, {'name': ''}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                replace(ZERO, **options)
        for options in ({'initial_cash': 0}, {'position_fraction': 2}, {'max_positions': True},
                        {'daily_loss_limit_pct': 0}, {'max_consecutive_losses': 1.2}, {'initial_cash': '1000'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                replace(POLICY, **options)


class RiskTests(unittest.TestCase):
    def run_sim(self, rows, policy=POLICY, scenario=ZERO):
        return simulate(rows, scenario, policy, horizon=5)

    def test_chronological_cash_and_same_timestamp_exit_before_entry(self):
        rows = [candidate(value=10), candidate('000660', '2026-11-09 10:05:00', value=-10)]
        result = self.run_sim(list(reversed(rows)))
        self.assertEqual(result['closed'], 2)
        self.assertEqual(result['ending_cash'], 990)
        self.assertAlmostEqual(result['portfolio_return_pct'], -1)
        self.assertAlmostEqual(result['max_cost_basis_drawdown_pct'], 10)
        self.assertEqual(result['trades'][1]['quantity'], 11)
        self.assertEqual(result, self.run_sim(rows))

    def test_overlapping_entries_cannot_spend_reserved_cash(self):
        rows = [candidate(), candidate('000660', '2026-11-09 10:01:00')]
        result = self.run_sim(rows)
        self.assertEqual(result['filled'], 1)
        self.assertEqual(result['decision_counts']['INSUFFICIENT_CASH'], 1)
        self.assertEqual(result['ending_cash'], 1010)

    def test_position_and_symbol_limits(self):
        rows = [candidate(), candidate('005930', '2026-11-09 10:01:00'), candidate('000660', '2026-11-09 10:02:00')]
        result = self.run_sim(rows, replace(POLICY, position_fraction=.25, max_positions=1))
        self.assertEqual(result['decision_counts']['SYMBOL_ALREADY_OPEN'], 1)
        self.assertEqual(result['decision_counts']['MAX_POSITIONS'], 1)

    def test_daily_trade_limit_resets_next_date(self):
        rows = [candidate(), candidate('000660', '2026-11-09 10:10:00'), candidate('000660', '2026-11-10 10:00:00')]
        result = self.run_sim(rows, replace(POLICY, max_trades_per_day=1))
        self.assertEqual(result['filled'], 2)
        self.assertEqual(result['decision_counts']['MAX_DAILY_TRADES'], 1)

    def test_loss_controls_only_see_realized_exits(self):
        rows = [candidate(value=-10), candidate('000660', '2026-11-09 10:01:00', value=10),
                candidate('035420', '2026-11-09 10:05:00')]
        result = self.run_sim(rows, replace(POLICY, position_fraction=.5, daily_loss_limit_pct=3))
        self.assertEqual(result['filled'], 2)  # Second entry precedes the first realized loss.
        self.assertEqual(result['decision_counts']['DAILY_LOSS_LIMIT'], 1)
        self.assertEqual(result['ending_cash'], 1000)

    def test_loss_limit_is_latched_after_recovery_until_next_day(self):
        rows = [candidate(value=-10), candidate('000660', '2026-11-09 10:01:00', value=20),
                candidate('035420', '2026-11-09 10:10:00'), candidate('035420', '2026-11-10 10:00:00')]
        result = self.run_sim(rows, replace(POLICY, position_fraction=.5, daily_loss_limit_pct=3))
        self.assertEqual(result['decision_counts']['DAILY_LOSS_LIMIT'], 1)
        self.assertEqual(result['filled'], 3)

    def test_consecutive_loss_stop(self):
        rows = [candidate(value=-1), candidate('000660', '2026-11-09 10:10:00', value=-1),
                candidate('035420', '2026-11-09 10:20:00')]
        result = self.run_sim(rows, replace(POLICY, max_consecutive_losses=2))
        self.assertEqual(result['decision_counts']['LOSS_STREAK_LIMIT'], 1)
        self.assertEqual(result['max_daily_realized_loss_streak'], 2)

    def test_missing_outcome_does_not_filter_entry_or_invent_cash_release(self):
        rows = [candidate(status='MISSING'), candidate('000660', '2026-11-09 10:10:00')]
        result = self.run_sim(rows)
        self.assertEqual(result['filled'], 1)
        self.assertEqual(result['unresolved'], 1)
        self.assertEqual(result['closed'], 0)
        self.assertEqual(result['reserved_entry_outlay'], 1000)
        self.assertEqual(result['ending_cash'], 0)
        self.assertIsNone(result['portfolio_return_pct'])
        self.assertIsNone(result['terminal_equity'])
        self.assertEqual(result['decision_counts']['INSUFFICIENT_CASH'], 1)
        self.assertIsNone(result['closed_net_stats']['mean'])

    def test_assumed_exit_failure_remains_unpriced(self):
        result = self.run_sim([candidate()], scenario=replace(ZERO, exit_fill_probability=0))
        self.assertEqual(result['unresolved_positions'][0]['exit_quality'], 'ASSUMED_EXIT_NO_FILL')
        self.assertEqual(result['unresolved'], 1)
        self.assertIsNone(result['portfolio_return_pct'])

    def test_fill_draws_are_reproducible_and_independent_of_future_return(self):
        scenario = replace(ZERO, fill_probability=.5)
        first = self.run_sim([candidate(value=10)], scenario=scenario)
        second = self.run_sim([candidate(value=-10)], scenario=scenario)
        self.assertEqual(first['decisions'][0]['status'], second['decisions'][0]['status'])
        self.assertEqual(first, self.run_sim([candidate(value=10)], scenario=scenario))

    def test_invalid_exit_retains_entry_and_invalid_entry_is_counted(self):
        row = candidate()
        row['price_5m'] = 900
        result = self.run_sim([row])
        self.assertEqual(result['unresolved'], 1)
        self.assertEqual(result['quality']['INVALID_METADATA'], 1)
        row = candidate(entry='2026-11-08 10:00:00')
        result = self.run_sim([row])
        self.assertEqual(result['entry_excluded'], 1)
        self.assertEqual(result['decision_counts']['INVALID_ENTRY'], 1)
        self.assertEqual(result['completeness'], 'NO_ELIGIBLE_ENTRIES')

    def test_unresolved_position_reserves_cash_across_dates(self):
        rows = [candidate(status='MISSING'), candidate('000660', '2026-11-10 10:00:00')]
        result = self.run_sim(rows, replace(POLICY, max_positions=1))
        self.assertEqual(result['decision_counts']['MAX_POSITIONS'], 1)
        self.assertEqual(result['reserved_entry_outlay'], 1000)
        self.assertEqual(result['completeness'], 'INCOMPLETE_UNPRICED_POSITIONS')

    def test_cash_conservation_with_fees_and_partial_fills(self):
        rows = [candidate(value=3), candidate('000660', '2026-11-09 10:02:00', value=-2),
                candidate('035420', '2026-11-09 10:10:00', status='MISSING')]
        costs = replace(ZERO, buy_fee_bps=10, sell_fee_bps=20, sell_tax_bps=30, spread_bps=20, fill_fraction=.5)
        result = self.run_sim(rows, replace(POLICY, position_fraction=.5), costs)
        self.assertAlmostEqual(result['ending_cash'] + result['reserved_entry_outlay'], POLICY.initial_cash + result['realized_net_pnl'])
        self.assertAlmostEqual(result['closed_gross_reference_pnl'] - result['closed_cost_drag'], result['realized_net_pnl'])
        self.assertGreater(result['known_explicit_costs'], 0)
        self.assertTrue(all(item['cash'] >= 0 for item in result['realized_ledger']))

    def test_duplicate_inputs_and_invalid_horizon_rejected(self):
        with self.assertRaises(ValueError):
            self.run_sim([candidate(), candidate()])
        with self.assertRaises(ValueError):
            simulate([], ZERO, POLICY, horizon=15)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'input.db'
        with patch.object(database, 'DB_FILE', str(self.db)):
            database.init_db()
            database.insert_signal_research(candidate())
            database.insert_signal_research(candidate('000660', '2026-11-10 10:00:00'))
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps(dict(portfolio=asdict(POLICY), scenarios=[asdict(ZERO)])), encoding='utf-8')
        self.output, self.json_output = self.root / 'report.md', self.root / 'report.json'

    def arguments(self):
        return ['--db', str(self.db), '--config', str(self.config), '--horizon', '5',
                '--output', str(self.output), '--json-output', str(self.json_output)]

    def test_artifacts_are_finite_and_sources_unchanged(self):
        before = (self.db.read_bytes(), self.config.read_bytes())
        result = main(self.arguments())
        self.assertEqual(result, json.loads(self.json_output.read_text(encoding='utf-8')))
        self.assertIn('ILLUSTRATIVE', self.output.read_text(encoding='utf-8'))
        self.assertEqual(before, (self.db.read_bytes(), self.config.read_bytes()))
        json.dumps(result, allow_nan=False)

    def test_standalone_cli_and_date_selection(self):
        script = Path(__file__).resolve().parents[1] / 'execution_simulation_report.py'
        completed = subprocess.run([sys.executable, str(script)] + self.arguments() + ['--start-date', '2026-11-10'],
                                   cwd=self.root, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(self.json_output.read_text(encoding='utf-8'))['results'][0]['input_rows'], 1)

    def test_input_overwrite_and_invalid_date_rejected(self):
        for additional in (['--output', str(self.db)], ['--output', str(self.config)],
                           ['--output', str(self.json_output)], ['--start-date', 'bad'],
                           ['--start-date', '2026-12-01', '--end-date', '2026-11-01']):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(self.arguments() + additional)

    def test_config_fields_are_required_and_names_unique(self):
        contents = json.loads(self.config.read_text())
        for modified in ({}, dict(contents, surprise=True), dict(contents, scenarios=[]),
                         dict(contents, scenarios=[asdict(ZERO), asdict(ZERO)]),
                         dict(contents, scenarios=[{'name': 'missing costs'}])):
            self.config.write_text(json.dumps(modified))
            with self.assertRaises(ValueError):
                load_config(self.config)

    def test_missing_db_is_not_created(self):
        missing = self.root / 'missing.db'
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(self.arguments() + ['--db', str(missing)])
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
