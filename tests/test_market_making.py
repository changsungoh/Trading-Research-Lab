from contextlib import redirect_stderr
from dataclasses import asdict, replace
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from market_making_report import experiment, load_config, load_tape, main
from simulation.market_making_simulator import MakerPolicy, MarketModel, TapeEvent, generate_tape, quote, simulate_market_maker

POLICY = MakerPolicy('test', 1000, 100, 1, 2, 10, 1, 100, 0, 1, 0, 100, 1)
MODEL = MarketModel(20, 100, 5, 0, .8, .3, 20, 5)


class MakerTests(unittest.TestCase):
    def test_roundtrip_spread_capture_and_cash_conservation(self):
        tape = [TapeEvent(1, 100, 'sell', 90, 2), TapeEvent(2, 100, 'buy', 110, 2)]
        result = simulate_market_maker(tape, POLICY)
        self.assertEqual(result['ending_inventory'], 0)
        self.assertEqual(result['ending_cash'], 1004)
        self.assertEqual(result['marked_pnl'], 4)
        self.assertEqual(result['quoted_edge_total'], 4)
        self.assertEqual(result['fills'], 2)

    def test_fees_charged_on_both_sides(self):
        tape = [TapeEvent(1, 100, 'sell', 90, 2), TapeEvent(2, 100, 'buy', 110, 2)]
        result = simulate_market_maker(tape, replace(POLICY, fee_bps=100))
        self.assertAlmostEqual(result['total_fees'], 4)
        self.assertAlmostEqual(result['marked_pnl'], 0)

    def test_inventory_skew_moves_both_quotes_down_for_long_inventory(self):
        policy = replace(POLICY, inventory_skew_bps=500)
        flat = quote(policy, 100, 0, 1000, 1000)
        long = quote(policy, 100, 5, 1000, 1000)
        short = quote(policy, 100, -5, 1000, 1000)
        self.assertLess(long['bid'], flat['bid'])
        self.assertLess(long['ask'], flat['ask'])
        self.assertGreater(short['bid'], flat['bid'])
        self.assertGreater(short['ask'], flat['ask'])
        self.assertLess(long['bid'], long['ask'])

    def test_cash_and_inventory_and_whole_share_constraints(self):
        tape = [TapeEvent(i, 100, 'sell', 90, 20) for i in range(1, 5)]
        result = simulate_market_maker(tape, replace(POLICY, max_inventory=3, quote_quantity=10))
        self.assertEqual(result['ending_inventory'], 3)
        self.assertEqual(result['max_abs_inventory'], 3)
        self.assertTrue(all(item['cash'] >= -1e-9 for item in result['ledger']))
        small = simulate_market_maker(tape, replace(POLICY, initial_cash=50))
        self.assertEqual(small['fills'], 0)
        self.assertEqual(small['ending_cash'], 50)

    def test_short_sale_proceeds_are_not_marked_profit(self):
        result = simulate_market_maker([TapeEvent(1, 100, 'buy', 110, 1)], POLICY)
        self.assertEqual(result['ending_inventory'], -1)
        self.assertEqual(result['ending_cash'], 1101)
        self.assertEqual(result['inventory_mark_value'], -100)
        self.assertEqual(result['marked_equity'], 1001)
        self.assertFalse(result['terminal_flat'])

    def test_notional_cap_limits_quotes(self):
        active = quote(replace(POLICY, max_notional_fraction=.1, quote_quantity=10), 100, 0, 1000, 1000)
        self.assertEqual(active['bid_quantity'], 1)
        self.assertEqual(active['ask_quantity'], 0)

    def test_non_crossing_flow_never_fills(self):
        result = simulate_market_maker([TapeEvent(1, 100, 'buy', 100, 20), TapeEvent(2, 100, 'sell', 100, 20)], POLICY)
        self.assertEqual(result['fills'], 0)
        self.assertIsNone(result['mean_future_markout'])

    def test_adverse_selection_and_end_of_tape_markouts(self):
        tape = [TapeEvent(1, 90, 'sell', 80, 1), TapeEvent(2, 80, 'none', 80, 0)]
        result = simulate_market_maker(tape, POLICY)
        fill = result['fills_log'][0]
        self.assertEqual(fill['quoted_edge'], 1)
        self.assertEqual(fill['immediate_markout'], -9)
        self.assertEqual(fill['future_markout'], -19)
        self.assertEqual(fill['future_mid_move'], -10)
        final = simulate_market_maker([TapeEvent(1, 100, 'sell', 90, 1)], POLICY)
        self.assertIsNone(final['fills_log'][0]['future_markout'])
        self.assertEqual(final['future_markout_count'], 0)
        self.assertEqual(final['fills'], 1)

    def test_current_and_future_event_prices_do_not_leak_into_current_quote(self):
        a = [TapeEvent(1, 100, 'sell', 90, 1), TapeEvent(2, 100, 'none', 100, 0)]
        b = [TapeEvent(1, 80, 'sell', 90, 1), TapeEvent(2, 500, 'none', 500, 0)]
        first, second = simulate_market_maker(a, POLICY), simulate_market_maker(b, POLICY)
        self.assertEqual(first['quotes'][0], second['quotes'][0])
        for field in ('price', 'quantity', 'side'):
            self.assertEqual(first['fills_log'][0][field], second['fills_log'][0][field])
        extended = a + [TapeEvent(3, 500, 'buy', 600, 1)]
        full = simulate_market_maker(extended, POLICY)
        self.assertEqual(first['quotes'], full['quotes'][:2])
        self.assertEqual(first['ledger'], full['ledger'][:2])

    def test_halt_cancels_future_quotes_but_does_not_invent_liquidation(self):
        tape = [TapeEvent(1, 70, 'sell', 60, 1), TapeEvent(2, 60, 'buy', 100, 20)]
        result = simulate_market_maker(tape, replace(POLICY, max_drawdown_pct=1))
        self.assertTrue(result['halted'])
        self.assertIsNone(result['quotes'][1]['quote'])
        self.assertEqual(result['ending_inventory'], 1)
        self.assertEqual(result['marked_pnl'], -39)
        self.assertGreater(result['max_marked_drawdown_pct'], 1)

    def test_pnl_equals_cash_plus_mark_and_sum_of_signed_fill_pnls(self):
        result = simulate_market_maker(generate_tape(MODEL, 23), replace(POLICY, tick_size=.01, half_spread_bps=1))
        calculated = sum((1 if fill['side'] == 'buy' else -1) * fill['quantity'] *
                         (result['final_mid'] - fill['price']) - fill['fee'] for fill in result['fills_log'])
        self.assertAlmostEqual(calculated, result['marked_pnl'])
        self.assertAlmostEqual(result['quoted_edge_total'] + result['fair_revaluation_pnl'] - result['total_fees'], result['marked_pnl'])
        self.assertAlmostEqual(result['ending_cash'] + result['ending_inventory'] * result['final_mid'], result['marked_equity'])

    def test_tick_rounding_and_uncrossed_quotes(self):
        active = quote(replace(POLICY, tick_size=.25, half_spread_bps=10), 100.13, 0, 1000, 1000)
        self.assertEqual(active['bid'] * 4, round(active['bid'] * 4))
        self.assertEqual(active['ask'] * 4, round(active['ask'] * 4))
        self.assertGreater(active['ask'], active['bid'])

    def test_replay_steps_must_be_ordered_and_contiguous(self):
        with self.assertRaises(ValueError):
            simulate_market_maker([TapeEvent(2, 100, 'none', 100, 0)], POLICY)
        with self.assertRaises(ValueError):
            simulate_market_maker([{}], POLICY)

    def test_invalid_numbers_and_settings_rejected(self):
        for change in ({'fee_bps': float('nan')}, {'initial_cash': '100'}, {'ewma_alpha': 0},
                       {'max_notional_fraction': 2}, {'quote_quantity': True}, {'tick_size': 100},
                       {'half_spread_bps': 10000}, {'markout_steps': 0}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(POLICY, **change)
        for args in ((1, float('inf'), 'none', 100, 0), (1, 100, 'none', 100, 1), (1, 100, 'BUY', 100, 1)):
            with self.assertRaises(ValueError):
                TapeEvent(*args)


class MarketTests(unittest.TestCase):
    def test_synthetic_tape_is_seeded_and_policy_independent(self):
        first, second = generate_tape(MODEL, 11), generate_tape(MODEL, 11)
        self.assertEqual(first, second)
        self.assertNotEqual(first, generate_tape(MODEL, 12))
        report = experiment(MODEL, [POLICY, replace(POLICY, name='skew', inventory_skew_bps=100)], [11, 12])
        self.assertEqual(len(report['results']), 4)
        self.assertEqual(report['results'][0]['tape_sha256'], report['results'][1]['tape_sha256'])
        self.assertNotEqual(report['results'][0]['tape_sha256'], report['results'][2]['tape_sha256'])

    def test_no_arrivals_and_supplied_tape_not_duplicated_per_seed(self):
        tape = generate_tape(replace(MODEL, arrival_probability=0), 11)
        self.assertTrue(all(event.quantity == 0 for event in tape))
        report = experiment(MODEL, [POLICY], [11, 12], tape)
        self.assertEqual(len(report['results']), 1)
        self.assertIsNone(report['results'][0]['seed'])
        self.assertEqual(report['results'][0]['fills'], 0)

    def test_invalid_market_settings_rejected(self):
        for change in ({'steps': True}, {'arrival_probability': 2}, {'informed_probability': -1},
                       {'volatility_bps': float('inf')}, {'drift_bps': '1'}):
            with self.assertRaises(ValueError):
                replace(MODEL, **change)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config, self.tape = self.root / 'config.json', self.root / 'tape.json'
        self.config.write_text(json.dumps(dict(market=asdict(MODEL),policies=[asdict(POLICY)],seeds=[11,12])))
        self.tape.write_text(json.dumps([asdict(TapeEvent(1,100,'sell',90,1))]))
        self.md, self.js = self.root / 'report.md', self.root / 'report.json'

    def args(self):
        return ['--config',str(self.config),'--output',str(self.md),'--json-output',str(self.js)]

    def test_reports_reproducible_finite_and_inputs_unchanged(self):
        before = self.config.read_bytes(), self.tape.read_bytes()
        first = main(self.args())
        self.assertEqual(first, json.loads(self.js.read_text(encoding='utf-8')))
        self.assertEqual(first, main(self.args()))
        supplied = main(self.args() + ['--tape',str(self.tape)])
        self.assertEqual(len(supplied['results']),1)
        self.assertEqual(before, (self.config.read_bytes(),self.tape.read_bytes()))
        json.dumps(supplied,allow_nan=False)
        self.assertIn('P4 market-making',self.md.read_text(encoding='utf-8'))

    def test_standalone_cli(self):
        script = Path(__file__).resolve().parents[1] / 'market_making_report.py'
        result = subprocess.run([sys.executable,str(script)] + self.args(),cwd=self.root,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_source_overwrite_and_missing_tape_rejected(self):
        for extra in (['--output',str(self.config)],['--output',str(self.js)],
                      ['--tape',str(self.tape),'--output',str(self.tape)],['--tape',str(self.root/'missing.json')]):
            with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                main(self.args()+extra)

    def test_config_and_tape_validation(self):
        base=json.loads(self.config.read_text())
        for value in (dict(base,extra=True),dict(base,seeds=[True]),dict(base,seeds=[1,1]),
                      dict(base,policies=[]),dict(base,policies=[asdict(POLICY)]*2),
                      dict(base,market=dict(asdict(MODEL),initial_mid=200))):
            self.config.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                load_config(self.config)
        for value in ([],[dict(asdict(TapeEvent(1,100,'none',100,0)),step=2)],[{'step':1}]):
            self.tape.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                load_tape(self.tape)


if __name__=='__main__':
    unittest.main()
