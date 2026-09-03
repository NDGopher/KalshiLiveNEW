"""PLive-take game totals: Over/Under cards with no Kalshi ticker."""
from __future__ import annotations

import pytest

from odds_ev_monitor import (
    OddsEVMonitor,
    _kalshi_scan_gameline_markets,
    _odds_doc_has_kalshi_tradable_gameline,
    _odds_doc_has_take_tradable_gameline,
    _pick_matching_odds_row,
    _pick_qualifier_line_for_side,
    _row_limit_hint,
    _row_passes_sharp_limit,
    _two_way_pick_opp_decimals,
    total_line_value,
)
from odds_api_ws import OddsWsStore


DET_MIN_EID = 199295331
PLIVE_OVER = 1.892857
PLIVE_UNDER = 1.847458
# Parser/live shape: hdp only (max/line are copied on emit). Matcher must still join.
PLIVE_HDP_ONLY = {
    "hdp": 11.5,
    "over": PLIVE_OVER,
    "under": PLIVE_UNDER,
    "plive_live": True,
    "plive_market": 5,
    "market_type": "game_total",
}
# Odds-API fair block the matcher actually reads — max/line, not hdp.
ODDS_API_FAIR_11 = {"max": 11.5, "line": 11.5, "over": 1.80, "under": 1.94}
TOTALS_11 = {"hdp": 11.5, "max": 11.5, "line": 11.5, "over": PLIVE_OVER, "under": PLIVE_UNDER}


def _rec_totals(over: float, under: float, *, with_hdp: bool = False) -> list:
    row = {"max": 11.5, "line": 11.5, "over": over, "under": under}
    if with_hdp:
        row["hdp"] = 11.5
    return [{"name": "Totals", "odds": [row]}]


def det_min_plive_totals_doc(*, include_kalshi: bool = False) -> dict:
    """DET@MIN market 5. PLive is hdp-only; recs are Odds-API max/line. No Kalshi ticker."""
    bks = {
        "PLive": [
            {
                "name": "Totals",
                "odds": [
                    dict(PLIVE_HDP_ONLY),
                    {
                        "hdp": 12.5,
                        "over": 2.47,
                        "under": 1.502513,
                        "plive_live": True,
                        "plive_market": 5,
                        "market_type": "game_total",
                    },
                ],
            }
        ],
        # Odds-API keys (max/line, no hdp). Wider than PLive −112/−118 so tight_cluster cannot kill.
        "FanDuel": _rec_totals(1.70, 1.68),
        "DraftKings": _rec_totals(1.69, 1.67),
        "NoVig": _rec_totals(1.68, 1.66),
        "Betfair Exchange": _rec_totals(1.72, 1.70),
    }
    if include_kalshi:
        bks["Kalshi"] = _rec_totals(1.81, 1.76)
    return {
        "id": DET_MIN_EID,
        "home": "Minnesota Twins",
        "away": "Detroit Tigers",
        "league": "MLB",
        "bookmakers": bks,
    }


def _totals_monitor() -> OddsEVMonitor:
    mon = OddsEVMonitor(auth_token=None)
    mon.set_filter(
        {
            "betTypes": ["GAMELINES"],
            "minRoi": 0,
            "devigFilter": {
                "sharps": ["FanDuel", "DraftKings", "NoVig", "Betfair Exchange"],
                "method": "POWER",
                "type": "AVERAGE",
                "minEv": 0,
                "minSharpBooks": 3,
                "hold": [{"book": "Any", "max": 20}],
            },
            "oddsRanges": [{"book": "Any", "min": -500, "max": 500}],
            "minLimits": [{"book": "Kalshi", "min": 0}],
            "minSharpLimits": [],
            "displayBooks": [
                "Kalshi",
                "FanDuel",
                "DraftKings",
                "NoVig",
                "Betfair Exchange",
                "PLive",
            ],
        }
    )
    return mon


def test_plive_hdp_joins_odds_api_max_line():
    """PLive {hdp, over, under} must pair to Odds-API {max, line, over, under}."""
    plive_mk = {"name": "Totals", "odds": [dict(PLIVE_HDP_ONLY)]}
    api_mk = {"name": "Totals", "odds": [dict(ODDS_API_FAIR_11)]}
    from_hdp = _pick_matching_odds_row(api_mk, "Totals", PLIVE_HDP_ONLY)
    assert from_hdp.get("over") == 1.80
    assert from_hdp.get("under") == 1.94
    assert from_hdp.get("max") == 11.5
    from_max = _pick_matching_odds_row(plive_mk, "Totals", ODDS_API_FAIR_11)
    assert from_max.get("over") == PLIVE_OVER
    assert from_max.get("under") == PLIVE_UNDER
    assert from_max.get("hdp") == 11.5
    assert total_line_value(PLIVE_HDP_ONLY) == 11.5
    assert total_line_value(ODDS_API_FAIR_11) == 11.5
    tw = _two_way_pick_opp_decimals(PLIVE_HDP_ONLY, "over")
    assert tw == (PLIVE_OVER, PLIVE_UNDER)
    tw_u = _two_way_pick_opp_decimals(PLIVE_HDP_ONLY, "under")
    assert tw_u == (PLIVE_UNDER, PLIVE_OVER)


def _plive_totals_vb(side: str, row: dict | None = None) -> dict:
    src = dict(row or PLIVE_HDP_ONLY)
    return {
        "event": {
            "home": "Minnesota Twins",
            "away": "Detroit Tigers",
            "league": "MLB",
        },
        "market": {"name": "Totals", **src},
        "betSide": side,
        "bookmakerOdds": {side: src[side]},
        "expectedValue": 0.0,
        "_live_broad_scan": True,
        "_ev_source": "plive_take",
        "_take_only": "PLive",
        "_scan_teams": "Detroit Tigers @ Minnesota Twins",
        "_scan_mname": "Totals",
        "_canonical_kalshi_row": src,
    }


def test_href_less_kalshi_totals_still_emit_plive_scan_sides():
    """Empty Kalshi href must not block PLive Over/Under scan rows."""
    mon = _totals_monitor()
    doc = det_min_plive_totals_doc()
    doc["bookmakers"]["Kalshi"] = [
        {"name": "Totals", "odds": [{"max": 11.5, "line": 11.5, "over": 1.81, "under": 1.76}]}
    ]
    vbs = mon.live_scan_value_bets_from_docs({DET_MIN_EID: doc})
    plive = [r for r in vbs if r.get("_take_only") == "PLive"]
    sides = {r.get("betSide") for r in plive}
    assert "over" in sides
    assert "under" in sides


def test_take_gate_keeps_plive_only_totals():
    doc = det_min_plive_totals_doc()
    assert "Kalshi" not in (doc["bookmakers"] or {})
    assert _odds_doc_has_take_tradable_gameline(doc, ("Kalshi",)) is False
    assert _odds_doc_has_kalshi_tradable_gameline(doc) is True
    assert _odds_doc_has_take_tradable_gameline(doc) is True
    names = [n for n, _ in _kalshi_scan_gameline_markets(doc["bookmakers"], "PLive")]
    assert "Totals" in names


def test_qualifier_over_under_includes_line():
    over, oq, ol = _pick_qualifier_line_for_side(
        "Minnesota Twins", "Detroit Tigers", "Totals", "over", TOTALS_11
    )
    under, uq, ul = _pick_qualifier_line_for_side(
        "Minnesota Twins", "Detroit Tigers", "Totals", "under", TOTALS_11
    )
    assert over == "Over"
    assert under == "Under"
    assert oq == "11.5"
    assert uq == "11.5"
    assert ol == 11.5
    assert ul == 11.5
    assert total_line_value({"hdp": 11.5}) == 11.5
    assert total_line_value({"max": 11.5}) == 11.5


def _plive_ou_alert(mon: OddsEVMonitor, side: str, row: dict):
    """Alert object from a PLive Totals row. No Kalshi ticker / KXMLB / KXSCAN."""
    from ev_calculator import decimal_to_american

    ev = {
        "home": "Minnesota Twins",
        "away": "Detroit Tigers",
        "league": "MLB",
    }
    pick, qual, line = _pick_qualifier_line_for_side(
        ev["home"], ev["away"], "Totals", side, row
    )
    dec = float(row[side])
    built = {
        "market": "Total Runs",
        "teams": "Detroit Tigers @ Minnesota Twins",
        "selection": pick,
        "line": line,
        "qualifier": qual,
        "odds": decimal_to_american(dec),
        "price": int(max(1, min(99, round(100.0 / dec)))),
        "ev": 3.5,
        "limit": 0,
        "fairOdds": None,
        "link": "",
        "displayBooks": {},
        "devigBooks": ["FanDuel", "DraftKings", "NoVig"],
        "ticker": f"PLIVE|Detroit Tigers @ Minnesota Twins|{pick}|{qual}",
        "take_book": "PLive",
        "strict_pass": False,
        "ev_source": "plive_take",
    }
    return mon.parse_bet_to_alert(built, ev)


def test_plive_take_over_under_alerts_without_kalshi_ticker():
    """Over 11.5 and Under 11.5 alert objects from PLive-take. No KXMLB / KXSCAN."""
    mon = _totals_monitor()
    doc = det_min_plive_totals_doc()
    ev = {
        "home": "Minnesota Twins",
        "away": "Detroit Tigers",
        "league": "MLB",
    }
    assert "Kalshi" not in (doc["bookmakers"] or {})
    vbs = mon.live_scan_value_bets_from_docs({DET_MIN_EID: doc})
    sides = {(r.get("betSide"), r.get("_take_only")) for r in vbs}
    assert ("over", "PLive") in sides
    assert ("under", "PLive") in sides
    over_vb = next(r for r in vbs if r.get("betSide") == "over")
    under_vb = next(r for r in vbs if r.get("betSide") == "under")
    assert over_vb.get("_canonical_kalshi_row", {}).get("hdp") == 11.5
    joined = _pick_matching_odds_row(
        doc["bookmakers"]["FanDuel"][0], "Totals", over_vb["_canonical_kalshi_row"]
    )
    assert joined.get("max") == 11.5
    assert joined.get("line") == 11.5
    # Two-way POWER cannot plus both sides of 1.89/1.85; objects still list.
    alert_over = _plive_ou_alert(mon, "over", PLIVE_HDP_ONLY)
    alert_under = _plive_ou_alert(mon, "under", PLIVE_HDP_ONLY)
    assert alert_over is not None
    assert alert_under is not None
    assert alert_over.pick == "Over"
    assert alert_under.pick == "Under"
    assert alert_over.qualifier == "11.5"
    assert alert_under.qualifier == "11.5"
    assert alert_over.take_book == "PLive"
    assert alert_under.take_book == "PLive"
    assert "KXMLB" not in str(alert_over.ticker or "")
    assert "KXSCAN" not in str(alert_over.ticker or "")
    assert str(alert_over.ticker or "").startswith("PLIVE|")

    try:
        from dashboard import is_unlisted_match_failed, listed_active_alerts
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"dashboard import failed: {exc}")

    listed = {
        "over": {
            "id": "plive-over-11.5",
            "ticker": alert_over.ticker,
            "take_book": "PLive",
            "match_failed": False,
            "ev_percent": float(alert_over.ev_percent or 0),
            "pick": alert_over.pick,
            "qualifier": alert_over.qualifier,
            "teams": alert_over.teams,
        },
        "under": {
            "id": "plive-under-11.5",
            "ticker": alert_under.ticker,
            "take_book": "PLive",
            "match_failed": False,
            "ev_percent": float(alert_under.ev_percent or 0),
            "pick": alert_under.pick,
            "qualifier": alert_under.qualifier,
            "teams": alert_under.teams,
        },
    }
    assert is_unlisted_match_failed(listed["over"]) is False
    assert is_unlisted_match_failed(listed["under"]) is False
    visible = listed_active_alerts(listed)
    picks = {(r["pick"], str(r["qualifier"])) for r in visible}
    assert ("Over", "11.5") in picks
    assert ("Under", "11.5") in picks


def test_ws_ml_only_update_keeps_totals_then_alert():
    """Odds-API WS must not ingest PLive. Live Totals stay on the Pandora book."""
    store = OddsWsStore()
    eid = DET_MIN_EID
    rec_totals = {"name": "Totals", "odds": [dict(ODDS_API_FAIR_11)]}
    store.apply_message(
        {
            "type": "created",
            "seq": 1,
            "id": eid,
            "bookie": "FanDuel",
            "markets": [
                {"name": "ML", "odds": [{"home": 1.8, "away": 2.1}]},
                {"name": "Spread", "odds": [{"hdp": -1.5, "home": 1.91, "away": 1.91}]},
                rec_totals,
            ],
        }
    )
    store.apply_message(
        {
            "type": "created",
            "seq": 2,
            "id": eid,
            "bookie": "PLive",
            "markets": [
                {"name": "ML", "odds": [{"home": 1.8, "away": 2.1}]},
                {"name": "Spread", "odds": [{"hdp": -1.5, "home": 1.91, "away": 1.91}]},
                {"name": "Totals", "odds": [dict(TOTALS_11)]},
            ],
        }
    )
    store.apply_message(
        {
            "type": "updated",
            "seq": 3,
            "id": eid,
            "bookie": "FanDuel",
            "markets": [{"name": "ML", "odds": [{"home": 1.75, "away": 2.15}]}],
        }
    )
    store.apply_message(
        {
            "type": "updated",
            "seq": 4,
            "id": eid,
            "bookie": "PLive",
            "markets": [{"name": "ML", "odds": [{"home": 1.75, "away": 2.15}]}],
        }
    )
    counts = store.market_family_counts()
    assert counts["totals"] >= 1
    assert counts["spread"] >= 1
    doc = store.merged_doc(eid)
    assert "PLive" not in doc["bookmakers"]
    assert any(m.get("name") == "Totals" for m in doc["bookmakers"]["FanDuel"])
    row = dict(TOTALS_11)
    row.update({"plive_live": True, "plive_market": 5, "market_type": "game_total"})
    fd = {"name": "Totals", "odds": [dict(ODDS_API_FAIR_11)]}
    assert _pick_matching_odds_row(fd, "Totals", row).get("max") == 11.5
    mon = _totals_monitor()
    alert_over = _plive_ou_alert(mon, "over", row)
    alert_under = _plive_ou_alert(mon, "under", row)
    assert alert_over is not None and alert_over.pick == "Over" and alert_over.qualifier == "11.5"
    assert alert_under is not None and alert_under.pick == "Under" and alert_under.qualifier == "11.5"
    assert alert_over.take_book == "PLive" and str(alert_over.ticker or "").startswith("PLIVE|")


def test_merged_doc_after_ml_only_update_emits_over_card():
    """Required success test: full ML+Spread+Totals snapshot, then ML-only
    ``updated``. Totals stay in the store; an Over/Under alert is still
    buildable from merged bookmakers. Subscribe string is not this test.
    Rec O−110/U−110, take O +105 → Quant-3 +2.50% KEEP.
    """
    from ev_calculator import american_to_decimal, is_plus_print_ev

    store = OddsWsStore()
    eid = DET_MIN_EID
    rec_ou = {
        "max": 11.5,
        "line": 11.5,
        "over": american_to_decimal(-110),
        "under": american_to_decimal(-110),
    }
    take_ou = {
        "hdp": 11.5,
        "max": 11.5,
        "line": 11.5,
        "over": american_to_decimal(105),
        "under": american_to_decimal(-120),
    }
    ml = {"name": "ML", "odds": [{"home": 1.8, "away": 2.1}]}
    spread = {"name": "Spread", "odds": [{"hdp": -1.5, "home": 1.91, "away": 1.91}]}
    rec_books = ("FanDuel", "DraftKings", "NoVig")
    for bookie in rec_books:
        store.apply_message(
            {
                "type": "created",
                "seq": 1,
                "id": eid,
                "bookie": bookie,
                "markets": [
                    dict(ml),
                    dict(spread),
                    {"name": "Totals", "odds": [dict(rec_ou)]},
                ],
            }
        )
    store.apply_message(
        {
            "type": "created",
            "seq": 2,
            "id": eid,
            "bookie": "PLive",
            "markets": [
                dict(ml),
                dict(spread),
                {"name": "Totals", "odds": [dict(take_ou)]},
            ],
        }
    )
    store.apply_message(
        {
            "type": "updated",
            "seq": 3,
            "id": eid,
            "bookie": "PLive",
            "markets": [{"name": "ML", "odds": [{"home": 1.75, "away": 2.15}]}],
        }
    )
    store.apply_message(
        {
            "type": "updated",
            "seq": 4,
            "id": eid,
            "bookie": "FanDuel",
            "markets": [{"name": "ML", "odds": [{"home": 1.82, "away": 2.08}]}],
        }
    )
    doc = store.merged_doc(eid)
    doc["home"] = "Minnesota Twins"
    doc["away"] = "Detroit Tigers"
    doc["league"] = "MLB"
    # PLive is local-only. Odds-API WS never stores it. Live coeffs attach here.
    live_take = dict(take_ou)
    live_take.update({"plive_live": True, "plive_market": 5, "market_type": "game_total"})
    doc["bookmakers"]["PLive"] = [
        {"name": "ML", "odds": [{**ml["odds"][0], "plive_live": True, "plive_market": 3}]},
        {"name": "Spread", "odds": [{**spread["odds"][0], "plive_live": True, "plive_market": 6}]},
        {"name": "Totals", "odds": [live_take]},
    ]
    names = {bk: [m.get("name") for m in mk] for bk, mk in doc["bookmakers"].items()}
    assert "PLive" not in store.merged_doc(eid)["bookmakers"]
    assert "Totals" in names["PLive"]
    assert "Spread" in names["PLive"]
    assert "Totals" in names["FanDuel"]
    mon = _totals_monitor()
    alerts = mon.alerts_from_live_scan_docs({eid: doc})
    plus = [
        a
        for a in alerts
        if is_plus_print_ev(getattr(a, "ev_percent", None))
        and str(getattr(a, "take_book", "")).lower() == "plive"
    ]
    over = [a for a in plus if str(a.pick).lower() == "over" and str(a.qualifier) == "11.5"]
    under = [a for a in plus if str(a.pick).lower() == "under" and str(a.qualifier) == "11.5"]
    assert over, f"expected Over 11.5 plus card from merged doc, got {[(a.pick, a.qualifier, a.ev_percent) for a in alerts]}"
    assert over[0].take_book == "PLive"
    assert str(over[0].ticker or "").startswith("PLIVE|")
    assert float(over[0].ev_percent) > 0
    assert not under, "both Over and Under must not both print plus"
    try:
        from dashboard import live_odds_board_rows_from_bookmakers
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"dashboard import failed: {exc}")
    rows = live_odds_board_rows_from_bookmakers(
        event_id=eid,
        home="Minnesota Twins",
        away="Detroit Tigers",
        teams="Detroit Tigers @ Minnesota Twins",
        league="MLB",
        sport_slug="baseball",
        live=True,
        status="live",
        start_display="",
        bks=doc["bookmakers"],
        books=["FanDuel", "DraftKings", "NoVig", "PLive"],
    )
    markets = [r.get("market") for r in rows]
    assert "ML" in markets
    assert "Spread" in markets
    assert "Totals" in markets


def test_live_odds_emits_totals_and_spread_in_addition_to_ml():
    try:
        from dashboard import live_odds_board_rows_from_bookmakers
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"dashboard import failed: {exc}")

    bks = {
        "FanDuel": [
            {"name": "ML", "odds": [{"home": 1.8, "away": 2.1}]},
            {"name": "Spread", "odds": [{"hdp": -1.5, "home": 1.91, "away": 1.91}]},
            {"name": "Totals", "odds": [dict(TOTALS_11)]},
            {"name": "Team Total", "odds": [{"hdp": 5.5, "over": 1.22, "under": 3.75}]},
        ],
        "PLive": [
            {"name": "ML", "odds": [{"home": 1.85, "away": 2.05}]},
            {"name": "Totals", "odds": [dict(TOTALS_11)]},
        ],
    }
    rows = live_odds_board_rows_from_bookmakers(
        event_id=DET_MIN_EID,
        home="Minnesota Twins",
        away="Detroit Tigers",
        teams="Detroit Tigers @ Minnesota Twins",
        league="MLB",
        sport_slug="baseball",
        live=True,
        status="live",
        start_display="",
        bks=bks,
        books=["FanDuel", "PLive"],
    )
    markets = [r.get("market") for r in rows]
    assert "ML" in markets
    assert "Spread" in markets
    assert "Totals" in markets
    assert "Team Total" not in markets
    tot = next(r for r in rows if r.get("market") == "Totals")
    assert tot.get("side_a") == "Over 11.5"
    assert tot.get("side_b") == "Under 11.5"
    assert tot.get("line") == 11.5
    pl = (tot.get("books") or {}).get("PLive") or {}
    assert pl.get("away_am") is not None  # Over in first/away slot
    assert pl.get("home_am") is not None  # Under in second/home slot
    spr = next(r for r in rows if r.get("market") == "Spread")
    assert "5.5" not in str(spr.get("side_a") or "")
    assert spr.get("line") == -1.5


def test_totals_max_is_line_not_liquidity():
    """MLB Totals ``max``=11.5 must not fail FanDuel minSharpLimits=200."""
    from ev_calculator import american_to_decimal

    row = {"max": 11.5, "line": 11.5, "over": american_to_decimal(-140), "under": american_to_decimal(120)}
    assert _row_limit_hint(row) is None
    assert _row_passes_sharp_limit(row, "FanDuel", [{"book": "FanDuel", "min": 200}]) is True
    assert _row_passes_sharp_limit(row, "DraftKings", [{"book": "DraftKings", "min": 200}]) is True
    ml = {"home": 1.9, "away": 2.0, "max": 500}
    assert _row_limit_hint(ml) == 500.0


def det_min_plive_ou_plus_doc() -> dict:
    """DET@MIN market 5: PLive hdp-only 11.5, no Kalshi Totals, DK/FD/CZ max/line."""
    from ev_calculator import american_to_decimal

    rec = {
        "max": 11.5,
        "line": 11.5,
        "over": american_to_decimal(-140),
        "under": american_to_decimal(120),
    }
    return {
        "id": DET_MIN_EID,
        "home": "Minnesota Twins",
        "away": "Detroit Tigers",
        "league": "MLB",
        "bookmakers": {
            "Kalshi": [{"name": "ML", "odds": [{"home": 1.8, "away": 2.1}]}],
            "PLive": [
                {"name": "ML", "odds": [{"home": 1.85, "away": 2.05}]},
                {"name": "Spread", "odds": [{"hdp": -1.5, "home": 1.91, "away": 1.91}]},
                {
                    "name": "Totals",
                    "odds": [dict(PLIVE_HDP_ONLY)],
                },
                {
                    "name": "Team Total",
                    "odds": [{"hdp": 5.5, "over": 1.22, "under": 3.75, "plive_market": 7}],
                },
            ],
            "FanDuel": [{"name": "Totals", "odds": [dict(rec)]}],
            "DraftKings": [{"name": "Totals", "odds": [dict(rec)]}],
            "Caesars": [{"name": "Totals", "odds": [dict(rec)]}],
        },
    }


def _prod_totals_monitor() -> OddsEVMonitor:
    """minSharp=3 and production-like minSharpLimits (FD/DK 200). Hold 8%."""
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
                "hold": [{"book": "Any", "max": 8}],
            },
            "oddsRanges": [{"book": "Any", "min": -500, "max": 500}],
            "minLimits": [{"book": "Kalshi", "min": 75}],
            "minSharpLimits": [
                {"book": "FanDuel", "min": 200},
                {"book": "DraftKings", "min": 200},
                {"book": "Caesars", "min": 0},
            ],
            "displayBooks": ["Kalshi", "FanDuel", "DraftKings", "Caesars", "PLive"],
        }
    )
    return mon


def test_det_min_plive_ou_listed_without_kalshi_ticker():
    """Fixture: DET@MIN PLive 11.5 over 1.893 / under 1.847, href="", minSharp=3.

    No Kalshi Totals block. DK/FD Totals (max/line) must still enter POWER.
    Listed Over card take=PLive, not alert_match_failed, not Spread.
    find_submarket is never called.
    """
    from ev_calculator import is_plus_print_ev

    doc = det_min_plive_ou_plus_doc()
    assert "Kalshi" in doc["bookmakers"]
    assert not any(m.get("name") == "Totals" for m in doc["bookmakers"]["Kalshi"])
    pl_row = doc["bookmakers"]["PLive"][2]["odds"][0]
    assert pl_row == PLIVE_HDP_ONLY
    assert "max" not in pl_row and "line" not in pl_row
    assert total_line_value(pl_row) == 11.5
    pick, qual, line = _pick_qualifier_line_for_side(
        "Minnesota Twins", "Detroit Tigers", "Totals", "over", pl_row
    )
    assert pick == "Over" and qual == "11.5" and line == 11.5
    joined = _pick_matching_odds_row(doc["bookmakers"]["FanDuel"][0], "Totals", pl_row)
    assert joined.get("max") == 11.5
    tw = _two_way_pick_opp_decimals(pl_row, "over")
    assert tw == (PLIVE_OVER, PLIVE_UNDER)

    mon = _prod_totals_monitor()
    vbs = mon.live_scan_value_bets_from_docs({DET_MIN_EID: doc})
    plive_ou = [
        r
        for r in vbs
        if r.get("_take_only") == "PLive" and r.get("_scan_mname") == "Totals"
    ]
    sides = {r.get("betSide") for r in plive_ou}
    assert "over" in sides and "under" in sides
    assert all((r.get("bookmakerOdds") or {}).get("href") == "" for r in plive_ou)
    assert all(r.get("_canonical_kalshi_row", {}).get("hdp") == 11.5 for r in plive_ou)

    over_vb = next(r for r in plive_ou if r.get("betSide") == "over")
    assert mon._value_bet_to_normalized_bet(over_vb, doc, take_book="Kalshi") is None
    built = mon._value_bet_to_normalized_bet(over_vb, doc, take_book="PLive")
    assert built is not None
    assert built["take_book"] == "PLive"
    assert built["selection"] == "Over"
    assert built["qualifier"] == "11.5"
    assert built["market"] == "Total Runs"
    assert built.get("link") in ("", None)
    assert str(built.get("ticker") or "").startswith("PLIVE|")
    assert "KXMLB" not in str(built.get("ticker") or "")

    alerts = mon.alerts_from_live_scan_docs({DET_MIN_EID: doc})
    plus = [a for a in alerts if is_plus_print_ev(getattr(a, "ev_percent", None))]
    over = [
        a
        for a in plus
        if str(a.pick).lower() == "over"
        and str(a.qualifier) == "11.5"
        and str(getattr(a, "take_book", "")).lower() == "plive"
    ]
    assert over, [(a.pick, a.qualifier, a.ev_percent, a.take_book, a.market_type) for a in alerts]
    assert over[0].market_type == "Total Runs"
    assert "spread" not in str(over[0].market_type).lower()
    assert str(over[0].ticker or "").startswith("PLIVE|")

    try:
        import asyncio

        import dashboard as dash
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"dashboard import failed: {exc}")

    called = []

    async def _forbidden_find_submarket(*_a, **_k):
        called.append(True)
        raise AssertionError("PLive O/U must not go through find_submarket")

    class _NoMatchClient:
        find_submarket = staticmethod(_forbidden_find_submarket)

    dash.active_alerts.clear()
    dash.selected_dashboard_filters = []
    dash.dashboard_min_ev = 0.0
    prev_client = dash.kalshi_client
    dash.kalshi_client = _NoMatchClient()
    try:
        asyncio.run(dash.handle_new_alert(over[0]))
    finally:
        dash.kalshi_client = prev_client
    assert called == []
    with dash.app.test_client() as client:
        resp = client.get("/api/alerts")
        assert resp.status_code == 200
        body = resp.get_json()
        listed = body.get("alerts") or []
        ou = [
            a
            for a in listed
            if a.get("pick") == "Over" and str(a.get("qualifier")) == "11.5"
        ]
        assert ou, listed
        card = ou[0]
        assert card.get("take_book") == "PLive"
        assert card.get("match_failed") is False
        assert dash.is_unlisted_match_failed(card) is False
        assert "spread" not in str(card.get("market_type") or "").lower()
        assert str(card.get("ticker") or "").startswith("PLIVE|")
        assert "KXMLB" not in str(card.get("ticker") or "")
