"""KEEP vs DROP sharp-panel rules — anonymous boards, no ticker allowlists.

Labeled live fixtures (pattern boards only; do not pin tickers):

- DROP (image 1): SF @ PIT Giants +1.5. Kalshi +186 vs Poly/DK/FD/BE/CA
  +145..+188, NoVig -154. UI printed +13.51% off the stale sign-flip.
  After NV is dropped this must not print +13%. A leftover small plus
  vs the plus-money pack is allowed — do not add filters just to kill it.

- KEEP (image 2): CWS @ HOU Astros ML. Kalshi -133 best (0 red boxes).
  FD -142 / CA -141 / NV -139 tight rec cluster; DK/BE juice -170..-185.
  UI +2.33%. Filters MUST still allow this small plus. Do not over-filter.

- KEEP smaller EV: Twins +317 vs +252/+270 pack — two-way POWER, not a
  13–17% one-sided implied fake. Brewers +113 vs a −110 pick'em pack.

- KILL: Pirates −104 vs FD/NV −110 fattened by Poly −178. Take worse
  than NV (Kalshi −488). 3-better kill stays.

Prior Cubs 3-better kill and Houston/Rays wrong-line drop still stand.
Auto-bet stays OFF. No SharpMoney.
"""
from __future__ import annotations

from ev_calculator import (
    american_to_decimal,
    apply_ev_hard_gates,
    decimal_to_american,
    evaluate_sharp_panel_ev,
    exclude_plive_from_fair,
    exchange_better_kill,
    fair_books_excluding_take,
    fair_books_for_panel,
    filter_sharp_panel,
    is_junk_vs_kalshi,
    is_novig_book,
    is_plive_book,
    is_polymarket_book,
    is_real_sign_flip,
    novig_better_than_take,
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
    """Plus take-venue with three strictly better books still inside 10c."""
    return 122, [
        _book("U1", 140),
        _book("U2", 145),
        _book("U3", 107),
        _book("U4", 150),
        _book("U5", 110),
        _book("U6", 115),
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
    assert is_plive_book("PLive") is True
    assert "PLive" not in {b["name"] for b in exclude_plive_from_fair(books)}


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
    surviving = filter_sharp_panel(books, kalshi_american=kalshi)
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


def test_keep_plive_does_not_change_fair_or_ev():
    """PLive is a tile only. Same KEEP board with/without PLive must match fair + EV."""
    kalshi, books = _fixture_keep_cws_hou_astros_ml()
    without = evaluate_sharp_panel_ev(books, kalshi, min_sharp_books=3)
    with_plive_books = books + [_book("PLive", -139)]
    with_plive = evaluate_sharp_panel_ev(with_plive_books, kalshi, min_sharp_books=3)
    assert "PLive" in with_plive["surviving_names"]
    fair_names = {b["name"] for b in fair_books_for_panel(with_plive["surviving"], kalshi)}
    assert "PLive" not in fair_names
    assert {"FD", "CA", "NV"}.issubset(fair_names)
    assert with_plive["fair_prob"] == without["fair_prob"]
    assert with_plive["ev_percent"] == without["ev_percent"]
    assert with_plive["raw_ev_percent"] == without["raw_ev_percent"]
    assert with_plive["plus_alert"] is without["plus_alert"]
    assert without["plus_alert"] is True


def test_plive_does_not_satisfy_min_sharp_alone():
    """Two recs + PLive is not three fair books."""
    books = [_book("A", -139), _book("B", -141), _book("PLive", -140)]
    out = evaluate_sharp_panel_ev(books, -133, min_sharp_books=3)
    assert "PLive" in out["surviving_names"]
    assert out["plus_alert"] is False
    assert "min_sharp" in out["reasons"]


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


def test_junk_vs_kalshi_ten_cent_or_sign_flip():
    """Anonymous boards. One threshold vs take — do not pin tickers."""
    assert is_junk_vs_kalshi(122, 245) is True  # ~16c
    assert is_junk_vs_kalshi(138, 245) is True  # ~13c
    assert is_junk_vs_kalshi(228, 245) is False  # ~1.5c stays
    assert is_junk_vs_kalshi(-139, -133) is False  # ~1c keep, not red (−139 worse)
    assert is_junk_vs_kalshi(-270, -133) is True  # ~16c
    assert is_junk_vs_kalshi(-154, 186) is True  # real sign flip / ~26c off-pack
    # Pick'em around 50% is not a flip (Brewers +113 vs −110).
    assert is_real_sign_flip(-110, 113) is False
    assert is_junk_vs_kalshi(-110, 113) is False
    assert is_junk_vs_kalshi(113, -110) is False
    assert is_real_sign_flip(-154, 186) is True


def test_plus245_drops_far_shorts_not_small_gap():
    """Kalshi +245 vs +228 pack; +122/+138 are junk and must not print a fat plus."""
    kalshi = 245
    books = [
        _book("CloseA", 228),
        _book("CloseB", 240),
        _book("CloseC", 245),
        _book("FarA", 122),
        _book("FarB", 138),
    ]
    surviving = filter_sharp_panel(books, kalshi_american=kalshi)
    names = {b["name"] for b in surviving}
    assert {"CloseA", "CloseB", "CloseC"}.issubset(names)
    assert "FarA" not in names
    assert "FarB" not in names
    out = evaluate_sharp_panel_ev(books, kalshi, min_sharp_books=3)
    assert out["ev_percent"] < 8.0
    assert out["raw_ev_percent"] < 11.0


def _fixture_keep_twins_plus317():
    """KEEP smaller EV: take +317 vs DK/FD/NV +252/+270/+260."""
    return 317, [
        _book("DK", 252, -320),
        _book("FD", 270, -340),
        _book("NV", 260, -330),
    ]


def _fixture_keep_brewers_plus113():
    """KEEP smaller EV: take +113 vs a −110 pick'em pack (not a sign flip)."""
    return 113, [
        _book("FD", -110, -110),
        _book("DK", -110, -110),
        _book("NV", -108, -112),
        _book("CA", -112, -108),
    ]


def _fixture_kill_pirates_poly_fat():
    """KILL: take −104 vs FD/NV −110, Poly −178 off-pack fattening."""
    return -104, [
        _book("FD", -110, -110),
        _book("NV", -110, -110),
        _book("DK", -108, -112),
        _book("CA", -112, -108),
        _book("Poly", -178, 145),
    ]


def _fixture_kill_take_worse_than_nv():
    """KILL: huge favorite take worse than NV/FD/DK."""
    return -488, [
        _book("NV", -400),
        _book("FD", -420),
        _book("DK", -410),
    ]


def test_keep_twins_plus317_smaller_ev_not_onesided_fake():
    take, books = _fixture_keep_twins_plus317()
    out = evaluate_sharp_panel_ev(books, take, min_sharp_books=3)
    assert out["plus_alert"] is True
    assert out["ev_percent"] > 0.0
    # Two-way POWER, not the ~15% one-sided implied fake.
    assert out["ev_percent"] < 13.0
    assert out["raw_ev_percent"] < 13.0


def test_keep_brewers_plus113_vs_minus110_pack():
    take, books = _fixture_keep_brewers_plus113()
    surviving = filter_sharp_panel(books, kalshi_american=take)
    names = {b["name"] for b in surviving}
    assert {"FD", "DK", "NV", "CA"}.issubset(names)
    out = evaluate_sharp_panel_ev(books, take, min_sharp_books=3)
    assert out["plus_alert"] is True
    assert 0.0 < out["ev_percent"] < 12.0
    assert out["ev_percent"] < 13.0


def test_kill_pirates_barely_better_fattened_by_poly():
    take, books = _fixture_kill_pirates_poly_fat()
    surviving = filter_sharp_panel(books, kalshi_american=take)
    names = {b["name"] for b in surviving}
    assert "Poly" not in names
    out = evaluate_sharp_panel_ev(books, take, min_sharp_books=3)
    assert out["plus_alert"] is False
    assert out["ev_percent"] <= 2.0


def test_kill_take_worse_than_nv():
    take, books = _fixture_kill_take_worse_than_nv()
    out = evaluate_sharp_panel_ev(books, take, min_sharp_books=3)
    assert out["plus_alert"] is False
    assert out["ev_percent"] <= 0.0
    assert out["better_count"] >= 3 or "median_gate" in out["reasons"] or "better_books" in out["reasons"]


def test_plive_card_may_use_kalshi_in_fair():
    take = -120
    books = [
        _book("Kalshi", -133),
        _book("FD", -142),
        _book("NV", -139),
        _book("CA", -141),
        _book("PLive", -120),
    ]
    kalshi_card = evaluate_sharp_panel_ev(books, -133, min_sharp_books=3, take_book="Kalshi")
    assert "PLive" in kalshi_card["surviving_names"]
    assert "PLive" not in kalshi_card["fair_names"]
    assert "Kalshi" not in kalshi_card["fair_names"]
    plive_card = evaluate_sharp_panel_ev(books, take, min_sharp_books=3, take_book="PLive")
    assert "PLive" not in plive_card["fair_names"]
    assert "Kalshi" in plive_card["fair_names"]
    fair = fair_books_excluding_take(plive_card["surviving"], "PLive")
    assert any(is_plive_book(b.get("name")) is False for b in fair)
    assert any(str(b.get("name")) == "Kalshi" for b in fair)


def _fixture_kill_plus335_nv_better_tied_rec():
    """KILL tape: Kalshi +335 vs BF +320 / FD +300 / CZ +300 / NV +456.

    Take is ~1c better than nearest rec (BF) and worse than NV. Printed
    +0.81% is noise — nv_better and tied_rec both drop the plus card.
    """
    return 335, [
        _book("BF", 320),
        _book("FD", 300),
        _book("CZ", 300),
        _book("NV", 456),
    ]


def test_kill_plus335_nv_better_and_tied_rec_no_plus():
    """KILL: Kalshi +335 vs +320/+300/+300 and NV +456 must not print +0.81%."""
    kalshi, books = _fixture_kill_plus335_nv_better_tied_rec()
    assert is_novig_book("NV") is True
    assert is_junk_vs_kalshi(456, 335) is False
    assert novig_better_than_take(books, kalshi) is True
    out = evaluate_sharp_panel_ev(books, kalshi, min_sharp_books=3)
    assert "NV" in out["surviving_names"]
    assert {"BF", "FD", "CZ"}.issubset(set(out["surviving_names"]))
    assert "nv_better" in out["reasons"]
    assert "tied_rec" in out["reasons"]
    assert out["plus_alert"] is False
    assert out["ev_percent"] <= 0.0


def test_tied_rec_alone_does_not_kill_astros_keep():
    """Control: Kalshi −133 best, NV −139 ~1c worse. tied_rec must not fire alone."""
    kalshi, books = _fixture_keep_cws_hou_astros_ml()
    assert novig_better_than_take(books, kalshi) is False
    out = evaluate_sharp_panel_ev(books, kalshi, min_sharp_books=3)
    assert "nv_better" not in out["reasons"]
    assert "tied_rec" not in out["reasons"]
    assert out["plus_alert"] is True
    assert 1.0 <= out["ev_percent"] <= 5.5


def test_pickem_plus113_vs_minus110_survives_panel():
    """+113 vs −110 is pick'em juice, not an off-pack sign-flip."""
    books = [_book("A", -110), _book("B", 105), _book("C", -105)]
    surviving = filter_sharp_panel(books, kalshi_american=113)
    names = {b["name"] for b in surviving}
    assert names == {"A", "B", "C"}


def test_long_plus_tight_pack_two_way_power_not_fake_seven():
    """Astros −1.5-style long plus vs a tight pack must not invent 7–14% from raw implied."""
    kalshi = 355
    books = [_book("A", 320), _book("B", 340), _book("C", 345), _book("D", 310)]
    out = evaluate_sharp_panel_ev(books, kalshi, min_sharp_books=3)
    assert out["raw_ev_percent"] < 7.0
    assert out["ev_percent"] < 7.0
    assert "nv_better" not in out["reasons"]


def test_astros_ml_two_exchange_hides_card():
    """Kill: Astros ML take −204 vs BF −163 and NV −130 — 2 exchanges better."""
    take = -204
    books = [
        _book("Betfair Exchange", -163),
        _book("NoVig", -130),
        _book("FD", -200),
        _book("DK", -198),
        _book("CZ", -195),
    ]
    # NV −130 is >10c vs −204 so it drops from POWER, but still counts for hide.
    assert exchange_better_kill(books, take) is True
    out = evaluate_sharp_panel_ev(books, take, min_sharp_books=3)
    assert out["plus_alert"] is False
    assert "exchange_better" in out["reasons"]


def test_junk_nv_bf_does_not_trigger_exchange_kill():
    """Junk NV +317 / BF +567 vs a +144 take must not hide the card."""
    take = 144
    books = [
        _book("NoVig", 317),
        _book("Betfair Exchange", 567),
        _book("FD", 110),
        _book("DK", 115),
        _book("CZ", 100),
    ]
    assert is_junk_vs_kalshi(317, take) is True
    assert is_junk_vs_kalshi(567, take) is True
    assert exchange_better_kill(books, take) is False
    out = evaluate_sharp_panel_ev(books, take, min_sharp_books=3)
    assert "exchange_better" not in out["reasons"]
    assert out["plus_alert"] is True


def test_dk_fd_cz_better_only_still_prints():
    """Soft books better than take do not trigger the 2-exchange hide."""
    take = 144
    books = [
        _book("DK", 155),
        _book("FD", 150),
        _book("CZ", 135),
        _book("NoVig", 128),
        _book("Betfair Exchange", 120),
    ]
    assert exchange_better_kill(books, take) is False
    out = evaluate_sharp_panel_ev(books, take, min_sharp_books=3)
    assert "exchange_better" not in out["reasons"]


def test_twins_plus15_kalshi_plus144_keep():
    """Keep: Twins +1.5 Kalshi +144 — show the card."""
    take = 144
    books = [
        _book("FD", 110),
        _book("DK", 115),
        _book("CZ", 100),
        _book("NoVig", 90),
        _book("Betfair Exchange", 80),
    ]
    out = evaluate_sharp_panel_ev(books, take, min_sharp_books=3)
    assert exchange_better_kill(books, take) is False
    assert out["plus_alert"] is True
    assert out["ev_percent"] > 0.0


def test_junk_poly_minus455_vs_plus163_out_of_power():
    """Junk Poly −455 vs take +163 is out of POWER (sign-flip / >10c)."""
    take = 163
    books = [
        _book("FD", 156),
        _book("DK", 160),
        _book("NV", 150),
        _book("Polymarket", -455, 350),
    ]
    assert is_junk_vs_kalshi(-455, take) is True
    out = evaluate_sharp_panel_ev(books, take, min_sharp_books=3)
    assert all(not is_polymarket_book(n) for n in out["fair_names"])
    assert "Polymarket" not in out["fair_names"]
