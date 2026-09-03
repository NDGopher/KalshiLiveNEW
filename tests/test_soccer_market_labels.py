"""Soccer gameline labels and 1X2 ML guardrails."""
from __future__ import annotations

from odds_ev_monitor import (
    OddsEVMonitor,
    _gameline_scan_sides,
    _ml_market_is_three_way,
    _soccer_ml_home_away_suppressed,
    _two_way_pick_opp_decimals,
    gameline_market_type_for_alert,
    gameline_totals_market_type,
)


def test_gameline_totals_label_soccer_vs_baseball():
    soccer_ev = {"sport": {"slug": "football"}}
    assert gameline_totals_market_type(ev=soccer_ev) == "Total Goals"
    assert gameline_totals_market_type(league="MLB") == "Total Runs"
    assert gameline_totals_market_type(league="NBA") == "Total Points"
    assert (
        gameline_market_type_for_alert("Totals", league="Saudi Pro League", ev=soccer_ev)
        == "Total Goals"
    )
    assert gameline_market_type_for_alert("Totals", league="MLB") == "Total Runs"


def test_soccer_under_totals_card_uses_total_goals_and_two_way_ev():
    """Stephen-style soccer totals: Under 2.5 stays valid two-way POWER (not 1X2)."""
    mon = OddsEVMonitor(auth_token=None)
    mon.set_filter(
        {
            "betTypes": ["GAMELINES"],
            "minRoi": 0,
            "devigFilter": {
                "sharps": ["Betfair Exchange", "Bet365", "FanDuel"],
                "method": "POWER",
                "type": "AVERAGE",
                "minEv": 0,
                "minSharpBooks": 2,
                "hold": [{"book": "Any", "max": 20}],
            },
            "oddsRanges": [{"book": "Any", "min": -500, "max": 500}],
            "displayBooks": ["PLive", "Betfair Exchange", "Bet365", "FanDuel", "Kalshi"],
        }
    )
    totals_row = {
        "max": 2.5,
        "line": 2.5,
        "hdp": 2.5,
        "over": 2.15,
        "under": 2.05,
    }
    sharp_under = 1.72
    doc = {
        "home": "Al-Fayha FC",
        "away": "Al-Kholood",
        "sport": {"slug": "football"},
        "league": {"name": "Saudi Pro League"},
        "bookmakers": {
            "PLive": [{"name": "Totals", "odds": [totals_row]}],
            "Betfair Exchange": [{"name": "Totals", "odds": [{"over": 2.05, "under": sharp_under, "max": 2.5}]}],
            "Bet365": [{"name": "Totals", "odds": [{"over": 2.08, "under": 1.74, "max": 2.5}]}],
            "FanDuel": [{"name": "Totals", "odds": [{"over": 2.06, "under": 1.73, "max": 2.5}]}],
        },
    }
    vb = {
        "event": {"home": "Al-Fayha FC", "away": "Al-Kholood", "league": "Saudi Pro League", "sport": {"slug": "football"}},
        "market": {"name": "Totals", **totals_row},
        "betSide": "under",
        "bookmakerOdds": {"under": totals_row["under"], "over": totals_row["over"]},
        "_live_broad_scan": True,
        "_ev_source": "plive_take",
        "_take_only": "PLive",
        "_canonical_kalshi_row": totals_row,
    }
    built = mon._value_bet_to_normalized_bet(vb, doc, take_book="PLive")
    assert built is not None
    assert built["market"] == "Total Goals"
    assert built["selection"] == "Under"
    assert float(built["qualifier"]) == 2.5
    assert float(built["ev"]) > 0.0
    tw = _two_way_pick_opp_decimals(totals_row, "under")
    assert tw is not None


def test_ml_row_with_draw_rejects_two_way_home_away():
    row = {"home": 2.10, "draw": 3.40, "away": 3.50}
    assert _two_way_pick_opp_decimals(row, "home") is None
    assert _two_way_pick_opp_decimals(row, "away") is None
    assert _two_way_pick_opp_decimals({"over": 1.9, "under": 1.95}, "under") is not None


def test_soccer_ml_home_suppressed_when_sharps_have_draw():
    ev = {"sport": {"slug": "football"}, "home": "A", "away": "B"}
    bks = {
        "Bet365": [{"name": "ML", "odds": [{"home": 2.0, "draw": 3.2, "away": 3.8}]}],
        "FanDuel": [{"name": "ML", "odds": [{"home": 2.05, "draw": 3.1, "away": 3.7}]}],
    }
    assert _ml_market_is_three_way(bks, "ML") is True
    assert _soccer_ml_home_away_suppressed(ev, "ML", "home", bks, None) is True
    assert _soccer_ml_home_away_suppressed(ev, "ML", "away", bks, None) is True
    assert _soccer_ml_home_away_suppressed(ev, "ML", "draw", bks, None) is False


def test_live_scan_skips_soccer_ml_home_away_when_draw_priced():
    doc = {
        "id": 9001,
        "home": "Arsenal",
        "away": "Chelsea",
        "sport": {"slug": "football"},
        "bookmakers": {
            "Kalshi": [
                {
                    "name": "ML",
                    "odds": [{"home": 2.0, "draw": 3.3, "away": 3.6, "href": "https://kalshi.com/x"}],
                }
            ],
            "FanDuel": [
                {"name": "ML", "odds": [{"home": 2.05, "draw": 3.25, "away": 3.55}]},
            ],
        },
    }
    mon = OddsEVMonitor(auth_token=None)
    rows = mon.live_scan_value_bets_from_docs({9001: doc})
    sides = {r.get("betSide") for r in rows if r.get("_scan_mname") == "ML"}
    assert "home" not in sides
    assert "away" not in sides


def test_gameline_scan_sides_allows_totals_and_blocks_ml_home():
    bks = {
        "Bet365": [{"name": "ML", "odds": [{"home": 2.0, "draw": 3.2, "away": 3.5}]}],
    }
    ml_row = {"home": 2.0, "draw": 3.2, "away": 3.5}
    assert _gameline_scan_sides("Totals", {"over": 1.9, "under": 1.95}, bks=bks) == ("over", "under")
    assert _gameline_scan_sides("ML", ml_row, bks=bks) == ("draw",)
    assert _gameline_scan_sides("ML", {"home": 2.0, "away": 3.5}, bks={}) == ("home", "away")
