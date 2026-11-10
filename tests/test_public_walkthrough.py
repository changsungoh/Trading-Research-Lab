import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from examples.run_research import run
from examples.verify_artifacts import verify


class WalkthroughTests(unittest.TestCase):
    def test_complete_walkthrough_is_synthetic_and_preserves_input(self):
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'demo'
            manifest=run(output)
            self.assertEqual(manifest['rows'],360)
            self.assertTrue(manifest['input_unchanged'])
            with sqlite3.connect(output/'synthetic.db') as connection:
                codes=[r[0] for r in connection.execute('SELECT DISTINCT code FROM signal_research')]
            connection.close()
            self.assertTrue(all(code.startswith('SYNTH_') for code in codes))
            for name,expected in manifest['files'].items():
                self.assertEqual(hashlib.sha256((output/name).read_bytes()).hexdigest(),expected)
            self.assertTrue((output/'portfolio.json').exists())
    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'keep.txt';path.write_text('preserve')
            with self.assertRaises(ValueError):run(temp)
            self.assertEqual(path.read_text(),'preserve')
    def test_artifact_verifier_detects_tampering_and_path_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'input.txt';path.write_bytes(b'synthetic\n')
            manifest=dict(files={'input.txt':hashlib.sha256(path.read_bytes()).hexdigest()})
            (root/'manifest.json').write_text(json.dumps(manifest))
            self.assertEqual(verify(root),1)
            path.write_bytes(b'changed')
            with self.assertRaises(ValueError):verify(root)
            (root/'manifest.json').write_text(json.dumps(dict(files={'../outside.txt':'bad'})))
            with self.assertRaises(ValueError):verify(root)
