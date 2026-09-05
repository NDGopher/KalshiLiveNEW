"""Auto-bet product lock: 3 Sharps Live. Switch stays OFF.

Stephen 2026-09-05: same_sign is the LINE, not American odds. Take +110
while the pack is −107/−110/−112 is +EV and must allow. 3 comparison
books on that line are enough. Kalshi need not be best. EV ≤20 is fine.
sister_required / display_only must not keep allow=false when EV and
line consensus already pass. PLive never auto.
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
    assert "≥3 same-line comparison books" in src
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


def test_plus_only_missing_sisters_does_not_block_when_ev_and_line_consensus():
    """3 same-line recs + EV 2–20: sister_required must not keep allow=false."""
    books = [
        {"name": "A", "american": 228, "decimal_pick": 3.28, "decimal_opp": 0},
        {"name": "B", "american": 234, "decimal_pick": 3.34, "decimal_opp": 0},
        {"name": "C", "american": 240, "decimal_pick": 3.40, "decimal_opp": 0},
    ]
    shape = autobet_product_shape(
        books, 300, take_book="Kalshi", ev_percent=15.0, plus_alert=True
    )
    assert shape["same_sign_recs"] >= 3
    assert shape["allow"] is True
    assert "sister_required" not in shape["reasons"]


def test_plus110_vs_minus110_pack_allows():
    """Take +110 vs pack −107/−110/−112 is juice, not an odds-sign reject."""
    books = [
        _book("FanDuel", -107, -113),
        _book("DraftKings", -110, -110),
        _book("Caesars", -112, -108),
        _book("PLive", -105, -115),
        _book("Poly", -455, 350),
    ]
    shape = autobet_product_shape(
        books, 110, take_book="Kalshi", ev_percent=5.0, plus_alert=True
    )
    assert shape["same_sign_recs"] >= 3
    assert shape["sisters"] >= 3
    assert shape["allow"] is True
    assert "same_sign_recs" not in shape["reasons"]
    assert "sister_required" not in shape["reasons"]
    out = evaluate_sharp_panel_ev(books, 110, min_sharp_books=3, take_book="Kalshi")
    assert out["plus_alert"] is True
    assert 2.0 <= out["ev_percent"] <= 20.0
    assert out["autobet_allow"] is True
    assert "same_sign_recs" not in out["autobet_reasons"]


def test_plus125_vs_minus120_pack_odds_sign_does_not_kill_allow():
    """Sided plus-take vs minus pack is still the same line. Allow must pass."""
    books = [
        _book("FanDuel", -115, -105),
        _book("DraftKings", -120, 100),
        _book("Caesars", -125, 105),
    ]
    shape = autobet_product_shape(
        books, 125, take_book="Kalshi", ev_percent=6.5, plus_alert=True
    )
    assert shape["same_sign_recs"] >= 3
    assert shape["allow"] is True
    assert "same_sign_recs" not in shape["reasons"]
    # POWER/paint may still drop far sided flips; allow must not follow.
    out = evaluate_sharp_panel_ev(books, 125, min_sharp_books=3, take_book="Kalshi")
    assert out["autobet_allow"] is True
    assert "same_sign_recs" not in out["autobet_reasons"]


def test_far_implied_sign_flip_still_blocked():
    """NV −154 vs take +186 is way-off-market junk, not juice."""
    books = [
        _book("NoVig", -154, 128),
        _book("DraftKings", -160, 130),
        _book("FanDuel", -148, 124),
    ]
    shape = autobet_product_shape(
        books, 186, take_book="Kalshi", ev_percent=12.0, plus_alert=True
    )
    assert shape["same_sign_recs"] < 3
    assert shape["allow"] is False
    assert "same_sign_recs" in shape["reasons"]


def test_opposite_line_does_not_count_as_same_sign_rec():
    """LINE sign flip (+7.5 vs −7.5) is not consensus. Odds sign is irrelevant."""
    books = [
        {**_book("FanDuel", -110, -110), "side_hdp": 7.5, "line": 7.5},
        {**_book("DraftKings", -108, -112), "side_hdp": 7.5, "line": 7.5},
        {**_book("Caesars", -112, -108), "side_hdp": 7.5, "line": 7.5},
    ]
    shape = autobet_product_shape(
        books,
        110,
        take_book="Kalshi",
        ev_percent=6.0,
        plus_alert=True,
        actual_side_hdp=-7.5,
        painted_side_hdp=-7.5,
    )
    assert shape["same_sign_recs"] == 0
    assert shape["allow"] is False
    assert "same_sign_recs" in shape["reasons"]


def test_same_line_plus_juice_vs_minus_pack_counts():
    """Spread −7.5 take +110 vs pack −110 on the same −7.5 line."""
    books = [
        {**_book("FanDuel", -107, -113), "side_hdp": -7.5, "line": -7.5},
        {**_book("DraftKings", -110, -110), "side_hdp": -7.5, "line": -7.5},
        {**_book("Caesars", -112, -108), "side_hdp": -7.5, "line": -7.5},
    ]
    shape = autobet_product_shape(
        books,
        110,
        take_book="Kalshi",
        ev_percent=5.0,
        plus_alert=True,
        actual_side_hdp=-7.5,
        painted_side_hdp=-7.5,
    )
    assert shape["same_sign_recs"] >= 3
    assert shape["allow"] is True


def test_display_only_reason_never_fails_allow():
    from odds_ev_monitor import _autobet_card_reasons

    books = _three_sharp_board()
    shape = autobet_product_shape(
        books, 163, take_book="Kalshi", ev_percent=6.5, plus_alert=True
    )
    assert shape["allow"] is True
    reasons = _autobet_card_reasons(shape["reasons"], display_only=True)
    assert "display_only" in reasons
    # Card may still tag display_only; allow bit stays true.
    assert shape["allow"] is True


def test_monitor_plus110_vs_minus_pack_allows_executable_kalshi():
    """Live card: Kalshi +110 vs −107/−110/−112 pack must set autobet_allow."""
    import time

    from ev_calculator import american_to_decimal
    from odds_ev_monitor import OddsEVMonitor

    now = time.time()
    take_d = american_to_decimal(110)
    fd_d = american_to_decimal(-107)
    dk_d = american_to_decimal(-110)
    ca_d = american_to_decimal(-112)
    opp_d = american_to_decimal(-110)
    ticker = "KXMLBGAME-26SEP051805BOSBAL-BAL"
    doc = {
        "id": 2609051810,
        "home": "Baltimore Orioles",
        "away": "Boston Red Sox",
        "sport": {"name": "Baseball", "slug": "baseball"},
        "league": {"name": "MLB", "slug": "usa-mlb"},
        "live": True,
        "urls": {"Kalshi": f"https://kalshi.com/markets/{ticker}"},
        "bookmakerIds": {"Kalshi": ticker},
        "book_updated_at": {
            "Kalshi": now - 3.0,
            "FanDuel": now - 2.0,
            "DraftKings": now - 2.0,
            "Caesars": now - 2.0,
        },
        "bookmakers": {
            "Kalshi": [{"name": "ML", "odds": [{"home": take_d, "away": opp_d, "ticker": ticker}]}],
            "FanDuel": [{"name": "ML", "odds": [{"home": fd_d, "away": american_to_decimal(-113)}]}],
            "DraftKings": [{"name": "ML", "odds": [{"home": dk_d, "away": dk_d}]}],
            "Caesars": [{"name": "ML", "odds": [{"home": ca_d, "away": american_to_decimal(-108)}]}],
        },
    }
    mon = OddsEVMonitor(auth_token=None)
    mon.set_filter(
        {
            "betTypes": ["GAMELINES"],
            "minRoi": 0,
            "devigFilter": {
                "sharps": ["FanDuel", "DraftKings", "Caesars"],
                "method": "POWER",
                "type": "AVERAGE",
                "minEv": 0,
                "minSharpBooks": 3,
            },
        }
    )
    rows = mon.live_scan_value_bets_from_docs({2609051810: doc})
    ml = [
        r
        for r in rows
        if str(r.get("_scan_mname") or "").upper() == "ML"
        and str(r.get("_take_only") or "").lower() == "kalshi"
        and r.get("betSide") == "home"
    ]
    assert ml
    built = mon._value_bet_to_normalized_bet(ml[0], doc, take_book="Kalshi")
    assert built is not None
    assert built["odds"] == 110
    assert 2.0 <= float(built["ev"]) <= 20.0
    assert built["autobet_allow"] is True
    assert "same_sign_recs" not in (built.get("autobet_reasons") or [])
    assert "sister_required" not in (built.get("autobet_reasons") or [])
    assert "display_only" not in (built.get("autobet_reasons") or [])


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
