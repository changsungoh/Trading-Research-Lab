"""End-to-end failure boundaries, not duplicate engine arithmetic tests."""
from contextlib import closing, redirect_stderr
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from examples import run_research as demo
from examples.generate_synthetic_demo import make_data
from examples.verify_artifacts import verify
from quant_research_report import main as statistics
from execution_simulation_report import main as execution
from portfolio_risk_report import main as portfolio
from market_making_report import main as market_making

ROOT = Path(__file__).resolve().parents[1]


def small_dataset():
    clean, _, cutoff = make_data(days=1, signals_per_day=2)
    return clean, copy.deepcopy(clean), cutoff


class FailureBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root/'results'
        self.original = self.root/'existing.db'
        self.original.write_bytes(b'preserve existing input')
        patcher = patch.object(demo.database, 'DB_FILE', str(self.original))
        patcher.start()
        self.addCleanup(patcher.stop)
        generated = patch.object(demo, 'make_data', side_effect=small_dataset)
        generated.start()
        self.addCleanup(generated.stop)

    def assert_failed(self, stage):
        self.assertEqual(demo.database.DB_FILE, str(self.original))
        self.assertEqual(self.original.read_bytes(), b'preserve existing input')
        self.assertFalse((self.output/'manifest.json').exists())
        self.assertFalse((self.output/'synthetic.db').exists())
        self.assertFalse((self.output/'.research-run.lock').exists())
        self.assertFalse(list(self.output.glob('.build-*')))
        state = json.loads((self.output/'run_status.json').read_text())
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['stage'], stage)
        with self.assertRaisesRegex(ValueError, 'incomplete or failed'):
            verify(self.output)

    def test_schema_creation_failure_restores_database_destination(self):
        with patch.object(demo.database, 'init_db', side_effect=sqlite3.OperationalError('synthetic')):
            with self.assertRaises(sqlite3.OperationalError):
                demo.run(self.output)
        self.assert_failed('build_database')

    def test_failure_after_first_insert_cannot_publish_partial_database(self):
        original_insert = demo.database.insert_signal_research
        inserted = []
        def fail_second(row):
            if inserted:
                raise sqlite3.OperationalError('synthetic second insert failure')
            inserted.append(row['code'])
            return original_insert(row)
        with patch.object(demo.database, 'insert_signal_research', side_effect=fail_second):
            with self.assertRaises(sqlite3.OperationalError):
                demo.run(self.output)
        self.assertEqual(len(inserted), 1)
        self.assert_failed('build_database')

    def test_data_generation_failure_is_marked_before_any_database_write(self):
        with patch.object(demo, 'make_data', side_effect=ValueError('synthetic generation failure')):
            with self.assertRaises(ValueError):
                demo.run(self.output)
        self.assert_failed('generate_data')

    def test_each_analysis_stage_failure_leaves_no_completed_result(self):
        for stage in ('statistics', 'execution', 'portfolio'):
            with self.subTest(stage=stage):
                self.output = self.root/stage
                with patch.object(demo, stage, side_effect=RuntimeError('synthetic analysis failure')):
                    with self.assertRaises(RuntimeError):
                        demo.run(self.output)
                self.assert_failed(stage)

    def test_interrupted_analysis_cleans_up_and_does_not_claim_success(self):
        with patch.object(demo, 'statistics', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                demo.run(self.output)
        self.assert_failed('statistics')

    def test_invalid_prices_are_rejected_before_analysis(self):
        for index, value in enumerate((None, 0, -1, float('nan'), float('inf'), True)):
            with self.subTest(value=value):
                self.output = self.root/f'bad-price-{index}'
                data = small_dataset()
                data[1][0]['entry_price'] = value
                with patch.object(demo, 'make_data', return_value=data), patch.object(demo, 'statistics') as analyze:
                    with self.assertRaises(ValueError):
                        demo.run(self.output)
                    analyze.assert_not_called()
                self.assert_failed('validate_data')

    def test_malformed_entry_target_and_observed_timestamps_fail_validation(self):
        for index, field in enumerate(('entry_time', 'target_timestamp_5m', 'observed_timestamp_5m')):
            with self.subTest(field=field):
                self.output = self.root/f'bad-time-{index}'
                data = small_dataset()
                data[1][0][field] = 'not-a-timestamp'
                with patch.object(demo, 'make_data', return_value=data):
                    with self.assertRaises(ValueError):
                        demo.run(self.output)
                self.assert_failed('validate_data')

    def test_valid_label_cannot_hide_wrong_target_price_or_late_receipt(self):
        cases = [('price_5m', 999), ('target_timestamp_5m', '2025-01-06 10:06:00'),
                 ('observed_timestamp_5m', '2025-01-06 10:07:00'), ('ret_5m', float('nan'))]
        for index, (field, value) in enumerate(cases):
            with self.subTest(field=field):
                self.output = self.root/f'bad-metadata-{index}'
                data = small_dataset()
                data[1][0][field] = value
                with patch.object(demo, 'make_data', return_value=data):
                    with self.assertRaises(ValueError):
                        demo.run(self.output)
                self.assert_failed('validate_data')

    def test_duplicate_research_identity_is_not_silently_counted_twice(self):
        data = small_dataset()
        data[1].append(copy.deepcopy(data[1][0]))
        with patch.object(demo, 'make_data', return_value=data):
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                demo.run(self.output)
        self.assert_failed('validate_data')

    def test_unavailable_observations_cannot_carry_returns(self):
        data = small_dataset()
        data[1][0]['status_5m'] = 'MISSING'
        data[1][0]['ret_5m'] = 7
        with patch.object(demo, 'make_data', return_value=data):
            with self.assertRaisesRegex(ValueError, 'cannot carry a return'):
                demo.run(self.output)
        self.assert_failed('validate_data')

    def test_empty_input_and_out_of_range_holdout_are_rejected(self):
        for index, data in enumerate((([], [], '2025-01-06'),
                                      (*small_dataset()[:2], '2099-01-01'))):
            with self.subTest(index=index):
                self.output = self.root/f'bad-dataset-{index}'
                with patch.object(demo, 'make_data', return_value=data):
                    with self.assertRaises(ValueError):
                        demo.run(self.output)
                self.assert_failed('validate_data')

    def test_non_synthetic_symbol_and_unknown_quality_status_are_rejected(self):
        for index, (field, value) in enumerate((('code', 'NOT_SYNTHETIC'), ('status_5m', 'UNRECOGNIZED'))):
            with self.subTest(field=field):
                self.output = self.root/f'bad-contract-{index}'
                data = small_dataset()
                data[1][0][field] = value
                with patch.object(demo, 'make_data', return_value=data):
                    with self.assertRaises(ValueError):
                        demo.run(self.output)
                self.assert_failed('validate_data')

    def test_legitimate_missing_prices_complete_with_unknown_terminal_equity(self):
        data = small_dataset()
        for row in data[1]:
            for minute in (5, 10, 30):
                row.update({f'status_{minute}m': 'MISSING', f'ret_{minute}m': None,
                            f'price_{minute}m': None, f'observed_timestamp_{minute}m': None})
        with patch.object(demo, 'make_data', return_value=data):
            manifest = demo.run(self.output)
        result = json.loads((self.output/'execution.json').read_text(encoding='utf-8'))['results'][0]
        self.assertEqual(manifest['status'], 'complete')
        self.assertGreater(result['unresolved'], 0)
        self.assertIsNone(result['terminal_equity'])
        self.assertGreater(verify(self.output), 0)

    def test_read_only_contract_violation_prevents_completion(self):
        original = demo.statistics
        def modifying_analysis(arguments):
            result = original(arguments)
            db = arguments[arguments.index('--db')+1]
            with closing(sqlite3.connect(db)) as connection, connection:
                connection.execute('UPDATE signal_research SET entry_price=entry_price+1')
            return result
        with patch.object(demo, 'statistics', side_effect=modifying_analysis):
            with self.assertRaisesRegex(RuntimeError, 'modified its input'):
                demo.run(self.output)
        self.assert_failed('verify_input')

    def test_manifest_publish_failure_is_detectably_incomplete(self):
        original = demo.os.replace
        def fail_commit(source, target):
            if Path(target).name == 'manifest.json':
                raise OSError('synthetic manifest publication failure')
            return original(source, target)
        with patch.object(demo.os, 'replace', side_effect=fail_commit):
            with self.assertRaises(OSError):
                demo.run(self.output)
        self.assertFalse((self.output/'manifest.json').exists())
        self.assertFalse((self.output/'.research-run.lock').exists())
        self.assertEqual(json.loads((self.output/'run_status.json').read_text())['status'], 'failed')
        with self.assertRaisesRegex(ValueError, 'incomplete or failed'):
            verify(self.output)

    def test_completion_status_failure_cannot_leave_a_complete_manifest(self):
        original = demo._status
        def fail_completion(output, state, stage, error=None):
            if state == 'complete':
                raise OSError('synthetic completion status failure')
            return original(output, state, stage, error)
        with patch.object(demo, '_status', side_effect=fail_completion):
            with self.assertRaises(OSError):
                demo.run(self.output)
        self.assertFalse((self.output/'manifest.json').exists())
        self.assertEqual(json.loads((self.output/'run_status.json').read_text())['status'], 'failed')
        with self.assertRaises(ValueError):
            verify(self.output)

    def test_existing_input_files_and_source_directory_are_never_output_targets(self):
        config = self.root/'config.json'
        config.write_text('{"preserve": true}')
        for target in (self.original, config, self.root):
            with self.subTest(target=target):
                before = (self.original.read_bytes(), config.read_bytes())
                with patch.object(demo, 'make_data') as generate:
                    with self.assertRaises(ValueError):
                        demo.run(target)
                    generate.assert_not_called()
                self.assertEqual((self.original.read_bytes(), config.read_bytes()), before)

    def test_second_run_cannot_take_over_in_progress_destination(self):
        entered, release = threading.Event(), threading.Event()
        def blocked_data():
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test synchronization timed out')
            return small_dataset()
        with ThreadPoolExecutor(max_workers=1) as executor, patch.object(demo, 'make_data', side_effect=blocked_data):
            future = executor.submit(demo.run, self.output)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(ValueError):
                    demo.run(self.output)
                self.assertEqual(json.loads((self.output/'run_status.json').read_text())['status'], 'in_progress')
                with self.assertRaisesRegex(ValueError, 'incomplete or failed'):
                    verify(self.output)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=20)['status'], 'complete')


class ReproducibilityAndVerifierTests(unittest.TestCase):
    def test_identical_inputs_configs_and_seeds_reproduce_all_analysis_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            first, second = Path(temp)/'first', Path(temp)/'second'
            left, right = demo.run(first), demo.run(second)
            self.assertEqual(left, right)
            for name in left['files']:
                self.assertEqual((first/name).read_bytes(), (second/name).read_bytes(), name)
            self.assertEqual(verify(first), len(left['files']))
            self.assertEqual(verify(second), len(right['files']))

    def test_failed_status_overrides_a_stale_complete_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'file.txt').write_bytes(b'synthetic')
            (root/'manifest.json').write_text(json.dumps(dict(files={'file.txt': hashlib.sha256(b'synthetic').hexdigest()})))
            (root/'run_status.json').write_text('{"status":"failed"}')
            with self.assertRaisesRegex(ValueError, 'incomplete or failed'):
                verify(root)

    def test_empty_missing_or_incomplete_manifest_cannot_verify(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, 'No completed'):
                verify(root)
            for record in (dict(files={}), dict(status='in_progress', files={'missing.txt':'bad'}),
                           dict(files={'missing.txt':'bad'})):
                (root/'manifest.json').write_text(json.dumps(record))
                with self.assertRaises(ValueError):
                    verify(root)


class ReportAliasSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = self.root/'input.db'
        with patch.object(demo.database, 'DB_FILE', str(self.db)):
            demo.database.init_db()
            demo.database.insert_signal_research(small_dataset()[1][0])

    def commands(self):
        return [(statistics, ['--db', str(self.db)]),
                (execution, ['--db', str(self.db), '--config', str(ROOT/'simulation/scenarios.example.json'), '--horizon', '5']),
                (portfolio, ['--db', str(self.db), '--config', str(ROOT/'simulation/allocation.example.json'), '--horizon', '5']),
                (market_making, ['--config', str(ROOT/'simulation/market_making.example.json')])]

    def rejects(self, function, arguments):
        error = io.StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as stopped:
            function(arguments)
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn('must differ', error.getvalue())

    def test_hardlinked_database_output_cannot_overwrite_input(self):
        before = self.db.read_bytes()
        for index, (function, arguments) in enumerate(self.commands()[:3]):
            alias = self.root/f'input-alias-{index}.md'
            os.link(self.db, alias)
            self.rejects(function, arguments+['--output', str(alias)])
            self.assertEqual(self.db.read_bytes(), before)

    def test_hardlinked_config_output_cannot_overwrite_input(self):
        for index, (function, arguments) in enumerate(self.commands()[1:]):
            original = Path(arguments[arguments.index('--config')+1])
            config = self.root/f'config-{index}.json'
            config.write_bytes(original.read_bytes())
            arguments = list(arguments)
            arguments[arguments.index('--config')+1] = str(config)
            alias = self.root/f'config-alias-{index}.json'
            os.link(config, alias)
            before = config.read_bytes()
            self.rejects(function, arguments+['--json-output', str(alias)])
            self.assertEqual(config.read_bytes(), before)

    def test_hardlinked_markdown_and_json_outputs_are_rejected_for_every_cli(self):
        for index, (function, arguments) in enumerate(self.commands()):
            markdown, structured = self.root/f'output-{index}.md', self.root/f'output-{index}.json'
            markdown.write_bytes(b'preserve output')
            os.link(markdown, structured)
            self.rejects(function, arguments+['--output', str(markdown), '--json-output', str(structured)])
            self.assertEqual(markdown.read_bytes(), b'preserve output')


if __name__ == '__main__':
    unittest.main()
