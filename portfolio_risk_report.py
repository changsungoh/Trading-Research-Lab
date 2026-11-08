"""P3 offline allocation-policy comparisons and simultaneous downside stresses."""
import argparse
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
import sqlite3
from research.statistical_analysis import load_rows, timestamp
from simulation.execution_simulator import Scenario, fingerprint
from simulation.risk_simulator import RiskPolicy, simulate
from simulation.portfolio_allocation import AllocationPolicy, Stress, PastSignalHistory, Allocator


LIMITATIONS = [
    'Research scenarios only: no calibrated fills, live orders, allocation recommendations or automatic winner selection.',
    'Volatility uses same-symbol, same-horizon historical signal returns, not daily asset volatility or independent samples.',
    'Only observations completed strictly before each entry can inform sizing; insufficient history blocks volatility sizing.',
    'Sector labels are supplied static metadata, not inferred from themes or historically verified classifications.',
    'Caps use entry outlay and cost-basis equity, not current market-value exposure; no forced rebalance after losses.',
    'Stresses simultaneously shock entry-reference prices, then assume full liquidation with scenario costs; not market marks, VaR or probabilities.',
    'Market and sector stress multipliers compound. Stress results never replace unknown actual exits or terminal equity.',
    'Unknown exits keep cash reserved; realized risk stops cannot limit intratrade losses. Portfolio drawdown is cost-basis only.',
    'Filtered experiments restart with fresh cash; history may include earlier dates as warmup but never future outcomes.',
    'Repeated policy/stress selection can overfit; freeze assumptions before untouched-period validation.',
    'P2 entry/exit fill, rounding, execution and closed-trade selection limitations continue to apply.'
]


def load_config(path):
    with open(path, encoding='utf-8-sig') as stream:
        config = json.load(stream)
    required = {'portfolio', 'scenarios', 'allocations', 'sectors', 'stresses'}
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError('Config requires exactly: ' + ', '.join(sorted(required)))
    if not isinstance(config['portfolio'], dict) or not isinstance(config['sectors'], dict):
        raise ValueError('portfolio and sectors must be objects')
    for key in ('scenarios', 'allocations', 'stresses'):
        if not isinstance(config[key], list) or not config[key]:
            raise ValueError(f'{key} must be a nonempty array')
    try:
        policy = RiskPolicy(**config['portfolio'])
        scenarios = [Scenario(**item) for item in config['scenarios']]
        allocations = [AllocationPolicy(**item) for item in config['allocations']]
        stresses = [Stress(**item) for item in config['stresses']]
    except TypeError as error:
        raise ValueError(f'Invalid/missing config field: {error}') from error
    for collection in (scenarios, allocations, stresses):
        if len({item.name for item in collection}) != len(collection):
            raise ValueError('Names must be unique within each config collection')
    for code, sector in config['sectors'].items():
        if not isinstance(code, str) or not code.strip() or not isinstance(sector, str) or not sector.strip():
            raise ValueError('Sector mapping requires nonempty string codes and labels')
    known_sectors = set(config['sectors'].values()) | {'UNKNOWN'}
    for stress in stresses:
        if not set(stress.sector_returns_pct).issubset(known_sectors):
            raise ValueError('Stress names a sector missing from the supplied mapping')
    return policy, scenarios, allocations, config['sectors'], stresses


def compare(rows, policy, scenarios, allocations, sectors, stresses, *, horizon, seed=1729,
            start_date=None, end_date=None, setup=None, rule_version=None):
    start = date.fromisoformat(start_date) if start_date else None
    end = date.fromisoformat(end_date) if end_date else None
    if start and end and start >= end:
        raise ValueError('start-date must precede end-date')
    if horizon not in (5, 10, 30):
        raise ValueError('Choose a 5, 10 or 30 minute horizon')
    rows = list(rows)
    history_rows = [row for row in rows if (setup is None or row.get('setup_quality') == setup)
                    and (rule_version is None or row.get('rule_version') == rule_version)]
    history = PastSignalHistory(history_rows, horizon)
    candidates, undated = [], 0
    for row in history_rows:
        if start or end:
            try:
                day = timestamp(row['entry_time']).date()
            except (ValueError, KeyError, TypeError):
                undated += 1
                continue
            if (start and day < start) or (end and day >= end):
                continue
        candidates.append(row)
    results = []
    for scenario in scenarios:
        for allocation in allocations:
            manager = Allocator(allocation, sectors, history, stresses)
            result = simulate(candidates, scenario, policy, horizon=horizon, seed=seed, allocation=manager)
            result['allocation_analysis'] = manager.report()
            results.append(result)
    config = dict(portfolio=asdict(policy), scenarios=[asdict(item) for item in scenarios],
                  allocations=[asdict(item) for item in allocations], sectors=sectors,
                  stresses=[asdict(item) for item in stresses])
    return dict(schema_version=1, model='OFFLINE_ALLOCATION_COMPARISON',
        input_sha256=fingerprint(sorted(fingerprint(row) for row in rows)),
        history_sha256=fingerprint(sorted(fingerprint(row) for row in history_rows)), config_sha256=fingerprint(config),
        selection=dict(horizon=horizon, start_date=start_date, end_date=end_date, setup=setup, rule_version=rule_version),
        seed=seed, candidate_rows=len(candidates), history_rows=len(history_rows), undated_excluded=undated,
        config=config, limitations=LIMITATIONS, results=results)


def render_markdown(report):
    def fmt(value):
        return 'N/A' if value is None else f'{value:.4f}'
    def safe(value):
        return str(value).replace('|', '/').replace('\n', ' ')
    lines = ['# P3 allocation and concentration comparison', '', 'ILLUSTRATIVE RESEARCH — no recommended allocation or automatic winner.', '',
             f"Selection: {report['selection']}; seed: {report['seed']}; candidates: {report['candidate_rows']}; history rows: {report['history_rows']}.",
             f"Input SHA256: {report['input_sha256']}; history SHA256: {report['history_sha256']}; config SHA256: {report['config_sha256']}", '',
             '| Execution scenario | Allocation | Filled / closed / unpriced | Terminal return % | Realized net P&L | Peak gross % | Peak sector % | Cost-basis drawdown % |',
             '|---|---|---:|---:|---:|---:|---:|---:|']
    for result in report['results']:
        allocation = result['allocation_analysis']
        lines.append('| ' + ' | '.join([safe(result['scenario']['name']), safe(allocation['allocation']['name']),
            f"{result['filled']} / {result['closed']} / {result['unresolved']}", fmt(result['portfolio_return_pct']),
            fmt(result['realized_net_pnl']), fmt(allocation['peak_gross_fraction'] * 100),
            fmt(allocation['peak_sector_fraction'] * 100), fmt(result['max_cost_basis_drawdown_pct'])]) + ' |')
    for result in report['results']:
        analysis = result['allocation_analysis']
        lines += ['', f"## {safe(result['scenario']['name'])} / {safe(analysis['allocation']['name'])}",
                  f"Sizing policy: {analysis['allocation']}", f"Shared risk policy: {result['policy']}",
                  f"Execution assumptions: {result['scenario']}", f"Completeness: {result['completeness']}",
                  f"Decisions: {result['decision_counts']}; exit quality: {result['quality']}",
                  f"Known cash: {fmt(result['ending_cash'])}; reserved outlay: {fmt(result['reserved_entry_outlay'])}"]
        for name, stress in analysis['stress_worst'].items():
            lines.append(f"- Stress {safe(name)}: worst loss {fmt(stress['loss_pct'])}% of snapshot cost-basis equity; time={stress['time']}; sector losses={stress['sector_loss']}")
    lines += ['', '## Stress assumptions', '', str(report['config']['stresses']), '', '## Limits', '']
    lines += ['- ' + item for item in report['limitations']]
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='trading_engine.db')
    parser.add_argument('--config', required=True)
    parser.add_argument('--horizon', required=True, type=int, choices=(5, 10, 30))
    parser.add_argument('--start-date')
    parser.add_argument('--end-date')
    parser.add_argument('--setup')
    parser.add_argument('--rule-version')
    parser.add_argument('--seed', type=int, default=1729)
    parser.add_argument('--output')
    parser.add_argument('--json-output')
    args = parser.parse_args(argv)
    sources = {Path(path).resolve() for path in (args.db, args.config)}
    outputs = [Path(path).resolve() for path in (args.output, args.json_output) if path]
    if len(set(outputs)) != len(outputs) or any(path in sources for path in outputs):
        parser.error('Report outputs must differ from inputs and each other')
    try:
        policy, scenarios, allocations, sectors, stresses = load_config(args.config)
        report = compare(load_rows(args.db), policy, scenarios, allocations, sectors, stresses,
                         horizon=args.horizon, seed=args.seed, start_date=args.start_date, end_date=args.end_date,
                         setup=args.setup, rule_version=args.rule_version)
        serialized = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + '\n'
        markdown = render_markdown(report)
        if args.output:
            Path(args.output).write_text(markdown, encoding='utf-8')
        else:
            print(markdown, end='')
        if args.json_output:
            Path(args.json_output).write_text(serialized, encoding='utf-8')
    except (OSError, ValueError, sqlite3.Error, ArithmeticError) as error:
        parser.exit(2, f'Allocation comparison failed: {error}\n')
    return report


if __name__ == '__main__':
    main()
