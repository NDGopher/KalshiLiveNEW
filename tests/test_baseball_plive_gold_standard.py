"""Gold-standard baseball PLive take. Do not change this path.

Stephen confirmed live (Kalshi Live dashboard vs betbck.com, same moment):
White Sox @ Astros, 6th, 6-2 Houston.
Dashboard: +1.11% Houston Astros -3.5, green-left PLV -164.
betbck Run Line: Astros -3.5 = -164, Sox +3.5 = +123.
Totals on PLive (not this card): o9.5 -155 / u9.5 +116, o10.5 +133 / u10.5 -178.

Live Pandora coeff for the exact market-6 run-line strike. Green left is the
take book. Price matches the PLive site. Not a Kalshi copy. Not -3 / -4 /
a total. Market-3 ML stays idx1. Auto-bet OFF.
"""
from __future__ import annotations

from ev_calculator import american_to_decimal, decimal_to_american, is_plus_print_ev
from odds_ev_monitor import OddsEVMonitor, _decimal_for_side, _pick_qualifier_line_for_side
from plive_pandora import PLIVE_LINE_SET, PliveStore, is_live_plive_side


EID = 199298371
HOME = "Houston Astros"
AWAY = "Chicago White Sox"
PLIVE_HOME_AM = -164
PLIVE_AWAY_AM = 123
KALSHI_HOME_AM = -179


def _am(american: int) -> float:
    return american_to_decimal(int(american))


def _mlb_mon() -> OddsEVMonitor:
    mon = OddsEVMonitor(auth_token=None)
    mon.set_filter(
        {
            "betTypes": ["GAMELINES"],
            "minRoi": 0,
            "devigFilter": {
                "sharps": [
                    "FanDuel",
                    "DraftKings",
                    "NoVig",
                    "Caesars",
                    "Bet365",
                    "BetMGM",
                ],
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
                "PLive",
                "BetMGM",
                "DraftKings",
                "Kalshi",
                "Bet365",
                "Caesars",
                "FanDuel",
                "NoVig",
            ],
        }
    )
    return mon


def _astros_live_store() -> PliveStore:
    """Market 6 -3.5 pair is the take. Market 3 idx1 is ML. Market 5 is totals only."""
    store = PliveStore()
    store.apply_meta(
        str(EID),
        {
            "sportId": 1,
            "leagueId": 8,
            "home": HOME,
            "away": AWAY,
            "leagueName": "MLB",
            "ip": True,
        },
    )
    store.apply_message(
        {
            "isDiff": False,
            "payload": {
                "c": {
                    "m": {
                        "3": {
                            "o": {
                                "1": {0: 9.99, 1: _am(-220)},
                                "2": {0: 8.88, 1: _am(180)},
                            }
                        },
                        "5": {
                            "o": {
                                "9.5": {0: _am(-155), 1: _am(116)},
                                "10.5": {0: _am(133), 1: _am(-178)},
                            }
                        },
                        "6": {
                            "o": {
                                "-2.5": {0: _am(-110), 1: _am(-110)},
                                "-3.5": {0: _am(PLIVE_HOME_AM), 1: _am(PLIVE_AWAY_AM)},
                                "-4.5": {0: _am(-250), 1: _am(190)},
                            }
                        },
                    }
                }
            },
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventCoefficients.{EID}",
    )
    return store


def _spread_row(home_am: int, away_am: int, hdp: float) -> dict:
    return {"hdp": hdp, "home": _am(home_am), "away": _am(away_am)}


def _astros_doc(store: PliveStore, *, kalshi_href: str = "") -> dict:
    # Screenshot rec pack on Astros -3.5. Away sides are two-way sisters only.
    rec_away = 150
    return {
        "id": EID,
        "home": HOME,
        "away": AWAY,
        "league": "MLB",
        "sport": {"slug": "baseball"},
        "live": True,
        "bookmakers": {
            "PLive": store.markets_for_event(str(EID)),
            "Kalshi": [
                {
                    "name": "Spread",
                    "odds": [
                        {
                            **_spread_row(KALSHI_HOME_AM, rec_away, -3.5),
                            "href": kalshi_href,
                        }
                    ],
                }
            ],
            "BetMGM": [{"name": "Spread", "odds": [_spread_row(-179, rec_away, -3.5)]}],
            "DraftKings": [{"name": "Spread", "odds": [_spread_row(-182, rec_away, -3.5)]}],
            "Bet365": [{"name": "Spread", "odds": [_spread_row(-185, rec_away, -3.5)]}],
            "Caesars": [{"name": "Spread", "odds": [_spread_row(-200, rec_away, -3.5)]}],
            "FanDuel": [{"name": "Spread", "odds": [_spread_row(-179, rec_away, -3.5)]}],
            "NoVig": [{"name": "Spread", "odds": [_spread_row(-180, rec_away, -3.5)]}],
        },
    }


def test_astros_minus35_plive_164_is_gold_standard_take():
    """PRIMARY: Astros -3.5 PLive -164. Exact market-6 strike. Not a Kalshi copy."""
    store = _astros_live_store()
    by_name = {m["name"]: m for m in store.markets_for_event(str(EID))}

    ml = by_name["ML"]["odds"][0]
    assert ml.get("plive_market") == 3
    assert decimal_to_american(ml["home"]) == -220
    assert decimal_to_american(ml["away"]) == 180
    assert ml["home"] != 9.99
    assert ml["away"] != 8.88

    spreads = by_name["Spread"]["odds"]
    assert all(r.get("plive_market") == 6 for r in spreads)
    assert all(r.get("market_type") == "run_line" for r in spreads)
    minus35 = next(r for r in spreads if abs(float(r["hdp"]) + 3.5) < 1e-9)
    assert decimal_to_american(minus35["home"]) == PLIVE_HOME_AM
    assert decimal_to_american(minus35["away"]) == PLIVE_AWAY_AM
    assert is_live_plive_side(minus35, "home")
    pick, qual, line = _pick_qualifier_line_for_side(HOME, AWAY, "Spread", "home", minus35)
    assert pick == HOME
    assert qual == "-3.5"
    assert float(line) == -3.5
    assert qual != "-3"
    assert qual != "-4"
    assert _decimal_for_side(minus35, "home") == _am(PLIVE_HOME_AM)

    totals = {float(r["hdp"]): r for r in by_name["Totals"]["odds"]}
    assert decimal_to_american(totals[9.5]["over"]) == -155
    assert decimal_to_american(totals[9.5]["under"]) == 116
    assert decimal_to_american(totals[10.5]["over"]) == 133
    assert decimal_to_american(totals[10.5]["under"]) == -178
    assert all(abs(float(r["hdp"]) - 3.5) > 1e-9 for r in by_name["Totals"]["odds"])
    assert all(r.get("over") != _am(PLIVE_HOME_AM) for r in by_name["Totals"]["odds"])

    mon = _mlb_mon()
    href = "https://kalshi.com/markets/KXMLBSPREAD-26SEP03CWSHOU-HOU3"
    doc = _astros_doc(store, kalshi_href=href)
    rows = mon.live_scan_value_bets_from_docs({EID: doc})
    plive_home = [
        r
        for r in rows
        if r.get("_take_only") == "PLive"
        and r.get("betSide") == "home"
        and str(r.get("_scan_mname") or "").upper() == "SPREAD"
        and abs(float((r.get("_canonical_kalshi_row") or {}).get("hdp") or 0) + 3.5) < 1e-9
    ]
    assert plive_home
    assert plive_home[0]["_ev_source"] == "plive_take"
    bo = plive_home[0]["bookmakerOdds"]
    assert decimal_to_american(float(bo["home"])) == PLIVE_HOME_AM
    assert decimal_to_american(float(bo["home"])) != KALSHI_HOME_AM

    built = mon._value_bet_to_normalized_bet(plive_home[0], doc, take_book="PLive")
    assert built is not None
    assert built["take_book"] == "PLive"
    assert built["selection"] == HOME
    assert built["qualifier"] == "-3.5"
    assert float(built["line"]) == -3.5
    assert float(built["line"]) not in (-3.0, -4.0, 9.5, 10.5)
    assert int(built["odds"]) == PLIVE_HOME_AM
    assert int(built["odds"]) != KALSHI_HOME_AM
    assert built["market"] == "Point Spread"
    assert is_plus_print_ev(built["ev"])
    assert built["autobet_allow"] is False
    left = (built["displayBooks"][built["selection"]] or [])[0]
    assert left["book"] == "PLive"
    assert int(left["odds"]) == PLIVE_HOME_AM
    kalshi_tiles = [
        t for t in built["displayBooks"][built["selection"]] if str(t.get("book")) == "Kalshi"
    ]
    if kalshi_tiles:
        assert int(kalshi_tiles[0]["odds"]) == KALSHI_HOME_AM
        assert int(kalshi_tiles[0]["odds"]) != PLIVE_HOME_AM

    assert mon._value_bet_to_normalized_bet(plive_home[0], doc, take_book="Kalshi") is None

    alerts = mon.alerts_from_live_scan_docs({EID: doc})
    plive_cards = [
        a
        for a in alerts
        if str(getattr(a, "take_book", "")).lower() == "plive"
        and str(getattr(a, "pick", "")) == HOME
        and str(getattr(a, "qualifier", "")) == "-3.5"
    ]
    assert plive_cards
    assert int(str(plive_cards[0].odds).replace("+", "")) == PLIVE_HOME_AM
    assert plive_cards[0].autobet_allow is False
    assert abs(float(plive_cards[0].line) + 3.5) < 1e-9
    assert "total" not in str(plive_cards[0].market_type).lower()
