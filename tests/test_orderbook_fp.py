"""Kalshi orderbook_fp (fixed-point) normalization.

Live Trade API GET /markets/{ticker}/orderbook now returns only ``orderbook_fp``
with ``yes_dollars`` / ``no_dollars`` string pairs. Legacy ``orderbook`` integer
cents must still parse. Auto-bet stays OFF.
"""
from __future__ import annotations

from pathlib import Path

from kalshi_client import (
    KalshiClient,
    build_normalized_orderbook,
    extract_raw_orderbook_sides,
    normalize_kalshi_orderbook,
    parse_orderbook_levels,
    parse_orderbook_price_dollars,
)

# Official example from https://docs.kalshi.com/getting_started/orderbook_responses
DOCS_ORDERBOOK_FP = {
    "orderbook_fp": {
        "yes_dollars": [
            ["0.0100", "200.00"],
            ["0.1500", "100.00"],
            ["0.2000", "50.00"],
            ["0.2500", "20.00"],
            ["0.3000", "11.00"],
            ["0.3100", "10.00"],
            ["0.3200", "10.00"],
            ["0.3300", "11.00"],
            ["0.3400", "9.00"],
            ["0.3500", "11.00"],
            ["0.4100", "10.00"],
            ["0.4200", "13.00"],
        ],
        "no_dollars": [
            ["0.0100", "100.00"],
            ["0.1600", "3.00"],
            ["0.2500", "50.00"],
            ["0.2800", "19.00"],
            ["0.3600", "5.00"],
            ["0.3700", "50.00"],
            ["0.3800", "300.00"],
            ["0.4400", "29.00"],
            ["0.4500", "20.00"],
            ["0.5600", "17.00"],
        ],
    }
}

# Live GET 2026-09-05 for KXNCAAFTOTAL-26SEP05BALLOSU-59 (keys: ['orderbook_fp']).
LIVE_CFB_ORDERBOOK_FP = {
    "orderbook_fp": {
        "yes_dollars": [["0.3800", "182.00"], ["0.3900", "102.00"]],
        "no_dollars": [["0.4300", "40.00"], ["0.5000", "1.00"]],
    }
}

LEGACY_CENTS = {
    "orderbook": {
        "yes": [[41, 10], [42, 13]],
        "no": [[45, 20], [56, 17]],
    }
}

WS_SNAPSHOT_MSG = {
    "type": "orderbook_snapshot",
    "sid": 2,
    "seq": 2,
    "msg": {
        "market_ticker": "KXNCAAFTOTAL-26SEP05BALLOSU-59",
        "market_id": "9b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a1",
        "yes_dollars_fp": [["0.0800", "300.00"], ["0.2200", "333.00"]],
        "no_dollars_fp": [["0.5400", "20.00"], ["0.5600", "146.00"]],
    },
}


def test_docs_orderbook_fp_normalizes_to_place_order_shape():
    book = normalize_kalshi_orderbook(DOCS_ORDERBOOK_FP)
    assert book is not None
    assert book["yes"]["best_bid"] == 0.42
    assert book["no"]["best_bid"] == 0.56
    assert abs(book["yes"]["best_ask"] - 0.44) < 1e-9
    assert abs(book["no"]["best_ask"] - 0.58) < 1e-9
    assert book["yes"]["best_ask_size"] == 17.0
    assert abs(book["yes"]["asks"][0]["price"] - 0.44) < 1e-9
    assert book["yes"]["asks"][0]["quantity"] == 17.0
    assert book["yes"]["bids"][-1]["quantity"] == 13.0


def test_live_cfb_orderbook_fp_is_not_empty():
    """The live bug: code only read ``orderbook`` and returned None."""
    yes, no = extract_raw_orderbook_sides(LIVE_CFB_ORDERBOOK_FP)
    assert yes[-1] == ["0.3900", "102.00"]
    assert no[-1] == ["0.5000", "1.00"]
    book = normalize_kalshi_orderbook(LIVE_CFB_ORDERBOOK_FP)
    assert book is not None
    assert book["yes"]["best_bid"] == 0.39
    assert book["no"]["best_bid"] == 0.50
    assert abs(book["yes"]["best_ask"] - 0.50) < 1e-9
    assert book["yes"]["best_ask_size"] == 1.0


def test_legacy_orderbook_cents_still_parse():
    book = normalize_kalshi_orderbook(LEGACY_CENTS)
    assert book["yes"]["best_bid"] == 0.42
    assert book["no"]["best_bid"] == 0.56
    assert abs(book["yes"]["best_ask"] - 0.44) < 1e-9


def test_unknown_envelope_returns_none():
    assert normalize_kalshi_orderbook({"error": "nope"}) is None
    assert extract_raw_orderbook_sides({"error": "nope"}) == (None, None)


def test_empty_orderbook_fp_returns_empty_book_not_none():
    book = normalize_kalshi_orderbook({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}})
    assert book is not None
    assert book["yes"]["bids"] == []
    assert book["yes"]["best_ask"] is None
    assert book["yes"]["best_ask_size"] == 0


def test_ws_snapshot_yes_dollars_fp():
    book = normalize_kalshi_orderbook(WS_SNAPSHOT_MSG)
    assert book["yes"]["best_bid"] == 0.22
    assert book["no"]["best_bid"] == 0.56
    assert abs(book["yes"]["best_ask"] - 0.44) < 1e-9


def test_ws_snapshot_via_client_apply():
    client = KalshiClient()
    updated = client._apply_orderbook_update(WS_SNAPSHOT_MSG)
    assert updated["yes"]["best_bid"] == 0.22
    assert updated["yes"]["asks"]
    assert "best_ask_size" in updated["yes"]


def test_ws_delta_fp_adds_and_removes_level():
    client = KalshiClient()
    ticker = "KXNCAAFTOTAL-26SEP05BALLOSU-59"
    snap = client._apply_orderbook_update(WS_SNAPSHOT_MSG)
    client.orderbooks[ticker] = snap

    add = client._apply_orderbook_update({
        "type": "orderbook_delta",
        "msg": {
            "market_ticker": ticker,
            "price_dollars": "0.2300",
            "delta_fp": "10.00",
            "side": "yes",
        },
    })
    assert add["yes"]["best_bid"] == 0.23
    assert any(abs(b["quantity"] - 10.0) < 1e-9 and abs(b["price"] - 0.23) < 1e-9 for b in add["yes"]["bids"])
    client.orderbooks[ticker] = add

    remove = client._apply_orderbook_update({
        "type": "orderbook_delta",
        "msg": {
            "market_ticker": ticker,
            "price_dollars": "0.2300",
            "delta_fp": "-10.00",
            "side": "yes",
        },
    })
    assert remove["yes"]["best_bid"] == 0.22
    assert all(abs(b["price"] - 0.23) > 1e-4 for b in remove["yes"]["bids"])


def test_legacy_ws_data_orderbook_still_works():
    client = KalshiClient()
    updated = client._apply_orderbook_update({
        "type": "orderbook_snapshot",
        "data": {
            "market_ticker": "KXTEST-1",
            "orderbook": {"yes": [[40, 5]], "no": [[55, 8]]},
        },
    })
    assert updated["yes"]["best_bid"] == 0.40
    assert abs(updated["yes"]["best_ask"] - 0.45) < 1e-9


def test_price_parser_string_dollars_vs_int_cents():
    assert parse_orderbook_price_dollars("0.4200") == 0.42
    assert parse_orderbook_price_dollars(42) == 0.42
    levels, total = parse_orderbook_levels([["0.3900", "102.00"], [38, 182]])
    assert [round(x["price"], 4) for x in levels] == [0.39, 0.38]
    assert total == 284.0


def test_normalized_book_has_place_order_keys():
    book = build_normalized_orderbook(
        [{"price": 0.39, "quantity": 102.0}],
        [{"price": 0.50, "quantity": 1.0}],
    )
    for side in ("yes", "no"):
        for key in ("best_bid", "best_ask", "best_ask_size", "bids", "asks", "total_liquidity"):
            assert key in book[side]


def test_dashboard_records_terminal_auto_bet_outcome():
    src = (Path(__file__).resolve().parents[1] / "dashboard.py").read_text(encoding="utf-8")
    assert "auto_bet_outcome" in src
    assert "Task ended without terminal outcome" in src
    assert "except asyncio.CancelledError:" in src
    assert "TERMINAL skip" in src
    assert "auto_bet_enabled = False" in src
