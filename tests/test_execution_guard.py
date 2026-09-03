"""Regression fixtures: wrong-side, wrong-line, away invert, stale ticker, limit price.

No live orders. Auto-bet stays OFF. Network is not required.
"""
from __future__ import annotations

from pathlib import Path

from execution_guard import (
    away_inverted_line,
    build_limit_order_payload,
    event_ticker_from_any,
    expected_side_for_alert,
    has_trading_credentials,
    parse_kalshi_ticker,
    prepare_executable_order,
    public_get_headers,
    same_event,
    validate_execution_intent,
)

# DET @ MIN. Home hdp +1.5 → away Tigers are −1.5 (KalshiBB / BookieBeats rule).
DET_MIN_EVENT = "KXMLBGAME-26SEP03DETTMIN"
DET_MINUS_15 = "KXMLBSPREAD-26SEP03DETTMIN-DET1"
MIN_MINUS_15 = "KXMLBSPREAD-26SEP03DETTMIN-MIN1"
STL_LAD_ML_STL = "KXMLBGAME-26SEP03STLLAD-STL"
STL_LAD_TOTAL_7 = "KXMLBTOTAL-26SEP03STLLAD-7"
STALE_OTHER_GAME = "KXMLBSPREAD-26SEP03CHCCIN-DET1"


def _tigers_alert(**overrides):
    row = {
        "ticker": DET_MINUS_15,
        "event_ticker": DET_MIN_EVENT,
        "side": "yes",
        "price_cents": 63,
        "market_type": "Point Spread",
        "pick": "Detroit Tigers",
        "teams": "Detroit Tigers @ Minnesota Twins",
        "line": -1.5,
        "qualifier": "-1.5",
        "take_book": "Kalshi",
        "home_hdp": 1.5,
        "bet_side": "away",
    }
    row.update(overrides)
    return row


def test_auto_bet_switch_stays_off():
    src = (Path(__file__).resolve().parents[1] / "dashboard.py").read_text(encoding="utf-8")
    assert "auto_bet_enabled = False" in src


def test_event_ticker_from_market_or_event():
    assert event_ticker_from_any(DET_MINUS_15) == DET_MIN_EVENT
    assert event_ticker_from_any(DET_MIN_EVENT) == DET_MIN_EVENT
    assert event_ticker_from_any(STL_LAD_TOTAL_7) == "KXMLBGAME-26SEP03STLLAD"
    assert event_ticker_from_any("PLIVE|x|Over|7.5") is None


def test_away_side_inversion_tigers_minus_not_plus():
    """Home hdp +1.5 → away Tigers −1.5. Acting on +1.5 is the wrong line."""
    assert away_inverted_line(1.5, "away") == -1.5
    assert away_inverted_line(1.5, "home") == 1.5
    ok = validate_execution_intent(
        ticker=DET_MINUS_15,
        side="yes",
        price_cents=63,
        market_type="Point Spread",
        pick="Detroit Tigers",
        teams="Detroit Tigers @ Minnesota Twins",
        line=-1.5,
        qualifier="-1.5",
        event_ticker=DET_MIN_EVENT,
        home_hdp=1.5,
        bet_side="away",
    )
    assert ok.ok is True
    assert expected_side_for_alert(
        market_type="Point Spread",
        pick="Detroit Tigers",
        line=-1.5,
        ticker=DET_MINUS_15,
        teams="Detroit Tigers @ Minnesota Twins",
    ) == "yes"

    inverted_wrong = validate_execution_intent(
        ticker=DET_MINUS_15,
        side="yes",
        price_cents=63,
        market_type="Point Spread",
        pick="Detroit Tigers",
        teams="Detroit Tigers @ Minnesota Twins",
        line=1.5,
        qualifier="+1.5",
        event_ticker=DET_MIN_EVENT,
        home_hdp=1.5,
        bet_side="away",
    )
    assert inverted_wrong.ok is False
    assert "away_side_not_inverted" in inverted_wrong.reasons


def test_wrong_side_favorite_yes_on_opponent_ticker():
    """Tigers −1.5 must not buy YES on the Twins ticker."""
    check = validate_execution_intent(
        ticker=MIN_MINUS_15,
        side="yes",
        price_cents=63,
        market_type="Point Spread",
        pick="Detroit Tigers",
        teams="Detroit Tigers @ Minnesota Twins",
        line=-1.5,
        event_ticker=DET_MIN_EVENT,
    )
    assert check.ok is False
    assert "wrong_side" in check.reasons or "side_unresolved" in check.reasons


def test_wrong_side_totals_under_as_yes():
    check = validate_execution_intent(
        ticker=STL_LAD_TOTAL_7,
        side="yes",
        price_cents=48,
        market_type="Total Runs",
        pick="Under",
        teams="St. Louis Cardinals @ Los Angeles Dodgers",
        line=7.5,
        event_ticker="KXMLBGAME-26SEP03STLLAD",
    )
    assert check.ok is False
    assert "wrong_side" in check.reasons


def test_wrong_side_moneyline_yes_on_opponent():
    check = validate_execution_intent(
        ticker=STL_LAD_ML_STL,
        side="yes",
        price_cents=55,
        market_type="Moneyline",
        pick="Los Angeles Dodgers",
        teams="St. Louis Cardinals @ Los Angeles Dodgers",
        event_ticker="KXMLBGAME-26SEP03STLLAD",
    )
    assert check.ok is False
    assert "wrong_side" in check.reasons


def test_wrong_line_spread_and_total():
    spread = validate_execution_intent(
        ticker=DET_MINUS_15,
        side="yes",
        price_cents=63,
        market_type="Point Spread",
        pick="Detroit Tigers",
        teams="Detroit Tigers @ Minnesota Twins",
        line=-2.5,
        event_ticker=DET_MIN_EVENT,
    )
    assert spread.ok is False
    assert "wrong_line" in spread.reasons

    total = validate_execution_intent(
        ticker=STL_LAD_TOTAL_7,
        side="yes",
        price_cents=48,
        market_type="Total Runs",
        pick="Over",
        teams="St. Louis Cardinals @ Los Angeles Dodgers",
        line=8.5,
        event_ticker="KXMLBGAME-26SEP03STLLAD",
    )
    assert total.ok is False
    assert "wrong_line" in total.reasons


def test_stale_or_mismatched_ticker_refuses():
    check = validate_execution_intent(
        ticker=DET_MINUS_15,
        side="yes",
        price_cents=63,
        market_type="Point Spread",
        pick="Detroit Tigers",
        teams="Detroit Tigers @ Minnesota Twins",
        line=-1.5,
        event_ticker=DET_MIN_EVENT,
        rebuilt_ticker=STALE_OTHER_GAME,
    )
    assert check.ok is False
    assert "stale_or_mismatched_ticker" in check.reasons
    assert same_event(DET_MINUS_15, STALE_OTHER_GAME) is False


def test_event_mismatch_refuses():
    check = validate_execution_intent(
        ticker=DET_MINUS_15,
        side="yes",
        price_cents=63,
        market_type="Point Spread",
        pick="Detroit Tigers",
        teams="Detroit Tigers @ Minnesota Twins",
        line=-1.5,
        event_ticker="KXMLBGAME-26SEP03STLLAD",
    )
    assert check.ok is False
    assert "event_mismatch" in check.reasons


def test_missing_fields_refuse_order():
    for kwargs in (
        {"ticker": None},
        {"side": None},
        {"price_cents": None},
        {"price_cents": 0},
        {"price_cents": 100},
        {"pick": ""},
        {"line": None, "qualifier": None, "market_type": "Point Spread"},
    ):
        row = _tigers_alert()
        row.update(kwargs)
        check = prepare_executable_order(row, require_credentials=False)
        assert check.ok is False, kwargs


def test_plive_ticker_never_executable():
    check = prepare_executable_order(
        {
            "ticker": "PLIVE|Detroit Tigers @ Minnesota Twins|Over|11.5",
            "side": "yes",
            "price_cents": 48,
            "market_type": "Total Runs",
            "pick": "Over",
            "line": 11.5,
            "take_book": "PLive",
        },
        require_credentials=False,
    )
    assert check.ok is False
    assert "plive_not_executable" in check.reasons


def test_limit_order_uses_displayed_price_not_market():
    payload, reasons = build_limit_order_payload(
        ticker=DET_MINUS_15,
        side="yes",
        count=10,
        price_cents=63,
        post_only=False,
    )
    assert reasons == []
    assert payload is not None
    assert payload["type"] == "limit"
    assert payload["action"] == "buy"
    assert payload["yes_price"] == 63
    assert "no_price" not in payload
    assert payload.get("post_only") is None
    assert "market" not in payload.get("type", "")

    no_side, no_reasons = build_limit_order_payload(
        ticker=STL_LAD_TOTAL_7,
        side="no",
        count=4,
        price_cents=41,
        post_only=True,
    )
    assert no_reasons == []
    assert no_side["no_price"] == 41
    assert no_side["type"] == "limit"
    assert no_side["post_only"] is True

    missing, miss_reasons = build_limit_order_payload(
        ticker=DET_MINUS_15,
        side="yes",
        count=10,
        price_cents=None,
    )
    assert missing is None
    assert "missing_or_invalid_price" in miss_reasons


def test_credentials_required_only_for_orders_not_public_reads():
    assert has_trading_credentials(None, None) is False
    assert public_get_headers(None, None, {"KALSHI-ACCESS-KEY": "x"}) == {}
    signed = {"KALSHI-ACCESS-KEY": "kid", "KALSHI-ACCESS-SIGNATURE": "sig"}
    assert public_get_headers(object(), "kid-1", signed) == signed
    check = prepare_executable_order(_tigers_alert(), require_credentials=True, has_credentials=False)
    assert check.ok is False
    assert "credentials_required_for_orders" in check.reasons
    ready = prepare_executable_order(_tigers_alert(), require_credentials=True, has_credentials=True)
    assert ready.ok is True
    assert ready.price_cents == 63


def test_good_underdog_is_no_on_favorite_ticker():
    """Twins +1.5 (home underdog) is NO on DET1, not YES on a +1.5 Twins ticker."""
    check = validate_execution_intent(
        ticker=DET_MINUS_15,
        side="no",
        price_cents=37,
        market_type="Point Spread",
        pick="Minnesota Twins",
        teams="Detroit Tigers @ Minnesota Twins",
        line=1.5,
        qualifier="+1.5",
        event_ticker=DET_MIN_EVENT,
        home_hdp=1.5,
        bet_side="home",
    )
    assert check.ok is True
    assert check.side == "no"


def test_parse_market_vs_event():
    mkt = parse_kalshi_ticker(DET_MINUS_15)
    ev = parse_kalshi_ticker(DET_MIN_EVENT)
    assert mkt is not None and ev is not None
    assert mkt.is_market is True
    assert ev.is_market is False
    assert mkt.team_code == "DET"
    assert mkt.line_int == 1
    assert mkt.family == "spread"
    assert ev.family == "moneyline"


def test_dashboard_and_client_wire_the_guard():
    dash = (Path(__file__).resolve().parents[1] / "dashboard.py").read_text(encoding="utf-8")
    client = (Path(__file__).resolve().parents[1] / "kalshi_client.py").read_text(encoding="utf-8")
    js = (Path(__file__).resolve().parents[1] / "static" / "script.js").read_text(encoding="utf-8")
    assert "from execution_guard import event_ticker_from_any, prepare_executable_order" in dash
    assert "prepare_executable_order" in dash
    assert "Execution identity failed" in dash
    assert "expected_price_cents or 50" not in dash
    assert "attempting fallback matching for manual bet" not in dash
    assert "public_ok=True" in client
    assert "Kalshi credentials required to place orders" in client
    assert "function canPlaceKalshiBet" in js
    assert "canPlaceKalshiBet(alert)" in js


def test_public_get_headers_on_client_without_key():
    import asyncio

    from kalshi_client import KalshiClient

    client = KalshiClient()
    client.auth.priv = None
    client.auth.kid = None
    assert client.has_trading_credentials() is False
    assert client._headers_for("GET", "/trade-api/v2/markets/KXTEST", public_ok=True) == {}
    refused = asyncio.run(
        client.place_order("KXMLBGAME-26SEP03STLLAD-STL", "yes", 1, expected_price_cents=55)
    )
    assert refused.get("error") == "Kalshi credentials required to place orders"


def test_build_market_ticker_does_not_double_suffix_from_href():
    """Odds-API href is often the market ticker. Rebuilding must not append the line twice."""
    from kalshi_client import KalshiClient

    client = KalshiClient()
    built = client.build_market_ticker(
        "KXNBATOTAL-26JAN11ATLGSW-143",
        "Total Points",
        143.5,
        "Over",
        "Atlanta Hawks @ Golden State Warriors",
    )
    assert built == "KXNBATOTAL-26JAN11ATLGSW-143"
    assert built.count("-143") == 1
