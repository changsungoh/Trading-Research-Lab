"""Chronological cash reservation and realized-risk controls for offline scenarios."""
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import heapq
from research.statistical_analysis import describe, finite, timestamp, valid_observation
from simulation.execution_simulator import Scenario, entry_order, close_order, fingerprint, assumed_exit_fill


@dataclass(frozen=True)
class RiskPolicy:
    initial_cash: float
    position_fraction: float
    max_positions: int
    max_trades_per_day: int
    daily_loss_limit_pct: float
    max_consecutive_losses: int

    def __post_init__(self):
        for field in ('initial_cash', 'position_fraction', 'daily_loss_limit_pct'):
            if type(getattr(self, field)) not in (int, float):
                raise ValueError(f'{field} must be a JSON number')
        if finite(self.initial_cash) <= 0:
            raise ValueError('initial_cash must be positive')
        if not 0 < finite(self.position_fraction) <= 1:
            raise ValueError('position_fraction must be in (0, 1]')
        if not 0 < finite(self.daily_loss_limit_pct) <= 100:
            raise ValueError('daily_loss_limit_pct must be in (0, 100]')
        for field in ('max_positions', 'max_trades_per_day', 'max_consecutive_losses'):
            value = getattr(self, field)
            if type(value) is not int or value < 1:
                raise ValueError(f'{field} must be a positive integer')


def simulate(rows, scenario, policy, *, horizon, seed=1729, allocation=None):
    if horizon not in (5, 10, 30):
        raise ValueError('Select exactly one horizon: 5, 10 or 30')
    rows = list(rows)
    identities, candidates, quality = set(), [], Counter()
    entry_exclusions = []
    for row in rows:
        identity = (row.get('code'), row.get('entry_time'), row.get('rule_version') or '')
        if identity in identities:
            raise ValueError('Duplicate research identity')
        identities.add(identity)
        observation, status = valid_observation(row, horizon)
        quality[status] += 1
        try:
            entry = timestamp(row['entry_time'])
            price = finite(row['entry_price'])
            code = str(row.get('code') or '')
            if not code or price <= 0 or entry.weekday() >= 5 or not (9, 0) <= (entry.hour, entry.minute) < (15, 30):
                raise ValueError('Invalid entry')
        except (ValueError, TypeError, KeyError):
            entry_exclusions.append(dict(identity=fingerprint(identity), code=str(row.get('code') or ''),
                                         entry_time=row.get('entry_time'), status='INVALID_ENTRY'))
            continue
        candidates.append((entry, code, str(row.get('rule_version') or ''), fingerprint(identity), row, observation, status))
    candidates.sort(key=lambda item: item[:4])
    cash = float(policy.initial_cash)
    realized_pnl = 0.0
    open_positions, exits, trades, decisions, ledger = {}, [], [], list(entry_exclusions), []
    daily = defaultdict(lambda: dict(realized_pnl=0.0, entries=0, exits=0, blocked=0))
    current_day, day_start_equity, day_pnl, streak, day_entries = None, cash, 0.0, 0, 0
    halted_reason = None
    peak, max_drawdown, worst_streak = cash, 0.0, 0

    def settle(until):
        nonlocal cash, realized_pnl, day_pnl, streak, peak, max_drawdown, worst_streak, halted_reason
        while exits and (until is None or exits[0][0] <= until):
            exit_time, identity, code, exit_price = heapq.heappop(exits)
            order = open_positions.pop(code)
            closed = close_order(order, exit_price, scenario)
            cash += closed['exit_proceeds']
            realized_pnl += closed['net_pnl']
            day_pnl += closed['net_pnl']
            streak = streak + 1 if closed['net_pnl'] < 0 else (0 if closed['net_pnl'] > 0 else streak)
            worst_streak = max(worst_streak, streak)
            if day_pnl <= -day_start_equity * policy.daily_loss_limit_pct / 100:
                halted_reason = 'DAILY_LOSS_LIMIT'
            elif streak >= policy.max_consecutive_losses:
                halted_reason = 'LOSS_STREAK_LIMIT'
            day = exit_time.date().isoformat()
            daily[day]['realized_pnl'] += closed['net_pnl']
            daily[day]['exits'] += 1
            trades.append(dict(order, **closed, exit_time=exit_time.isoformat(sep=' ')))
            # Cost-basis ledger is not marked-to-market portfolio equity.
            book_equity = cash + sum(position['entry_outlay'] for position in open_positions.values())
            peak = max(peak, book_equity)
            max_drawdown = max(max_drawdown, (peak - book_equity) / peak * 100)
            if allocation is not None:
                allocation.snapshot(exit_time, cash, open_positions, scenario)
            ledger.append(dict(time=exit_time.isoformat(sep=' '), cash=cash,
                               cost_basis_equity=book_equity, realized_pnl=realized_pnl))

    for entry, code, rule, identity, row, observation, status in candidates:
        settle(entry)  # Known exits at a timestamp happen before entries at that timestamp.
        day = entry.date().isoformat()
        if day != current_day:
            current_day = day
            day_start_equity = cash + sum(position['entry_outlay'] for position in open_positions.values())
            day_pnl, streak, day_entries, halted_reason = 0.0, 0, 0, None
        decision = dict(identity=identity, code=code, entry_time=entry.isoformat(sep=' '), exit_quality=status)
        reason = halted_reason
        if reason is None:
            if code in open_positions:
                reason = 'SYMBOL_ALREADY_OPEN'
            elif len(open_positions) >= policy.max_positions:
                reason = 'MAX_POSITIONS'
            elif day_entries >= policy.max_trades_per_day:
                reason = 'MAX_DAILY_TRADES'
        if reason:
            decision['status'] = reason
            daily[day]['blocked'] += 1
            decisions.append(decision)
            continue
        book_equity = cash + sum(position['entry_outlay'] for position in open_positions.values())
        budget = min(cash, book_equity * policy.position_fraction)
        if allocation is not None:
            budget, details = allocation.budget(row, entry, cash, open_positions, book_equity)
            budget = min(budget, cash, book_equity * policy.position_fraction)
            details.update(allocated_budget=budget, common_position_cap=book_equity * policy.position_fraction)
            decision.update(details)
            if budget <= 0:
                decision['status'] = details.get('allocation_status', 'ALLOCATION_BLOCKED')
                decisions.append(decision)
                daily[day]['blocked'] += 1
                continue
        order = entry_order(row['entry_price'], budget, scenario, identity, seed)
        decision.update(order)
        decisions.append(decision)
        if order['status'] != 'FILLED':
            continue
        cash -= order['entry_outlay']
        # Clamp only arithmetic noise; a material shortfall is an implementation error.
        if cash < -1e-7 * max(1, policy.initial_cash):
            raise ArithmeticError('Cash overspent')
        cash = max(0.0, cash)
        day_entries += 1
        daily[day]['entries'] += 1
        position = dict(order, identity=identity, code=code, entry_time=entry.isoformat(sep=' '),
                        exit_quality=status, entry_reference_price=row['entry_price'])
        if allocation is not None:
            position['sector'] = allocation.sector(code)
        open_positions[code] = position
        if allocation is not None:
            allocation.snapshot(entry, cash, open_positions, scenario)
        if observation is not None:
            _, observed, _ = observation
            position['gross_return_pct'] = 100 * (finite(row[f'price_{horizon}m']) / finite(row['entry_price']) - 1)
            if assumed_exit_fill(scenario, identity, seed):
                heapq.heappush(exits, (observed, identity, code, finite(row[f'price_{horizon}m'])))
            else:
                position['exit_quality'] = 'ASSUMED_EXIT_NO_FILL'
        # Unknown exits never release reserved cash and never become zero-return trades.
    settle(None)
    closed_samples = [(timestamp(trade['entry_time']).date().isoformat(), trade['net_return_pct']) for trade in trades]
    gross_samples = [(timestamp(trade['entry_time']).date().isoformat(), trade['gross_return_pct']) for trade in trades]
    remaining = sorted(open_positions.values(), key=lambda order: (order['entry_time'], order['code']))
    return dict(scenario=asdict(scenario), policy=asdict(policy), horizon=horizon, seed=seed,
        input_sha256=fingerprint(sorted(fingerprint(row) for row in rows)), input_rows=len(rows),
        quality=dict(sorted(quality.items())), decision_counts=dict(sorted(Counter(item['status'] for item in decisions).items())),
        filled=sum(item['status'] == 'FILLED' for item in decisions), closed=len(trades), unresolved=len(remaining),
        completeness=('INCOMPLETE_UNPRICED_POSITIONS' if remaining else
                      'NO_ELIGIBLE_ENTRIES' if not candidates else
                      'NO_FILLED_POSITIONS' if not trades else 'ALL_FILLED_POSITIONS_CLOSED'),
        entry_excluded=len(entry_exclusions),
        ending_cash=cash, reserved_entry_outlay=sum(item['entry_outlay'] for item in remaining),
        closed_gross_reference_pnl=sum(trade['gross_reference_pnl'] for trade in trades),
        closed_cost_drag=sum(trade['total_cost_drag'] for trade in trades),
        closed_adverse_price_cost=sum(trade['adverse_price_cost'] for trade in trades),
        known_explicit_costs=(sum(item.get('entry_fee', 0) for item in decisions) +
                              sum(trade['exit_fee'] + trade['exit_tax'] for trade in trades)),
        realized_net_pnl=realized_pnl, terminal_equity=None if remaining else cash,
        portfolio_return_pct=None if remaining else (cash / policy.initial_cash - 1) * 100,
        max_cost_basis_drawdown_pct=max_drawdown, max_daily_realized_loss_streak=worst_streak,
        closed_gross_stats=describe(gross_samples), closed_net_stats=describe(closed_samples),
        daily=dict(sorted(daily.items())), decisions=decisions, trades=trades,
        unresolved_positions=remaining, realized_ledger=ledger)
