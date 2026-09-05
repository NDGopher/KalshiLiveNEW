"""P0: Create Order V2 NO/Under cost basis is T, not 100−T.

2026-09-05 ~18:03–18:22 CT: Under fills logged executed=100−take
(WSH@LAD Under 5.5 take 34¢ → executed 66¢, 69 contracts, cost $45.54).
V2 quotes the YES book: buy NO @ 34¢ is ask @ 0.6600. The YES-leg fill
is 66¢; NO economics are 34¢. Auto-bet stays OFF. No live orders.

Docs: https://docs.kalshi.com/api-reference/orders/create-order-v2
BookSide: bid=buy YES, ask=sell YES ≡ buy NO at 1−price.
"""
from __future__ import annotations

from pathlib import Path

import asyncio
from unittest.mock import MagicMock

from auto_bet_sizing import size_auto_bet_order
from execution_guard import (
    KALSHI_CREATE_ORDER_V2_PATH,
    build_limit_order_payload,
    is_complement_no_cost,
    no_payload_quotes_yes_leg_complement,
    v2_fill_side_economics,
    yes_leg_limit_cents,
    yes_leg_to_side_cents,
)

REPO = Path(__file__).resolve().parents[1]
WSH_LAD_UNDER_55 = "KXMLBTOTAL-26SEP05WSHLAD-6"
WYO_ML = "KXNCAAFGAME-26SEP05WYOCSU-WYO"


def test_no_34_v2_payload_is_ask_at_06600():
    """Official mapping: buy NO @ 34¢ → ask at 0.6600 on the YES book."""
    assert yes_leg_limit_cents(side="no", price_cents=34) == 66
    payload, reasons = build_limit_order_payload(
        ticker=WSH_LAD_UNDER_55,
        side="no",
        count=69,
        price_cents=34,
        post_only=False,
    )
    assert reasons == []
    assert payload["side"] == "ask"
    assert payload["price"] == "0.6600"
    assert payload["count"] == "69.00"
    assert payload["time_in_force"] == "immediate_or_cancel"
    assert no_payload_quotes_yes_leg_complement(34, payload)
    assert payload.get("type") != "market"


def test_no_34_yes_leg_fill_66_is_34_cost_basis_not_45_dollars():
    """WSH@LAD Under 5.5: V2 fill 0.6600 → executed 34¢, cost ~$23.46 not $45.54."""
    executed, cost = v2_fill_side_economics(
        side="no",
        yes_leg_cents=66,
        fill_count=69,
        fees_cents=0,
    )
    assert executed == 34
    assert cost == 69 * 34
    assert abs(cost / 100.0 - 23.46) < 1e-9
    assert cost != 69 * 66
    assert abs(cost / 100.0 - 45.54) > 1.0
    assert not is_complement_no_cost(34, executed)
    assert yes_leg_to_side_cents(side="no", yes_leg_cents=66) == 34


def test_never_pay_66_for_a_34_cent_no_take():
    """Regression: never book 66¢ / $45.54 as the cost of a 34¢ NO take."""
    executed, cost = v2_fill_side_economics(side="no", yes_leg_cents=66, fill_count=69)
    assert executed != 66
    assert cost != 4554
    assert is_complement_no_cost(34, 66) is True
    assert is_complement_no_cost(34, 34) is False
    # Incident siblings: take T, raw YES-leg fill 100−T → side T.
    for take in (42, 49, 46):
        yes_leg = 100 - take
        exe, cst = v2_fill_side_economics(side="no", yes_leg_cents=yes_leg, fill_count=10)
        assert exe == take
        assert cst == 10 * take
        assert cst != 10 * yes_leg
        assert not is_complement_no_cost(take, exe)


def test_yes_47_unchanged():
    """Buy YES @ 47¢ stays bid @ 0.4700; fill 0.4700 stays 47¢ economics."""
    payload, reasons = build_limit_order_payload(
        ticker=WYO_ML,
        side="yes",
        count=53,
        price_cents=47,
        post_only=False,
    )
    assert reasons == []
    assert payload["side"] == "bid"
    assert payload["price"] == "0.4700"
    executed, cost = v2_fill_side_economics(side="yes", yes_leg_cents=47, fill_count=53)
    assert executed == 47
    assert cost == 53 * 47
    assert yes_leg_to_side_cents(side="yes", yes_leg_cents=47) == 47


def test_refuse_uncomplemented_no_payload():
    """ask @ 0.3400 for a 34¢ NO take would buy NO at 66¢ — fail-closed."""
    bad = {
        "ticker": WSH_LAD_UNDER_55,
        "side": "ask",
        "count": "69.00",
        "price": "0.3400",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
    }
    assert no_payload_quotes_yes_leg_complement(34, bad) is False
    flipped = dict(bad, side="bid", price="0.6600")
    assert no_payload_quotes_yes_leg_complement(34, flipped) is False
    good, _ = build_limit_order_payload(
        ticker=WSH_LAD_UNDER_55,
        side="no",
        count=69,
        price_cents=34,
    )
    assert no_payload_quotes_yes_leg_complement(34, good) is True


def test_stake_sizes_at_no_34_not_complement_66():
    """$25 filter at 34¢ NO → ~73 contracts / ~$25, not 37 contracts at 66¢."""
    stake, contracts, cost = size_auto_bet_order(25.0, 34)
    assert stake == 25.0
    assert contracts == 73
    assert abs(cost - 73 * 0.34) < 1e-9
    assert cost <= 25.0 + 1e-9
    _, complement_contracts, complement_cost = size_auto_bet_order(25.0, 66)
    assert complement_contracts == 37
    assert contracts != complement_contracts
    assert cost < 30.0
    assert complement_cost < 25.0 + 1e-9


class _V2FillResp:
    def __init__(self, body, status=201):
        self.status = status
        self.headers = {}
        self._body = body

    async def json(self):
        return self._body

    async def text(self):
        return ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


class _V2Session:
    def __init__(self, body):
        self.body = body
        self.posted = []

    def post(self, url, headers=None, data=None):
        self.posted.append({"url": url, "data": data})
        return _V2FillResp(self.body)


def _client_for_v2_fill(body, ticker):
    from kalshi_client import KalshiClient

    client = KalshiClient()
    client.has_trading_credentials = lambda: True
    client.bet_lock = asyncio.Lock()
    client.session = _V2Session(body)
    client.auth = MagicMock()
    client.auth.priv = object()
    client.auth.kid = "test-kid"
    client.auth.sign.return_value = ("1", "sig")
    client.demo_mode = False
    client.orderbooks[ticker] = {}
    return client


def test_place_order_no_34_v2_fill_066_returns_34_cost_not_66():
    """Live-shaped V2 response: average_fill_price 0.6600 + 69 fills → NO 34¢."""
    from kalshi_client import KALSHI_CREATE_ORDER_V2_PATH

    body = {
        "order_id": "ord-wsh-lad-under-55",
        "fill_count": "69.00",
        "remaining_count": "0.00",
        "average_fill_price": "0.6600",
        "average_fee_paid": "0.0000",
        "ts_ms": 1,
        "fills": [{"count": "69.00", "price": "0.6600"}],
        "taker_fees": 0,
        "maker_fees": 0,
    }
    client = _client_for_v2_fill(body, WSH_LAD_UNDER_55)

    async def run():
        return await client.place_order(
            ticker=WSH_LAD_UNDER_55,
            side="no",
            count=69,
            expected_price_cents=34,
            validate_odds=False,
            skip_duplicate_check=True,
        )

    result = asyncio.run(run())
    assert result.get("success") is True
    assert result["executed_price_cents"] == 34
    assert result["executed_price_cents"] != 66
    assert result["price_cents"] == 34
    assert result["fill_count"] == 69
    assert result["total_cost_cents"] == 69 * 34
    assert abs(result["total_cost_cents"] / 100.0 - 45.54) > 1.0
    posted = client.session.posted
    assert posted
    raw = posted[0]["data"]
    assert isinstance(raw, (bytes, bytearray))
    text = raw.decode("utf-8")
    assert '"side":"ask"' in text
    assert '"price":"0.6600"' in text
    assert KALSHI_CREATE_ORDER_V2_PATH in posted[0]["url"]


def test_place_order_yes_47_v2_fill_unchanged():
    body = {
        "order_id": "ord-yes-47",
        "fill_count": "53.00",
        "remaining_count": "0.00",
        "average_fill_price": "0.4700",
        "fills": [{"count": "53.00", "price": "0.4700"}],
        "taker_fees": 0,
        "maker_fees": 0,
    }
    client = _client_for_v2_fill(body, WYO_ML)

    async def run():
        return await client.place_order(
            ticker=WYO_ML,
            side="yes",
            count=53,
            expected_price_cents=47,
            validate_odds=False,
            skip_duplicate_check=True,
        )

    result = asyncio.run(run())
    assert result.get("success") is True
    assert result["executed_price_cents"] == 47
    assert result["total_cost_cents"] == 53 * 47
    text = client.session.posted[0]["data"].decode("utf-8")
    assert '"side":"bid"' in text
    assert '"price":"0.4700"' in text


def test_place_order_converts_yes_leg_fill_and_guards_no_payload():
    client = (REPO / "kalshi_client.py").read_text(encoding="utf-8")
    assert "v2_fill_side_economics(" in client
    assert "no_payload_quotes_yes_leg_complement(" in client
    assert "no_must_ask_at_yes_leg_complement" in client
    assert "YES-leg fill" in client
    assert KALSHI_CREATE_ORDER_V2_PATH == "/trade-api/v2/portfolio/events/orders"
    assert 'path = "/trade-api/v2/portfolio/orders"' not in client
    dash = (REPO / "dashboard.py").read_text(encoding="utf-8")
    assert "from auto_bet_sizing import size_auto_bet_order" in dash
    odds = (REPO / "odds_api_client.py").read_text(encoding="utf-8")
    assert "DEFAULT_ODDS_API_BOOKMAKERS" in odds
