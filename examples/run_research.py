"""One-command synthetic SQLite -> P1/P2/P3 walkthrough. No real data required."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
import database
from examples.generate_synthetic_demo import make_data,LABEL
from quant_research_report import main as statistics
from execution_simulation_report import main as execution
from portfolio_risk_report import main as portfolio


def run(output):
    output=Path(output).resolve()
    if output.exists() and any(output.iterdir()):raise ValueError('Use a new or empty output directory; existing files are preserved')
    output.mkdir(parents=True,exist_ok=True)
    _,rows,cutoff=make_data()
    db=output/'synthetic.db'
    previous=database.DB_FILE
    try:
        database.DB_FILE=str(db)
        database.init_db()
        for row in rows:database.insert_signal_research(row)
    finally:database.DB_FILE=previous
    before=hashlib.sha256(db.read_bytes()).hexdigest()
    statistics(['--db',str(db),'--holdout-start',cutoff,'--output',str(output/'statistics.md'),
                '--json-output',str(output/'statistics.json')])
    execution(['--db',str(db),'--config',str(ROOT/'simulation/scenarios.example.json'),'--horizon','5',
               '--output',str(output/'execution.md'),'--json-output',str(output/'execution.json')])
    portfolio(['--db',str(db),'--config',str(ROOT/'simulation/allocation.example.json'),'--horizon','5',
               '--output',str(output/'portfolio.md'),'--json-output',str(output/'portfolio.json')])
    if hashlib.sha256(db.read_bytes()).hexdigest()!=before:raise RuntimeError('Analysis unexpectedly modified its input database')
    for name in ('statistics','execution','portfolio'):
        markdown=output/(name+'.md')
        markdown.write_text('> '+LABEL+'\n\n'+markdown.read_text(encoding='utf-8'),encoding='utf-8',newline='\n')
        data=output/(name+'.json')
        payload=json.loads(data.read_text(encoding='utf-8'))
        data.write_text(json.dumps(dict(data_label=LABEL,**payload),indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf-8',newline='\n')
    (output/'README.md').write_text('# Synthetic SQLite walkthrough\n\n'+LABEL+'\n\n'
        '360 fictional candidate windows. Quality masking preserves missing/stale/unverified outcomes.\n\n'
        '- statistics.md / statistics.json: P1 quality and uncertainty\n'
        '- execution.md / execution.json: P2 illustrative costs, fills and cash reservation\n'
        '- portfolio.md / portfolio.json: P3 allocation and simultaneous stress\n\n'
        'Unknown exits remain unresolved; null terminal equity is intentional. No real collector, broker, credentials or private entry rules are used.\n',encoding='utf-8',newline='\n')
    files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()}
    manifest=dict(label=LABEL,rows=len(rows),holdout_start=cutoff,input_unchanged=True,files=files)
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8',newline='\n')
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'demo_output')
    args=parser.parse_args()
    run(args.output)
    print('Generated synthetic SQLite and P1/P2/P3 reports:',args.output)
