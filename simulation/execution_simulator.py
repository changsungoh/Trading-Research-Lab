"""Long-only adverse-price and explicit cost assumptions, not observed fills."""
from dataclasses import dataclass
import hashlib
import json
import math
import random
from research.statistical_analysis import finite


@dataclass(frozen=True)
class Scenario:
    name: str
    buy_fee_bps: float
    sell_fee_bps: float
    sell_tax_bps: float
    spread_bps: float
    slippage_bps: float
    fill_probability: float
    fill_fraction: float
    exit_fill_probability: float

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError('Scenario name must be a nonempty string')
        for field in ('buy_fee_bps', 'sell_fee_bps', 'sell_tax_bps', 'spread_bps', 'slippage_bps', 'fill_probability', 'fill_fraction', 'exit_fill_probability'):
            if type(getattr(self, field)) not in (int, float):
                raise ValueError(f'{field} must be a JSON number')
        for field in ('buy_fee_bps', 'sell_fee_bps', 'sell_tax_bps', 'spread_bps', 'slippage_bps'):
            value = finite(getattr(self, field))
            if not 0 <= value < 10000:
                raise ValueError(f'{field} must be in [0, 10000)')
        if self.spread_bps / 2 + self.slippage_bps >= 10000:
            raise ValueError('Adverse exit price must stay positive')
        if self.sell_fee_bps + self.sell_tax_bps >= 10000:
            raise ValueError('Sell fees plus tax must be less than proceeds')
        if not 0 <= finite(self.fill_probability) <= 1:
            raise ValueError('fill_probability must be in [0, 1]')
        if not 0 <= finite(self.exit_fill_probability) <= 1:
            raise ValueError('exit_fill_probability must be in [0, 1]')
        if not 0 < finite(self.fill_fraction) <= 1:
            raise ValueError('fill_fraction must be in (0, 1]')


def assumed_entry(reference_price, scenario):
    reference_price = finite(reference_price)
    if reference_price <= 0:
        raise ValueError('Entry reference must be positive')
    return reference_price * (1 + (scenario.spread_bps / 2 + scenario.slippage_bps) / 10000)


def entry_order(reference_price, budget, scenario, identity, seed):
    """Use entry data only. Stable identity-based draw avoids outcome/order dependence."""
    price = assumed_entry(reference_price, scenario)
    unit_outlay = price * (1 + scenario.buy_fee_bps / 10000)
    requested = math.floor(finite(budget) / unit_outlay)
    if requested < 1:
        return dict(status='INSUFFICIENT_CASH', requested_quantity=0, quantity=0)
    draw = random.Random(f'{seed}:{identity}').random()
    if draw >= scenario.fill_probability:
        return dict(status='NO_FILL', requested_quantity=requested, quantity=0)
    quantity = math.floor(requested * scenario.fill_fraction)
    if quantity < 1:
        return dict(status='PARTIAL_FILL_ZERO', requested_quantity=requested, quantity=0)
    notional = price * quantity
    fee = notional * scenario.buy_fee_bps / 10000
    return dict(status='FILLED', requested_quantity=requested, quantity=quantity,
                entry_reference_price=finite(reference_price), entry_fill_price=price, entry_notional=notional, entry_fee=fee,
                entry_outlay=notional + fee)


def close_order(order, exit_reference, scenario):
    price = finite(exit_reference)
    if price <= 0:
        raise ValueError('Exit reference must be positive')
    price *= 1 - (scenario.spread_bps / 2 + scenario.slippage_bps) / 10000
    notional = price * order['quantity']
    fee = notional * scenario.sell_fee_bps / 10000
    tax = notional * scenario.sell_tax_bps / 10000
    proceeds = notional - fee - tax
    pnl = proceeds - order['entry_outlay']
    gross_reference_pnl = (finite(exit_reference) - finite(order['entry_reference_price'])) * order['quantity']
    explicit_cost = order['entry_fee'] + fee + tax
    adverse_price_cost = ((order['entry_fill_price'] - finite(order['entry_reference_price'])) +
                          (finite(exit_reference) - price)) * order['quantity']
    return dict(gross_reference_pnl=gross_reference_pnl, explicit_cost=explicit_cost,
                adverse_price_cost=adverse_price_cost, total_cost_drag=gross_reference_pnl - pnl,
                exit_fill_price=price, exit_notional=notional, exit_fee=fee,
                exit_tax=tax, exit_proceeds=proceeds, net_pnl=pnl,
                net_return_pct=100 * pnl / order['entry_outlay'])


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode('utf-8')).hexdigest()


def assumed_exit_fill(scenario, identity, seed):
    return random.Random(f'{seed}:{identity}:exit').random() < scenario.exit_fill_probability
