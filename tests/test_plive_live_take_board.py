"""PLive take must be a live Pandora coeff. Never copy Kalshi / Odds-API PLive.

Screenshot / live-laptop cases:
- Kortrijk Draw: live X +311, Kalshi +400. Green tile must not be +400.
- Hearts Draw: Kalshi +317 must not be assigned to PLive.
- Lille / Celta Under 2.5: live board ≠ Kalshi; frozen -112 / +104 is a copy.
- Jays -4.5: live -141; neighbor -5 is -117; Odds-API overlay -116 is dropped.
- Leuven: two filters, one card per take book.
"""
from __future__ import annotations

import asyncio
import time

from ev_calculator import american_to_decimal, decimal_to_american
from odds_ev_monitor import (
    OddsEVMonitor,
    _build_display_books_payload,
    _decimal_for_side,
    _pick_matching_odds_row,
    _sharp_row_for_market,
)
from plive_pandora import (
    PLIVE_LINE_SET,
    PlivePandoraFeed,
    PliveStore,
    is_live_plive_row,
    merge_plive_into_docs,
    merge_plive_market_lists,
    parse_soccer_1x2_outcome,
    peek_shared_plive_feed,
    reset_shared_plive_feed,
    strip_odds_api_plive_book,
)


KICKOFF = 1_780_000_000
NOW = 1_780_000_050.0


def _am(dec: float) -> int:
    return int(decimal_to_american(dec))


def _kortrijk_store(*, draw_am: int = 311) -> PliveStore:
    store = PliveStore()
    store.apply_meta(
        "2201001",
        {
            "sportId": 5,
            "home": "RSC Anderlecht",
            "away": "KV Kortrijk",
            "leagueName": "Belgium Jupiler League",
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
                        "3": {"o": {"1": {"1": 1.40}, "2": {"1": 8.00}}},
                        "1": {
                            "o": {
                                "1": {0: 1.45, 1: 1.80},
                                "X": {0: american_to_decimal(draw_am), 1: american_to_decimal(400)},
                                "2": {0: 7.50, 1: 9.00},
                            }
                        },
                        "5": {
                            "o": {
                                "over_2.5": {0: 1.80, 1: 2.00},
                                "under_2.5": {0: american_to_decimal(-147), 1: american_to_decimal(-112)},
                            }
                        },
                    }
                }
            },
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventCoefficients.2201001",
    )
    store.events["2201001"]["coeff_updated_at"] = time.time()
    return store


def _lille_store(*, under_am: int = -170) -> PliveStore:
    store = PliveStore()
    store.apply_meta(
        "2201002",
        {
            "sportId": 5,
            "home": "Toulouse FC",
            "away": "Lille OSC",
            "leagueName": "France Ligue 1",
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
                        "5": {
                            "o": {
                                "over_2.5": {0: american_to_decimal(117), 1: 1.70},
                                "under_2.5": {0: american_to_decimal(under_am), 1: american_to_decimal(-112)},
                            }
                        }
                    }
                }
            },
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventCoefficients.2201002",
    )
    store.events["2201002"]["coeff_updated_at"] = time.time()
    return store


def _jays_store() -> PliveStore:
    store = PliveStore()
    store.apply_meta(
        "1994001",
        {
            "sportId": 1,
            "home": "Cleveland Guardians",
            "away": "Toronto Blue Jays",
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
                        "6": {
                            "o": {
                                "3.5": {0: american_to_decimal(-254), 1: american_to_decimal(210)},
                                "4.5": {0: american_to_decimal(-200), 1: american_to_decimal(-141)},
                                "5": {0: american_to_decimal(-180), 1: american_to_decimal(-117)},
                                "5.5": {0: american_to_decimal(-160), 1: american_to_decimal(119)},
                            }
                        }
                    }
                }
            },
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventCoefficients.1994001",
    )
    store.events["1994001"]["coeff_updated_at"] = time.time()
    return store


def _gameline_mon(*, min_sharp: int = 2) -> OddsEVMonitor:
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
                "minSharpBooks": min_sharp,
                "hold": [{"book": "Any", "max": 20}],
            },
            "oddsRanges": [{"book": "Any", "min": -500, "max": 500}],
            "displayBooks": ["PLive", "Kalshi", "Betfair Exchange", "Bet365", "FanDuel"],
        }
    )
    return mon


def test_parse_soccer_1x2_draw_keys():
    assert parse_soccer_1x2_outcome("X") == "draw"
    assert parse_soccer_1x2_outcome("draw") == "draw"
    assert parse_soccer_1x2_outcome("1") == "home"
    assert parse_soccer_1x2_outcome("2") == "away"


def test_kortrijk_draw_live_311_not_kalshi_400():
    store = _kortrijk_store(draw_am=311)
    ml = next(m for m in store.markets_for_event("2201001") if m["name"] == "ML")
    row = ml["odds"][0]
    assert is_live_plive_row(row)
    assert _am(row["draw"]) == 311
    assert _am(row["draw"]) != 400
    assert row.get("plive_draw_market") == 1


def test_hearts_draw_does_not_copy_kalshi_317():
    store = _kortrijk_store(draw_am=280)
    store.apply_meta(
        "2201001",
        {"sportId": 5, "home": "Hibernian FC", "away": "Heart of Midlothian FC", "ip": True},
    )
    ml = next(m for m in store.markets_for_event("2201001") if m["name"] == "ML")
    assert _am(ml["odds"][0]["draw"]) == 280
    assert _am(ml["odds"][0]["draw"]) != 317


def test_hearts_draw_plive_take_is_live_not_kalshi_317():
    """Screenshot: Hearts Draw PLive +317 was Kalshi +317. That assignment is illegal."""
    mon = _gameline_mon()
    kalshi = american_to_decimal(317)
    live = american_to_decimal(340)
    pack = american_to_decimal(280)
    vb = {
        "event": {
            "home": "Hibernian FC",
            "away": "Heart of Midlothian FC",
            "league": "Scotland Premiership",
            "sport": {"slug": "football"},
        },
        "market": {"name": "ML", "home": 1.9, "draw": kalshi, "away": 4.0},
        "betSide": "draw",
        "bookmakerOdds": {"draw": kalshi, "home": 1.9, "away": 4.0, "href": "https://kalshi.com/x"},
        "_live_broad_scan": True,
        "_ev_source": "live_event_scan",
        "_canonical_kalshi_row": {"home": 1.9, "draw": kalshi, "away": 4.0},
    }
    overlay = {
        "home": "Hibernian FC",
        "away": "Heart of Midlothian FC",
        "sport": {"slug": "football"},
        "bookmakers": {
            "Kalshi": [{"name": "ML", "odds": [{"home": 1.9, "draw": kalshi, "away": 4.0}]}],
            "PLive": [{"name": "ML", "odds": [{"home": 1.9, "draw": kalshi, "away": 4.0}]}],
            "Betfair Exchange": [{"name": "ML", "odds": [{"home": 1.90, "draw": pack, "away": 4.0}]}],
            "Bet365": [{"name": "ML", "odds": [{"home": 1.88, "draw": pack, "away": 4.1}]}],
            "FanDuel": [{"name": "ML", "odds": [{"home": 1.90, "draw": pack, "away": 3.9}]}],
        },
    }
    assert mon._value_bet_to_normalized_bet(vb, overlay, take_book="PLive") is None
    live_doc = {
        **overlay,
        "bookmakers": {
            **overlay["bookmakers"],
            "PLive": [
                {
                    "name": "ML",
                    "odds": [
                        {
                            "home": 1.90,
                            "away": 4.00,
                            "draw": live,
                            "plive_market": 1,
                            "plive_draw_market": 1,
                            "plive_live": True,
                            "market_type": "game_winner",
                        }
                    ],
                }
            ],
        },
    }
    plive = mon._value_bet_to_normalized_bet(vb, live_doc, take_book="PLive")
    kalshi_card = mon._value_bet_to_normalized_bet(vb, live_doc, take_book="Kalshi")
    assert plive is not None
    assert int(plive["odds"]) == 340
    assert int(plive["odds"]) != 317
    if kalshi_card is not None:
        assert int(kalshi_card["odds"]) == 317
        assert int(plive["odds"]) != int(kalshi_card["odds"])


def test_idx1_draw_is_not_the_take():
    store = PliveStore()
    store.apply_meta("x", {"sportId": 5, "home": "A", "away": "B", "ip": True})
    store.set_coeff("x", 1, "X", 0, american_to_decimal(311))
    store.set_coeff("x", 1, "X", 1, american_to_decimal(400))
    store.set_coeff("x", 3, "1", 1, 1.80)
    store.set_coeff("x", 3, "2", 1, 2.10)
    ml = next(m for m in store.markets_for_event("x") if m["name"] == "ML")
    assert _am(ml["odds"][0]["draw"]) == 311


def test_no_live_draw_omits_plive_draw():
    store = PliveStore()
    store.apply_meta("x", {"sportId": 5, "home": "A", "away": "B", "ip": True})
    store.set_coeff("x", 3, "1", 1, 1.80)
    store.set_coeff("x", 3, "2", 1, 2.10)
    ml = next(m for m in store.markets_for_event("x") if m["name"] == "ML")
    assert ml["odds"][0].get("draw") is None


def test_lille_under_25_live_170_not_112():
    store = _lille_store(under_am=-170)
    tot = next(m for m in store.markets_for_event("2201002") if m["name"] == "Totals")
    row = next(r for r in tot["odds"] if abs(float(r["hdp"]) - 2.5) < 1e-9)
    assert _am(row["under"]) == -170
    assert _am(row["under"]) != -112
    assert is_live_plive_row(row)


def test_celta_under_25_does_not_copy_kalshi_104():
    store = _lille_store(under_am=-147)
    tot = next(m for m in store.markets_for_event("2201002") if m["name"] == "Totals")
    row = next(r for r in tot["odds"] if abs(float(r["hdp"]) - 2.5) < 1e-9)
    assert _am(row["under"]) == -147
    assert _am(row["under"]) != 104


def test_celta_under_25_plive_take_is_live_not_kalshi_104():
    """Screenshot: Celta Under 2.5 PLive +104 was Kalshi +104. That assignment is illegal."""
    mon = _gameline_mon()
    kalshi = american_to_decimal(104)
    live = 2.15
    pack_under = 1.72
    pack_over = 2.05
    opp = 2.15
    vb = {
        "event": {
            "home": "Celta de Vigo",
            "away": "Getafe CF",
            "league": "Spain La Liga",
            "sport": {"slug": "football"},
        },
        "market": {"name": "Totals", "max": 2.5, "over": opp, "under": kalshi},
        "betSide": "under",
        "bookmakerOdds": {"under": kalshi, "over": opp, "href": "https://kalshi.com/x"},
        "_live_broad_scan": True,
        "_canonical_kalshi_row": {"max": 2.5, "hdp": 2.5, "over": opp, "under": kalshi},
    }
    overlay = {
        "home": "Celta de Vigo",
        "away": "Getafe CF",
        "sport": {"slug": "football"},
        "bookmakers": {
            "Kalshi": [{"name": "Totals", "odds": [{"max": 2.5, "hdp": 2.5, "over": opp, "under": kalshi}]}],
            "PLive": [{"name": "Totals", "odds": [{"max": 2.5, "hdp": 2.5, "over": opp, "under": kalshi}]}],
            "Betfair Exchange": [{"name": "Totals", "odds": [{"max": 2.5, "over": pack_over, "under": pack_under}]}],
            "Bet365": [{"name": "Totals", "odds": [{"max": 2.5, "over": 2.08, "under": 1.74}]}],
            "FanDuel": [{"name": "Totals", "odds": [{"max": 2.5, "over": 2.06, "under": 1.73}]}],
        },
    }
    assert mon._value_bet_to_normalized_bet(vb, overlay, take_book="PLive") is None
    live_row = {
        "hdp": 2.5,
        "max": 2.5,
        "line": 2.5,
        "over": 2.15,
        "under": live,
        "plive_market": 5,
        "plive_live": True,
        "market_type": "game_total",
    }
    assert is_live_plive_row(live_row)
    assert _am(live_row["under"]) != 104
    assert _decimal_for_side(live_row, "under") == live
    painted = _build_display_books_payload(
        "Under",
        {
            "Kalshi": overlay["bookmakers"]["Kalshi"],
            "PLive": [{"name": "Totals", "odds": [live_row]}],
            "FanDuel": overlay["bookmakers"]["FanDuel"],
        },
        "Totals",
        "under",
        ["Kalshi", "PLive", "FanDuel"],
        104,
        {"max": 2.5, "under": kalshi},
        take_book="Kalshi",
    )
    plive_tile = [r for r in painted["Under"] if r["book"] == "PLive"]
    assert plive_tile and int(plive_tile[0]["odds"]) == _am(live)
    assert int(plive_tile[0]["odds"]) != 104


def test_jays_45_is_141_not_neighbor_117_or_overlay_116():
    store = _jays_store()
    spr = next(m for m in store.markets_for_event("1994001") if m["name"] == "Spread")
    by_hdp = {float(r["hdp"]): r for r in spr["odds"]}
    assert 4.5 in by_hdp
    assert 5.0 in by_hdp
    assert _am(by_hdp[4.5]["away"]) == -141
    assert _am(by_hdp[5.0]["away"]) == -117
    assert _am(by_hdp[4.5]["away"]) != -116
    assert _pick_matching_odds_row(spr, "Spread", {"hdp": 4.5})["away"] == by_hdp[4.5]["away"]
    assert _pick_matching_odds_row(spr, "Spread", {"hdp": -4.5}) == {}


def test_merge_drops_odds_api_plive_when_no_live_match():
    async def _run():
        await reset_shared_plive_feed()
        feed = PlivePandoraFeed(connect_fn=lambda _f: None)
        feed.connected = True
        feed._running = True
        import plive_pandora as pp

        pp._shared_plive = feed
        kalshi = american_to_decimal(-112)
        doc = {
            "home": "Toulouse FC",
            "away": "Lille OSC",
            "sport": {"slug": "football"},
            "bookmakers": {
                "Kalshi": [{"name": "Totals", "odds": [{"max": 2.5, "over": 1.9, "under": kalshi}]}],
                "PLive": [{"name": "Totals", "odds": [{"max": 2.5, "over": 1.9, "under": kalshi}]}],
            },
        }
        n = merge_plive_into_docs([doc])
        assert n == 0
        assert "PLive" not in doc["bookmakers"]
        await reset_shared_plive_feed()

    asyncio.run(_run())
    assert peek_shared_plive_feed() is None


def test_merge_replaces_odds_api_overlay_with_live_under_170():
    async def _run():
        await reset_shared_plive_feed()
        feed = PlivePandoraFeed(connect_fn=lambda _f: None)
        feed.connected = True
        feed._running = True
        store = _lille_store(under_am=-170)
        feed.store = store
        feed._last_coeff_at = time.time()
        import plive_pandora as pp

        pp._shared_plive = feed
        overlay = american_to_decimal(-112)
        doc = {
            "home": "Toulouse FC",
            "away": "Lille OSC",
            "sport": {"slug": "football"},
            "league": {"name": "France Ligue 1"},
            "startTime": KICKOFF,
            "live": True,
            "bookmakers": {
                "PLive": [{"name": "Totals", "odds": [{"max": 2.5, "over": 1.5, "under": overlay}]}],
                "Kalshi": [{"name": "Totals", "odds": [{"max": 2.5, "over": 1.5, "under": overlay}]}],
            },
        }
        n = merge_plive_into_docs([doc])
        assert n == 1
        pl = doc["bookmakers"]["PLive"]
        tot = next(m for m in pl if m["name"] == "Totals")
        row = next(r for r in tot["odds"] if abs(float(r["hdp"]) - 2.5) < 1e-9)
        assert _am(row["under"]) == -170
        assert _am(row["under"]) != -112
        assert is_live_plive_row(row)
        assert isinstance(doc["book_updated_at"]["PLive"], float)
        await reset_shared_plive_feed()

    asyncio.run(_run())


def test_strip_odds_api_plive_never_keeps_kalshi_copy():
    doc = {
        "bookmakers": {
            "PLive": [{"name": "ML", "odds": [{"draw": american_to_decimal(400)}]}],
            "Kalshi": [{"name": "ML", "odds": [{"draw": american_to_decimal(400)}]}],
        },
        "book_updated_at": {"PLive": 1.0, "Kalshi": 1.0},
    }
    strip_odds_api_plive_book(doc)
    assert "PLive" not in doc["bookmakers"]
    assert "PLive" not in doc["book_updated_at"]
    assert "Kalshi" in doc["bookmakers"]


def test_merge_plive_market_lists_drops_odds_api_overlay():
    stale = [{"name": "Totals", "odds": [{"hdp": 2.5, "over": 1.5, "under": american_to_decimal(-112)}]}]
    assert merge_plive_market_lists(stale, []) == []
    live = [
        {
            "name": "Totals",
            "odds": [
                {
                    "hdp": 2.5,
                    "over": 1.8,
                    "under": american_to_decimal(-170),
                    "plive_market": 5,
                    "plive_live": True,
                    "market_type": "game_total",
                }
            ],
        }
    ]
    merged = merge_plive_market_lists(stale, live)
    row = merged[0]["odds"][0]
    assert _am(row["under"]) == -170


def test_display_payload_skips_odds_api_plive_copy():
    kalshi = american_to_decimal(-112)
    live = american_to_decimal(-170)
    bks = {
        "Kalshi": [{"name": "Totals", "odds": [{"max": 2.5, "over": 1.9, "under": kalshi}]}],
        "PLive": [{"name": "Totals", "odds": [{"max": 2.5, "over": 1.9, "under": kalshi}]}],
        "FanDuel": [{"name": "Totals", "odds": [{"max": 2.5, "over": 1.85, "under": american_to_decimal(-146)}]}],
    }
    painted = _build_display_books_payload(
        "Under",
        bks,
        "Totals",
        "under",
        ["Kalshi", "PLive", "FanDuel"],
        -112,
        {"max": 2.5, "under": kalshi},
        take_book="Kalshi",
    )
    books = [r["book"] for r in painted["Under"]]
    assert "PLive" not in books
    bks["PLive"] = [
        {
            "name": "Totals",
            "odds": [
                {
                    "hdp": 2.5,
                    "max": 2.5,
                    "over": 2.17,
                    "under": live,
                    "plive_market": 5,
                    "plive_live": True,
                    "market_type": "game_total",
                }
            ],
        }
    ]
    painted2 = _build_display_books_payload(
        "Under",
        bks,
        "Totals",
        "under",
        ["Kalshi", "PLive", "FanDuel"],
        -112,
        {"max": 2.5, "under": kalshi},
        take_book="Kalshi",
    )
    plive = [r for r in painted2["Under"] if r["book"] == "PLive"]
    assert plive and int(plive[0]["odds"]) == -170
    assert int(plive[0]["odds"]) != -112


def test_plive_take_requires_live_row_not_kalshi():
    mon = _gameline_mon()
    kalshi = american_to_decimal(400)
    live = american_to_decimal(430)
    pack = american_to_decimal(300)
    vb = {
        "event": {
            "home": "RSC Anderlecht",
            "away": "KV Kortrijk",
            "league": "Belgium Jupiler League",
            "sport": {"slug": "football"},
        },
        "market": {"name": "ML", "home": 1.5, "draw": kalshi, "away": 6.0},
        "betSide": "draw",
        "bookmakerOdds": {"draw": kalshi, "home": 1.5, "away": 6.0, "href": "https://kalshi.com/x"},
        "_live_broad_scan": True,
        "_ev_source": "live_event_scan",
        "_canonical_kalshi_row": {"home": 1.5, "draw": kalshi, "away": 6.0},
    }
    overlay_doc = {
        "home": "RSC Anderlecht",
        "away": "KV Kortrijk",
        "sport": {"slug": "football"},
        "bookmakers": {
            "Kalshi": [{"name": "ML", "odds": [{"home": 1.5, "draw": kalshi, "away": 6.0}]}],
            "PLive": [{"name": "ML", "odds": [{"home": 1.5, "draw": kalshi, "away": 6.0}]}],
            "Betfair Exchange": [{"name": "ML", "odds": [{"home": 1.50, "draw": pack, "away": 6.0}]}],
            "Bet365": [{"name": "ML", "odds": [{"home": 1.48, "draw": pack, "away": 6.5}]}],
            "FanDuel": [{"name": "ML", "odds": [{"home": 1.50, "draw": pack, "away": 6.8}]}],
        },
    }
    assert mon._value_bet_to_normalized_bet(vb, overlay_doc, take_book="PLive") is None
    kalshi_card = mon._value_bet_to_normalized_bet(vb, overlay_doc, take_book="Kalshi")
    if kalshi_card:
        tiles = kalshi_card["displayBooks"][kalshi_card["selection"]]
        assert all(str(t.get("book")) != "PLive" for t in tiles)

    live_row = {
        "home": 1.40,
        "away": 8.00,
        "draw": live,
        "plive_market": 1,
        "plive_draw_market": 1,
        "plive_live": True,
        "market_type": "game_winner",
    }
    live_doc = {
        **overlay_doc,
        "bookmakers": {
            **overlay_doc["bookmakers"],
            "PLive": [{"name": "ML", "odds": [live_row]}],
        },
    }
    plive = mon._value_bet_to_normalized_bet(vb, live_doc, take_book="PLive")
    assert plive is not None
    assert plive["take_book"] == "PLive"
    assert int(plive["odds"]) == 430
    assert int(plive["odds"]) != 400
    left = (plive["displayBooks"][plive["selection"]] or [])[0]
    assert left["book"] == "PLive"
    assert int(left["odds"]) == 430


def test_both_takes_print_when_prices_differ():
    """Baseball-style: Kalshi-take and PLive-take are separate cards with separate prices."""
    mon = OddsEVMonitor(auth_token=None)
    mon.set_filter(
        {
            "betTypes": ["GAMELINES"],
            "minRoi": 0,
            "devigFilter": {
                "sharps": ["FanDuel", "DraftKings", "NoVig"],
                "method": "POWER",
                "type": "AVERAGE",
                "minEv": 0,
                "minSharpBooks": 3,
                "hold": [{"book": "Any", "max": 20}],
            },
            "oddsRanges": [{"book": "Any", "min": -500, "max": 500}],
            "minLimits": [{"book": "Kalshi", "min": 0}],
            "minSharpLimits": [],
            "displayBooks": ["Kalshi", "FanDuel", "DraftKings", "NoVig", "PLive"],
        }
    )
    k_dec = american_to_decimal(-133)
    p_dec = american_to_decimal(-120)
    fd = american_to_decimal(-142)
    dk = american_to_decimal(-141)
    nv = american_to_decimal(-139)
    opp = american_to_decimal(125)
    vb = {
        "event": {"home": "Houston Astros", "away": "Chicago White Sox", "league": "MLB"},
        "market": {"name": "ML", "home": k_dec, "away": opp},
        "betSide": "home",
        "bookmakerOdds": {"home": k_dec, "away": opp, "href": "https://kalshi.com/markets/KXTEST"},
        "expectedValue": 0.0,
    }
    doc = {
        "id": 1,
        "home": "Houston Astros",
        "away": "Chicago White Sox",
        "bookmakers": {
            "Kalshi": [{"name": "ML", "odds": [{"home": k_dec, "away": opp}]}],
            "PLive": [
                {
                    "name": "ML",
                    "odds": [
                        {
                            "home": p_dec,
                            "away": opp,
                            "plive_live": True,
                            "plive_market": 3,
                            "market_type": "game_winner",
                        }
                    ],
                }
            ],
            "FanDuel": [{"name": "ML", "odds": [{"home": fd, "away": opp}]}],
            "DraftKings": [{"name": "ML", "odds": [{"home": dk, "away": opp}]}],
            "NoVig": [{"name": "ML", "odds": [{"home": nv, "away": opp}]}],
        },
    }
    kalshi = mon._value_bet_to_normalized_bet(vb, doc, take_book="Kalshi")
    plive = mon._value_bet_to_normalized_bet(vb, doc, take_book="PLive")
    assert kalshi is not None
    assert plive is not None
    assert kalshi["take_book"] == "Kalshi"
    assert plive["take_book"] == "PLive"
    assert int(kalshi["odds"]) == -133
    assert int(plive["odds"]) == -120
    assert int(kalshi["odds"]) != int(plive["odds"])


def test_leuven_two_filters_one_card_per_take():
    from dashboard import (
        DEFAULT_FILTER_NAME,
        SOCCER_FILTER_NAME,
        alert_card_identity,
        dedupe_listed_alert_rows,
        prefer_alert_card,
    )

    base = {
        "teams": "Oud-Heverlee Leuven @ KAA Gent",
        "market_type": "Moneyline",
        "pick": "Draw",
        "qualifier": None,
        "line": None,
        "take_book": "PLive",
        "match_failed": False,
        "ticker": "PLIVE|Oud-Heverlee Leuven @ KAA Gent|Draw|None",
    }
    loose = {**base, "id": "a", "ev_percent": 0.96, "filter_name": SOCCER_FILTER_NAME}
    strict = {**base, "id": "b", "ev_percent": 0.46, "filter_name": DEFAULT_FILTER_NAME}
    assert alert_card_identity(loose) == alert_card_identity(strict)
    kept = prefer_alert_card(loose, strict)
    assert kept["filter_name"] == DEFAULT_FILTER_NAME
    assert kept["ev_percent"] == 0.46
    visible = dedupe_listed_alert_rows([loose, strict])
    assert len(visible) == 1
    assert visible[0]["ev_percent"] == 0.46
    kalshi = {**base, "id": "c", "take_book": "Kalshi", "ev_percent": 1.1, "filter_name": DEFAULT_FILTER_NAME}
    both = dedupe_listed_alert_rows([loose, strict, kalshi])
    takes = {str(r["take_book"]) for r in both}
    assert takes == {"PLive", "Kalshi"}
    assert len(both) == 2


def test_live_scan_skips_odds_api_plive_overlay():
    mon = _gameline_mon()
    kalshi = american_to_decimal(317)
    doc = {
        "id": 9001,
        "home": "Hibernian FC",
        "away": "Heart of Midlothian FC",
        "sport": {"slug": "football"},
        "league": {"name": "Scotland Premiership"},
        "live": True,
        "bookmakers": {
            "Kalshi": [
                {
                    "name": "ML",
                    "odds": [{"home": 1.9, "draw": kalshi, "away": 4.0, "href": "https://kalshi.com/x"}],
                }
            ],
            "PLive": [{"name": "ML", "odds": [{"home": 1.9, "draw": kalshi, "away": 4.0}]}],
            "Betfair Exchange": [{"name": "ML", "odds": [{"home": 1.85, "draw": 3.80, "away": 4.2}]}],
            "Bet365": [{"name": "ML", "odds": [{"home": 1.88, "draw": 3.70, "away": 4.1}]}],
            "FanDuel": [{"name": "ML", "odds": [{"home": 1.90, "draw": 3.75, "away": 4.0}]}],
        },
    }
    rows = mon.live_scan_value_bets_from_docs({9001: doc})
    plive_only = [r for r in rows if r.get("_take_only") == "PLive" or r.get("_ev_source") == "plive_take"]
    assert plive_only == []


def test_draw_from_non_1x2_market_is_omitted():
    store = PliveStore()
    store.apply_meta("x", {"sportId": 5, "home": "A", "away": "B", "ip": True})
    store.set_coeff("x", 3, "1", 1, 1.80)
    store.set_coeff("x", 3, "2", 1, 2.10)
    store.set_coeff("x", 9, "X", 0, american_to_decimal(317))
    ml = next(m for m in store.markets_for_event("x") if m["name"] == "ML")
    assert ml["odds"][0].get("draw") is None


def test_create_alert_id_ignores_filter_name():
    from ev_alert import EvAlert
    from dashboard import create_alert_id

    common = {
        "market": "Moneyline",
        "teams": "Oud-Heverlee Leuven @ KAA Gent",
        "selection": "Draw",
        "line": None,
        "odds": 285,
        "ev": 0.96,
        "limit": 0,
        "ticker": "PLIVE|leuven|Draw|None",
        "take_book": "PLive",
        "ev_source": "plive_take",
    }
    a = EvAlert(common)
    a.filter_name = "Soccer Live (2 Sharps)"
    b = EvAlert({**common, "ev": 0.46})
    b.filter_name = "Kalshi All Sports (3 Sharps Live)"
    assert create_alert_id(a) == create_alert_id(b)
    c = EvAlert({**common, "take_book": "Kalshi", "ticker": "KXTEST", "ev_source": "live_event_scan"})
    c.filter_name = a.filter_name
    assert create_alert_id(a) != create_alert_id(c)
