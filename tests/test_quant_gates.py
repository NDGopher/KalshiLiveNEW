"""Quant EV spec — PR gate. Formulas and numbered tests 1–9.

Do not invent Royals/Twins sister prices to reprint 9.59%/5.09%.
Auto-bet stays OFF. No network. Anonymous boards.
"""
from __future__ import annotations

from ev_calculator import (
    american_to_decimal,
    count_better_exchanges,
    evaluate_sharp_panel_ev,
    ev_percent_vs_take_american,
    filter_sharp_panel,
    implied_prob_from_american,
    is_junk_vs_kalshi,
    is_polymarket_book,
    is_real_sign_flip,
    two_way_power_ev,
    two_way_power_fair,
)
from odds_ev_monitor import (
    _pick_qualifier_line_for_side,
    _two_way_pick_opp_decimals,
    side_handicap,
    side_signed_line,
)
from tests.test_sharp_panel_filter import _book
from tests.test_zero_ev_totals_flood import _am, _ou_monitor


def _plus_only_mean_ev(picks: list[int], take: int) -> float:
    """Forbidden estimator: mean of pick implieds, no sister POWER."""
    mean = sum(implied_prob_from_american(int(a)) for a in picks) / float(len(picks))
    ev = ev_percent_vs_take_american(mean, int(take))
    assert ev is not None
    return float(ev)


def _totals_ou_row(over_am: int, under_am: int, line: float = 8.5) -> dict:
    """Totals row uses Over/Under keys only — never ML home/away."""
    return {
        "hdp": line,
        "max": line,
        "line": line,
        "over": _am(over_am),
        "under": _am(under_am),
    }


def _totals_rec_book(name: str, over_am: int, under_am: int) -> dict:
    """Build a POWER book from Totals Over/Under keys (not ML home/away)."""
    row = _totals_ou_row(over_am, under_am)
    assert "home" not in row and "away" not in row
    tw = _two_way_pick_opp_decimals(row, "over")
    assert tw is not None
    pick_d, opp_d = tw
    return {
        "name": name,
        "american": int(over_am),
        "decimal_pick": pick_d,
        "decimal_opp": opp_d,
    }


def _totals_take_doc(take_over: int, take_under: int = -125) -> dict:
    rec = _totals_ou_row(-110, -110)
    take = _totals_ou_row(take_over, take_under)
    return {
        "id": 199300003,
        "home": "Team Home",
        "away": "Team Away",
        "league": "MLB",
        "bookmakers": {
            "PLive": [{"name": "Totals", "odds": [take]}],
            "FanDuel": [{"name": "Totals", "odds": [rec]}],
            "DraftKings": [{"name": "Totals", "odds": [rec]}],
            "Caesars": [{"name": "Totals", "odds": [rec]}],
        },
    }


def _totals_over_vb(take_over: int, take_under: int = -125) -> dict:
    take = _totals_ou_row(take_over, take_under)
    return {
        "event": {"home": "Team Home", "away": "Team Away", "league": "MLB"},
        "market": {"name": "Totals", **take},
        "betSide": "over",
        "bookmakerOdds": {"over": take["over"], "under": take["under"]},
        "expectedValue": 0.0,
        "_live_broad_scan": True,
        "_take_only": "PLive",
        "_scan_teams": "Team Away @ Team Home",
        "_scan_mname": "Totals",
        "_canonical_kalshi_row": take,
    }


def test_american_odds_spec():
    """a>0 d=1+a/100; a<0 d=1+100/|a|; p=1/d. Strictly better = higher d."""
    assert american_to_decimal(300) == 4.0
    assert american_to_decimal(-300) == 1.0 + 100.0 / 300.0
    assert abs(implied_prob_from_american(300) - 0.25) < 1e-12
    assert abs(american_to_decimal(-204) - 1.490) < 0.001
    assert abs(american_to_decimal(-163) - 1.613) < 0.001
    assert abs(american_to_decimal(-130) - 1.769) < 0.001


def test_1_two_way_near_even_not_plus_only():
    ev = two_way_power_ev(299, -300, 300)
    assert ev is not None
    assert -1.0 <= ev <= 1.0
    # Plus-only mean implied of +228/+234/+240 vs +300 is ~+19.8% and must not emit.
    plus_only = _plus_only_mean_ev([228, 234, 240], 300)
    assert plus_only is not None and plus_only > 18.0
    assert ev < 2.0
    assert abs(ev - plus_only) > 10.0
    # Missing sister → no EV (Brewers fake +14% path).
    assert two_way_power_ev(228, 0, 300) is None
    pack = evaluate_sharp_panel_ev(
        [
            _book("FanDuel", 299, -300),
            _book("DraftKings", 299, -300),
            _book("Caesars", 299, -300),
        ],
        300,
        min_sharp_books=3,
    )
    assert pack["plus_alert"] is False or abs(pack["ev_percent"]) <= 1.0
    assert pack["ev_percent"] < 2.0


def test_2_pickem_minus110_vs_plus113():
    ev = two_way_power_ev(-110, -110, 113)
    assert ev is not None
    assert abs(ev - 6.50) < 0.1
    gap = abs(implied_prob_from_american(-110) - implied_prob_from_american(113))
    assert abs(gap - 0.054) < 0.005
    assert is_real_sign_flip(-110, 113) is False
    assert is_junk_vs_kalshi(-110, 113) is False
    # Not the one-sided +13% fake.
    raw = ev_percent_vs_take_american(implied_prob_from_american(-110), 113)
    assert raw is not None
    assert abs(raw - 13.0) > 1.0
    assert abs(ev - 13.0) > 1.0


def test_3_totals_two_way_keep_and_no_plus():
    """TOTALS first-class: Over/Under keys, not ML home/away."""
    rec = _totals_ou_row(-110, -110)
    assert set(rec) >= {"over", "under"}
    assert "home" not in rec and "away" not in rec
    tw = _two_way_pick_opp_decimals(rec, "over")
    assert tw is not None
    assert tw == (_am(-110), _am(-110))
    assert _two_way_pick_opp_decimals({"home": _am(-110), "away": _am(-110)}, "over") is None
    assert _two_way_pick_opp_decimals(rec, "home") is None

    keep_unit = two_way_power_ev(-110, -110, 105)
    kill_unit = two_way_power_ev(-110, -110, -105)
    assert keep_unit is not None and abs(keep_unit - 2.50) < 0.1
    assert kill_unit is not None and abs(kill_unit + 2.38) < 0.1

    recs = [
        _totals_rec_book("FanDuel", -110, -110),
        _totals_rec_book("DraftKings", -110, -110),
        _totals_rec_book("Caesars", -110, -110),
    ]
    keep_panel = evaluate_sharp_panel_ev(recs, 105, min_sharp_books=3)
    kill_panel = evaluate_sharp_panel_ev(recs, -105, min_sharp_books=3)
    assert abs(keep_panel["ev_percent"] - 2.50) < 0.1
    assert keep_panel["plus_alert"] is True
    assert abs(kill_panel["ev_percent"] + 2.38) < 0.1
    assert kill_panel["plus_alert"] is False

    mon = _ou_monitor()
    payload = dict(mon.filter_payload)
    df = dict(payload.get("devigFilter") or {})
    df["sharps"] = ["FanDuel", "DraftKings", "Caesars"]
    payload["devigFilter"] = df
    mon.set_filter(payload)

    keep_doc = _totals_take_doc(105)
    keep_built = mon._value_bet_to_normalized_bet(
        _totals_over_vb(105), keep_doc, take_book="PLive"
    )
    assert keep_built is not None
    assert keep_built["market"] == "Total Runs"
    assert keep_built["selection"] == "Over"
    assert keep_built["qualifier"] == "8.5"
    assert abs(float(keep_built["ev"]) - 2.50) < 0.1

    keep_alerts = mon.alerts_from_live_scan_docs({int(keep_doc["id"]): keep_doc})
    over_keep = [
        a
        for a in keep_alerts
        if str(a.pick).lower() == "over" and str(a.qualifier) == "8.5"
    ]
    assert over_keep
    assert abs(float(over_keep[0].ev_percent) - 2.50) < 0.1
    assert str(over_keep[0].market_type) == "Total Runs"

    kill_doc = _totals_take_doc(-105)
    kill_built = mon._value_bet_to_normalized_bet(
        _totals_over_vb(-105), kill_doc, take_book="PLive"
    )
    assert kill_built is None
    kill_alerts = mon.alerts_from_live_scan_docs({int(kill_doc["id"]): kill_doc})
    assert not any(
        str(a.pick).lower() == "over" and float(getattr(a, "ev_percent", 0) or 0) > 0
        for a in kill_alerts
    )


def test_4_worse_favorite_not_fake_fourteen():
    ev = two_way_power_ev(-113, -106, -122)
    assert ev is not None
    assert abs(ev + 7.53) < 0.15
    assert ev < 0
    raw = ev_percent_vs_take_american(implied_prob_from_american(-113), -122)
    assert raw is not None
    assert abs(raw + 3.46) < 0.15
    # max(POWER, raw_mean) of a worse favorite must not print +14%.
    assert max(ev, raw) < 0
    assert abs(ev - 14.0) > 10.0
    panel = evaluate_sharp_panel_ev(
        [
            _book("FanDuel", -113, -106),
            _book("DraftKings", -113, -106),
            _book("Caesars", -113, -106),
        ],
        -122,
        min_sharp_books=3,
    )
    assert panel["plus_alert"] is False
    assert panel["ev_percent"] <= 0


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
    assert "two_exchange" in hide["reasons"]
    assert hide["exchange_better"] >= 2
    assert "nv_better" not in hide["reasons"]
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
    assert is_junk_vs_kalshi(317, 163) is True
    assert is_junk_vs_kalshi(567, 163) is True
    assert count_better_exchanges(
        [_book("NoVig", 317), _book("Betfair Exchange", 567)],
        163,
    ) == 0
    assert "two_exchange" not in no_hide["reasons"]
    assert "nv_better" not in no_hide["reasons"]


def test_6_poly_junk_vs_on_pack():
    dp = abs(implied_prob_from_american(-455) - implied_prob_from_american(163))
    assert abs(dp - 0.440) < 0.005
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
    # Do not invert −455 to the sister +350 as the pick.
    inverted = two_way_power_ev(350, -455, 163)
    if inverted is not None:
        assert junk.get("raw_ev_percent") != inverted
        assert junk.get("ev_percent") != inverted

    on_pack = filter_sharp_panel(
        [_book("Poly", 118, -140), _book("FanDuel", 116), _book("DraftKings", 105)],
        kalshi_american=163,
    )
    assert any(is_polymarket_book(b.get("name")) for b in on_pack)
    assert is_junk_vs_kalshi(118, 163) is False


def test_7_plive_on_pack_may_paint():
    """PLive +118 vs take +163: |Δp|=7.8c same sign → on-pack, may paint, not gray."""
    dp = abs(implied_prob_from_american(118) - implied_prob_from_american(163))
    assert abs(dp - 0.078) < 0.005
    assert is_real_sign_flip(118, 163) is False
    assert is_junk_vs_kalshi(118, 163) is False
    on_pack = filter_sharp_panel(
        [_book("PLive", 118, -138), _book("FanDuel", 116, -136)],
        kalshi_american=163,
    )
    assert any(b.get("name") == "PLive" for b in on_pack)
    out = evaluate_sharp_panel_ev(
        [
            _book("Betfair Exchange", 134, -154),
            _book("FanDuel", 116, -136),
            _book("Caesars", 110, -130),
            _book("PLive", 118, -138),
            _book("Poly", -455, 350),
        ],
        163,
        min_sharp_books=3,
        take_book="Kalshi",
    )
    assert "PLive" in out["surviving_names"]
    assert "PLive" not in out["fair_names"]
    assert "Poly" not in out["fair_names"]

    from tests.test_tile_paint import _eval_paint

    data = _eval_paint()
    if data is not None:
        pl = data["royals"]["PLive"]
        assert pl["skip"] is False
        assert pl["take"] is False
        assert pl == {"skip": False, "take": False, "better": False}


def test_8_tigers_away_sign_and_plive_not_best():
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
        painted_side_hdp=-1.5,
        kalshi_side_hdp=-1.5,
        rec_side_hdp=-1.5,
    )
    assert out["plus_alert"] is False
    assert out["ev_percent"] <= 0
    assert "plive_not_best" in out["reasons"]
    assert "nv_better" not in out["reasons"]
    assert "two_exchange" not in out["reasons"]
    assert count_better_exchanges(
        [
            _book("Bet365", 475, -650),
            _book("NoVig", 525, -750),
            _book("Caesars", 333, -430),
            _book("DraftKings", 307, -390),
            _book("Kalshi", 317, -410),
        ],
        369,
    ) == 1


def test_missing_sister_no_ev():
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


def test_9_royals_twins_live_bands_two_way_not_plus_only():
    """Kalshi longest vs ≥5 same-sign two-ways → KEEP few-percent to ~10%.

    Sisters are ordinary juice pairs so POWER can run. Do not tune them to
    reprint 9.59%/5.09%. Plus-only on the pick pluses must not be the EV.
    """
    royals_recs = [
        _book("Betfair Exchange", 134, -154),
        _book("DraftKings", 105, -125),
        _book("FanDuel", 116, -136),
        _book("Bet365", 100, -120),
        _book("Caesars", 110, -130),
    ]
    royals = evaluate_sharp_panel_ev(
        royals_recs + [_book("PLive", 118, -138), _book("Poly", -455, 350)],
        163,
        min_sharp_books=3,
        take_book="Kalshi",
    )
    twins_recs = [
        _book("DraftKings", 170, -200),
        _book("FanDuel", 175, -210),
        _book("Caesars", 168, -198),
        _book("Bet365", 165, -195),
        _book("NoVig", 180, -220),
    ]
    twins = evaluate_sharp_panel_ev(twins_recs, 203, min_sharp_books=3)

    assert len(royals_recs) >= 5
    assert len(twins_recs) >= 5
    assert all(int(b["american"]) > 0 for b in royals_recs)
    assert all(int(b["american"]) > 0 for b in twins_recs)
    assert all(int(b["american"]) < 163 for b in royals_recs)
    assert all(int(b["american"]) < 203 for b in twins_recs)
    assert all(float(b["decimal_opp"]) > 1.0 for b in royals_recs + twins_recs)

    assert royals["plus_alert"] is True
    assert twins["plus_alert"] is True
    assert "PLive" not in royals["fair_names"]
    assert "Poly" not in royals["fair_names"]
    assert royals["autobet_allow"] is True
    # Few-percent to ~10%. Not the live 9.59/5.09 reprint, not plus-only teens.
    assert 1.0 <= royals["ev_percent"] <= 10.5
    assert 1.0 <= twins["ev_percent"] <= 10.5

    royals_plus_only = _plus_only_mean_ev([134, 105, 116, 100, 110], 163)
    twins_plus_only = _plus_only_mean_ev([170, 175, 168, 165, 180], 203)
    assert royals_plus_only > 18.0
    assert twins_plus_only > royals["ev_percent"] or twins_plus_only > twins["ev_percent"]
    assert abs(royals["ev_percent"] - royals_plus_only) > 5.0
    assert abs(twins["ev_percent"] - twins_plus_only) > 2.0
    assert royals["ev_percent"] < royals_plus_only
    assert twins["ev_percent"] < twins_plus_only


def test_auto_bet_stays_off():
    import dashboard as dash

    assert dash.auto_bet_enabled is False
