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
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')


def graphs(directory, stats, executions, allocation, maker):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10, 'axes.spines.top': False,
                         'axes.spines.right': False, 'axes.titleweight': 'bold', 'figure.facecolor': '#f6f8fb',
                         'axes.facecolor': '#ffffff', 'savefig.facecolor': '#f6f8fb'})
    blue, green, orange, red = '#2458a6', '#178875', '#db9437', '#c45162'
    def save(fig, name, title, subtitle):
        fig.suptitle(title, fontsize=19, fontweight='bold', x=.07, ha='left', y=.99)
        fig.text(.07, .91, subtitle, color='#536174', fontsize=10)
        fig.text(.07, .018, LABEL + ' | Fixed seeds; illustrative assumptions; no selected winner.',
                 color='#8d3647', fontsize=9, fontweight='bold')
        fig.subplots_adjust(left=.09, right=.97, top=.81, bottom=.17, wspace=.38, hspace=.5)
        fig.savefig(directory / name, dpi=170, metadata={'Software': 'Synthetic research showcase'})
        plt.close(fig)
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.8))
    bottom = [0., 0., 0.]
    for status, color in [('VALID', green), ('MISSING', orange), ('STALE', red), ('LEGACY_UNVERIFIED', '#99a5b5')]:
        values = [overall(stats, minute)['quality'].get(status, 0) / stats['selected_rows'] for minute in (5,10,30)]
        axes[0].bar(['5m','10m','30m'], values, bottom=bottom, color=color, label=status, width=.55)
        bottom = [a+b for a,b in zip(bottom,values)]
    axes[0].set_title('Observation quality by horizon', loc='left')
    axes[0].yaxis.set_major_formatter(PercentFormatter(1)); axes[0].set_ylim(0,1.05)
    axes[0].legend(fontsize=8, loc='upper left', bbox_to_anchor=(0,-.1), ncol=2, frameon=False)
    for y, period in enumerate(('all','exploration','holdout')):
        item = overall(stats, partition=period)['stock']; lo,hi = item['mean_ci95']
        axes[1].errorbar(item['mean'], y, xerr=[[item['mean']-lo],[hi-item['mean']]], fmt='o',
                         color=blue, capsize=5, markersize=7)
    axes[1].set_yticks(range(3), ['All dates','Exploration','Fixed date split*'])
    axes[1].axvline(0,color='#8994a3',ls='--',lw=1); axes[1].set_xlabel('10m mean return (percentage points)')
    axes[1].set_title('Date-clustered 95% intervals', loc='left')
    save(fig,'01_quality_and_uncertainty.png','Measure quality before interpreting returns',
         '360 fictional candidates / 60 synthetic weekdays. *Fixed demonstration split; not a sealed real holdout.')
    fig, axes = plt.subplots(1,2,figsize=(12,5.8))
    known = executions[:2]; labels=['Zero cost','Friction']
    gross=[r['closed_gross_stats']['mean'] for r in known]; net=[r['closed_net_stats']['mean'] for r in known]
    for x,(g,n) in enumerate(zip(gross,net)):
        axes[0].bar(x-.16,g,width=.3,color=blue,label='Reference gross' if x==0 else None)
        axes[0].bar(x+.16,n,width=.3,color=orange,label='After assumed costs' if x==0 else None)
    axes[0].set_xticks([0,1],labels); axes[0].axhline(0,color='#8994a3',lw=1)
    axes[0].set_ylabel('Mean closed-trade return (percentage points)')
    axes[0].legend(fontsize=8,frameon=False); axes[0].set_title('Complete-input controlled comparison',loc='left')
    status_labels=['Complete inputs','Quality gaps','Partial / nonfills']
    selected=[executions[1],executions[3],executions[2]]
    axes[1].barh(status_labels,[r['closed'] for r in selected],color=green,label='Closed')
    axes[1].barh(status_labels,[r['unresolved'] for r in selected],left=[r['closed'] for r in selected],color=red,label='Unpriced')
    axes[1].set_xlabel('Filled positions'); axes[1].legend(frameon=False,fontsize=8)
    axes[1].set_title('Missing exits keep capital reserved',loc='left')
    save(fig,'02_costs_and_unknown_exits.png','Costs and missing exits change the answer',
         '10m long-only scenarios. Clean inputs are a separate counterfactual, never repaired real observations.')
    fig, axes = plt.subplots(1,2,figsize=(12,5.8))
    colors=[blue,green,orange]; names=['Fixed cash','Fixed fraction','Inverse volatility']
    for result,color,label in zip(allocation['results'],colors,names):
        snap=result['allocation_analysis']['snapshots']
        axes[0].plot(range(len(snap)),[s['gross_fraction']*100 for s in snap],color=color,alpha=.8,lw=1,label=label)
    axes[0].axhline(80,color=red,ls='--',lw=1,label='Entry gross cap')
    axes[0].set_ylim(0,100); axes[0].set_xlabel('Entry/exit snapshot index (policy-specific)')
    axes[0].set_ylabel('Cost-basis gross exposure (%)'); axes[0].legend(fontsize=8,frameon=False)
    axes[0].set_title('Exposure paths; no market-value claims',loc='left')
    stress_names=list(allocation['results'][0]['allocation_analysis']['stress_worst'])
    for offset,(stress,color) in enumerate(zip(stress_names,[blue,red])):
        values=[r['allocation_analysis']['stress_worst'][stress]['loss_pct'] for r in allocation['results']]
        axes[1].bar([i+(offset-.5)*.34 for i in range(3)],values,width=.32,color=color,label=stress)
    axes[1].set_xticks(range(3),['Fixed\ncash','Fixed\nfraction','Inverse\nvolatility'])
    axes[1].set_ylabel('Worst assumed snapshot loss (%)'); axes[1].legend(fontsize=7,frameon=False)
    axes[1].set_title('Simultaneous downside assumptions',loc='left')
    save(fig,'03_allocation_and_stress.png','Allocation, exposure and simultaneous stress',
         'Last 30 synthetic weekdays; earlier outcomes supply past-only volatility warmup. Not VaR or optimal sizing.')
    fig, axes = plt.subplots(2,1,figsize=(12,6.7),sharex=True)
    for result,color in zip(maker['results'],[blue,green]):
        log=result['ledger']; name=result['policy']['name']
        axes[0].plot([p['step'] for p in log],[p['inventory'] for p in log],color=color,lw=1.3,label=name)
        axes[1].plot([0]+[p['step'] for p in log],[0]+[p['equity']-result['policy']['initial_cash'] for p in log],color=color,lw=1.4,label=name)
    axes[0].axhline(0,color='#8994a3',lw=.7);axes[1].axhline(0,color='#8994a3',lw=.7)
    axes[0].set_ylabel('Signed inventory (shares)');axes[1].set_ylabel('Marked P&L (arbitrary units)')
    axes[1].set_xlabel('Synthetic event step');axes[0].legend(frameon=False,fontsize=9)
    save(fig,'04_market_making_inventory.png','Quoting creates inventory and price risk',
         'Both policies replay the same 500-event tape (seed 11). Residual inventory is marked, not liquidated.')


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
    (output/'statistics.md').write_text('> '+LABEL+'\n\n'+render_statistics(stats),encoding='utf-8')
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
    (output/'README.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
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
