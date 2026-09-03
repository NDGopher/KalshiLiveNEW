"""Soccer PLive totals: exact strikes, idx0 take, fail-closed on mismatch.

Al-Kholood @ Al-Fayha: raw Under 2.5 +186 must not paint as +245, and
2.5 / 3.5 / 4.5 must not cross-contaminate. Not the MLB market-3 idx1 rule.
"""
from __future__ import annotations

from ev_calculator import american_to_decimal, decimal_to_american

from odds_ev_monitor import (
    OddsEVMonitor,
    _decimal_for_side,
    _fmt_american_from_dec,
    _pick_matching_odds_row,
    _sharp_row_for_market,
)
from plive_pandora import (
    PLIVE_LINE_SET,
    PliveStore,
    merge_plive_market_lists,
    parse_soccer_total_outcome,
)


KICKOFF = 1_780_000_000
EID = "220986541"
UNDER_25_TAKE = american_to_decimal(186)
UNDER_25_WRONG = american_to_decimal(245)
OVER_25 = 1.52
UNDER_35 = 1.72
OVER_35 = 2.20
UNDER_45 = 1.28
OVER_45 = 3.60


def _store_with_al_kholood_side_named() -> PliveStore:
    """Live soccer market 5: side-named outcomes with idx0 take / idx1 junk."""
    store = PliveStore()
    store.apply_meta(
        EID,
        {
            "sportId": 5,
            "home": "Al-Fayha FC",
            "away": "Al-Kholood",
            "leagueName": "Saudi Pro League",
            "start": KICKOFF,
            "ip": True,
        },
    )
    store.apply_message(
        {
            "isDiff": False,
            "payload": {
                "c": {
                    "m": {
                        "3": {"o": {"1": {"1": 2.10}, "2": {"1": 3.40}}},
                        "5": {
                            "o": {
                                "over_2.5": {0: OVER_25, 1: 1.61},
                                "under_2.5": {0: UNDER_25_TAKE, 1: UNDER_25_WRONG},
                                "over_3.5": {0: OVER_35, 1: 2.45},
                                "under_3.5": {0: UNDER_35, 1: 2.05},
                                "over_4.5": {0: OVER_45, 1: 4.10},
                                "under_4.5": {0: UNDER_45, 1: 1.50},
                            }
                        },
                    }
                }
            },
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventCoefficients.{EID}",
    )
    return store


def _totals_by_line(store: PliveStore, eid: str = EID) -> dict:
    mk = next(m for m in store.markets_for_event(eid) if m["name"] == "Totals")
    out = {}
    for row in mk["odds"]:
        out[float(row["hdp"])] = row
    return out


def test_parse_soccer_total_outcome_keeps_strikes():
    assert parse_soccer_total_outcome("under_2.5") == (2.5, "under")
    assert parse_soccer_total_outcome("over_3.5") == (3.5, "over")
    assert parse_soccer_total_outcome("4.5") == (4.5, None)
    assert parse_soccer_total_outcome("over") == (None, "over")
    assert parse_soccer_total_outcome("u-2.5") == (2.5, "under")


def test_al_kholood_under_25_is_186_not_245():
    store = _store_with_al_kholood_side_named()
    by_line = _totals_by_line(store)
    assert 2.5 in by_line
    row = by_line[2.5]
    assert decimal_to_american(row["under"]) == 186
    assert decimal_to_american(row["under"]) != 245
    assert abs(float(row["under"]) - UNDER_25_TAKE) < 1e-9
    assert abs(float(row["under"]) - UNDER_25_WRONG) > 1e-6
    assert abs(float(row["over"]) - OVER_25) < 1e-9
    assert _fmt_american_from_dec(_decimal_for_side(row, "under")) == "+186"


def test_soccer_totals_strikes_do_not_cross_contaminate():
    store = _store_with_al_kholood_side_named()
    by_line = _totals_by_line(store)
    assert set(by_line) >= {2.5, 3.5, 4.5}
    assert abs(float(by_line[2.5]["under"]) - UNDER_25_TAKE) < 1e-9
    assert abs(float(by_line[3.5]["under"]) - UNDER_35) < 1e-9
    assert abs(float(by_line[4.5]["under"]) - UNDER_45) < 1e-9
    assert abs(float(by_line[3.5]["over"]) - OVER_35) < 1e-9
    assert abs(float(by_line[4.5]["over"]) - OVER_45) < 1e-9
    assert decimal_to_american(by_line[3.5]["under"]) != 186
    assert decimal_to_american(by_line[3.5]["under"]) != 245
    assert decimal_to_american(by_line[4.5]["under"]) != 186
    assert decimal_to_american(by_line[4.5]["over"]) != 186
    assert decimal_to_american(by_line[4.5]["over"]) != 245


def test_slot2_price_cannot_retarget_strike_to_345():
    """Coeff index 2 is a price, not a 3.45 total line."""
    store = PliveStore()
    store.apply_meta(EID, {"sportId": 5, "home": "Al-Fayha FC", "away": "Al-Kholood", "ip": True})
    store.set_coeff(EID, 5, "2.5", 0, OVER_25)
    store.set_coeff(EID, 5, "2.5", 1, UNDER_25_TAKE)
    store.set_coeff(EID, 5, "2.5", 2, UNDER_25_WRONG)
    by_line = _totals_by_line(store)
    assert 2.5 in by_line
    assert 3.45 not in by_line
    assert abs(float(by_line[2.5]["under"]) - UNDER_25_TAKE) < 1e-9


def test_invalid_ou_pair_on_line_key_is_rejected():
    """+186 and +245 are not a 2-way of the same strike — fail closed."""
    store = PliveStore()
    store.apply_meta(EID, {"sportId": 5, "home": "Al-Fayha FC", "away": "Al-Kholood", "ip": True})
    store.set_coeff(EID, 5, "2.5", 0, UNDER_25_TAKE)
    store.set_coeff(EID, 5, "2.5", 1, UNDER_25_WRONG)
    store.set_coeff(EID, 5, "3.5", 0, OVER_35)
    store.set_coeff(EID, 5, "3.5", 1, UNDER_35)
    mk = {m["name"]: m for m in store.markets_for_event(EID)}
    assert "Totals" in mk
    lines = {float(r["hdp"]) for r in mk["Totals"]["odds"]}
    assert 2.5 not in lines
    assert 3.5 in lines
    row35 = next(r for r in mk["Totals"]["odds"] if abs(float(r["hdp"]) - 3.5) < 1e-9)
    assert abs(float(row35["over"]) - OVER_35) < 1e-9
    assert abs(float(row35["under"]) - UNDER_35) < 1e-9


def test_nested_under_strikes_do_not_collapse_to_last_line():
    store = PliveStore()
    store.apply_meta(EID, {"sportId": 5, "home": "Al-Fayha FC", "away": "Al-Kholood", "ip": True})
    store.apply_coeff_tree(
        EID,
        {
            "c": {
                "m": {
                    "5": {
                        "o": {
                            "over": {"2.5": OVER_25, "3.5": OVER_35, "4.5": OVER_45},
                            "under": {
                                "2.5": UNDER_25_TAKE,
                                "3.5": UNDER_35,
                                "4.5": UNDER_45,
                            },
                        }
                    }
                }
            }
        },
    )
    by_line = _totals_by_line(store)
    assert abs(float(by_line[2.5]["under"]) - UNDER_25_TAKE) < 1e-9
    assert abs(float(by_line[3.5]["under"]) - UNDER_35) < 1e-9
    assert abs(float(by_line[4.5]["under"]) - UNDER_45) < 1e-9


def test_mlb_market5_pair_path_unchanged():
    store = PliveStore()
    store.apply_meta("199295331", {"sportId": 1, "home": "Minnesota Twins", "away": "Detroit Tigers"})
    store.set_coeff("199295331", 5, "11.5", 0, 1.892857)
    store.set_coeff("199295331", 5, "11.5", 1, 1.847458)
    tot = next(m for m in store.markets_for_event("199295331") if m["name"] == "Totals")
    row = tot["odds"][0]
    assert row["over"] == 1.892857
    assert row["under"] == 1.847458


def test_merge_replaces_total_goals_family_with_live_totals():
    existing = [
        {
            "name": "Total Goals",
            "odds": [{"hdp": 2.5, "max": 2.5, "over": 1.40, "under": UNDER_25_TAKE}],
        }
    ]
    incoming = [
        {
            "name": "Totals",
            "odds": [
                {"hdp": 2.5, "max": 2.5, "over": OVER_25, "under": UNDER_25_TAKE},
                {"hdp": 3.5, "max": 3.5, "over": OVER_35, "under": UNDER_35},
            ],
        }
    ]
    merged = merge_plive_market_lists(existing, incoming)
    names = [m["name"] for m in merged]
    assert names.count("Totals") == 1
    assert "Total Goals" not in names
    rows = next(m for m in merged if m["name"] == "Totals")["odds"]
    assert {float(r["hdp"]) for r in rows} == {2.5, 3.5}


def test_pick_matching_odds_row_does_not_reuse_nearby_strike():
    plive = {
        "name": "Totals",
        "odds": [
            {"hdp": 3.5, "max": 3.5, "over": OVER_35, "under": UNDER_35},
            {"hdp": 4.5, "max": 4.5, "over": OVER_45, "under": UNDER_45},
        ],
    }
    assert _pick_matching_odds_row(plive, "Totals", {"hdp": 2.5, "max": 2.5}) == {}
    hit = _pick_matching_odds_row(plive, "Totals", {"hdp": 3.5, "max": 3.5})
    assert abs(float(hit["under"]) - UNDER_35) < 1e-9
    mon = OddsEVMonitor(auth_token=None)
    assert mon._match_kalshi_row({"max": 2.5, "over": 1.5, "under": 2.8}, plive["odds"]) == {}


def test_dashboard_take_matches_raw_under_186():
    store = _store_with_al_kholood_side_named()
    plive_mk = next(m for m in store.markets_for_event(EID) if m["name"] == "Totals")
    ref = {"hdp": 2.5, "max": 2.5, "line": 2.5, "over": OVER_25, "under": UNDER_25_TAKE}
    row = _sharp_row_for_market(plive_mk, "Totals", ref)
    assert decimal_to_american(row["under"]) == 186
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
            "displayBooks": ["PLive", "Betfair Exchange", "Bet365", "FanDuel"],
        }
    )
    doc = {
        "id": 9002,
        "home": "Al-Fayha FC",
        "away": "Al-Kholood",
        "sport": {"slug": "football"},
        "league": {"name": "Saudi Pro League"},
        "bookmakers": {
            "PLive": [plive_mk],
            "Betfair Exchange": [
                {"name": "Totals", "odds": [{"max": 2.5, "over": 1.55, "under": 2.40}]}
            ],
            "Bet365": [{"name": "Totals", "odds": [{"max": 2.5, "over": 1.56, "under": 2.38}]}],
            "FanDuel": [{"name": "Totals", "odds": [{"max": 2.5, "over": 1.54, "under": 2.42}]}],
        },
    }
    vbs = mon.live_scan_value_bets_from_docs({9002: doc})
    under25 = [
        r
        for r in vbs
        if str(r.get("betSide")) == "under"
        and abs(float((r.get("_canonical_kalshi_row") or {}).get("hdp") or 0) - 2.5) < 1e-9
    ]
    assert under25
    built = mon._value_bet_to_normalized_bet(under25[0], doc, take_book="PLive")
    assert built is not None
    assert built["market"] == "Total Goals"
    assert built["selection"] == "Under"
    assert float(built["qualifier"]) == 2.5
    assert int(built["odds"]) == 186
    assert decimal_to_american(float(under25[0]["bookmakerOdds"]["under"])) == 186
    assert decimal_to_american(float(under25[0]["bookmakerOdds"]["under"])) != 245
    under35 = [
        r
        for r in vbs
        if str(r.get("betSide")) == "under"
        and abs(float((r.get("_canonical_kalshi_row") or {}).get("hdp") or 0) - 3.5) < 1e-9
    ]
    if under35:
        assert decimal_to_american(float(under35[0]["bookmakerOdds"]["under"])) != 186
        assert decimal_to_american(float(under35[0]["bookmakerOdds"]["under"])) != 245
