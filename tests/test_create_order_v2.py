"""Create Order V2 payload + path. No live orders. Auto-bet stays OFF.

Official: https://docs.kalshi.com/api-reference/orders/create-order-v2
POST /trade-api/v2/portfolio/events/orders
Required: ticker, side (bid|ask), count fp, price dollar fp, time_in_force,
self_trade_prevention_type.

Mapping (YES-leg quote):
  buy YES @ P¢ → bid at "0.P000"
  buy NO  @ P¢ → ask at "(100-P)/100" dollars
"""
from __future__ import annotations

from pathlib import Path

from execution_guard import (
    KALSHI_CANCEL_ORDER_V2_PATH_TMPL,
    KALSHI_CREATE_ORDER_V2_PATH,
    build_limit_order_payload,
    count_fp,
    dollars_fp_from_cents,
    yes_leg_limit_cents,
)
from kalshi_client import parse_fill_price_cents, parse_order_count_fp

REPO = Path(__file__).resolve().parents[1]
WYO_ML = "KXNCAAFGAME-26SEP05WYOCSU-WYO"


def test_create_order_v2_path_is_events_orders():
    assert KALSHI_CREATE_ORDER_V2_PATH == "/trade-api/v2/portfolio/events/orders"
    client = (REPO / "kalshi_client.py").read_text(encoding="utf-8")
    assert "KALSHI_CREATE_ORDER_V2_PATH" in client
    assert 'path = "/trade-api/v2/portfolio/orders"' not in client
    assert KALSHI_CANCEL_ORDER_V2_PATH_TMPL.format(order_id="x") == (
        "/trade-api/v2/portfolio/events/orders/x"
    )


def test_v2_fp_helpers():
    assert dollars_fp_from_cents(43) == "0.4300"
    assert dollars_fp_from_cents(1) == "0.0100"
    assert count_fp(10) == "10.00"
    assert count_fp(1) == "1.00"
    assert yes_leg_limit_cents(side="yes", price_cents=43) == 43
    assert yes_leg_limit_cents(side="no", price_cents=43) == 57
    assert yes_leg_limit_cents(side="no", price_cents=41) == 59
    assert yes_leg_limit_cents(side="no", price_cents=34) == 66
    assert yes_leg_limit_cents(side="yes", price_cents=47) == 47


def test_wyoming_class_buy_yes_is_bid_at_alert_dollars():
    """Live 410 case: KXNCAAFGAME-26SEP05WYOCSU-WYO side=yes must be V2 bid."""
    payload, reasons = build_limit_order_payload(
        ticker=WYO_ML,
        side="yes",
        count=10,
        price_cents=43,
        post_only=False,
        client_order_id="wyo-test-client-id",
    )
    assert reasons == []
    assert payload == {
        "ticker": WYO_ML,
        "side": "bid",
        "count": "10.00",
        "price": "0.4300",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": "wyo-test-client-id",
    }
    assert "yes_price" not in payload
    assert "no_price" not in payload
    assert payload.get("type") != "market"


def test_buy_no_is_ask_at_yes_leg_complement():
    payload, reasons = build_limit_order_payload(
        ticker="KXNCAAFTOTAL-26SEP05BALLOSU-60",
        side="no",
        count=7,
        price_cents=43,
        post_only=False,
    )
    assert reasons == []
    assert payload["side"] == "ask"
    assert payload["price"] == "0.5700"
    assert payload["count"] == "7.00"
    assert payload["time_in_force"] == "immediate_or_cancel"


def test_v2_response_fp_parsers():
    assert parse_order_count_fp("3.00") == 3.0
    assert parse_order_count_fp("0.00") == 0.0
    assert parse_order_count_fp(None, None) is None
    assert parse_fill_price_cents("0.4300") == 43
    assert parse_fill_price_cents(0.43) == 43
    assert parse_fill_price_cents(43) == 43


def test_dashboard_does_not_terminal_skip_solely_on_strict_pass():
    dash = (REPO / "dashboard.py").read_text(encoding="utf-8")
    assert '_terminal_skip("strict_pass=False"' not in dash
    assert "Alert failed strict_pass gate" not in dash
    assert '"autobet_allow=False"' in dash
    assert "Do not _terminal_skip solely for" in dash
