"""Deterministic public-safe synthetic showcase. Never reads a DB, account or environment secret."""
import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from research.statistical_analysis import analyze, render_markdown as render_statistics, valid_observation
from simulation.execution_simulator import Scenario
from simulation.risk_simulator import RiskPolicy, simulate
from simulation.portfolio_allocation import AllocationPolicy, Stress
from portfolio_risk_report import compare
from market_making_report import experiment
from simulation.market_making_simulator import MakerPolicy, MarketModel

SEED = 20261109
LABEL = 'SYNTHETIC DATA ONLY - NOT LIVE PERFORMANCE'


def make_data(seed=SEED, days=60, signals_per_day=6):
    rng = random.Random(seed)
    dates, current = [], datetime(2025, 1, 6)
    while len(dates) < days:
        if current.weekday() < 5:
            dates.append(current)
        current += timedelta(days=1)
    clean, quality = [], []
    for day in dates:
        market_day, shared_day = rng.gauss(0, .18), rng.gauss(0, .10)
        for index in range(signals_per_day):
            start = day.replace(hour=10) + timedelta(minutes=5 * index)
            symbol = f'SYNTH_{index % 3 + 1:02d}'
            setup = 'SYNTH_A' if index % 2 == 0 else 'SYNTH_B'
            entry = 100.0 + (index % 3) * 20
            row = dict(code=symbol, name=symbol, entry_time=start.isoformat(sep=' '), entry_price=entry,
                       setup_quality=setup, rule_version='SYNTHETIC_DEMO_V1')
            for minute in (5, 10, 30):
                # Fictional data-generating process, not extracted/tuned from any strategy.
                benchmark = market_day * (minute / 10) ** .5 + rng.gauss(0, .08)
                value = benchmark + shared_day + rng.gauss(0, .32 * (minute / 10) ** .5)
                target = start + timedelta(minutes=minute)
                observed = target + timedelta(seconds=rng.randint(0, 45))
                row.update({f'target_timestamp_{minute}m': target.isoformat(sep=' '),
                            f'observed_timestamp_{minute}m': observed.isoformat(sep=' '),
                            f'price_{minute}m': entry * (1 + value / 100),
                            f'ret_{minute}m': round(value, 2), f'status_{minute}m': 'VALID'})
            clean.append(row)
            dirty = dict(row)
            for minute in (5, 10, 30):
                draw = rng.random()
                if draw < .08:
                    dirty.update({f'status_{minute}m': 'MISSING', f'ret_{minute}m': None,
                                  f'price_{minute}m': None, f'observed_timestamp_{minute}m': None})
                elif draw < .14:
                    dirty.update({f'status_{minute}m': 'STALE', f'ret_{minute}m': None,
                        f'observed_timestamp_{minute}m': (start + timedelta(minutes=minute, seconds=180)).isoformat(sep=' ')})
                elif draw < .18:
                    dirty[f'status_{minute}m'] = None  # Explicit legacy-unverified example.
            quality.append(dirty)
    return clean, quality, dates[days // 2].date().isoformat()


def configuration():
    risk = RiskPolicy(100000, 1, 3, 10, 100, 100)
    zero = Scenario('Zero-cost reference', 0, 0, 0, 0, 0, 1, 1, 1)
    friction = Scenario('Illustrative friction', 2, 2, 20, 10, 5, 1, 1, 1)
    sparse = Scenario('Illustrative partial/nonfills', 2, 2, 20, 10, 5, .8, .5, .95)
    common = dict(fixed_cash=15000, fraction=.2, target_volatility_pct=.08, volatility_floor_pct=.2,
                  lookback=30, min_observations=10, min_days=5, max_symbol_fraction=.3,
                  max_sector_fraction=.5, max_gross_fraction=.8, unknown_sector_policy='block')
    allocations = [AllocationPolicy(name=mode, mode=mode, **common)
                   for mode in ('fixed_cash', 'fixed_fraction', 'volatility_target')]
    sectors = {'SYNTH_01': 'GROUP_A', 'SYNTH_02': 'GROUP_A', 'SYNTH_03': 'GROUP_B'}
    stresses = [Stress('Market -10%', -10, {}), Stress('Market -5%, group A -15%', -5, {'GROUP_A': -15})]
    market = MarketModel(500, 100, 5, 0, .8, .3, 20, 15)
    maker = dict(initial_cash=100000, initial_mid=100, tick_size=.01, quote_quantity=10, max_inventory=100,
                 max_notional_fraction=.5, half_spread_bps=10, ewma_alpha=.5, fee_bps=1,
                 max_drawdown_pct=5, markout_steps=5)
    makers = [MakerPolicy(name='Symmetric', inventory_skew_bps=0, **maker),
              MakerPolicy(name='Inventory skew', inventory_skew_bps=30, **maker)]
    return risk, [zero, friction, sparse], allocations, sectors, stresses, market, makers


def overall(stats, minute=10, partition='all'):
    return next(item for item in stats['sections'] if item['group_by'] == 'overall'
                and item['horizon'] == minute and item['partition'] == partition)


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8',newline='\n')


def graphs(directory, stats, executions, allocation, maker):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.ticker import PercentFormatter
    ink, muted, grid = '#111827', '#4b5563', '#e5e7eb'
    plt.rcParams.update({
        'font.family': 'DejaVu Sans', 'font.size': 11.5, 'axes.titlesize': 13, 'axes.titleweight': 'bold',
        'axes.titlelocation': 'left', 'axes.titlepad': 12, 'axes.labelsize': 11, 'axes.labelcolor': muted,
        'xtick.color': muted, 'ytick.color': muted, 'xtick.labelsize': 10.5, 'ytick.labelsize': 10.5,
        'axes.edgecolor': '#9ca3af', 'axes.spines.top': False, 'axes.spines.right': False,
        'axes.grid': True, 'axes.grid.axis': 'y', 'grid.color': grid, 'grid.linewidth': .8, 'axes.axisbelow': True,
        'figure.facecolor': 'white', 'axes.facecolor': 'white', 'savefig.facecolor': 'white',
        'legend.frameon': False, 'legend.fontsize': 10})
    blue, green, orange, red, slate = '#2458a6', '#178875', '#db9437', '#c45162', '#99a5b5'

    def save(fig, name, title, subtitle, note=None, top=.8, bottom=.22):
        fig.text(.04, .955, title, fontsize=19, fontweight='bold', color=ink, ha='left', va='center')
        fig.text(.04, .905, subtitle, fontsize=10.5, color=muted, ha='left', va='center')
        if note:
            fig.text(.04, .862, note, fontsize=10, style='italic', color='#8d3647', ha='left', va='center')
        fig.text(.965, .955, 'SYNTHETIC DATA', fontsize=9, fontweight='bold', color='white', ha='right', va='center',
                 bbox=dict(boxstyle='round,pad=.4', fc='#8d3647', ec='none'))
        fig.add_artist(Line2D([.04, .965], [.07, .07], color=grid, lw=1))
        fig.text(.04, .033, LABEL + ' | Fixed seeds; illustrative assumptions; no selected winner.',
                 fontsize=8.5, color='#6b7280', ha='left', va='center')
        fig.subplots_adjust(left=.09, right=.965, top=top, bottom=bottom, wspace=.4, hspace=.3)
        fig.savefig(directory / name, dpi=200, metadata={'Software': 'Synthetic research showcase'})
        plt.close(fig)

    # 1. Quality and uncertainty
    fig, axes = plt.subplots(1, 2, figsize=(11, 5.6))
    bottom = [0., 0., 0.]
    for status, color in [('VALID', green), ('MISSING', orange), ('STALE', red), ('LEGACY_UNVERIFIED', slate)]:
        values = [overall(stats, minute)['quality'].get(status, 0) / stats['selected_rows'] for minute in (5, 10, 30)]
        axes[0].bar(['5m', '10m', '30m'], values, bottom=bottom, color=color, label=status.replace('_', ' ').title()
                    if status == 'LEGACY_UNVERIFIED' else status.title(), width=.55)
        if status == 'VALID':
            for x, value in enumerate(values):
                axes[0].text(x, value / 2, f'{value:.1%}', ha='center', va='center', color='white',
                             fontsize=12, fontweight='bold')
        bottom = [a + b for a, b in zip(bottom, values)]
    axes[0].set_title('Observation quality by horizon')
    axes[0].yaxis.set_major_formatter(PercentFormatter(1, decimals=0)); axes[0].set_ylim(0, 1.02)
    axes[0].legend(loc='upper center', bbox_to_anchor=(.5, -.1), ncol=4, fontsize=9.5, columnspacing=1.2, handlelength=1.2)
    for y, period in enumerate(('all', 'exploration', 'holdout')):
        item = overall(stats, partition=period)['stock']; lo, hi = item['mean_ci95']
        axes[1].errorbar(item['mean'], y, xerr=[[item['mean'] - lo], [hi - item['mean']]], fmt='o',
                         color=blue, capsize=5, markersize=8, lw=2)
        axes[1].annotate(f"{item['mean']:+.3f}   [{lo:+.3f}, {hi:+.3f}]", (item['mean'], y), xytext=(0, 13),
                         textcoords='offset points', ha='center', fontsize=9.5, color=muted,
                         bbox=dict(boxstyle='round,pad=.2', fc='white', ec='none'), zorder=5)
    axes[1].set_yticks(range(3), ['All dates', 'Exploration', 'Fixed date split*'])
    axes[1].set_ylim(-.7, 2.7)
    axes[1].grid(axis='x'); axes[1].grid(axis='y', visible=False)
    axes[1].axvline(0, color='#6b7280', ls='--', lw=1)
    axes[1].set_xlabel('10m mean return (percentage points)')
    axes[1].set_title('Date-clustered 95% intervals')
    save(fig, '01_quality_and_uncertainty.png', 'Measure quality before interpreting returns',
         '360 fictional candidates / 60 synthetic weekdays. *Fixed demonstration split; not a sealed real holdout.', top=.82)

    # 2. Costs and unknown exits
    fig, axes = plt.subplots(1, 2, figsize=(11, 5.6))
    known = executions[:2]; labels = ['Zero cost', 'Friction']
    gross = [r['closed_gross_stats']['mean'] for r in known]; net = [r['closed_net_stats']['mean'] for r in known]
    for x, (g, n) in enumerate(zip(gross, net)):
        axes[0].bar(x - .16, g, width=.3, color=blue, label='Reference gross' if x == 0 else None)
        axes[0].bar(x + .16, n, width=.3, color=orange, label='After assumed costs' if x == 0 else None)
        axes[0].text(x - .16, g - .012, f'{g:.3f}', ha='center', va='top', fontsize=9.5, color=ink)
        axes[0].text(x + .16, n - .012, f'{n:.3f}', ha='center', va='top', fontsize=9.5, color=ink)
    axes[0].set_ylim(min(net + gross) - .07, .02)
    axes[0].set_xticks([0, 1], labels); axes[0].axhline(0, color='#6b7280', lw=1)
    axes[0].set_ylabel('Mean closed-trade return (percentage points)')
    axes[0].legend(loc='lower left'); axes[0].set_title('Complete-input controlled comparison')
    status_labels = ['Complete inputs', 'Quality gaps', 'Partial / nonfills']
    selected = [executions[1], executions[3], executions[2]]
    axes[1].barh(status_labels, [r['closed'] for r in selected], color=green, label='Closed', height=.55)
    axes[1].barh(status_labels, [r['unresolved'] for r in selected], left=[r['closed'] for r in selected],
                 color=red, label='Unpriced', height=.55)
    peak = max(r['closed'] + r['unresolved'] for r in selected)
    axes[1].set_xlim(0, peak * 1.55)
    for y, r in enumerate(selected):
        axes[1].text(r['closed'] + r['unresolved'] + peak * .02, y, f"{r['closed']} closed / {r['unresolved']} unpriced",
                     va='center', fontsize=9.5, color=ink)
    axes[1].grid(axis='x'); axes[1].grid(axis='y', visible=False)
    axes[1].set_xlabel('Filled positions'); axes[1].legend(loc='upper right')
    axes[1].set_title('Missing exits keep capital reserved')
    save(fig, '02_costs_and_unknown_exits.png', 'Costs and missing exits change the answer',
         '10m long-only scenarios. Clean inputs are a separate counterfactual, never repaired real observations.', top=.82)

    # 3. Allocation and stress
    fig, axes = plt.subplots(1, 2, figsize=(11, 5.6))
    colors = [blue, green, orange]; names = ['Fixed cash', 'Fixed fraction', 'Inverse volatility']
    for result, color, label in zip(allocation['results'], colors, names):
        peaks = {}
        for item in result['allocation_analysis']['snapshots']:
            day = item['time'][:10]; peaks[day] = max(peaks.get(day, 0), item['gross_fraction'] * 100)
        axes[0].plot(range(1, len(peaks) + 1), list(peaks.values()), color=color, lw=2.2, marker='o', markersize=3.5, label=label)
    axes[0].axhline(80, color=red, ls='--', lw=1.2, label='Entry gross cap (80%)')
    axes[0].set_ylim(0, 100); axes[0].set_xlabel('Synthetic trading day (fixed split onward)')
    axes[0].set_ylabel('Daily peak gross exposure (%)'); axes[0].legend(loc='lower right', fontsize=9.5)
    axes[0].set_title('Daily peak cost-basis exposure')
    stress_names = list(allocation['results'][0]['allocation_analysis']['stress_worst'])
    top_value = 0
    for offset, (stress, color) in enumerate(zip(stress_names, [blue, red])):
        values = [r['allocation_analysis']['stress_worst'][stress]['loss_pct'] for r in allocation['results']]
        positions = [i + (offset - .5) * .34 for i in range(3)]
        axes[1].bar(positions, values, width=.32, color=color, label=stress)
        for x, value in zip(positions, values):
            axes[1].text(x, value + .15, f'{value:.1f}%', ha='center', va='bottom', fontsize=9.5, color=ink)
        top_value = max(top_value, *values)
    axes[1].set_ylim(0, top_value * 1.25)
    axes[1].set_xticks(range(3), ['Fixed\ncash', 'Fixed\nfraction', 'Inverse\nvolatility'])
    axes[1].set_ylabel('Worst assumed snapshot loss (%)'); axes[1].legend(loc='upper left', fontsize=9.5)
    axes[1].set_title('Simultaneous downside assumptions')
    save(fig, '03_allocation_and_stress.png', 'Allocation, exposure and simultaneous stress',
         'Last 30 synthetic weekdays; earlier outcomes supply past-only volatility warmup. Not VaR or optimal sizing.', top=.82)

    # 4. Market making
    fig, axes = plt.subplots(2, 1, figsize=(11, 6.6), sharex=True)
    finals = [r['ledger'][-1]['equity'] - r['policy']['initial_cash'] for r in maker['results']]
    for index, (result, color) in enumerate(zip(maker['results'], [blue, green])):
        log = result['ledger']; name = result['policy']['name']
        steps = [p['step'] for p in log]
        pnl = [p['equity'] - result['policy']['initial_cash'] for p in log]
        axes[0].plot(steps, [p['inventory'] for p in log], color=color, lw=1.6, label=name)
        axes[1].plot([0] + steps, [0] + pnl, color=color, lw=1.8, label=name)
        axes[0].annotate(f"{log[-1]['inventory']:+d}", (steps[-1], log[-1]['inventory']), xytext=(6, 0),
                         textcoords='offset points', va='center', fontsize=9.5, color=color, fontweight='bold')
        lift = 9 if pnl[-1] >= min(finals) and pnl[-1] == max(finals) else -9
        axes[1].annotate(f'{pnl[-1]:+.0f}', (steps[-1], pnl[-1]), xytext=(6, lift),
                         textcoords='offset points', va='center', fontsize=9.5, color=color, fontweight='bold')
    for axis in axes:
        axis.axhline(0, color='#6b7280', lw=.9)
    axes[0].set_xlim(-5, 535)
    axes[0].set_ylabel('Signed inventory (shares)'); axes[1].set_ylabel('Marked P&L (arbitrary units)')
    axes[0].set_title('Residual inventory', fontsize=12.5); axes[1].set_title('Marked P&L', fontsize=12.5)
    axes[1].set_xlabel('Synthetic event step'); axes[0].legend(loc='upper left', ncol=2)
    save(fig, '04_market_making_inventory.png', 'Quoting creates inventory and price risk',
         'Both policies replay the same 500-event tape (seed 11). Residual inventory is marked, not liquidated.',
         note='Model assumption, not evidence: crossing orders always fill and no competing makers exist, so positive P&L reflects the toy rules.',
         top=.78, bottom=.175)


def build(output, plots=True):
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    clean,dirty,cutoff=make_data()
    risk,scenarios,allocations,sectors,stresses,market,makers=configuration()
    stats=analyze(dirty,holdout_start=cutoff,seed=SEED,resamples=1000)
    executions=[simulate(clean,s,risk,horizon=10,seed=SEED) for s in scenarios]
    executions.append(simulate(dirty,scenarios[1],risk,horizon=10,seed=SEED))
    allocation=compare(clean,risk,[scenarios[1]],allocations,sectors,stresses,horizon=10,seed=SEED,start_date=cutoff)
    maker=experiment(market,makers,[11])
    write_json(output/'synthetic_inputs.json',dict(label=LABEL,seed=SEED,clean_counterfactual=clean,quality_stress=dirty))
    write_json(output/'statistics.json',stats)
    write_json(output/'execution.json',dict(label=LABEL,cases=['clean_zero','clean_friction','clean_partial','quality_friction'],results=executions))
    write_json(output/'allocation.json',allocation);write_json(output/'market_making.json',maker)
    (output/'statistics.md').write_text('> '+LABEL+'\n\n'+render_statistics(stats),encoding='utf-8',newline='\n')
    if plots:
        (output/'figures').mkdir(exist_ok=True)
        graphs(output/'figures',stats,executions,allocation,maker)
    def f(value):return 'N/A (unpriced positions)' if value is None else f'{value:.4f}'
    lines=['# Synthetic research walkthrough','', '> **'+LABEL+'**', '',
       'This is an executable demonstration of research safeguards, not evidence of a profitable strategy. No real market, account, broker, private signal rules or trading records are used.', '',
       '## Reproduce', '', 'From the repository root:', '', '```bash',
       'python -m pip install -r examples/requirements.txt',
       'python examples/generate_synthetic_demo.py', '```', '',
       'The research engines remain standard-library-only. Matplotlib is an optional dependency of this example generator; `--no-plots` rebuilds data/reports without it. Fixed seeds reproduce values with the same Python/runtime versions. No database or credentials are needed.', '',
       '## Dataset and assumptions','',
       f'- Seed: `{SEED}`; 60 synthetic weekdays beginning 2025-01-06; six candidates per day; 360 total.',
       f'- Fixed exploration/holdout date boundary: `{cutoff}`. This synthetic calendar skips weekends only, not actual exchange holidays.',
       '- All symbols (`SYNTH_01`–`SYNTH_03`), setup labels and groups are fictional.',
       '- Gross returns combine fictional shared day shocks and Gaussian noise, with no embedded profitable strategy or tuned winner.',
       '- Complete and quality-stressed datasets share latent simulated prices. Independent quality masking creates approximately 8% missing, 6% stale and 4% legacy rows per horizon; realized counts vary by seed.',
       '- Complete inputs are a separate controlled counterfactual, **not** a filtered surviving subset or a repair procedure for real missing data.',
       '- Costs and fill probabilities below are fictional assumptions. Monetary values use arbitrary simulation units.',
       '- P2 deliberately permits an entire 100,000-unit account per entry (100% common cap), with at most three positions, ten filled entries per day and very loose demonstration loss stops. This concentrates turnover and cost drag; it is not a proposed risk policy. P3 instead applies its separate symbol/sector/gross allocation caps.', '',
       '## 1. Quality and uncertainty','',
       '![Synthetic observation quality and confidence intervals](figures/01_quality_and_uncertainty.png)','',
       '| Horizon | Valid / total | Coverage | Mean gross return (pp) | 95% date-cluster CI (pp) |',
       '|---|---:|---:|---:|---|']
    for minute in (5,10,30):
        item=overall(stats,minute); st=item['stock']; ci=st['mean_ci95']
        lines.append(f"| {minute}m | {st['n']} / {item['total']} | {item['coverage']:.1%} | {f(st['mean'])} | [{f(ci[0])}, {f(ci[1])}] |")
    lines += ['', 'Intervals resample whole dates (1,000 replications). Missing/stale/legacy records are excluded independently per horizon and never zero-filled. The illustrative split and confidence intervals do not seal a holdout, correct multiple comparisons, or establish an edge.', '',
       '## 2. Costs, partial fills and unknown exits', '',
       '![Synthetic cost and unpriced-exit comparison](figures/02_costs_and_unknown_exits.png)','',
       '| Input / scenario | Filled | Closed | Unpriced | Closed gross mean (pp) | Closed net mean (pp) | Terminal return % |',
       '|---|---:|---:|---:|---:|---:|---:|']
    labels=['Complete / zero cost','Complete / friction','Complete / partial and nonfills','Quality gaps / friction']
    for label,result in zip(labels,executions):
        lines.append(f"| {label} | {result['filled']} | {result['closed']} | {result['unresolved']} | {f(result['closed_gross_stats']['mean'])} | {f(result['closed_net_stats']['mean'])} | {f(result['portfolio_return_pct'])} |")
    lines += ['', 'Friction assumptions: buy fee 2 bps, sell fee 2 bps, sell tax 20 bps, full spread 10 bps and additional slippage 5 bps per side. Controlled cost comparison assumes complete fills; the separate partial/nonfill case assumes entry probability 0.8, entry fraction 0.5 and exit probability 0.95. These are **not current rates or calibrated execution estimates**.', '',
       'Missing exits do not prevent a hypothetical entry in advance. Filled positions keep their cash reserved. Terminal returns remain null while any position is unpriced. Closed-only averages are conditional and can be selection-biased.', '',
       '## 3. Allocation and simultaneous downside', '',
       '![Synthetic allocation exposures and stress losses](figures/03_allocation_and_stress.png)','',
       '| Allocation | Filled | Peak gross exposure | Peak sector exposure | Market -10% assumed loss |',
       '|---|---:|---:|---:|---:|']
    for result in allocation['results']:
        a=result['allocation_analysis']; loss=a['stress_worst']['Market -10%']['loss_pct']
        lines.append(f"| {a['allocation']['name']} | {result['filled']} | {a['peak_gross_fraction']:.1%} | {a['peak_sector_fraction']:.1%} | {loss:.2f}% |")
    lines += ['', 'The three policies share the friction assumptions and start fresh capital on the fixed split date. Earlier clean synthetic observations provide warmup, but only outcomes completed strictly before a decision enter inverse-volatility sizing. Exposure is cost-basis, not marked market value. Stress compounds market and group shocks from entry references and assumes liquidation; it is not VaR, a probability or a live loss bound.', '',
       '## 4. Inventory-aware market making', '',
       '![Synthetic market-making inventory and marked profit](figures/04_market_making_inventory.png)','',
       '| Policy | Fills | Ending inventory | Marked P&L | Fees | Mean future markout per fill |',
       '|---|---:|---:|---:|---:|---:|']
    for result in maker['results']:
        lines.append(f"| {result['policy']['name']} | {result['fills']} | {result['ending_inventory']} | {f(result['marked_pnl'])} | {f(result['total_fees'])} | {f(result['mean_future_markout'])} |")
    lines += ['', 'Both policies see the same seed-11 tape. Quotes use previous observations; future markouts are post-run diagnostics. Ending inventory is marked at the final synthetic midpoint, not liquidated. The tape omits queue competition, measured latency, market impact and actual borrowing constraints. No winner is selected.', '',
       '## Audit trail', '',
       '- [Input data](synthetic_inputs.json): complete counterfactual and quality-stressed records.',
       '- [Statistics](statistics.json) and [full statistical details](statistics.md).',
       '- [Execution decisions and ledgers](execution.json).',
       '- [Allocation assumptions, decisions and snapshots](allocation.json).',
       '- [Market-making assumptions, quotes, fills and ledger](market_making.json).',
       '- [Manifest and file hashes](manifest.json).', '',
       'These files are generated solely from the checked-in demo, public research engines and explicit constants. They contain no API keys, account identifiers, real trades, internal entry rules or inferred real performance.']
    if not plots:
        lines=[line for line in lines if not line.startswith('![')]
    (output/'README.md').write_text('\n'.join(lines)+'\n',encoding='utf-8',newline='\n')
    files=sorted(path for path in output.rglob('*') if path.is_file() and path.name!='manifest.json')
    write_json(output/'manifest.json',dict(label=LABEL,seed=SEED,holdout_start=cutoff,
        python=sys.version.split()[0],plotting=__import__('matplotlib').__version__ if plots else None,
        files={path.relative_to(output).as_posix():hashlib.sha256(path.read_bytes()).hexdigest() for path in files}))
    return dict(stats=stats,executions=executions,allocation=allocation,maker=maker)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'examples'/'synthetic')
    parser.add_argument('--no-plots',action='store_true')
    args=parser.parse_args()
    build(args.output,plots=not args.no_plots)
    print('Generated synthetic showcase:',args.output)
