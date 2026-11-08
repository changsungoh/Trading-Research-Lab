"""P4 reproducible synthetic/tape market-making experiments, no live trading."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
from simulation.market_making_simulator import MakerPolicy, MarketModel, TapeEvent, generate_tape, simulate_market_maker
from simulation.execution_simulator import fingerprint

LIMITATIONS = [
    'Synthetic single-asset research model, not a calibrated market-making strategy or live-order implementation.',
    'Fair value is an EWMA estimate of observed mids, not known fundamental value or future prices.',
    'Quotes use previous observations; this step flow and mid are revealed only after quote placement.',
    'Crossing taker flow deterministically fills available quoted size. No queue priority, depth, competing makers, latency calibration or exchange matching rules.',
    'Synthetic informed flow sees the current price shock before the maker, but no later shocks; assumptions are fictional.',
    'Bounded short inventory is permitted; no securities-lending availability, borrow fees, margin calls, taxes or real tick schedule are modeled.',
    'Cash/inventory/notional constraints apply when quoting; price shocks can breach marked exposure/drawdown limits before quotes can be cancelled.',
    'A drawdown halt cancels later quotes and retains inventory; it does not claim to cap losses or execute liquidation.',
    'Terminal residual inventory is marked at the final supplied mid, not liquidated. Marked P&L is not realized or executable profit.',
    'Future markouts are post-run diagnostics; end-of-tape missing markouts remain null. Missing diagnostics do not remove fills.',
    'Different policies share identical exogenous tapes/seeds; flow ignores maker impact and policy responses.',
    'Repeated policy/seed selection can overfit. No optimal policy or profitability verdict is selected automatically.'
]


def load_config(path):
    with open(path, encoding='utf-8-sig') as stream:
        config = json.load(stream)
    if not isinstance(config, dict) or set(config) != {'market', 'policies', 'seeds'}:
        raise ValueError('Config requires exactly market, policies and seeds')
    if not isinstance(config['market'], dict) or not isinstance(config['policies'], list) or not config['policies']:
        raise ValueError('Require a market object and nonempty policies array')
    try:
        market = MarketModel(**config['market'])
        policies = [MakerPolicy(**policy) for policy in config['policies']]
    except TypeError as error:
        raise ValueError(f'Invalid/missing config fields: {error}') from error
    seeds = config['seeds']
    if not isinstance(seeds, list) or not seeds or any(type(seed) is not int for seed in seeds) or len(set(seeds)) != len(seeds):
        raise ValueError('Seeds must be a nonempty array of unique integers')
    if len({policy.name for policy in policies}) != len(policies):
        raise ValueError('Policy names must be unique')
    if any(policy.initial_mid != market.initial_mid for policy in policies):
        raise ValueError('Policy and market initial_mid must match')
    return market, policies, seeds


def load_tape(path):
    with open(path, encoding='utf-8-sig') as stream:
        data = json.load(stream)
    if not isinstance(data, list) or not data:
        raise ValueError('Tape must be a nonempty array of event objects')
    try:
        events = [TapeEvent(**item) for item in data]
    except TypeError as error:
        raise ValueError(f'Invalid tape fields: {error}') from error
    if any(event.step != index + 1 for index, event in enumerate(events)):
        raise ValueError('Tape steps must be contiguous from 1')
    return events


def experiment(market, policies, seeds, tape=None):
    config = dict(market=asdict(market), policies=[asdict(policy) for policy in policies], seeds=seeds)
    results = []
    # Supplied tape is deterministic: do not pretend different seeds create new data.
    for seed in ([None] if tape is not None else seeds):
        events = list(tape) if tape is not None else generate_tape(market, seed)
        for policy in policies:
            result = simulate_market_maker(events, policy)
            result['seed'] = seed
            results.append(result)
    return dict(schema_version=1, model='SINGLE_ASSET_OFFLINE_MARKET_MAKING',
                tape_source='SUPPLIED_UNCALIBRATED_TAPE' if tape is not None else 'SYNTHETIC',
                config=config, config_sha256=fingerprint(config), limitations=LIMITATIONS, results=results)


def render_markdown(report):
    def safe(value):
        return str(value).replace('|', '/').replace('\n', ' ')
    lines = ['# P4 market-making laboratory', '', 'ILLUSTRATIVE RESEARCH — marked results, not actual fills or a profitability verdict.', '',
             f"Source: {report['tape_source']}; config SHA256: {report['config_sha256']}", '',
             '| Policy | Seed | Fills | Ending inventory | Marked P&L | Fees | Marked drawdown % | Future markout count | Mean future markout | Halted |',
             '|---|---|---:|---:|---:|---:|---:|---:|---:|---|']
    for result in report['results']:
        markout = result['mean_future_markout']
        lines.append('| ' + ' | '.join([safe(result['policy']['name']), str(result['seed']), str(result['fills']),
            str(result['ending_inventory']), f"{result['marked_pnl']:.4f}", f"{result['total_fees']:.4f}",
            f"{result['max_marked_drawdown_pct']:.4f}", str(result['future_markout_count']),
            'N/A' if markout is None else f'{markout:.4f}', str(result['halted'])]) + ' |')
    for result in report['results']:
        lines += ['', f"## {safe(result['policy']['name'])} / seed {result['seed']}",
                  f"Policy: {result['policy']}", f"Tape SHA256: {result['tape_sha256']}",
                  f"Known cash: {result['ending_cash']}; residual inventory mark: {result['inventory_mark_value']}; terminal flat: {result['terminal_flat']}.",
                  f"Quoted edge total: {result['quoted_edge_total']}; fair revaluation residual: {result['fair_revaluation_pnl']}; immediate markout total: {result['immediate_markout_total']}; subsequent signed mid move: {result['future_mid_move_total']}.",
                  f"Maximum absolute inventory: {result['max_abs_inventory']}; maximum marked inventory notional: {result['max_marked_notional']}."]
    lines += ['', '## Market assumptions', '', str(report['config']['market']), '', '## Limits', '']
    lines += ['- ' + item for item in report['limitations']]
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--tape', help='Optional explicit JSON events; otherwise generate synthetic events')
    parser.add_argument('--output')
    parser.add_argument('--json-output')
    args = parser.parse_args(argv)
    sources = {Path(path).resolve() for path in (args.config, args.tape) if path}
    outputs = [Path(path).resolve() for path in (args.output, args.json_output) if path]
    if len(set(outputs)) != len(outputs) or any(path in sources for path in outputs):
        parser.error('Report outputs must differ from inputs and each other')
    try:
        market, policies, seeds = load_config(args.config)
        report = experiment(market, policies, seeds, load_tape(args.tape) if args.tape else None)
        serialized = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + '\n'
        if args.output:
            Path(args.output).write_text(render_markdown(report), encoding='utf-8')
        else:
            print(render_markdown(report), end='')
        if args.json_output:
            Path(args.json_output).write_text(serialized, encoding='utf-8')
    except (OSError, ValueError, ArithmeticError) as error:
        parser.exit(2, f'Market-making experiment failed: {error}\n')
    return report


if __name__ == '__main__':
    main()
