"""KEEP vs DROP sharp-panel rules — anonymous boards, no ticker allowlists.

Labeled live fixtures (pattern boards only; do not pin tickers):

- DROP (image 1): SF @ PIT Giants +1.5. Kalshi +186 vs Poly/DK/FD/BE/CA
  +145..+188, NoVig -154. UI printed +13.51% off the stale sign-flip.
  After NV is dropped this must not print +13%. A leftover small plus
  vs the plus-money pack is allowed — do not add filters just to kill it.

- KEEP (image 2): CWS @ HOU Astros ML. Kalshi -133 best (0 red boxes).
  FD -142 / CA -141 / NV -139 tight rec cluster; DK/BE juice -170..-185.
  UI +2.33%. Filters MUST still allow this small plus. Do not over-filter.

Prior Cubs 3-better kill and Houston/Rays wrong-line drop still stand.
Auto-bet stays OFF. No SharpMoney.
"""
from __future__ import annotations

from ev_calculator import (
    american_to_decimal,
    apply_ev_hard_gates,
    decimal_to_american,
    evaluate_sharp_panel_ev,
    filter_sharp_panel,
)


def _book(name: str, pick_am: int, opp_am: int | None = None, match_failed: bool = False) -> dict:
    dp = american_to_decimal(int(pick_am))
    if opp_am is None:
        p = 1.0 / dp
        opp_p = max(0.08, min(0.92, 1.045 - p))
        do = 1.0 / opp_p
    else:
        do = american_to_decimal(int(opp_am))
    return {
        "name": name,
        "american": int(pick_am),
        "decimal_pick": dp,
        "decimal_opp": do,
        "match_failed": match_failed,
    }


def _favorite_best_keep_board():
    """Kalshi best on a same-sign favorite pack. Nearby recs, 0 better books."""
    # Pattern: take venue -133 vs NV -139 / CA -149 / juice to -200.
    return -133, [
        _book("B1", -163),
        _book("B2", -169),
        _book("B3", -200),
        _book("B4", -172),
        _book("B5", -180),
        _book("B6", -149),
        _book("B7", -139),
    ]


def _even_tied_keep_board():
    """Kalshi tied for best (+100) vs one rec at +100 and a juice pack."""
    return 100, [
        _book("R1", 100),
        _book("R2", -149),
        _book("R3", -166),
        _book("R4", -145),
        _book("R5", -154),
    ]


def _plus_money_offsign_drop_board():
    """Plus-money pack with one minus rec that would fake a fat plus."""
    return 186, [
        _book("S1", 170),
        _book("S2", 180),
        _book("S3", 188),
        _book("S4", 145),
        _book("S5", 150),
        _book("S6", -154),
    ]


def _plus_pack_wrong_line_drop_board():
    """Prior Houston/Rays-style DROP: underdog pack + two favorite spikes."""
    return 156, [
        _book("X1", -233),
        _book("X2", -217),
        _book("X3", 154),
        _book("X4", 182),
        _book("X5", 170),
        _book("X6", 190),
        _book("X7", 182),
    ]


def _three_better_minus_drop_board():
    """Prior Cubs-style 3-better kill: favorite worse than three survivors."""
    return -127, [
        _book("T1", -117),
        _book("T2", -147),
        _book("T3", 107),
        _book("T4", -158),
        _book("T5", -125),
        _book("T6", -175),
        _book("T7", -118),
    ]


def _three_better_plus_drop_board():
    """Plus take-venue with four strictly better plus-money survivors."""
    return 122, [
        _book("U1", -117),
        _book("U2", 192),
        _book("U3", 107),
        _book("U4", 220),
        _book("U5", -125),
        _book("U6", 152),
        _book("U7", 223),
    ]


def _two_better_control_board():
    """Exactly two books better than the take venue after the screen."""
    return 150, [
        _book("C1", 170),
        _book("C2", 165),
        _book("C3", 140),
        _book("C4", 135),
        _book("C5", 145),
    ]


def test_filter_accepts_plive_named_row():
    books = [_book("PLive", -139), _book("A", -149), _book("B", -155)]
    names = {b["name"] for b in filter_sharp_panel(books)}
    assert "PLive" in names


def test_hard_rejects_never_survive():
    books = [
        _book("SpikePoly", -1333),
        _book("SpikeBfx", -5000),
        _book("Broken", -110, match_failed=True),
        _book("CleanA", -115),
        _book("CleanB", -120),
        _book("CleanC", -118),
    ]
    surviving = filter_sharp_panel(books)
    names = {b["name"] for b in surviving}
    assert "SpikePoly" not in names
    assert "SpikeBfx" not in names
    assert "Broken" not in names
    assert names == {"CleanA", "CleanB", "CleanC"}


def test_match_failed_never_alerts():
    books = [
        _book("A", -110, match_failed=True),
        _book("B", -115, match_failed=True),
        _book("C", -120, match_failed=True),
    ]
    out = evaluate_sharp_panel_ev(books, -108, min_sharp_books=3)
    assert out["surviving"] == []
    assert out["plus_alert"] is False


def test_min_sharp_three_two_clean_plus_outlier_no_alert():
    books = [
        _book("A", 170),
        _book("B", 175),
        _book("Out", -233),
    ]
    out = evaluate_sharp_panel_ev(books, 156, min_sharp_books=3)
    assert len(out["surviving"]) < 3
    assert out["plus_alert"] is False


def test_keep_favorite_best_still_prints_small_plus():
    kalshi, books = _favorite_best_keep_board()
    out = evaluate_sharp_panel_ev(books, kalshi, min_sharp_books=3)
    assert out["better_count"] <= 1
    # Same-sign juice stays; shorts are not treated as a flip.
    assert len(out["surviving"]) >= 3
    assert "tight_cluster" not in out["reasons"]
    assert "median_gate" not in out["reasons"]
    assert "better_books" not in out["reasons"]
    assert out["plus_alert"] is True
    assert 0.0 < out["ev_percent"] < 12.0


def test_keep_even_tied_still_prints():
    kalshi, books = _even_tied_keep_board()
    out = evaluate_sharp_panel_ev(books, kalshi, min_sharp_books=3)
    assert out["better_count"] <= 1
    assert out["ev_percent"] < 8.0
    # Tied take venue vs one matching rec may be ~0 after the pack screen;
    # it must not become a 13–16% fake plus.
    assert out["plus_alert"] is True or out["ev_percent"] <= 2.0


def test_drop_offsign_rec_on_plus_pack_kills_fat_plus():
    kalshi, books = _plus_money_offsign_drop_board()
    raw_names = {b["name"] for b in books}
    assert "S6" in raw_names
    surviving = filter_sharp_panel(books)
    assert "S6" not in {b["name"] for b in surviving}
    out = evaluate_sharp_panel_ev(books, kalshi, min_sharp_books=3)
    assert out["ev_percent"] < 8.0
    assert out["raw_ev_percent"] < 8.0 or out["plus_alert"] is False
    # Must not reprint the pre-filter ~13% lie.
    assert out["ev_percent"] < 13.0


def test_drop_wrong_line_favorites_on_plus_pack():
    kalshi, books = _plus_pack_wrong_line_drop_board()
    surviving = filter_sharp_panel(books)
    names = {b["name"] for b in surviving}
    assert "X1" not in names
    assert "X2" not in names
    out = evaluate_sharp_panel_ev(books, kalshi, min_sharp_books=3)
    assert out["plus_alert"] is False
    assert out["ev_percent"] <= 0.0


def test_drop_three_or_more_better_minus_pack():
    kalshi, books = _three_better_minus_drop_board()
    surviving = filter_sharp_panel(books)
    assert all(b["american"] != 107 for b in surviving)
    out = evaluate_sharp_panel_ev(books, kalshi, min_sharp_books=3)
    assert out["better_count"] >= 3 or out["plus_alert"] is False
    assert out["plus_alert"] is False
    assert out["ev_percent"] <= 0.0


def test_drop_three_or_more_better_plus_pack():
    kalshi, books = _three_better_plus_drop_board()
    out = evaluate_sharp_panel_ev(books, kalshi, min_sharp_books=3)
    assert out["plus_alert"] is False
    assert out["ev_percent"] <= 0.0
    assert out["better_count"] >= 3 or "better_books" in out["reasons"] or "median_gate" in out["reasons"]


def test_two_better_control_may_still_compute():
    kalshi, books = _two_better_control_board()
    out = evaluate_sharp_panel_ev(books, kalshi, min_sharp_books=3)
    assert out["better_count"] == 2
    assert "better_books" not in out["reasons"]
    # Allowed to be a plus if other gates pass; not required to be huge.
    if out["plus_alert"]:
        assert out["ev_percent"] < 16.0


def test_tight_cluster_identity_not_fat_plus():
    """Take venue -118 vs {-120,-115,-110} cannot print a +13% identity lie."""
    kalshi = -118
    books = [_book("A", -120), _book("B", -115), _book("C", -110)]
    out = evaluate_sharp_panel_ev(books, kalshi, min_sharp_books=3)
    assert abs(out["ev_percent"]) <= 2.0
    assert out["plus_alert"] is False or abs(out["ev_percent"]) <= 2.0


def test_fallback_never_plus_alert():
    books = [_book("A", -110), _book("B", -115), _book("C", -120)]
    out = evaluate_sharp_panel_ev(books, -118, min_sharp_books=3, used_fallback=True)
    assert out["plus_alert"] is False
    assert out["ev_percent"] <= 0.0
    assert "fallback" in out["reasons"]


def test_keep_band_wider_than_four_cents_is_not_identity():
    """~8c of same-sign juice (57c vs 65c) must not trip the 4c identity gate."""
    kalshi, books = _favorite_best_keep_board()
    k_imp = 1.0 / american_to_decimal(kalshi)
    surv = filter_sharp_panel(books)
    imps = [1.0 / b["decimal_pick"] for b in surv] + [k_imp]
    assert max(imps) - min(imps) > 0.04
    gated = apply_ev_hard_gates(3.0, kalshi, surv, min_sharp_books=3)
    assert "tight_cluster" not in gated["reasons"]


def test_american_helper_roundtrip_used_by_filter():
    assert decimal_to_american(american_to_decimal(-133)) == -133
    assert decimal_to_american(american_to_decimal(186)) == 186


# --- Labeled live fixtures (pattern boards; names are labels only) ---


def _fixture_drop_sf_pit_giants_plus15():
    """DROP (image 1): SF @ PIT Giants +1.5. Kalshi +186, NoVig -154 sign-flip.

    Plus-money cluster Poly/DK/FD/BE/CA around +145..+188. UI +13.51% was
    not +EV — it only beat stale NV. Drop NV as off-market (wrong sign).
    """
    return 186, [
        _book("Poly", 170),
        _book("DK", 180),
        _book("FD", 188),
        _book("BE", 145),
        _book("CA", 150),
        _book("NV", -154),
    ]


def _fixture_keep_cws_hou_astros_ml():
    """KEEP (image 2): CWS @ HOU Astros ML. Kalshi -133 best, no red boxes.

    Tight rec cluster FD -142 / CA -141 / NV -139. Same-sign juice DK/BE
    -170..-185 stays off fair. UI +2.33% is a real small plus — do not
    over-filter this board.
    """
    return -133, [
        _book("Wynn", -163),
        _book("BE1", -185),
        _book("DK", -185),
        _book("FD", -142),
        _book("BE2", -170),
        _book("CA", -141),
        _book("NV", -139),
    ]


def test_drop_sf_pit_giants_plus15_novig_signflip():
    """DROP: after NV -154 is removed this must not print +13%."""
    kalshi, books = _fixture_drop_sf_pit_giants_plus15()
    surviving = filter_sharp_panel(books, kalshi_american=kalshi)
    names = {b["name"] for b in surviving}
    assert "NV" not in names
    assert {"Poly", "DK", "FD"}.issubset(names)
    out = evaluate_sharp_panel_ev(books, kalshi, min_sharp_books=3)
    # Fat plus was NV in the average. Residual vs the plus-money pack
    # may still be a small plus; it cannot reprint the UI +13.51% lie.
    assert out["ev_percent"] < 8.0
    assert out["ev_percent"] < 13.0
    assert out["raw_ev_percent"] < 13.0


def test_keep_cws_hou_astros_ml_still_small_plus():
    """KEEP: Kalshi best vs a tight rec cluster must still print a small plus."""
    kalshi, books = _fixture_keep_cws_hou_astros_ml()
    out = evaluate_sharp_panel_ev(books, kalshi, min_sharp_books=3)
    names = set(out["surviving_names"])
    assert {"FD", "CA", "NV"}.issubset(names)
    assert out["better_count"] == 0
    assert "tight_cluster" not in out["reasons"]
    assert "median_gate" not in out["reasons"]
    assert "better_books" not in out["reasons"]
    assert out["plus_alert"] is True
    assert 1.0 <= out["ev_percent"] <= 5.5


def test_bimodal_steam_does_not_eat_close_rec():
    """Kalshi -133 + close rec -139 vs far shorts -217..-270. Not a +14% print."""
    kalshi = -133
    books = [
        _book("Close", -139),
        _book("F1", -217),
        _book("F2", -230),
        _book("F3", -250),
        _book("F4", -270),
    ]
    surviving = filter_sharp_panel(books, kalshi_american=kalshi)
    names = {b["name"] for b in surviving}
    assert "Close" in names
    assert "F1" not in names
    out = evaluate_sharp_panel_ev(books, kalshi, min_sharp_books=3)
    assert out["ev_percent"] < 8.0
    assert out["plus_alert"] is False or out["ev_percent"] <= 5.5
