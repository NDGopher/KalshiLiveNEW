"""Auto-bet product lock: Royals-shape allowlist. Switch stays OFF."""
from __future__ import annotations

from pathlib import Path

from ev_calculator import autobet_product_shape, evaluate_sharp_panel_ev
from tests.test_sharp_panel_filter import _book


def _royals_board():
    return [
        _book("Betfair Exchange", 134, -154),
        _book("DraftKings", 105, -125),
        _book("FanDuel", 116, -136),
        _book("Bet365", 100, -120),
        _book("Caesars", 110, -130),
        _book("PLive", 118, -138),
        _book("Poly", -455, 350),
    ]


def test_auto_bet_switch_stays_off():
    src = (Path(__file__).resolve().parents[1] / "dashboard.py").read_text(encoding="utf-8")
    assert "auto_bet_enabled = False" in src
    assert "only Royals-like Kalshi-best" in src
    assert "per_event_max_bet = 404.0" in src


def test_royals_shape_allows_when_switch_later_on():
    books = _royals_board()
    out = evaluate_sharp_panel_ev(books, 163, min_sharp_books=3, take_book="Kalshi")
    assert out["plus_alert"] is True
    assert "PLive" not in out["fair_names"]
    assert "Poly" not in out["fair_names"]
    assert 4.0 <= out["ev_percent"] <= 12.0
    shape = autobet_product_shape(
        books,
        163,
        take_book="Kalshi",
        ev_percent=out["ev_percent"],
        plus_alert=out["plus_alert"],
    )
    assert shape["allow"] is True
    assert shape["same_sign_recs"] >= 5
    assert out["autobet_allow"] is True


def test_plive_118_is_confirm_not_fair():
    books = _royals_board()
    out = evaluate_sharp_panel_ev(books, 163, min_sharp_books=3, take_book="Kalshi")
    assert "PLive" not in out["fair_names"]
    shape = autobet_product_shape(
        books, 163, take_book="Kalshi", ev_percent=out["ev_percent"], plus_alert=True
    )
    assert "plive_off_pack" not in shape["reasons"]


def test_brewers_plus_only_teens_blocked():
    books = [
        {"name": "A", "american": 228, "decimal_pick": 3.28, "decimal_opp": 0},
        {"name": "B", "american": 234, "decimal_pick": 3.34, "decimal_opp": 0},
        {"name": "C", "american": 240, "decimal_pick": 3.40, "decimal_opp": 0},
        {"name": "D", "american": 220, "decimal_pick": 3.20, "decimal_opp": 0},
        {"name": "E", "american": 225, "decimal_pick": 3.25, "decimal_opp": 0},
    ]
    shape = autobet_product_shape(
        books, 300, take_book="Kalshi", ev_percent=14.0, plus_alert=True
    )
    assert shape["allow"] is False
    assert "ev_teens" in shape["reasons"] or "sister_required" in shape["reasons"]


def test_away_sign_wrong_blocked():
    books = _royals_board()
    shape = autobet_product_shape(
        books,
        163,
        take_book="Kalshi",
        ev_percent=9.59,
        plus_alert=True,
        painted_side_hdp=1.5,
        actual_side_hdp=-1.5,
    )
    assert shape["allow"] is False
    assert "spread_label_mismatch" in shape["reasons"]


def test_plive_take_never_autobets():
    books = _royals_board()
    shape = autobet_product_shape(
        books, 163, take_book="PLive", ev_percent=9.59, plus_alert=True
    )
    assert shape["allow"] is False
    assert "take_not_kalshi" in shape["reasons"]


def test_take_not_best_blocked():
    books = [
        _book("Bet365", 475, -650),
        _book("NoVig", 525, -750),
        _book("Caesars", 333, -430),
        _book("DraftKings", 307, -390),
        _book("FanDuel", 310, -400),
        _book("PLive", 369, -480),
    ]
    shape = autobet_product_shape(
        books, 369, take_book="Kalshi", ev_percent=1.18, plus_alert=True
    )
    assert shape["allow"] is False
    assert "take_not_best" in shape["reasons"]
