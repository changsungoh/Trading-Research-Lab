"""One-command synthetic SQLite -> P1/P2/P3 walkthrough. No real data required."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import os
import tempfile
from datetime import date

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
import database
from research.statistical_analysis import finite, timestamp, valid_observation
from examples.generate_synthetic_demo import make_data,LABEL
from quant_research_report import main as statistics
from execution_simulation_report import main as execution
from portfolio_risk_report import main as portfolio


def validate_rows(rows, cutoff):
    if not isinstance(rows, list) or not rows:
        raise ValueError('Synthetic input must contain rows')
    cutoff = date.fromisoformat(cutoff)
    identities, days = set(), set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not str(row.get('code', '')).startswith('SYNTH_'):
            raise ValueError(f'Row {index}: synthetic symbol required')
        entry = timestamp(row.get('entry_time'))
        days.add(entry.date())
        if finite(row.get('entry_price')) <= 0:
            raise ValueError(f'Row {index}: entry price must be positive')
        identity = (row['code'], entry, row.get('rule_version') or '')
        if identity in identities:
            raise ValueError(f'Row {index}: duplicate research identity')
        identities.add(identity)
        for minute in (5, 10, 30):
            status = row.get(f'status_{minute}m')
            if status not in (None, '', 'VALID', 'MISSING', 'STALE', 'OUT_OF_SESSION', 'LEGACY_UNVERIFIED'):
                raise ValueError(f'Row {index}: unknown observation status')
            for field in ('target_timestamp', 'observed_timestamp'):
                value = row.get(f'{field}_{minute}m')
                if value not in (None, ''):
                    timestamp(value)
            price = row.get(f'price_{minute}m')
            if price not in (None, '') and finite(price) <= 0:
                raise ValueError(f'Row {index}: observed price must be positive')
            value = row.get(f'ret_{minute}m')
            if value not in (None, ''):
                finite(value)
            if status in ('MISSING', 'STALE', 'OUT_OF_SESSION') and value not in (None, ''):
                raise ValueError(f'Row {index}: unavailable observation cannot carry a return')
            if status == 'VALID' and valid_observation(row, minute)[0] is None:
                raise ValueError(f'Row {index}: invalid VALID observation metadata')
    if not min(days) <= cutoff <= max(days):
        raise ValueError('Holdout boundary must be within the synthetic input dates')


def _build(output, rows, cutoff, stage):
    db = output / 'synthetic.db'
    stage('build_database')
    previous=database.DB_FILE
    try:
        database.DB_FILE=str(db)
        database.init_db()
        for row in rows:database.insert_signal_research(row)
    finally:database.DB_FILE=previous
    before=hashlib.sha256(db.read_bytes()).hexdigest()
    stage('statistics')
    statistics(['--db',str(db),'--holdout-start',cutoff,'--output',str(output/'statistics.md'),
                '--json-output',str(output/'statistics.json')])
    stage('execution')
    execution(['--db',str(db),'--config',str(ROOT/'simulation/scenarios.example.json'),'--horizon','5',
               '--output',str(output/'execution.md'),'--json-output',str(output/'execution.json')])
    stage('portfolio')
    portfolio(['--db',str(db),'--config',str(ROOT/'simulation/allocation.example.json'),'--horizon','5',
               '--output',str(output/'portfolio.md'),'--json-output',str(output/'portfolio.json')])
    stage('verify_input')
    if hashlib.sha256(db.read_bytes()).hexdigest()!=before:raise RuntimeError('Analysis unexpectedly modified its input database')
    stage('label_reports')
    for name in ('statistics','execution','portfolio'):
        markdown=output/(name+'.md')
        markdown.write_text('> '+LABEL+'\n\n'+markdown.read_text(encoding='utf-8'),encoding='utf-8',newline='\n')
        data=output/(name+'.json')
        payload=json.loads(data.read_text(encoding='utf-8'))
        data.write_text(json.dumps(dict(data_label=LABEL,**payload),indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf-8',newline='\n')
    (output/'README.md').write_text('# Synthetic SQLite walkthrough\n\n'+LABEL+'\n\n'
        f'{len(rows)} fictional candidate windows. Quality masking preserves missing/stale/unverified outcomes.\n\n'
        '- statistics.md / statistics.json: P1 quality and uncertainty\n'
        '- execution.md / execution.json: P2 illustrative costs, fills and cash reservation\n'
        '- portfolio.md / portfolio.json: P3 allocation and simultaneous stress\n\n'
        'Unknown exits remain unresolved; null terminal equity is intentional. No real collector, broker, credentials or private entry rules are used.\n',encoding='utf-8',newline='\n')
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()}
    manifest = dict(schema_version=1, status='complete', label=LABEL, rows=len(rows),
                    holdout_start=cutoff, input_unchanged=True, files=files)
    (output/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8', newline='\n')
    return manifest


def _status(output, state, stage, error=None):
    payload = dict(status=state, stage=stage)
    if error is not None:
        payload['error_type'] = type(error).__name__
    temporary = output / '.run_status.tmp'
    temporary.write_text(json.dumps(payload)+'\n', encoding='utf-8', newline='\n')
    os.replace(temporary, output/'run_status.json')


def run(output):
    requested = Path(output)
    if requested.is_symlink():
        raise ValueError('Output must not be a directory symlink')
    output = requested.resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError('Use a new or empty output directory; existing files are preserved')
    output.mkdir(parents=True, exist_ok=True)
    lock = output / '.research-run.lock'
    handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(handle)
    current = ['generate_data']
    def stage(value):
        current[0] = value
    try:
        _status(output, 'in_progress', current[0])
        _, rows, cutoff = make_data()
        stage('validate_data')
        validate_rows(rows, cutoff)
        with tempfile.TemporaryDirectory(dir=output, prefix='.build-') as temporary:
            work = Path(temporary)
            manifest = _build(work, rows, cutoff, stage)
            stage('publish')
            for name in manifest['files']:
                os.replace(work/name, output/name)
            # The manifest is the final commit marker, never a partial result.
            os.replace(work/'manifest.json', output/'manifest.json')
            _status(output, 'complete', 'publish')
        return manifest
    except BaseException as error:
        try:
            (output/'manifest.json').unlink(missing_ok=True)
            _status(output, 'failed', current[0], error)
        except OSError:
            pass  # No manifest exists; failure must not mask the original error.
        raise
    finally:
        lock.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'demo_output')
    args = parser.parse_args()
    run(args.output)
    print('Generated synthetic SQLite and P1/P2/P3 reports:', args.output)
