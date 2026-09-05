"""Auto-bet stake sizing.

Configured filter amount is the order budget. Spend ~amount dollars at the
limit price: ``contracts = floor(amount / price)``. Fail-closed / clamp so
order cost (contracts * price) never silently exceeds the filter amount.

Incident (2026-09-05 17:30 CT, HCU@Rice Over 47.5): filter amount was $25
but place used ~198 contracts / cost ~$97 at 49¢ because ``total_bet_amount``
($101) silently replaced the filter stake. Market-type defaults (ML $151 /
Total $101 / Spread $75 / NHL over $202) and PX+Novig 2x must not 4x size.
"""
from __future__ import annotations

from typing import Any, Tuple


def resolve_auto_bet_order_dollars(filter_amount_dollars: Any) -> float:
    """Return the configured filter amount, or 0.0 if invalid (fail-closed)."""
    try:
        amount = float(filter_amount_dollars)
    except (TypeError, ValueError):
        return 0.0
    if amount <= 0:
        return 0.0
    return amount


def auto_bet_stake_respecting_filter(
    filter_amount_dollars: Any,
    proposed_dollars: Any = None,
) -> float:
    """Order budget is the configured filter amount.

    Legacy market-type / NHL / PX+Novig sizes may be passed as
    ``proposed_dollars`` for logging. If they exceed the filter amount they
    are clamped down. If they are below it, still spend the filter amount
    (prefer ~amount dollars at the limit price). Invalid filter → 0.
    """
    cap = resolve_auto_bet_order_dollars(filter_amount_dollars)
    if cap <= 0:
        return 0.0
    if proposed_dollars is None:
        return cap
    try:
        proposed = float(proposed_dollars)
    except (TypeError, ValueError):
        return cap
    if proposed > cap:
        return cap
    return cap


def contracts_for_stake_dollars(dollars: Any, price_cents: Any) -> int:
    """Contracts to spend ~dollars at the limit price.

    ``contracts = floor(amount / price)``. Invalid amount/price, or amount
    too small for one contract, returns 0 (fail-closed). Never rounds up,
    so ``contracts * price <= amount``.
    """
    amount = resolve_auto_bet_order_dollars(dollars)
    try:
        cents = int(price_cents)
    except (TypeError, ValueError):
        return 0
    if amount <= 0 or cents <= 0:
        return 0
    # Integer cents: floor(amount_cents / price_cents).
    amount_cents = int(amount * 100.0)
    if amount_cents <= 0:
        return 0
    return amount_cents // cents


def order_cost_dollars(contracts: int, price_cents: Any) -> float:
    """``contracts * price`` in dollars. 0 if invalid."""
    try:
        count = int(contracts)
        cents = int(price_cents)
    except (TypeError, ValueError):
        return 0.0
    if count <= 0 or cents <= 0:
        return 0.0
    return (count * cents) / 100.0


def size_auto_bet_order(
    filter_amount_dollars: Any,
    price_cents: Any,
    proposed_dollars: Any = None,
) -> Tuple[float, int, float]:
    """Filter-capped stake, contract count, and limit-price cost.

    Returns ``(stake_dollars, contracts, cost_dollars)``. Fail-closed zeros
    when the filter amount or price cannot size a legal order.
    """
    stake = auto_bet_stake_respecting_filter(filter_amount_dollars, proposed_dollars)
    contracts = contracts_for_stake_dollars(stake, price_cents)
    cost = order_cost_dollars(contracts, price_cents)
    if contracts < 1 or cost - stake > 1e-9:
        return (stake, 0, 0.0)
    return (stake, contracts, cost)
