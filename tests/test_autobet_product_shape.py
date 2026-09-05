"""Auto-bet product lock: 3 Sharps Live. Switch stays OFF.

Stephen 2026-09-05: "We definitely do not need 5 same sign sharps and we
don’t need Kalshi best. If it passes our 3 sharps it should work. EV under
20 is fine."
"""
from __future__ import annotations

from pathlib import Path

from ev_calculator import (
    AUTOBET_MAX_EV_PCT,
    AUTOBET_MIN_SAME_SIGN_RECS,
    autobet_product_shape,
    evaluate_sharp_panel_ev,
)
from tests.test_sharp_panel_filter import _book

REPO = Path(__file__).resolve().parents[1]


def _three_sharp_board(*, kalshi_not_best: bool = False):
    """Three same-sign two-way recs inside the 10c junk screen. Poly/PLive never count."""
    recs = [
        _book("[REDACTED]", 134 if not kalshi_not_best else 175, -154 if not kalshi_not_best else -210),
        _book("FanDuel", 116, -136),
        _book("Caesars", 110, -130),
    ]
    return recs + [
        _book("PLive", 118, -138),
        _book("Poly", -455, 350),
    ]


def _cfb_spread_board():
    """CFB favorite spread: 3 two-way recs, Kalshi not strictly best."""
    return [
        _book("DraftKings", -108, -112),
        _book("FanDuel", -118, -102),
        _book("Bet365", -122, 102),
        _book("PLive", -105, -115),
        _book("Poly", -455, 350),
    ]


def test_product_constants_are_three_sharps_and_ev20():
    assert AUTOBET_MIN_SAME_SIGN_RECS == 3
    assert AUTOBET_MAX_EV_PCT == 20.0
    src = (REPO / "ev_calculator.py").read_text(encoding="utf-8")
    fn = src.split("def autobet_product_shape", 1)[1].split("\ndef ", 1)[0]
    assert "take_not_best" not in fn
    assert "AUTOBET_MIN_SAME_SIGN_RECS" in fn


def test_auto_bet_switch_stays_off():
    src = (REPO / "dashboard.py").read_text(encoding="utf-8")
    assert "auto_bet_enabled = False" in src
    assert "≥3 same-LINE comparison recs" in src
    assert "per_event_max_bet = 404.0" in src
    js = (REPO / "static" / "script.js").read_text(encoding="utf-8")
    assert "autobet-badge" in js
    assert "autobet_reasons" in js


def test_three_same_sign_allows_when_switch_later_on():
    books = _three_sharp_board()
    out = evaluate_sharp_panel_ev(books, 163, min_sharp_books=3, take_book="Kalshi")
    assert out["plus_alert"] is True
    assert "PLive" not in out["fair_names"]
    assert "Poly" not in out["fair_names"]
    assert 1.0 <= out["ev_percent"] <= 20.0
    shape = autobet_product_shape(
        books,
        163,
        take_book="Kalshi",
        ev_percent=out["ev_percent"],
        plus_alert=out["plus_alert"],
    )
    assert shape["allow"] is True
    assert shape["same_sign_recs"] >= 3
    assert shape["sisters"] >= 3
    assert "take_not_best" not in shape["reasons"]
    assert out["autobet_allow"] is True


def test_kalshi_not_best_still_allows():
    books = _three_sharp_board(kalshi_not_best=True)
    shape = autobet_product_shape(
        books, 110, take_book="Kalshi", ev_percent=6.5, plus_alert=True
    )
    assert shape["same_sign_recs"] >= 3
    assert shape["allow"] is True
    assert "take_not_best" not in shape["reasons"]


def test_fifteen_pct_ev_with_three_sharps_allows():
    books = _three_sharp_board()
    shape = autobet_product_shape(
        books, 163, take_book="Kalshi", ev_percent=15.0, plus_alert=True
    )
    assert shape["allow"] is True
    assert "ev_teens" not in shape["reasons"]
    assert "take_not_best" not in shape["reasons"]


def test_ev_over_20_denied_as_ev_teens():
    books = _three_sharp_board()
    shape = autobet_product_shape(
        books, 163, take_book="Kalshi", ev_percent=20.01, plus_alert=True
    )
    assert shape["allow"] is False
    assert "ev_teens" in shape["reasons"]


def test_two_same_sign_recs_blocked():
    books = [
        _book("DraftKings", 105, -125),
        _book("FanDuel", 116, -136),
        _book("PLive", 118, -138),
    ]
    shape = autobet_product_shape(
        books, 163, take_book="Kalshi", ev_percent=8.0, plus_alert=True
    )
    assert shape["allow"] is False
    assert "same_sign_recs" in shape["reasons"]


def test_cfb_spread_three_sharps_allows():
    books = _cfb_spread_board()
    out = evaluate_sharp_panel_ev(
        books,
        -110,
        min_sharp_books=3,
        take_book="Kalshi",
        painted_side_hdp=-7.5,
        kalshi_side_hdp=-7.5,
        rec_side_hdp=-7.5,
    )
    shape = autobet_product_shape(
        books,
        -110,
        take_book="Kalshi",
        ev_percent=4.0 if out["ev_percent"] <= 0 else out["ev_percent"],
        plus_alert=True,
        painted_side_hdp=-7.5,
        actual_side_hdp=-7.5,
    )
    assert shape["same_sign_recs"] >= 3
    assert shape["sisters"] >= 3
    assert shape["allow"] is True
    assert "take_not_best" not in shape["reasons"]
    assert "spread_label_mismatch" not in shape["reasons"]


def test_plive_118_is_confirm_not_fair():
    books = _three_sharp_board()
    out = evaluate_sharp_panel_ev(books, 163, min_sharp_books=3, take_book="Kalshi")
    assert "PLive" not in out["fair_names"]
    shape = autobet_product_shape(
        books, 163, take_book="Kalshi", ev_percent=out["ev_percent"], plus_alert=True
    )
    assert "plive_off_pack" not in shape["reasons"]
    assert shape["allow"] is True


def test_plus110_take_vs_minus110_pack_allows():
    """Take +110 vs −107/−110/−112 is a play. Odds-sign must not block allow."""
    books = [
        _book("DraftKings", -107, -113),
        _book("FanDuel", -110, -110),
        _book("Caesars", -112, -108),
        _book("PLive", -109, -111),
        _book("Poly", -455, 350),
    ]
    shape = autobet_product_shape(
        books, 110, take_book="Kalshi", ev_percent=5.5, plus_alert=True
    )
    assert shape["same_sign_recs"] >= 3
    assert shape["allow"] is True
    assert "same_sign_recs" not in shape["reasons"]
    assert "sister_required" not in shape["reasons"]
    out = evaluate_sharp_panel_ev(books, 110, min_sharp_books=3, take_book="Kalshi")
    assert out["autobet_allow"] is True
    assert "same_sign_recs" not in out["autobet_reasons"]


def test_plus_only_three_books_on_line_not_killed_by_sister():
    """3 comparison books on the same LINE are enough. Missing sisters do not deny."""
    books = [
        {"name": "A", "american": 228, "decimal_pick": 3.28, "decimal_opp": 0},
        {"name": "B", "american": 234, "decimal_pick": 3.34, "decimal_opp": 0},
        {"name": "C", "american": 240, "decimal_pick": 3.40, "decimal_opp": 0},
    ]
    shape = autobet_product_shape(
        books, 245, take_book="Kalshi", ev_percent=4.0, plus_alert=True
    )
    assert shape["same_sign_recs"] >= 3
    assert shape["allow"] is True
    assert "sister_required" not in shape["reasons"]


def test_book_line_mismatch_does_not_count_as_same_line_rec():
    """Neighbor / opposite hdp is fail-closed. Same-sign odds cannot rescue it."""
    books = [
        {**_book("DraftKings", -110, -110), "hdp": 1.5},
        {**_book("FanDuel", -108, -112), "hdp": 1.5},
        {**_book("Caesars", -112, -108), "hdp": 1.5},
    ]
    shape = autobet_product_shape(
        books,
        110,
        take_book="Kalshi",
        ev_percent=8.0,
        plus_alert=True,
        painted_side_hdp=-1.5,
        actual_side_hdp=-1.5,
    )
    assert shape["allow"] is False
    assert shape["same_sign_recs"] < 3
    assert "same_sign_recs" in shape["reasons"] or "spread_label_mismatch" in shape["reasons"]


def test_away_sign_wrong_blocked():
    books = _three_sharp_board()
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
    books = _three_sharp_board()
    shape = autobet_product_shape(
        books, 163, take_book="PLive", ev_percent=9.59, plus_alert=True
    )
    assert shape["allow"] is False
    assert "take_not_kalshi" in shape["reasons"]


def test_take_not_best_is_gone_even_when_books_beat_kalshi():
    """Former Royals-museum take_not_best board must now allow."""
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
    assert "take_not_best" not in shape["reasons"]
    assert shape["same_sign_recs"] >= 3
    assert shape["allow"] is True


def test_evalert_exposes_autobet_reasons():
    from ev_alert import EvAlert

    alert = EvAlert(
        {
            "teams": "ODU vs App State",
            "pick": "ODU",
            "ev_percent": 6.5,
            "autobet_allow": False,
            "autobet_reasons": ["same_sign_recs"],
            "strict_pass": False,
        }
    )
    assert alert.autobet_allow is False
    assert alert.autobet_reasons == ["same_sign_recs"]
    dumped = alert.to_dict()
    assert dumped["autobet_reasons"] == ["same_sign_recs"]
    assert dumped["autobet_allow"] is False


def test_monitor_card_reasons_mark_display_only():
    from odds_ev_monitor import _autobet_card_reasons

    assert _autobet_card_reasons(["same_sign_recs"], display_only=True) == [
        "same_sign_recs",
        "display_only",
    ]
    assert _autobet_card_reasons([], take_book="PLive") == ["take_not_kalshi"]
    assert _autobet_card_reasons([], ticker="KALSHI|ODU|App State|-7.5") == ["paper_ticker"]
