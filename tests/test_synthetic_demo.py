"""Check synthetic demo semantics and delivered audit artifacts without plotting dependencies."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from examples.generate_synthetic_demo import build, make_data
from research.statistical_analysis import valid_observation


class SyntheticDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temp.name)
        cls.results = build(cls.output, plots=False)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_dataset_is_seeded_fictional_and_clean_metadata_valid(self):
        clean, dirty, cutoff = make_data()
        self.assertEqual((clean, dirty, cutoff), make_data())
        self.assertEqual(len(clean), 360)
        self.assertEqual(len({row['entry_time'][:10] for row in clean}), 60)
        self.assertTrue(all(row['code'].startswith('SYNTH_') for row in clean))
        self.assertTrue(all(valid_observation(row, m)[1] == 'VALID' for row in clean for m in (5,10,30)))
        self.assertTrue(any(valid_observation(row,10)[0] is None for row in dirty))
        # Complete counterfactual is not a surviving subset of the degraded input.
        self.assertEqual([r['entry_time'] for r in clean], [r['entry_time'] for r in dirty])

    def test_quality_gaps_cannot_be_reported_as_a_priced_terminal_result(self):
        zero, friction, partial, degraded = self.results['executions']
        self.assertEqual(zero['known_explicit_costs'], 0)
        self.assertGreater(friction['known_explicit_costs'], 0)
        self.assertGreater(zero['portfolio_return_pct'], friction['portfolio_return_pct'])
        self.assertEqual(zero['unresolved'],0)
        for result in (partial,degraded):
            self.assertGreater(result['unresolved'],0)
            self.assertIsNone(result['portfolio_return_pct'])
            self.assertGreater(result['reserved_entry_outlay'],0)

    def test_allocation_history_is_past_only_and_makers_share_tape(self):
        volatility = self.results['allocation']['results'][2]
        decisions = [d for d in volatility['decisions'] if d.get('latest_history_observed')]
        self.assertTrue(decisions)
        self.assertTrue(all(datetime.fromisoformat(d['latest_history_observed']) < datetime.fromisoformat(d['entry_time']) for d in decisions))
        first,second = self.results['maker']['results']
        self.assertEqual(first['tape_sha256'],second['tape_sha256'])
        for result in (first,second):
            self.assertAlmostEqual(result['marked_equity'],result['ending_cash']+result['inventory_mark_value'])

    def test_manifest_hashes_and_labels_match_delivered_files(self):
        manifest=json.loads((self.output/'manifest.json').read_text(encoding='utf-8'))
        self.assertIn('SYNTHETIC DATA ONLY',manifest['label'])
        for name,expected in manifest['files'].items():
            self.assertEqual(hashlib.sha256((self.output/name).read_bytes()).hexdigest(),expected)
        text=(self.output/'README.md').read_text(encoding='utf-8')
        self.assertIn('NOT LIVE PERFORMANCE',text)
        self.assertIn('counterfactual',text)
        self.assertIn('N/A (unpriced positions)',text)
        for name in ('statistics.json','execution.json','allocation.json','market_making.json'):
            json.loads((self.output/name).read_text(encoding='utf-8'))


if __name__=='__main__':
    unittest.main()
