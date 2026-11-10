"""P2 offline execution/risk scenarios on P0 observations, with explicit assumptions."""
from research.safe_paths import unsafe_outputs
import argparse
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
import sqlite3
from research.statistical_analysis import load_rows, timestamp
from simulation.execution_simulator import Scenario, fingerprint
from simulation.risk_simulator import RiskPolicy, simulate


LIMITATIONS = [
    'ILLUSTRATIVE ASSUMPTIONS: no quotes/order-book/queue evidence; no observed fills or calibrated probabilities.',
    'Long-only whole shares; market-style adverse prices; half the stated spread plus slippage on each side.',
    'Fees charged on respective fill notionals; sell tax on sell notional. No account/tax advice or current statutory rates.',
    'Entry is the signal receipt reference, with no extra latency model, tick rounding, fee minimum or fee rounding.',
    'Entry nonfills cancel, unfilled fractions cancel; exit nonfills remain fully unpriced after the horizon.',
    'Missing/invalid exit observations do not filter entry decisions: filled positions stay unresolved and cash remains reserved.',
    'Realized risk controls cannot cap intratrade losses; no stop-loss/TP fills are inferred from extrema.',
    'Cost-basis drawdown is a realized bookkeeping diagnostic, not marked-to-market drawdown or worst-case risk.',
    'Closed-trade statistics condition on selected fills with known exits; missing exits can create selection bias.',
    'Terminal equity/return are withheld when positions remain unpriced; no invented liquidation value.',
    'Simultaneous exits precede entries; simultaneous candidates use code/rule/identity order, an artificial allocation priority.',
    'Probabilities/fractions are user assumptions, independent of liquidity, size and future returns; scenarios are not calibrated backtests.',
    'Repeated scenario/setup selection can overfit. P1 holdout discipline still applies; no automatic profitability claim.'
]


def load_config(path):
    with open(path, encoding='utf-8-sig') as stream:
        config = json.load(stream)
    if not isinstance(config, dict) or set(config) != {'portfolio', 'scenarios'}:
        raise ValueError('Config requires exactly portfolio and scenarios')
    if not isinstance(config['portfolio'], dict) or not isinstance(config['scenarios'], list):
        raise ValueError('portfolio must be an object and scenarios must be an array')
    try:
        policy = RiskPolicy(**config['portfolio'])
        scenarios = [Scenario(**item) for item in config['scenarios']]
    except TypeError as error:
        raise ValueError(f'Invalid or missing configuration fields: {error}') from error
    if not scenarios or len({item.name for item in scenarios}) != len(scenarios):
        raise ValueError('Require at least one scenario with unique names')
    return policy, scenarios


def render_markdown(report):
    def fmt(value):
        return 'N/A' if value is None else f'{value:.4f}'
    def safe(value):
        return str(value).replace('|', '/').replace('\n', ' ')
    lines = ['# P2 execution and risk scenarios', '', 'ILLUSTRATIVE SCENARIOS — not actual fills or a profitability verdict.', '',
             f"Selection: {report['selection']}; seed: {report['seed']}; undated excluded by date selection: {report['undated_excluded']}.",
             f"Input snapshot SHA256: {report['input_sha256']}; config SHA256: {report['config_sha256']}", '',
             '| Scenario | Filled / closed / unresolved | Closed gross mean % | Closed net mean % | Realized net P&L | Terminal return % | Cost-basis drawdown % |',
             '|---|---:|---:|---:|---:|---:|---:|']
    for result in report['results']:
        lines.append('| ' + ' | '.join([safe(result['scenario']['name']),
            f"{result['filled']} / {result['closed']} / {result['unresolved']}",
            fmt(result['closed_gross_stats']['mean']), fmt(result['closed_net_stats']['mean']),
            fmt(result['realized_net_pnl']), fmt(result['portfolio_return_pct']),
            fmt(result['max_cost_basis_drawdown_pct'])]) + ' |')
    for result in report['results']:
        lines += ['', f"## {safe(result['scenario']['name'])}",
                  f"Assumptions: {result['scenario']}", f"Portfolio policy: {result['policy']}",
                  f"Status: {result['completeness']}; cash: {fmt(result['ending_cash'])}; reserved entry outlay: {fmt(result['reserved_entry_outlay'])}.",
                  f"Observation quality: {result['quality']}; entry excluded: {result['entry_excluded']}; decisions: {result['decision_counts']}.",
                  f"Closed reference-price P&L: {fmt(result['closed_gross_reference_pnl'])}; closed cost drag: {fmt(result['closed_cost_drag'])}; known explicit fees/taxes (including unresolved entry fees): {fmt(result['known_explicit_costs'])}.",
                  f"Closed net-return distribution: {result['closed_net_stats']}",
                  f"Maximum daily realized loss streak: {result['max_daily_realized_loss_streak']}."]
        for day, stats in result['daily'].items():
            lines.append(f"- {day}: {stats}")
    lines += ['', '## Limits', ''] + ['- ' + item for item in report['limitations']]
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='trading_engine.db')
    parser.add_argument('--config', required=True, help='JSON with explicit portfolio policy and cost/fill scenarios')
    parser.add_argument('--horizon', type=int, choices=(5, 10, 30), required=True)
    parser.add_argument('--setup')
    parser.add_argument('--rule-version')
    parser.add_argument('--start-date', help='Inclusive Korea-local ISO date; can select a predeclared holdout')
    parser.add_argument('--end-date', help='Exclusive Korea-local ISO date')
    parser.add_argument('--seed', type=int, default=1729)
    parser.add_argument('--output')
    parser.add_argument('--json-output')
    args = parser.parse_args(argv)
    sources = {Path(path).resolve() for path in (args.db, args.config)}
    outputs = [Path(path).resolve() for path in (args.output, args.json_output) if path]
    if unsafe_outputs(sources, outputs):
        parser.error('Report outputs must differ from inputs and each other')
    try:
        start = date.fromisoformat(args.start_date) if args.start_date else None
        end = date.fromisoformat(args.end_date) if args.end_date else None
        if start and end and start >= end:
            raise ValueError('start-date must precede end-date')
        policy, scenarios = load_config(args.config)
        rows, undated = [], 0
        for row in load_rows(args.db):
            if args.setup is not None and row.get('setup_quality') != args.setup:
                continue
            if args.rule_version is not None and row.get('rule_version') != args.rule_version:
                continue
            if start or end:
                try:
                    day = timestamp(row['entry_time']).date()
                except (ValueError, TypeError, KeyError):
                    undated += 1
                    continue
                if (start and day < start) or (end and day >= end):
                    continue
            rows.append(row)
        results = [simulate(rows, scenario, policy, horizon=args.horizon, seed=args.seed) for scenario in scenarios]
        report = dict(schema_version=1, model='LONG_ONLY_ILLUSTRATIVE_COST_AND_FILL_SCENARIOS',
                      selection=dict(horizon=args.horizon, setup=args.setup, rule_version=args.rule_version,
                                     start_date=args.start_date, end_date=args.end_date),
                      seed=args.seed, input_sha256=results[0]['input_sha256'],
                      config_sha256=fingerprint(dict(portfolio=asdict(policy), scenarios=[asdict(item) for item in scenarios])),
                      undated_excluded=undated, limitations=LIMITATIONS, results=results)
        markdown = render_markdown(report)
        serialized = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + '\n'
        if args.output:
            Path(args.output).write_text(markdown, encoding='utf-8')
        else:
            print(markdown, end='')
        if args.json_output:
            Path(args.json_output).write_text(serialized, encoding='utf-8')
    except (OSError, ValueError, sqlite3.Error, ArithmeticError) as error:
        parser.exit(2, f'Simulation failed: {error}\n')
    return report


if __name__ == '__main__':
    main()
