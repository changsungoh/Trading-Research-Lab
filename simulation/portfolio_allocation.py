"""P3 allocation comparisons using past-only signal returns and cost-basis exposures."""
from bisect import bisect_left
from collections import defaultdict
from dataclasses import asdict, dataclass
from statistics import stdev
from research.statistical_analysis import finite, valid_observation
from simulation.execution_simulator import close_order


@dataclass(frozen=True)
class AllocationPolicy:
    name: str
    mode: str
    fixed_cash: float
    fraction: float
    target_volatility_pct: float
    volatility_floor_pct: float
    lookback: int
    min_observations: int
    min_days: int
    max_symbol_fraction: float
    max_sector_fraction: float
    max_gross_fraction: float
    unknown_sector_policy: str

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError('Allocation name is required')
        if self.mode not in ('fixed_cash', 'fixed_fraction', 'volatility_target'):
            raise ValueError('Unknown allocation mode')
        if self.unknown_sector_policy not in ('block', 'bucket'):
            raise ValueError('Unknown-sector policy must be block or bucket')
        for field in ('fixed_cash', 'fraction', 'target_volatility_pct', 'volatility_floor_pct',
                      'max_symbol_fraction', 'max_sector_fraction', 'max_gross_fraction'):
            value = getattr(self, field)
            if type(value) not in (int, float) or finite(value) <= 0:
                raise ValueError(f'{field} must be a positive JSON number')
        for field in ('fraction', 'max_symbol_fraction', 'max_sector_fraction', 'max_gross_fraction'):
            if getattr(self, field) > 1:
                raise ValueError(f'{field} must not exceed 1')
        for field in ('lookback', 'min_observations', 'min_days'):
            if type(getattr(self, field)) is not int or getattr(self, field) < 1:
                raise ValueError(f'{field} must be a positive integer')
        if self.min_observations < 2 or self.lookback < self.min_observations or self.min_days > self.lookback:
            raise ValueError('Require lookback >= min_observations >= 2 and min_days <= lookback')


@dataclass(frozen=True)
class Stress:
    name: str
    market_return_pct: float
    sector_returns_pct: dict

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip() or not isinstance(self.sector_returns_pct, dict):
            raise ValueError('Stress needs a name and sector_returns_pct object')
        for key in self.sector_returns_pct:
            if not isinstance(key, str) or not key.strip():
                raise ValueError('Stress sector names must be nonempty strings')
        for value in [self.market_return_pct, *self.sector_returns_pct.values()]:
            if type(value) not in (int, float) or not -100 <= finite(value) <= 0:
                raise ValueError('Stress returns must be JSON numbers in [-100, 0]')


class PastSignalHistory:
    """Pre-index canonical data; only outcomes observed strictly before entry are accessible."""
    def __init__(self, rows, horizon):
        records = defaultdict(list)
        seen = set()
        for row in rows:
            identity = (row.get('code'), row.get('entry_time'), row.get('rule_version') or '')
            if identity in seen:
                raise ValueError('Duplicate history identity')
            seen.add(identity)
            observed, _ = valid_observation(row, horizon)
            if observed is None:
                continue
            entry, end, value = observed
            records[str(row['code'])].append((end, entry, value))
        self.records = {code: sorted(items) for code, items in records.items()}
        self.times = {code: [item[0] for item in items] for code, items in self.records.items()}

    def estimate(self, code, entry, policy):
        records = self.records.get(code, [])
        stop = bisect_left(self.times.get(code, []), entry)
        window = records[max(0, stop - policy.lookback):stop]
        days = len({item[1].date() for item in window})
        sigma = stdev([item[2] for item in window]) if len(window) >= 2 else None
        return dict(history_n=len(window), history_days=days, volatility_pct=sigma,
                    latest_history_observed=None if not window else window[-1][0].isoformat(sep=' '),
                    history_ready=len(window) >= policy.min_observations and days >= policy.min_days)


class Allocator:
    def __init__(self, policy, sectors, history, stresses):
        self.policy, self.sectors, self.history, self.stresses = policy, sectors, history, stresses
        self.snapshots = []
        self.stress_worst = {stress.name: dict(loss_pct=0.0, time=None, liquidation_equity=None,
                                             reference_cost_basis_equity=None, sector_loss={}) for stress in stresses}

    def sector(self, code):
        return self.sectors.get(code, 'UNKNOWN')

    def budget(self, row, entry, cash, positions, equity):
        policy = self.policy
        sector = self.sector(str(row['code']))
        details = dict(sector=sector, allocation_status='ALLOCATED')
        if sector == 'UNKNOWN' and policy.unknown_sector_policy == 'block':
            return 0, dict(details, allocation_status='UNKNOWN_SECTOR')
        if policy.mode == 'fixed_cash':
            requested = policy.fixed_cash
        elif policy.mode == 'fixed_fraction':
            requested = equity * policy.fraction
        else:
            estimate = self.history.estimate(str(row['code']), entry, policy)
            details.update(estimate)
            if not estimate['history_ready']:
                return 0, dict(details, allocation_status='INSUFFICIENT_PAST_VOLATILITY')
            sigma = max(estimate['volatility_pct'], policy.volatility_floor_pct)
            requested = equity * policy.target_volatility_pct / sigma
        gross = sum(item['entry_outlay'] for item in positions.values())
        sector_used = sum(item['entry_outlay'] for item in positions.values() if item['sector'] == sector)
        symbol_used = sum(item['entry_outlay'] for item in positions.values() if item['code'] == str(row['code']))
        limits = dict(cash=cash, symbol=max(0, equity * policy.max_symbol_fraction - symbol_used),
                      sector=max(0, equity * policy.max_sector_fraction - sector_used),
                      gross=max(0, equity * policy.max_gross_fraction - gross))
        budget = min(requested, *limits.values())
        details.update(requested_budget=requested, allocated_budget=budget, remaining_limits=limits)
        if budget <= 1e-10:
            return 0, dict(details, allocation_status='CONCENTRATION_OR_CASH_LIMIT')
        return budget, details

    def snapshot(self, when, cash, positions, scenario):
        equity = cash + sum(item['entry_outlay'] for item in positions.values())
        sector_amounts = defaultdict(float)
        symbols = {}
        for code, order in positions.items():
            symbols[code] = order['entry_outlay'] / equity
            sector_amounts[order['sector']] += order['entry_outlay']
        sectors = {key: amount / equity for key, amount in sorted(sector_amounts.items())}
        gross = sum(symbols.values())
        self.snapshots.append(dict(time=when.isoformat(sep=' '), cash=cash, cost_basis_equity=equity,
                                   gross_fraction=gross, symbol_fractions=symbols, sector_fractions=sectors,
                                   sector_hhi=None if gross == 0 else sum((value / gross) ** 2 for value in sectors.values())))
        for stress in self.stresses:
            liquidation, sector_loss = cash, defaultdict(float)
            for order in positions.values():
                market_factor = 1 + stress.market_return_pct / 100
                sector_factor = 1 + stress.sector_returns_pct.get(order['sector'], 0) / 100
                reference = finite(order['entry_reference_price']) * market_factor * sector_factor
                proceeds = 0 if reference == 0 else close_order(order, reference, scenario)['exit_proceeds']
                liquidation += proceeds
                sector_loss[order['sector']] += order['entry_outlay'] - proceeds
            loss_pct = max(0, (equity - liquidation) / equity * 100)
            previous = self.stress_worst[stress.name]
            if previous['time'] is None or loss_pct > previous['loss_pct']:
                self.stress_worst[stress.name] = dict(loss_pct=loss_pct, time=when.isoformat(sep=' '),
                    liquidation_equity=liquidation, reference_cost_basis_equity=equity,
                    sector_loss=dict(sorted(sector_loss.items())))

    def report(self):
        return dict(allocation=asdict(self.policy), snapshots=self.snapshots,
                    peak_gross_fraction=max((item['gross_fraction'] for item in self.snapshots), default=0),
                    peak_symbol_fraction=max((max(item['symbol_fractions'].values(), default=0) for item in self.snapshots), default=0),
                    peak_sector_fraction=max((max(item['sector_fractions'].values(), default=0) for item in self.snapshots), default=0),
                    stress_worst=self.stress_worst)
