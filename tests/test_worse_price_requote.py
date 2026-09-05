"""1¢ worse-price re-quote. Stephen lock 2026-09-05: do not widen to 2–3¢.

CIN ML live: alert 42¢ (+138) failed 3x with
"Price got worse by 2¢ (max allowed 1¢)" while still +EV.

Policy:
  * book 1¢ worse than alert + EV still ≥ ev_min → place at the new ask
  * book 2¢+ worse → fail-closed (same reason string as live)
  * stake / $25 clamp / Order V2 / #38 tickers / ODDS_API_BOOKMAKERS untouched

No live orders. Auto-bet stays OFF.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock

from execution_guard import (
    KALSHI_CREATE_ORDER_V2_PATH,
    build_limit_order_payload,
)
from kalshi_client import (
    MAX_WORSE_PRICE_CENTS,
    ev_percent_at_limit_cents,
    parse_fill_price_cents,
    prob_to_cents,
    resolve_worse_price_limit,
)

REPO = Path(__file__).resolve().parents[1]
CIN_ML = "KXMLBGAME-26SEP05CINXXX-CIN"


def test_max_worse_stays_one_cent_strict():
    assert MAX_WORSE_PRICE_CENTS == 1
    src = (REPO / "kalshi_client.py").read_text(encoding="utf-8")
    assert "MAX_WORSE_PRICE_CENTS = 1" in src
    assert "Do not widen to 2–3¢" in src
    assert "resolve_worse_price_limit(" in src


def test_prob_to_cents_rounds_ieee_43():
    """int(0.43 * 100) is 42; a 43¢ ask must read as 43."""
    assert int(0.43 * 100) == 42
    assert prob_to_cents(0.43) == 43
    assert prob_to_cents(0.42) == 42
    assert prob_to_cents(0.44) == 44
    assert parse_fill_price_cents("0.4300") == 43


def test_alert_42_book_43_places_at_43_when_ev_ok():
    """CIN ML: 42¢ alert, 43¢ ask, EV still ≥ 2% at 43 → re-quote 43."""
    ev_at_43 = ev_percent_at_limit_cents(6.0, 42, 43)
    assert ev_at_43 is not None and ev_at_43 + 1e-9 >= 2.0
    limit, err = resolve_worse_price_limit(42, 43, ev_percent=6.0, ev_min=2.0)
    assert err is None
    assert limit == 43
    payload, reasons = build_limit_order_payload(
        ticker=CIN_ML,
        side="yes",
        count=10,
        price_cents=limit,
        post_only=False,
    )
    assert reasons == []
    assert payload["price"] == "0.4300"
    assert payload["side"] == "bid"
    assert payload["time_in_force"] == "immediate_or_cancel"


def test_alert_42_book_44_rejects_two_cents_worse():
    """CIN ML: 42¢ alert, 44¢ ask → reject. Do not widen to 2¢."""
    limit, err = resolve_worse_price_limit(42, 44, ev_percent=6.0, ev_min=2.0)
    assert limit is None
    assert err is not None
    assert err["error"] == "Odds changed"
    assert err["expected"] == 42
    assert err["current"] == 44
    assert err["delta"] == 2
    assert err["reason"] == "Price got worse by 2¢ (max allowed: 1¢)"


def test_one_cent_worse_rejects_when_ev_drops_below_filter():
    """1¢ worse is allowed only if EV at the new ask still clears ev_min."""
    ev_at_43 = ev_percent_at_limit_cents(2.6, 42, 43)
    assert ev_at_43 is not None and ev_at_43 < 2.0
    limit, err = resolve_worse_price_limit(42, 43, ev_percent=2.6, ev_min=2.0)
    assert limit is None
    assert err is not None
    assert err["error"] == "Odds changed"
    assert "below filter min" in err["reason"]


def test_same_or_better_keeps_alert_limit():
    assert resolve_worse_price_limit(42, 42, ev_percent=4.0, ev_min=2.0) == (42, None)
    assert resolve_worse_price_limit(42, 41, ev_percent=4.0, ev_min=2.0) == (42, None)


def test_manual_path_requotes_without_ev_args():
    limit, err = resolve_worse_price_limit(42, 43)
    assert err is None
    assert limit == 43


def test_check_and_auto_bet_passes_ev_gate_into_place_order():
    dash = (REPO / "dashboard.py").read_text(encoding="utf-8")
    assert "ev_min=current_ev_min" in dash
    assert "alert_ev_percent=ev_percent" in dash
    assert "auto_bet_amount = 25.0" in dash
    assert "auto_bet_enabled = False" in dash


def test_does_not_touch_v2_or_odds_api_bookmakers():
    client = (REPO / "kalshi_client.py").read_text(encoding="utf-8")
    assert "KALSHI_CREATE_ORDER_V2_PATH" in client
    assert KALSHI_CREATE_ORDER_V2_PATH == "/trade-api/v2/portfolio/events/orders"
    assert 'path = "/trade-api/v2/portfolio/orders"' not in client
    odds = (REPO / "odds_api_client.py").read_text(encoding="utf-8")
    assert "DEFAULT_ODDS_API_BOOKMAKERS" in odds


def _yes_book(ask_dollars: float) -> dict:
    return {
        "yes": {
            "best_ask": ask_dollars,
            "best_bid": ask_dollars - 0.02,
            "best_ask_size": 200,
            "asks": [{"price": ask_dollars, "quantity": 200}],
            "bids": [{"price": ask_dollars - 0.02, "quantity": 20}],
        },
        "no": {
            "best_ask": 1.0 - (ask_dollars - 0.02),
            "best_bid": 1.0 - ask_dollars,
            "asks": [],
            "bids": [{"price": 1.0 - ask_dollars, "quantity": 200}],
        },
        "fetched_at": time.time(),
    }


def _place_and_capture(book_ask: float, *, ev_percent=6.0, ev_min=2.0):
    from kalshi_client import KalshiClient

    captured = {}

    async def run():
        client = KalshiClient()
        client.has_trading_credentials = lambda: True
        client.bet_lock = asyncio.Lock()
        client.session = object()
        client.orderbooks[CIN_ML] = _yes_book(book_ask)

        def _capture(**kwargs):
            captured.update(kwargs)
            return None, ["test-stop-before-http"]

        client.auth = MagicMock()
        import kalshi_client as kc

        orig = kc.build_limit_order_payload
        kc.build_limit_order_payload = _capture
        try:
            return await client.place_order(
                ticker=CIN_ML,
                side="yes",
                count=10,
                validate_odds=True,
                expected_price_cents=42,
                skip_duplicate_check=True,
                ev_min=ev_min,
                alert_ev_percent=ev_percent,
            )
        finally:
            kc.build_limit_order_payload = orig

    result = asyncio.run(run())
    return result, captured


def test_place_order_cin_ml_42_to_43_uses_new_limit():
    result, captured = _place_and_capture(0.43, ev_percent=6.0, ev_min=2.0)
    assert captured.get("price_cents") == 43
    assert result.get("error") == "Invalid limit order"


def test_place_order_cin_ml_42_to_44_rejects():
    result, captured = _place_and_capture(0.44, ev_percent=6.0, ev_min=2.0)
    assert captured == {}
    assert result["error"] == "Odds changed"
    assert result["reason"] == "Price got worse by 2¢ (max allowed: 1¢)"
    assert result["expected"] == 42
    assert result["current"] == 44
