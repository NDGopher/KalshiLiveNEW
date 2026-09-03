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
    _pick_qualifier_line_for_side,
    format_spread_qualifier,
    side_handicap,
)
from tests.test_sharp_panel_filter import _book


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
