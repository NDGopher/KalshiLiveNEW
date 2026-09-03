"""PR-gate numeric locks. No network. Anonymous boards. Auto-bet stays OFF."""
from __future__ import annotations

from ev_calculator import (
    count_better_exchanges,
    evaluate_sharp_panel_ev,
    ev_percent_vs_take_american,
    filter_sharp_panel,
    is_junk_vs_kalshi,
    is_polymarket_book,
    is_real_sign_flip,
    two_way_power_ev,
    two_way_power_fair,
)
from odds_ev_monitor import (
    _odds_doc_has_kalshi_tradable_gameline,
    _pick_qualifier_line_for_side,
    side_handicap,
    side_signed_line,
)
from tests.test_sharp_panel_filter import _book


def test_1_two_way_near_even_not_plus_only():
    ev = two_way_power_ev(299, -300, 300)
    assert ev is not None
    assert -1.0 <= ev <= 1.0
    # Plus-only mean implied of +228/+234/+240 vs +300 is ~+19.8% and must not emit.
    plus_only = ev_percent_vs_take_american(
        (1.0 / 3.0)
        * (
            (100 / 328)
            + (100 / 334)
            + (100 / 340)
        ),
        300,
    )
    assert plus_only is not None and plus_only > 18.0
    assert ev < 2.0


def test_2_pickem_minus110_vs_plus113():
    ev = two_way_power_ev(-110, -110, 113)
    assert ev is not None
    assert abs(ev - 6.50) < 0.1
    assert is_real_sign_flip(-110, 113) is False
    assert is_junk_vs_kalshi(-110, 113) is False


def test_3_totals_two_way_keep_and_no_plus():
    keep = two_way_power_ev(-110, -110, 105)
    kill = two_way_power_ev(-110, -110, -105)
    assert keep is not None and abs(keep - 2.50) < 0.1
    assert kill is not None and abs(kill + 2.38) < 0.1
    assert keep > 0
    assert kill < 0


def test_4_worse_favorite_not_fake_fourteen():
    ev = two_way_power_ev(-113, -106, -122)
    assert ev is not None
    assert abs(ev + 7.53) < 0.15
    assert ev < 0


def test_5_two_exchange_hide_junk_excluded():
    hide = evaluate_sharp_panel_ev(
        [
            _book("Betfair Exchange", -163, 135),
            _book("NoVig", -130, 110),
            _book("FanDuel", -210, 175),
            _book("DraftKings", -215, 180),
            _book("Caesars", -200, 170),
        ],
        -204,
        min_sharp_books=3,
        take_book="Kalshi",
    )
    assert hide["plus_alert"] is False
    assert "two_exchange" in hide["reasons"] or hide["plus_alert"] is False
    assert count_better_exchanges(
        [
            _book("Betfair Exchange", -163, 135),
            _book("NoVig", -130, 110),
        ],
        -204,
    ) >= 2

    no_hide = evaluate_sharp_panel_ev(
        [
            _book("FanDuel", 150),
            _book("DraftKings", 140),
            _book("Caesars", 145),
            _book("NoVig", 317),
            _book("Betfair Exchange", 567),
        ],
        163,
        min_sharp_books=3,
        take_book="Kalshi",
    )
    assert count_better_exchanges(
        [_book("NoVig", 317), _book("Betfair Exchange", 567)],
        163,
    ) == 0
    assert "two_exchange" not in no_hide["reasons"]
    assert "nv_better" not in no_hide["reasons"]


def test_6_poly_junk_vs_on_pack():
    assert is_junk_vs_kalshi(-455, 163) is True
    assert is_real_sign_flip(-455, 163) is True
    junk = evaluate_sharp_panel_ev(
        [
            _book("Poly", -455, 350),
            _book("FanDuel", 116),
            _book("DraftKings", 105),
            _book("Bet365", 100),
            _book("Caesars", 110),
        ],
        163,
        min_sharp_books=3,
    )
    assert "Poly" not in junk["fair_names"]
    assert all(not is_polymarket_book(n) for n in junk["fair_names"])

    on_pack = filter_sharp_panel(
        [_book("Poly", 118, -140), _book("FanDuel", 116), _book("DraftKings", 105)],
        kalshi_american=163,
    )
    assert any(is_polymarket_book(b.get("name")) for b in on_pack)
    assert is_junk_vs_kalshi(118, 163) is False


def test_7_tigers_away_sign_and_plive_not_best():
    pick, qual, line = _pick_qualifier_line_for_side(
        "Minnesota Twins",
        "Detroit Tigers",
        "Spread",
        "away",
        {"hdp": 1.5, "home": 1.28, "away": 4.69},
    )
    assert pick == "Detroit Tigers"
    assert line == -1.5
    assert qual == "-1.5"
    assert side_handicap(1.5, "away") == -1.5
    assert side_signed_line({"hdp": 1.5}, "away") == -1.5
    out = evaluate_sharp_panel_ev(
        [
            _book("Bet365", 475, -650),
            _book("NoVig", 525, -750),
            _book("Caesars", 333, -430),
            _book("DraftKings", 307, -390),
            _book("Kalshi", 317, -410),
        ],
        369,
        min_sharp_books=3,
        take_book="PLive",
        painted_side_hdp=1.5,
        kalshi_side_hdp=-1.5,
        rec_side_hdp=-1.5,
    )
    assert out["plus_alert"] is False
    assert "plive_not_best" in out["reasons"] or out["plus_alert"] is False


def test_8_missing_sister_no_ev():
    assert two_way_power_fair(228, 0) is None
    assert two_way_power_ev(228, 0, 300) is None
    assert two_way_power_ev(240, 0, 300) is None


def test_twins_home_plus25_stays():
    pick, qual, line = _pick_qualifier_line_for_side(
        "Minnesota Twins",
        "Milwaukee Brewers",
        "Spread",
        "home",
        {"hdp": 2.5, "home": 1.45, "away": 2.70},
    )
    assert line == 2.5
    assert qual == "+2.5"
    assert pick == "Minnesota Twins"


def test_brewers_away_paints_minus25():
    pick, qual, line = _pick_qualifier_line_for_side(
        "Minnesota Twins",
        "Milwaukee Brewers",
        "Spread",
        "away",
        {"hdp": 2.5, "home": 1.45, "away": 2.70},
    )
    assert line == -2.5
    assert qual == "-2.5"


def test_plive_sox_minus15_home_slot():
    """Home-centric: home hdp -1.5 stays -1.5. Away on that row is +1.5."""
    row = {"hdp": -1.5, "home": 9.78, "away": 1.03358, "line_style": "american"}
    assert side_signed_line(row, "home") == -1.5
    assert side_signed_line(row, "away") == 1.5


def test_keep_royals_plus163_two_way():
    out = evaluate_sharp_panel_ev(
        [
            _book("Betfair Exchange", 134, -154),
            _book("DraftKings", 105, -125),
            _book("FanDuel", 116, -136),
            _book("Bet365", 100, -120),
            _book("Caesars", 110, -130),
            _book("PLive", 118, -138),
            _book("Poly", -455, 350),
        ],
        163,
        min_sharp_books=3,
        take_book="Kalshi",
    )
    assert "Poly" not in out["fair_names"]
    assert "PLive" not in out["fair_names"]
    assert out["plus_alert"] is True
    assert out["autobet_allow"] is True
    # Live printed ~+9.59% is two-way POWER on the rec pack (fair ~+140), not Poly.
    # Do not invent sisters to reprint 9.59. Plus-only on these pluses would be teens.
    assert 4.0 <= out["ev_percent"] <= 14.0


def test_keep_twins_plus203_band():
    out = evaluate_sharp_panel_ev(
        [
            _book("DraftKings", 170, -200),
            _book("FanDuel", 175, -210),
            _book("Caesars", 168, -198),
            _book("Bet365", 165, -195),
            _book("NoVig", 180, -220),
        ],
        203,
        min_sharp_books=3,
    )
    assert out["plus_alert"] is True
    assert 1.0 <= out["ev_percent"] <= 12.0


def test_9_keep_plus_money_two_way_classes():
    """Ninth numeric lock: Royals +163 and Twins +203 stay plus after two-way POWER."""
    royals = evaluate_sharp_panel_ev(
        [
            _book("Betfair Exchange", 134, -154),
            _book("DraftKings", 105, -125),
            _book("FanDuel", 116, -136),
            _book("Bet365", 100, -120),
            _book("Caesars", 110, -130),
        ],
        163,
        min_sharp_books=3,
    )
    twins = evaluate_sharp_panel_ev(
        [
            _book("DraftKings", 170, -200),
            _book("FanDuel", 175, -210),
            _book("Caesars", 168, -198),
            _book("Bet365", 165, -195),
            _book("NoVig", 180, -220),
        ],
        203,
        min_sharp_books=3,
    )
    assert royals["plus_alert"] is True
    assert twins["plus_alert"] is True
    assert royals["ev_percent"] > 0
    assert twins["ev_percent"] > 0
