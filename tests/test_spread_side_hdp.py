"""Run-line sign: negate away hdp. Not a team-total / game-total leak."""
from __future__ import annotations

from ev_calculator import (
    american_to_decimal,
    evaluate_sharp_panel_ev,
    filter_sharp_panel,
    is_junk_vs_kalshi,
    is_polymarket_book,
    spread_keep_on_labeled_side,
)
from odds_ev_monitor import (
    _pick_matching_odds_row,
    _pick_qualifier_line_for_side,
    format_spread_qualifier,
    side_handicap,
    side_signed_line,
)
from tests.test_sharp_panel_filter import _book


def test_marlins_away_at_kc_paints_minus15_not_plus15():
    """Marlins @ KC: home-centric hdp +1.5 → away Marlins is −1.5. Home Twins +2.5 stays."""
    away_pick, away_qual, away_line = _pick_qualifier_line_for_side(
        "Kansas City Royals",
        "Miami Marlins",
        "Spread",
        "away",
        {"hdp": 1.5, "home": 1.45, "away": 2.70},
    )
    assert away_pick == "Miami Marlins"
    assert away_line == -1.5
    assert away_qual == "-1.5"
    twins_pick, twins_qual, twins_line = _pick_qualifier_line_for_side(
        "Minnesota Twins",
        "Milwaukee Brewers",
        "Spread",
        "home",
        {"hdp": 2.5, "home": 1.45, "away": 2.70},
    )
    assert twins_pick == "Minnesota Twins"
    assert twins_line == 2.5
    assert twins_qual == "+2.5"
    assert side_handicap(2.5, "home") == 2.5
    assert side_handicap(1.5, "away") == -1.5


def test_away_run_line_negates_home_hdp():
    """White Sox @ Astros: home hdp +1.5 → away Sox is −1.5, not painted +1.5."""
    pick, qual, line = _pick_qualifier_line_for_side(
        "Houston Astros",
        "Chicago White Sox",
        "Spread",
        "away",
        {"hdp": 1.5, "home": 1.28, "away": 4.76},
    )
    assert pick == "Chicago White Sox"
    assert line == -1.5
    assert qual == "-1.5"
    assert side_handicap(1.5, "away") == -1.5
    assert side_handicap(1.5, "home") == 1.5
    assert format_spread_qualifier(-1.5) == "-1.5"


def test_brewers_away_plus25_label_was_minus25_prices():
    """Twins home +2.5 stays +2.5. Brewers away must paint −2.5, not +2.5."""
    home_pick, home_qual, home_line = _pick_qualifier_line_for_side(
        "Minnesota Twins",
        "Milwaukee Brewers",
        "Spread",
        "home",
        {"hdp": 2.5, "home": 1.45, "away": 2.70},
    )
    assert home_pick == "Minnesota Twins"
    assert home_line == 2.5
    assert home_qual == "+2.5"
    away_pick, away_qual, away_line = _pick_qualifier_line_for_side(
        "Minnesota Twins",
        "Milwaukee Brewers",
        "Spread",
        "away",
        {"hdp": 2.5, "home": 1.45, "away": 2.70},
    )
    assert away_pick == "Milwaukee Brewers"
    assert away_line == -2.5
    assert away_qual == "-2.5"


def test_plive_american_minus15_slot_away_stays_minus15():
    """Dump −1.5 slot: Astros −1.5 / Sox −1.5. Do not paint Sox +1.5."""
    row = {"hdp": -1.5, "home": 9.78, "away": 1.03358, "line_style": "american"}
    home_pick, home_qual, home_line = _pick_qualifier_line_for_side(
        "Houston Astros", "Chicago White Sox", "Spread", "home", row
    )
    away_pick, away_qual, away_line = _pick_qualifier_line_for_side(
        "Houston Astros", "Chicago White Sox", "Spread", "away", row
    )
    assert home_pick == "Houston Astros"
    assert home_line == -1.5
    assert home_qual == "-1.5"
    assert away_pick == "Chicago White Sox"
    assert away_line == -1.5
    assert away_qual == "-1.5"
    assert side_signed_line(row, "away") == -1.5
    assert side_signed_line(row, "home") == -1.5


def test_do_not_keep_on_painted_plus15_when_actual_is_minus15():
    """Card said Sox +1.5; prices were the −1.5 slot. KEEP on the labeled side is invalid."""
    gate = spread_keep_on_labeled_side(
        painted_hdp=1.5, actual_hdp=-1.5, kalshi_hdp=-1.5, rec_hdp=-1.5
    )
    assert gate["allow_keep"] is False
    assert "spread_label_mismatch" in gate["reasons"]
    books = [
        _book("DK", 298),
        _book("B365", 255),
        _book("CZ", 275),
        _book("NV", 239),
    ]
    out = evaluate_sharp_panel_ev(
        books,
        376,
        painted_side_hdp=1.5,
        kalshi_side_hdp=-1.5,
        rec_side_hdp=-1.5,
    )
    assert out["plus_alert"] is False
    # After negate-away the label matches the slot — EV gates may still KEEP.
    ok = spread_keep_on_labeled_side(
        painted_hdp=-1.5, actual_hdp=-1.5, kalshi_hdp=-1.5, rec_hdp=-1.5
    )
    assert ok["allow_keep"] is True


def test_poly_minus455_vs_plus_pack_is_junk_not_inverted():
    """Royals-style: pack +120..+156 vs Poly −455. Flip of −455 is +455, not ~+150."""
    take = 140
    books = [
        _book("FD", 120),
        _book("DK", 140),
        _book("NV", 156),
        _book("Poly", -455, 350),
    ]
    assert is_junk_vs_kalshi(-455, take) is True
    surviving = filter_sharp_panel(books, kalshi_american=take)
    names = {b["name"] for b in surviving}
    assert "Poly" not in names
    out = evaluate_sharp_panel_ev(books, take, min_sharp_books=3)
    assert all(not is_polymarket_book(n) for n in out["fair_names"])
    assert "Poly" not in out["fair_names"]
    # Must not average an inverted +455 into fair.
    if out["fair_prob"] is not None:
        inverted = 1.0 / american_to_decimal(455)
        assert abs(out["fair_prob"] - inverted) > 0.05


def test_poly_minus178_vs_minus104_same_sign_junk_not_invert():
    """Pirates-style same-sign Poly −178 vs −104 is >10c junk, not an invert."""
    assert is_junk_vs_kalshi(-178, -104) is True
    surviving = filter_sharp_panel(
        [_book("Poly", -178, 145), _book("FD", -110), _book("NV", -110)],
        kalshi_american=-104,
    )
    assert "Poly" not in {b["name"] for b in surviving}
    out = evaluate_sharp_panel_ev(
        [_book("FD", -110), _book("NV", -110), _book("DK", -108), _book("Poly", -178, 145)],
        -104,
        min_sharp_books=3,
    )
    assert "Poly" not in out["fair_names"]


def test_on_pack_poly_stays_in_power():
    """On-pack Poly (+158 vs take +163) is a sharp — in POWER names."""
    take = 163
    books = [
        _book("FD", 156),
        _book("DK", 160),
        _book("NV", 150),
        _book("Polymarket", 158),
    ]
    assert is_junk_vs_kalshi(158, take) is False
    surviving = filter_sharp_panel(books, kalshi_american=take)
    assert "Polymarket" in {b["name"] for b in surviving}
    out = evaluate_sharp_panel_ev(books, take, min_sharp_books=3)
    assert any(is_polymarket_book(n) for n in out["fair_names"])
    assert "Polymarket" in out["fair_names"]


def test_spread_matcher_ignores_game_and_team_totals():
    """Guard: market 5 / 7-8 prices must never attach to a Spread tile."""
    totals = {
        "name": "Totals",
        "odds": [{"hdp": 4.5, "over": 3.79, "under": 1.24, "max": 4.5}],
    }
    team_total = {
        "name": "Team Total",
        "odds": [{"hdp": 2.5, "over": 4.25, "under": 1.14, "max": 2.5}],
    }
    ref = {"hdp": 2.5, "home": 1.45, "away": 2.70}
    assert _pick_matching_odds_row(totals, "Spread", ref) == {}
    assert _pick_matching_odds_row(team_total, "Spread", ref) == {}
    assert _pick_matching_odds_row(totals, "Spread", {"hdp": 1.5}) == {}
