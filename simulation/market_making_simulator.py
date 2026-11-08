"""P4 causal, single-asset synthetic/tape market-making laboratory; never sends orders."""
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import math
import random
from statistics import mean
from simulation.execution_simulator import fingerprint
from research.statistical_analysis import finite


def number(value, name, low, high=None):
    if type(value) not in (int, float) or not math.isfinite(value) or value < low or (high is not None and value > high):
        raise ValueError(f'Invalid {name}')


@dataclass(frozen=True)
class MakerPolicy:
    name: str
    initial_cash: float
    initial_mid: float
    tick_size: float
    quote_quantity: int
    max_inventory: int
    max_notional_fraction: float
    half_spread_bps: float
    inventory_skew_bps: float
    ewma_alpha: float
    fee_bps: float
    max_drawdown_pct: float
    markout_steps: int

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError('Policy name required')
        for field in ('initial_cash', 'initial_mid', 'tick_size', 'half_spread_bps'):
            number(getattr(self, field), field, 1e-12)
        if self.tick_size >= self.initial_mid:
            raise ValueError('tick_size must be below initial_mid')
        for field in ('inventory_skew_bps', 'fee_bps'):
            number(getattr(self, field), field, 0, 9999)
        number(self.ewma_alpha, 'ewma_alpha', 1e-12, 1)
        number(self.max_notional_fraction, 'max_notional_fraction', 1e-12, 1)
        number(self.max_drawdown_pct, 'max_drawdown_pct', 1e-12, 100)
        for field in ('quote_quantity', 'max_inventory', 'markout_steps'):
            if type(getattr(self, field)) is not int or getattr(self, field) < 1:
                raise ValueError(f'{field} must be a positive integer')
        if self.half_spread_bps >= 10000:
            raise ValueError('half_spread_bps must be below 10000')


@dataclass(frozen=True)
class TapeEvent:
    step: int
    mid: float
    taker_side: str
    limit_price: float
    quantity: int

    def __post_init__(self):
        if type(self.step) is not int or self.step < 1:
            raise ValueError('Event step must be a positive integer')
        number(self.mid, 'mid', 1e-12)
        number(self.limit_price, 'limit_price', 1e-12)
        if self.taker_side not in ('buy', 'sell', 'none'):
            raise ValueError('taker_side must be buy, sell or none')
        if type(self.quantity) is not int or self.quantity < 0:
            raise ValueError('Quantity must be a nonnegative integer')
        if self.taker_side == 'none' and self.quantity != 0:
            raise ValueError('No-order event must have zero quantity')


@dataclass(frozen=True)
class MarketModel:
    steps: int
    initial_mid: float
    volatility_bps: float
    drift_bps: float
    arrival_probability: float
    informed_probability: float
    taker_reach_bps: float
    max_taker_quantity: int

    def __post_init__(self):
        for field in ('steps', 'max_taker_quantity'):
            if type(getattr(self, field)) is not int or getattr(self, field) < 1:
                raise ValueError(f'{field} must be a positive integer')
        number(self.initial_mid, 'initial_mid', 1e-12)
        number(self.volatility_bps, 'volatility_bps', 0, 10000)
        number(self.drift_bps, 'drift_bps', -10000, 10000)
        number(self.arrival_probability, 'arrival_probability', 0, 1)
        number(self.informed_probability, 'informed_probability', 0, 1)
        number(self.taker_reach_bps, 'taker_reach_bps', 0, 9999)


def generate_tape(model, seed):
    """Exogenous tape independent of maker policies: informed flow sees this step's price shock."""
    rng = random.Random(seed)
    mid, events = model.initial_mid, []
    for step in range(1, model.steps + 1):
        previous = mid
        mid *= math.exp((model.drift_bps + rng.gauss(0, model.volatility_bps)) / 10000)
        side, quantity = 'none', 0
        limit = mid
        if rng.random() < model.arrival_probability:
            informed = rng.random() < model.informed_probability
            side = ('buy' if mid >= previous else 'sell') if informed else rng.choice(('buy', 'sell'))
            reference = mid if informed else previous
            limit = reference * (1 + (1 if side == 'buy' else -1) * model.taker_reach_bps / 10000)
            quantity = rng.randint(1, model.max_taker_quantity)
        events.append(TapeEvent(step, mid, side, limit, quantity))
    return events


def round_tick(price, tick, rounding):
    return float((Decimal(str(price)) / Decimal(str(tick))).to_integral_value(rounding=rounding) * Decimal(str(tick)))


def quote(policy, fair, inventory, cash, equity):
    # Positive inventory shifts both quotes down to encourage inventory reduction.
    center = fair * (1 - policy.inventory_skew_bps / 10000 * inventory / policy.max_inventory)
    bid = round_tick(max(policy.tick_size, center - fair * policy.half_spread_bps / 10000), policy.tick_size, ROUND_FLOOR)
    ask = round_tick(max(bid + policy.tick_size, center + fair * policy.half_spread_bps / 10000), policy.tick_size, ROUND_CEILING)
    max_long = min(policy.max_inventory, math.floor(max(0, equity) * policy.max_notional_fraction / bid))
    max_short = min(policy.max_inventory, math.floor(max(0, equity) * policy.max_notional_fraction / ask))
    buy_quantity = max(0, min(policy.quote_quantity, max_long - inventory,
                              math.floor(max(0, cash) / (bid * (1 + policy.fee_bps / 10000)))))
    sell_quantity = max(0, min(policy.quote_quantity, max_short + inventory))
    return dict(fair=fair, center=center, bid=bid, ask=ask, bid_quantity=buy_quantity, ask_quantity=sell_quantity)


def simulate_market_maker(events, policy):
    events = list(events)
    if any(not isinstance(event, TapeEvent) for event in events):
        raise ValueError('Events must be validated TapeEvent objects')
    if any(event.step != index + 1 for index, event in enumerate(events)):
        raise ValueError('Tape steps must be contiguous and strictly increasing from 1')
    cash, inventory, fair, last_mid = policy.initial_cash, 0, policy.initial_mid, policy.initial_mid
    peak, drawdown, halted = policy.initial_cash, 0.0, False
    fills, ledger, quote_log = [], [], []
    total_fees, max_inventory, max_notional = 0.0, 0, 0.0
    for event in events:
        # Event.mid/flow are deliberately unavailable to the quote calculation.
        before = cash + inventory * last_mid
        active = None if halted else quote(policy, fair, inventory, cash, before)
        quote_log.append(dict(step=event.step, halted=halted, quote=active))
        if active is not None and event.quantity > 0:
            side = None
            if event.taker_side == 'buy' and event.limit_price >= active['ask']:
                side, quantity, price = 'sell', min(event.quantity, active['ask_quantity']), active['ask']
            elif event.taker_side == 'sell' and event.limit_price <= active['bid']:
                side, quantity, price = 'buy', min(event.quantity, active['bid_quantity']), active['bid']
            if side is not None and quantity > 0:
                signed = quantity if side == 'buy' else -quantity
                fee = quantity * price * policy.fee_bps / 10000
                cash -= signed * price + fee
                inventory += signed
                total_fees += fee
                fills.append(dict(step=event.step, side=side, quantity=quantity, price=price, fee=fee,
                    fair_at_quote=active['fair'], quoted_edge=signed * (active['fair'] - price),
                    contemporaneous_mid=event.mid, immediate_markout=signed * (event.mid - price),
                    future_markout=None, future_mid_move=None))
        last_mid = event.mid
        fair = policy.ewma_alpha * event.mid + (1 - policy.ewma_alpha) * fair
        equity = cash + inventory * event.mid
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak * 100)
        if equity <= 0 or drawdown >= policy.max_drawdown_pct:
            halted = True  # A breach cancels future quotes; it does not invent liquidation.
        max_inventory = max(max_inventory, abs(inventory))
        max_notional = max(max_notional, abs(inventory) * event.mid)
        ledger.append(dict(step=event.step, cash=cash, inventory=inventory, mid=event.mid,
                           estimated_fair=fair, equity=equity, halted=halted))
    # Diagnostics only, evaluated after the causal decision loop is complete.
    for fill in fills:
        future_index = fill['step'] - 1 + policy.markout_steps
        if future_index < len(events):
            signed = fill['quantity'] if fill['side'] == 'buy' else -fill['quantity']
            future_mid = events[future_index].mid
            fill['future_markout'] = signed * (future_mid - fill['price'])
            fill['future_mid_move'] = signed * (future_mid - fill['contemporaneous_mid'])
    final_equity = cash + inventory * last_mid
    return dict(policy=asdict(policy), tape_sha256=fingerprint([asdict(event) for event in events]),
        steps=len(events), fills=len(fills), buy_fills=sum(fill['side'] == 'buy' for fill in fills),
        sell_fills=sum(fill['side'] == 'sell' for fill in fills), ending_cash=cash, ending_inventory=inventory,
        final_mid=last_mid, marked_equity=final_equity, marked_pnl=final_equity-policy.initial_cash,
        marked_return_pct=(final_equity/policy.initial_cash-1)*100, total_fees=total_fees,
        inventory_mark_value=inventory*last_mid, max_abs_inventory=max_inventory, max_marked_notional=max_notional,
        max_marked_drawdown_pct=drawdown, halted=halted, terminal_flat=inventory == 0,
        quoted_edge_total=sum(fill['quoted_edge'] for fill in fills),
        fair_revaluation_pnl=final_equity-policy.initial_cash+total_fees-sum(fill['quoted_edge'] for fill in fills),
        immediate_markout_total=sum(fill['immediate_markout'] for fill in fills),
        future_markout_count=sum(fill['future_markout'] is not None for fill in fills),
        mean_future_markout=(mean(fill['future_markout'] for fill in fills if fill['future_markout'] is not None)
                             if any(fill['future_markout'] is not None for fill in fills) else None),
        future_mid_move_total=sum(fill['future_mid_move'] for fill in fills if fill['future_mid_move'] is not None),
        fills_log=fills, quotes=quote_log, ledger=ledger)
