"""Kalshi ticker suffix = ceil(|line|), locked to live 2026-09-05 titles.

Public Trade API (no credentials):

* KXNCAAFTOTAL-26SEP05DUQAFA-39  title "Over 38.5 points scored"  floor=38.5
* KXNCAAFTOTAL-26SEP05DUQAFA-38  404 — does not exist
* KXNCAAFTOTAL-26SEP05BALLOSU-60 title "Over 59.5 points scored"  floor=59.5
* KXNCAAFTOTAL-26SEP05BALLOSU-59 title "Over 58.5 points scored"  floor=58.5
* KXNCAAFSPREAD-26SEP05DUQAFA-AFA10 title "Air Force wins by over 9.5" floor=9.5
* KXMLBSPREAD-…-CHC2 title "Chicago C wins by over 1.5 runs?" floor=1.5
* KXEPLTOTAL-…-3 title "Will over 2.5 goals be scored?" floor=2.5

int(abs(line)) is the neighboring strike. Auto-bet stays OFF. No live orders.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from execution_guard import (
    expected_side_for_alert,
    href_ticker_agrees_with_alert,
    kalshi_line_int,
    market_floor_strike_matches_alert,
    prepare_executable_order,
    ticker_line_matches_alert,
    validate_execution_intent,
)
from kalshi_client import KalshiClient

# Live-shaped CFB fixtures (event + market tickers from Kalshi 2026-09-05).
DUQ_EVENT = "KXNCAAFGAME-26SEP05DUQAFA"
DUQ_TOTAL_38 = "KXNCAAFTOTAL-26SEP05DUQAFA-38"
DUQ_TOTAL_39 = "KXNCAAFTOTAL-26SEP05DUQAFA-39"
BALL_EVENT = "KXNCAAFGAME-26SEP05BALLOSU"
BALL_TOTAL_59 = "KXNCAAFTOTAL-26SEP05BALLOSU-59"
BALL_TOTAL_60 = "KXNCAAFTOTAL-26SEP05BALLOSU-60"
AFA_SPREAD_10 = "KXNCAAFSPREAD-26SEP05DUQAFA-AFA10"
AFA_SPREAD_9 = "KXNCAAFSPREAD-26SEP05DUQAFA-AFA9"

# Frozen live mapping: sportsbook line → (ticker_suffix, floor_strike, title fragment)
LIVE_TOTAL_PAIRS = (
    (38.5, 39, 38.5, "Over 38.5"),
    (59.5, 60, 59.5, "Over 59.5"),
    (58.5, 59, 58.5, "Over 58.5"),
    (0.5, 1, 0.5, "over 0.5"),
    (2.5, 3, 2.5, "over 2.5"),
    (7.5, 8, 7.5, "Over 7.5"),
    (1.5, 2, 1.5, "over 1.5"),
    (9.5, 10, 9.5, "over 9.5"),
    (143.5, 144, 143.5, "Over 143.5"),
)


def test_ceil_suffix_matches_live_kalshi_titles():
    for line, suffix, floor, _title in LIVE_TOTAL_PAIRS:
        assert kalshi_line_int(line) == suffix
        assert kalshi_line_int(-line) == suffix
        assert market_floor_strike_matches_alert({"floor_strike": floor}, line) is True
        if abs(line - 59.5) < 1e-9:
            assert market_floor_strike_matches_alert({"floor_strike": 58.5}, line) is False


def test_floor_int_abs_is_the_neighbor_not_the_contract():
    """The previous int(abs(line)) rule mapped 59.5 → 59 (Over 58.5)."""
    assert int(abs(38.5)) == 38
    assert int(abs(59.5)) == 59
    assert kalshi_line_int(38.5) == 39
    assert kalshi_line_int(59.5) == 60
    assert kalshi_line_int(38.5) != int(abs(38.5))
    assert kalshi_line_int(59.5) != int(abs(59.5))


def test_build_cfb_totals_from_event_ticker():
    client = KalshiClient()
    assert client.build_market_ticker(DUQ_EVENT, "Total Points", 38.5, "Over") == DUQ_TOTAL_39
    assert client.build_market_ticker(DUQ_EVENT, "Total Points", 38.5, "Under") == DUQ_TOTAL_39
    assert client.build_market_ticker(BALL_EVENT, "Total Points", 59.5, "Under") == BALL_TOTAL_60
    assert client.build_market_ticker(BALL_EVENT, "Total Points", 59.5, "Over") == BALL_TOTAL_60
    # Href already a market ticker: coerce to event, rebuild once.
    assert client.build_market_ticker(DUQ_TOTAL_39, "Total Points", 38.5, "Over") == DUQ_TOTAL_39
    assert client.build_market_ticker(BALL_TOTAL_60, "Total Points", 59.5, "Under") == BALL_TOTAL_60


def test_href_trust_only_when_ceil_agrees():
    assert href_ticker_agrees_with_alert(DUQ_TOTAL_39, 38.5) is True
    assert href_ticker_agrees_with_alert(DUQ_TOTAL_38, 38.5) is False
    assert href_ticker_agrees_with_alert(BALL_TOTAL_60, 59.5) is True
    assert href_ticker_agrees_with_alert(BALL_TOTAL_59, 59.5) is False
    assert href_ticker_agrees_with_alert(DUQ_EVENT, 38.5) is False  # event, not market


def test_over_yes_under_no_on_same_total_ticker():
    assert expected_side_for_alert(
        market_type="Total Points", pick="Over", line=38.5, ticker=DUQ_TOTAL_39,
    ) == "yes"
    assert expected_side_for_alert(
        market_type="Total Points", pick="Under", line=38.5, ticker=DUQ_TOTAL_39,
    ) == "no"
    over_no = validate_execution_intent(
        ticker=DUQ_TOTAL_39,
        side="no",
        price_cents=41,
        market_type="Total Points",
        pick="Over",
        teams="Duquesne @ Air Force",
        line=38.5,
        event_ticker=DUQ_EVENT,
    )
    under_yes = validate_execution_intent(
        ticker=BALL_TOTAL_60,
        side="yes",
        price_cents=41,
        market_type="Total Points",
        pick="Under",
        teams="Ball State @ Ohio State",
        line=59.5,
        event_ticker=BALL_EVENT,
    )
    assert over_no.ok is False and "wrong_side" in over_no.reasons
    assert under_yes.ok is False and "wrong_side" in under_yes.reasons


def test_cfb_spread_ceil_9_5_is_10():
    """Live: AFA10 = 'Air Force wins by over 9.5'. Floor suffix AFA9 is a neighbor."""
    client = KalshiClient()
    built = client.build_market_ticker(
        DUQ_EVENT, "Point Spread", -9.5, "Air Force", "Duquesne @ Air Force",
    )
    assert built is not None
    assert built.endswith("10"), built
    assert kalshi_line_int(-9.5) == 10
    assert ticker_line_matches_alert(AFA_SPREAD_10, -9.5) is True
    assert ticker_line_matches_alert(AFA_SPREAD_9, -9.5) is False
    wrong = validate_execution_intent(
        ticker=AFA_SPREAD_9,
        side="yes",
        price_cents=63,
        market_type="Point Spread",
        pick="Air Force",
        teams="Duquesne @ Air Force",
        line=-9.5,
        event_ticker=DUQ_EVENT,
    )
    assert wrong.ok is False
    assert "wrong_line" in wrong.reasons


def test_find_submarket_rejects_neighbor_floor_strike():
    """If GET returns the neighbor contract, do not treat existence as a match."""
    client = KalshiClient()

    async def _fake_get(ticker):
        if ticker == BALL_TOTAL_59:
            return {
                "ticker": BALL_TOTAL_59,
                "title": "Over 58.5 points scored",
                "floor_strike": 58.5,
            }
        if ticker == BALL_TOTAL_60:
            return {
                "ticker": BALL_TOTAL_60,
                "title": "Over 59.5 points scored",
                "floor_strike": 59.5,
            }
        return None

    client.get_market_by_ticker = _fake_get

    neighbor = asyncio.run(client.find_submarket(BALL_EVENT, "Total Points", 59.5, "Under"))
    # Rebuild is now …-60; fake 60 is the real strike.
    assert neighbor is not None
    assert neighbor["ticker"] == BALL_TOTAL_60
    assert neighbor["floor_strike"] == 59.5

    async def _only_neighbor(ticker):
        if ticker.endswith("-59"):
            return {"ticker": ticker, "floor_strike": 58.5, "title": "Over 58.5 points scored"}
        return None

    client.get_market_by_ticker = _only_neighbor
    # Force the old floor build by feeding a ticker that exists with wrong strike.
    # find_submarket builds …-60; fake returns None → no match (fail closed).
    missing = asyncio.run(client.find_submarket(BALL_EVENT, "Total Points", 59.5, "Under"))
    assert missing is None


def test_prepare_order_uses_alert_floor_strike():
    row = {
        "ticker": DUQ_TOTAL_39,
        "event_ticker": DUQ_EVENT,
        "side": "yes",
        "price_cents": 48,
        "market_type": "Total Points",
        "pick": "Over",
        "teams": "Duquesne @ Air Force",
        "line": 38.5,
        "qualifier": "38.5",
        "take_book": "Kalshi",
        "floor_strike": 38.5,
    }
    assert prepare_executable_order(row, require_credentials=False).ok is True
    row["floor_strike"] = 37.5
    bad = prepare_executable_order(row, require_credentials=False)
    assert bad.ok is False
    assert "wrong_line" in bad.reasons


def test_auto_bet_default_off_and_autobet_allow_terminal_skip():
    dash = (Path(__file__).resolve().parents[1] / "dashboard.py").read_text(encoding="utf-8")
    assert "auto_bet_enabled = False" in dash
    assert '_terminal_skip(' in dash
    assert '"autobet_allow=False"' in dash
    assert "Product-shape autobet_allow is False" in dash
    assert "filter auto-bet disabled" in dash
    assert "PLive take does not auto-bet on Kalshi" in dash
    src_guard = (Path(__file__).resolve().parents[1] / "execution_guard.py").read_text(encoding="utf-8")
    assert "ceil(|line|)" in src_guard or "ceil(abs" in src_guard
    assert "int(abs(float(line)))" not in src_guard
