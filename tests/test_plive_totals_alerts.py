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
    _two_way_pick_opp_decimals,
    total_line_value,
)
from odds_api_ws import OddsWsStore


DET_MIN_EID = 199295331
PLIVE_OVER = 1.892857
PLIVE_UNDER = 1.847458
# Parser/live shape: hdp only (max/line are copied on emit). Matcher must still join.
PLIVE_HDP_ONLY = {"hdp": 11.5, "over": PLIVE_OVER, "under": PLIVE_UNDER}
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
                    {"hdp": 12.5, "over": 2.47, "under": 1.502513},
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
    store = OddsWsStore()
    eid = DET_MIN_EID
    totals = {
        "name": "Totals",
        "odds": [dict(TOTALS_11), {"hdp": 12.5, "max": 12.5, "over": 2.47, "under": 1.502513}],
    }
    store.apply_message(
        {
            "type": "created",
            "seq": 1,
            "id": eid,
            "bookie": "PLive",
            "markets": [
                {"name": "ML", "odds": [{"home": 1.8, "away": 2.1}]},
                {"name": "Spread", "odds": [{"hdp": -1.5, "home": 1.91, "away": 1.91}]},
                totals,
            ],
        }
    )
    store.apply_message(
        {
            "type": "updated",
            "seq": 2,
            "id": eid,
            "bookie": "PLive",
            "markets": [{"name": "ML", "odds": [{"home": 1.75, "away": 2.15}]}],
        }
    )
    counts = store.market_family_counts()
    assert counts["totals"] >= 1
    assert counts["spread"] >= 1
    doc = store.merged_doc(eid)
    assert any(m.get("name") == "Totals" for m in doc["bookmakers"]["PLive"])
    pl_tot = next(m for m in doc["bookmakers"]["PLive"] if m.get("name") == "Totals")
    row = next(r for r in pl_tot["odds"] if abs(float(r.get("hdp") or r.get("max")) - 11.5) < 1e-9)
    fd = {"name": "Totals", "odds": [dict(ODDS_API_FAIR_11)]}
    assert _pick_matching_odds_row(fd, "Totals", row).get("max") == 11.5
    mon = _totals_monitor()
    alert_over = _plive_ou_alert(mon, "over", row)
    alert_under = _plive_ou_alert(mon, "under", row)
    assert alert_over is not None and alert_over.pick == "Over" and alert_over.qualifier == "11.5"
    assert alert_under is not None and alert_under.pick == "Under" and alert_under.qualifier == "11.5"
    assert alert_over.take_book == "PLive" and str(alert_over.ticker or "").startswith("PLIVE|")


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
