"""PLive Pandora client tests — no live Socket.IO connection."""
from __future__ import annotations

from plive_pandora import (
    EXPECTED_SUBSCRIBED_ROOMS,
    EXPECTED_SYSTEM_EVENT_ROOMS,
    PLIVE_BOOK_NAME,
    PLIVE_LINE_SET,
    PLIVE_MLB_LEAGUE_ID,
    PLIVE_PARTNER_ID,
    PLIVE_SPORT_CATALOG_FALLBACK,
    PlivePandoraFeed,
    PliveStore,
    align_plive_markets_to_odds_fixture,
    coeff_room_for_event,
    event_id_from_channel,
    extra_local_bookmakers,
    handshake_emits,
    match_plive_event_to_odds_doc,
    merge_plive_market_lists,
    plive_orientation_swapped_vs_odds,
    plive_sport_id,
    note_handshake_ack,
    parse_coeff_path,
    parse_event_hash,
    parse_sport_hash,
    plive_wanted,
    public_ui_subscribe_topics,
)


def test_plive_enabled_by_default(monkeypatch):
    monkeypatch.delenv("PLIVE_ENABLED", raising=False)
    assert plive_wanted() is True
    monkeypatch.setenv("PLIVE_ENABLED", "false")
    assert plive_wanted() is False


def test_parse_coeff_path_matches_unified_betting():
    p = parse_coeff_path("/c/m/10/o/2/0")
    assert p == {"market": 10, "outcome": "2", "index": 0, "full_path": "/c/m/10/o/2/0"}
    p2 = parse_coeff_path("/c/m/10/o/2/1")
    assert p2["index"] == 1
    assert parse_coeff_path("/foo") is None


def test_event_id_from_channel():
    ch = "live.main.U0VWU1NWUkJSMFU9.eventCoefficients.170286421"
    assert event_id_from_channel(ch) == "170286421"


def test_json_patch_builds_mlb_moneyline():
    store = PliveStore()
    eid = "170286421"
    store.apply_message(
        {
            "isDiff": True,
            "payload": [
                {"op": "replace", "path": "/c/m/3/o/1/1", "value": 1.85},
                {"op": "replace", "path": "/c/m/3/o/2/1", "value": 2.05},
            ],
        },
        event_name=f"live.main.xxx.eventCoefficients.{eid}",
    )
    mk = store.markets_for_event(eid)
    names = [m["name"] for m in mk]
    assert "ML" in names
    ml = next(m for m in mk if m["name"] == "ML")
    assert ml["odds"][0]["home"] == 1.85
    assert ml["odds"][0]["away"] == 2.05


def test_totals_and_spread_two_way():
    store = PliveStore()
    eid = "1"
    store.apply_json_patch(
        eid,
        [
            {"op": "replace", "path": "/c/m/5/o/over_8.5/1", "value": 1.91},
            {"op": "replace", "path": "/c/m/5/o/under_8.5/1", "value": 1.91},
            {"op": "replace", "path": "/c/m/6/o/-1.5/1", "value": 1.95},
            {"op": "replace", "path": "/c/m/6/o/1.5/1", "value": 1.87},
        ],
    )
    names = {m["name"] for m in store.markets_for_event(eid)}
    assert "Totals" in names
    assert "Spread" in names


def test_replace_not_merge_markets():
    store = PliveStore()
    store.set_coeff("7", 3, "1", 1, 1.9)
    store.set_coeff("7", 3, "2", 1, 2.0)
    assert store.markets_for_event("7")[0]["odds"][0]["home"] == 1.9
    store.set_coeff("7", 3, "1", 1, 1.7)
    assert store.markets_for_event("7")[0]["odds"][0]["home"] == 1.7


def test_team_match_to_odds_doc():
    store = PliveStore()
    store.apply_meta("99", {"home": "New York Yankees", "away": "Boston Red Sox", "sportId": 1})
    store.set_coeff("99", 3, "1", 1, 1.8)
    store.set_coeff("99", 3, "2", 1, 2.1)
    eid = match_plive_event_to_odds_doc(
        store.mlb_events(), "New York Yankees", "Boston Red Sox"
    )
    assert eid == "99"
    assert PLIVE_BOOK_NAME == "PLive"


def test_plive_is_its_own_book_not_betfair():
    from odds_api_client import _canonical_odds_api_bookmaker, api_wire_bookmakers

    assert _canonical_odds_api_bookmaker("PLive") == "PLive"
    assert _canonical_odds_api_bookmaker("plive") == "PLive"
    assert _canonical_odds_api_bookmaker("PLive") != "Betfair Exchange"
    assert "PLive" not in api_wire_bookmakers(["PLive", "Betfair Exchange", "Kalshi"])
    assert extra_local_bookmakers() == ["PLive"] or extra_local_bookmakers() == []


def test_sport_1_filter_drops_other_sports():
    store = PliveStore()
    store.apply_meta("mlb", {"sportId": 1, "home": "A", "away": "B"})
    store.apply_meta("nba", {"sportId": 2, "home": "C", "away": "D"})
    assert "mlb" in store.mlb_events()
    assert "nba" not in store.mlb_events()


def test_mlb_is_catalog_sport_1(monkeypatch):
    monkeypatch.delenv("PLIVE_PAGE", raising=False)
    monkeypatch.delenv("PLIVE_HASH", raising=False)
    monkeypatch.delenv("PLIVE_SPORT_ID", raising=False)
    assert PLIVE_SPORT_CATALOG_FALLBACK[1] == "Baseball"
    assert plive_sport_id() == 1


def test_handshake_matches_public_ui():
    emits = handshake_emits()
    names = [e[0] for e in emits]
    assert names == ["setSocketMetadata", "subscribeSystemEvents", "subscribe", "getCache"]
    assert emits[0][1] == {"partnerId": PLIVE_PARTNER_ID, "flavor": "live"}
    assert emits[1][1] == {"partnerId": 113}
    topics = emits[2][1]
    assert "live.sports" in topics
    assert "live.events" not in topics
    assert f"live.main.{PLIVE_LINE_SET}.eventData" in topics
    assert f"live.main.{PLIVE_LINE_SET}.eventCoefficients" not in topics
    assert not any(t.endswith(".eventCoefficients") for t in topics)
    assert "live.leagues" not in topics
    assert emits[3][1] == topics
    for room in EXPECTED_SUBSCRIBED_ROOMS:
        assert room in topics
    assert EXPECTED_SYSTEM_EVENT_ROOMS == ("system-events", "notifications.partner.113")
    assert note_handshake_ack("setSocketMetadata", {"event": "socketMetadataSet", "data": {}}) == "socketMetadataSet"
    assert note_handshake_ack("subscribedSystemEvents", {"rooms": list(EXPECTED_SYSTEM_EVENT_ROOMS)}) == "subscribedSystemEvents"


def test_sport_hash_top_soccer_is_220_not_mlb():
    assert parse_sport_hash("#!/sport/220") == 220
    assert parse_sport_hash("https://plive.becoms.co/live/?#!/sport/220") == 220
    assert parse_sport_hash("#!/sport/1") == 1
    assert PLIVE_SPORT_CATALOG_FALLBACK[1] == "Baseball"
    assert PLIVE_SPORT_CATALOG_FALLBACK[220] == "Top Soccer"
    # Live catalog, not the old Selenium nfl=2 / nba=3 map.
    assert PLIVE_SPORT_CATALOG_FALLBACK[2] == "Basketball"
    assert PLIVE_SPORT_CATALOG_FALLBACK[3] == "Football"


def test_event_data_snapshot_extracts_teams(monkeypatch):
    store = PliveStore()
    store.apply_message(
        {
            "isDiff": False,
            "payload": {
                "1701": {
                    "id": 1701,
                    "si": 1,
                    "p": {"1": {"n": "Boston Red Sox"}, "2": {"n": "New York Yankees"}},
                },
                "2209": {
                    "id": 2209,
                    "si": 220,
                    "p": {"1": {"n": "Arsenal"}, "2": {"n": "Chelsea"}},
                },
            },
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventData",
    )
    assert "1701" in store.mlb_events()
    ev = store.events["1701"]
    assert ev["away"] == "Boston Red Sox"
    assert ev["home"] == "New York Yankees"
    assert "2209" not in store.mlb_events()
    assert coeff_room_for_event("1701").endswith(".eventCoefficients.1701")


def test_live_sports_catalog_overrides_fallback():
    store = PliveStore()
    store.apply_message(
        {"1": {"id": 1, "name": "Baseball"}, "2": {"id": 2, "name": "Basketball"}},
        event_name="live.sports",
    )
    assert store.sport_catalog[1] == "Baseball"
    assert store.sport_catalog[2] == "Basketball"


def test_public_ui_topics_include_required_rooms():
    topics = public_ui_subscribe_topics()
    assert topics == ["live.sports", f"live.main.{PLIVE_LINE_SET}.eventData"]
    assert any(t.endswith(".eventData") for t in topics)
    assert not any(t.endswith(".eventCoefficients") for t in topics)
    assert "live.events" not in topics
    assert coeff_room_for_event("199298371") == (
        f"live.main.{PLIVE_LINE_SET}.eventCoefficients.199298371"
    )


def test_parse_event_hash_is_pandora_id_no_html():
    """#!/event/{id} is a client-side route. Do not scrape the HTML page."""
    assert parse_event_hash("https://plive.becoms.co/live/?#!/event/199298371") == "199298371"
    assert parse_event_hash("#!/event/199298371") == "199298371"
    assert parse_event_hash("https://plive.becoms.co/live/?#!/sport/1") is None
    assert parse_event_hash("") is None


def test_unsubscribe_finished_mlb_coefficients():
    import asyncio

    class _FakeSio:
        def __init__(self) -> None:
            self.emits: list = []

        async def emit(self, event, payload=None):
            self.emits.append((event, payload))

        def on(self, _name):
            def _deco(fn):
                return fn

            return _deco

    feed = PlivePandoraFeed(connect_fn=lambda _f: None)
    feed.store.apply_meta(
        "199298371",
        {
            "home": "Houston Astros",
            "away": "Chicago White Sox",
            "sportId": 1,
            "leagueId": 8,
            "ip": True,
        },
    )
    sio = _FakeSio()
    asyncio.run(feed._subscribe_mlb_coefficients(sio))
    room = coeff_room_for_event("199298371")
    assert room in feed._coeff_subscribed
    assert any(e[0] == "subscribe" and room in (e[1] or []) for e in sio.emits)
    assert any(e[0] == "getCache" and room in (e[1] or []) for e in sio.emits)

    feed.store.apply_meta("199298371", {"finished": True})
    assert feed.store.wants_mlb_coeff(feed.store.events["199298371"]) is False
    asyncio.run(feed._subscribe_mlb_coefficients(sio))
    assert room not in feed._coeff_subscribed
    assert any(e[0] == "unsubscribe" and room in (e[1] or []) for e in sio.emits)


def test_event_data_s_tree_marks_missing_mlb_finished():
    store = PliveStore()
    store.apply_message(
        {
            "isDiff": False,
            "payload": {
                "s": {
                    "1": {
                        "2": {
                            "8": {
                                "199298371": [
                                    ["Houston Astros", "", "", 2],
                                    ["Chicago White Sox", "", "", 1],
                                    1,
                                    None,
                                    {"ip": True},
                                ]
                            }
                        }
                    }
                }
            },
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventData",
    )
    assert store.wants_mlb_coeff(store.events["199298371"]) is True
    store.apply_message(
        {
            "isDiff": False,
            "payload": {"s": {"1": {"2": {"8": {}}}}},
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventData",
    )
    assert store.events["199298371"].get("finished") is True
    assert store.wants_mlb_coeff(store.events["199298371"]) is False


def test_event_data_s_tree_extracts_mlb_teams():
    store = PliveStore()
    store.apply_message(
        {
            "isDiff": False,
            "payload": {
                "db": {"x": 1},
                "s": {
                    "1": {
                        "2": {
                            "8": {
                                "199992971": [
                                    ["Chicago Cubs", "", "", 1],
                                    ["Milwaukee Brewers", "", "", 2],
                                    1788392100,
                                    None,
                                    {"ip": True},
                                ]
                            },
                            "3415": {
                                "188359511": [
                                    ["Nashville Sounds", "", ""],
                                    ["Louisville Bats", "", ""],
                                    1,
                                ]
                            },
                        }
                    },
                    "220": {
                        "1": {
                            "9": {
                                "1": [
                                    ["Arsenal", "", ""],
                                    ["Chelsea", "", ""],
                                    1,
                                ]
                            }
                        }
                    },
                },
            },
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventData",
    )
    mlb = store.mlb_events()
    assert "199992971" in mlb
    # Live tree is [home, away]. Row 0 is stadium home.
    assert mlb["199992971"]["home"] == "Chicago Cubs"
    assert mlb["199992971"]["away"] == "Milwaukee Brewers"
    assert mlb["199992971"].get("league_id") == PLIVE_MLB_LEAGUE_ID
    assert store.wants_mlb_coeff(mlb["199992971"]) is True
    assert "188359511" in mlb  # still baseball; Odds-API team-match drops MiLB
    assert store.wants_mlb_coeff(mlb["188359511"]) is False
    assert "1" not in mlb


def test_game_period_markets_ml_spread_totals():
    store = PliveStore()
    store.apply_message(
        {
            "isDiff": False,
            "payload": {
                "id": 199992971,
                "c": {
                    "m": {
                        "3": {"o": {"1": 1.29, "2": 3.45}},
                        "5": {"o": {"14.5": [1.80, 1.94]}, "r": 14.5},
                        "6": {"o": {"-2": [1.93, 1.81]}, "r": -2},
                    }
                },
            },
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventCoefficients.199992971",
    )
    by_name = {m["name"]: m for m in store.markets_for_event("199992971")}
    assert by_name["ML"]["odds"][0]["home"] == 1.29
    assert by_name["ML"]["odds"][0]["away"] == 3.45
    assert by_name["Totals"]["odds"][0]["hdp"] == 14.5
    assert by_name["Totals"]["odds"][0]["max"] == 14.5
    assert by_name["Totals"]["odds"][0]["line"] == 14.5
    assert by_name["Totals"]["odds"][0]["over"] == 1.80
    assert by_name["Totals"]["odds"][0]["under"] == 1.94
    assert by_name["Spread"]["odds"][0]["hdp"] == -2.0
    assert by_name["Spread"]["odds"][0]["home"] == 1.93
    assert by_name["Spread"]["odds"][0]["away"] == 1.81


def test_status_snapshot_reports_priced_mlb():
    feed = PlivePandoraFeed(connect_fn=lambda _f: None)
    feed.connected = True
    feed._running = True
    feed.store.apply_meta("99", {"home": "New York Yankees", "away": "Boston Red Sox", "sportId": 1})
    feed.store.set_coeff("99", 3, "1", 1, 1.8)
    feed.store.set_coeff("99", 3, "2", 1, 2.1)
    snap = feed.status_snapshot()
    assert snap["connected"] is True
    assert snap["partner_id"] == 113
    assert snap["flavor"] == "live"
    assert snap["sport_id"] == 1
    assert snap["mlb_events"] == 1
    assert snap["mlb_with_prices"] == 1
    assert snap["receiving_prices"] is True
    assert snap["samples"]


def test_event_data_199298371_is_home_then_away():
    """Live path s/1/2/8/199298371: [0]=Astros home, [1]=Sox away."""
    store = PliveStore()
    store.apply_message(
        {
            "isDiff": False,
            "payload": {
                "s": {
                    "1": {
                        "2": {
                            "8": {
                                "199298371": [
                                    ["Houston Astros", "", "", 2],
                                    ["Chicago White Sox", "", "", 1],
                                    1788392100,
                                    None,
                                    {"ip": True},
                                ]
                            }
                        }
                    }
                }
            },
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventData",
    )
    ev = store.events["199298371"]
    assert ev["home"] == "Houston Astros"
    assert ev["away"] == "Chicago White Sox"
    assert ev.get("league_id") == PLIVE_MLB_LEAGUE_ID
    assert ev.get("sport_id") == 1
    # Odds-API fixture is Sox @ Astros — same orientation, no price remap.
    assert match_plive_event_to_odds_doc(
        store.mlb_events(), "Houston Astros", "Chicago White Sox"
    ) == "199298371"


def test_market3_is_game_winner_not_first5():
    """Market 3 idx1 is Game Winner. Market 10 (first-5 / other) must not paint ML."""
    store = PliveStore()
    store.set_coeff("e", 3, "1", 0, 3.89)
    store.set_coeff("e", 3, "1", 1, 2.61)
    store.set_coeff("e", 3, "2", 0, 2.20)
    store.set_coeff("e", 3, "2", 1, 1.463)
    store.set_coeff("e", 10, "1", 1, 1.69)
    store.set_coeff("e", 10, "2", 1, 2.10)
    ml = next(m for m in store.markets_for_event("e") if m["name"] == "ML")
    assert ml["odds"][0]["home"] == 2.61
    assert ml["odds"][0]["away"] == 1.463
    assert ml["odds"][0].get("plive_market") == 3
    assert ml["odds"][0]["home"] != 1.69
    assert ml["odds"][0]["home"] != 3.89


def test_do_not_remap_plive_labels_to_odds_api():
    """Odds-API fixture axis wins. Do not flip prices from Pandora t1/t2 names."""
    assert plive_orientation_swapped_vs_odds(
        "Chicago White Sox", "Houston Astros", "Houston Astros", "Chicago White Sox"
    )
    markets = [
        {"name": "Spread", "odds": [{"hdp": -1.5, "home": 2.65, "away": 1.446}]},
        {"name": "ML", "odds": [{"home": 2.10, "away": 1.69}]},
    ]
    aligned = align_plive_markets_to_odds_fixture(
        markets,
        plive_home="Chicago White Sox",
        plive_away="Houston Astros",
        odds_home="Houston Astros",
        odds_away="Chicago White Sox",
    )
    sp = next(m for m in aligned if m["name"] == "Spread")
    assert sp["odds"][0]["hdp"] == -1.5
    assert sp["odds"][0]["home"] == 2.65
    assert sp["odds"][0]["away"] == 1.446
    ml = next(m for m in aligned if m["name"] == "ML")
    assert ml["odds"][0]["home"] == 2.10


def test_event_199298371_live_ws_dump_pair_and_away_sign():
    """Verified live dump ~9:03 CT. Odds-API: Sox @ Astros, Astros home."""
    from odds_ev_monitor import (
        _decimal_for_side,
        _pick_matching_odds_row,
        _pick_qualifier_line_for_side,
    )

    store = PliveStore()
    eid = "199298371"
    store.apply_message(
        {
            "isDiff": False,
            "payload": {
                "c": {
                    "m": {
                        "3": {"o": {"1": {"1": 2.11}, "2": {"1": 1.675676}}},
                        "5": {"o": {"4.5": [3.79, 1.240385]}},
                        "6": {
                            "o": {
                                "-1.5": {0: 9.78, 1: 1.03358},
                                "1.5": {0: 1.671141, 1: 2.12},
                                "2.5": {0: 1.301205, 1: 3.32},
                            }
                        },
                        "7": {"o": {"2.5": {0: 4.97, 1: 1.144928}}},
                        "8": {"o": {"2.5": {0: 3.12, 1: 1.319489}}},
                    }
                }
            },
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventCoefficients.{eid}",
    )
    by_name = {m["name"]: m for m in store.markets_for_event(eid)}
    assert "Spread" in by_name
    rows = by_name["Spread"]["odds"]
    minus15 = next(r for r in rows if abs(float(r["hdp"]) + 1.5) < 1e-9)
    plus15 = next(r for r in rows if abs(float(r["hdp"]) - 1.5) < 1e-9)
    plus25 = next(r for r in rows if abs(float(r["hdp"]) - 2.5) < 1e-9)
    # 2-way pair: idx0 home, idx1 away. 9.78 / 1.03 is a real ~7% hold, keep it.
    assert minus15["home"] == 9.78
    assert minus15["away"] == 1.03358
    assert minus15["line_style"] == "american"
    assert plus15["home"] == 1.671141
    assert plus15["away"] == 2.12
    assert plus25["home"] == 1.301205
    assert plus25["away"] == 3.32
    # Guard: game total +279 and team totals never sit on Spread.
    assert all(r.get("home") not in (3.79, 4.97, 3.12, 4.25, 4.1) for r in rows)
    assert all(r.get("away") not in (4.97, 3.12, 4.25, 4.1) for r in rows)
    assert all(r.get("plive_market") == 6 for r in rows)
    # Market 3 Game Winner (+111 / -148) is the live ML take.
    assert "ML" in by_name
    assert by_name["ML"]["odds"][0]["home"] == 2.11
    assert abs(float(by_name["ML"]["odds"][0]["away"]) - 1.675676) < 1e-9

    odds_home, odds_away = "Houston Astros", "Chicago White Sox"
    # Home-centric: Astros −1.5 stays −1.5. Away Sox on hdp −1.5 is +1.5.
    h_pick, h_qual, h_line = _pick_qualifier_line_for_side(
        odds_home, odds_away, "Spread", "home", minus15
    )
    a_pick, a_qual, a_line = _pick_qualifier_line_for_side(
        odds_home, odds_away, "Spread", "away", minus15
    )
    assert h_pick == odds_home
    assert h_line == -1.5
    assert h_qual == "-1.5"
    assert a_pick == odds_away
    assert a_line == 1.5
    assert a_qual == "+1.5"
    assert _decimal_for_side(minus15, "home") == 9.78
    assert _decimal_for_side(minus15, "away") == 1.03358
    totals = {"name": "Totals", "odds": [{"hdp": 4.5, "over": 3.79, "under": 1.24}]}
    assert _pick_matching_odds_row(totals, "Spread", {"hdp": -1.5}) == {}
    existing_ml = [{"name": "ML", "odds": [{"home": 1.662, "away": 2.14}]}]
    merged = merge_plive_market_lists(existing_ml, store.markets_for_event(eid))
    ml = next(m for m in merged if m["name"] == "ML")
    assert ml["odds"][0]["home"] == 2.11
    assert ml["odds"][0]["home"] != 1.662


def test_event_199295331_market5_totals_not_on_spread():
    """DET@MIN live market 5: 11.5 over 1.892857 / under 1.847458. Market 7 stays off Spread."""
    store = PliveStore()
    eid = "199295331"
    store.apply_message(
        {
            "isDiff": False,
            "payload": {
                "c": {
                    "m": {
                        "5": {
                            "o": {
                                "11.5": {0: 1.892857, 1: 1.847458},
                                "12.5": {0: 2.47, 1: 1.502513},
                            }
                        },
                        "7": {"o": {"5.5": {0: 1.220264, 1: 3.75}}},
                    }
                }
            },
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventCoefficients.{eid}",
    )
    by_name = {m["name"]: m for m in store.markets_for_event(eid)}
    assert "Totals" in by_name
    t11 = next(r for r in by_name["Totals"]["odds"] if abs(float(r["hdp"]) - 11.5) < 1e-9)
    assert t11["over"] == 1.892857
    assert t11["under"] == 1.847458
    assert t11.get("max") == 11.5
    assert t11.get("line") == 11.5
    t12 = next(r for r in by_name["Totals"]["odds"] if abs(float(r["hdp"]) - 12.5) < 1e-9)
    assert t12["over"] == 2.47
    assert t12["under"] == 1.502513
    # Later hunt: same event, 11.5 moved to {0: 1.735294, 1: 2.03}. Nested lists are 7/8, not 5.
    store.apply_message(
        {
            "isDiff": False,
            "payload": {"c": {"m": {"5": {"o": {"11.5": {0: 1.735294, 1: 2.03}}}}}},
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventCoefficients.{eid}",
    )
    later = next(
        r
        for r in next(m for m in store.markets_for_event(eid) if m["name"] == "Totals")["odds"]
        if abs(float(r["hdp"]) - 11.5) < 1e-9
    )
    assert later["over"] == 1.735294
    assert later["under"] == 2.03
    assert later.get("max") == 11.5
    assert later.get("line") == 11.5
    if "Spread" in by_name:
        assert all(
            r.get("home") != 1.220264 and r.get("away") != 3.75
            for r in by_name["Spread"]["odds"]
        )
    assert all(r.get("over") != 1.220264 for r in by_name["Totals"]["odds"])


def test_merge_drops_odds_api_plive_ml():
    """Odds-API / Kalshi-copied PLive is not a live coeff. Never keep it."""
    existing = [{"name": "ML", "odds": [{"home": 1.69, "away": 2.10}]}]
    incoming = [{"name": "Spread", "odds": [{"hdp": 1.5, "home": 1.45, "away": 2.65}]}]
    merged = merge_plive_market_lists(existing, incoming)
    assert all(m.get("name") != "ML" for m in merged)
    assert any(m["name"] == "Spread" for m in merged)


def test_event_199298371_sox_tt_over_325_never_on_spread():
    """7th 0–0 Game tab: Astros −1.5 is —. Only +325 is Sox team total Over 2.5."""
    from ev_calculator import american_to_decimal, decimal_to_american
    from odds_ev_monitor import (
        _build_display_books_payload,
        _pick_matching_odds_row,
        _pick_qualifier_line_for_side,
    )

    over_325 = american_to_decimal(325)
    astros_plus15 = american_to_decimal(-392)
    sox_minus15 = american_to_decimal(266)
    astros_ml = american_to_decimal(-145)
    sox_ml = american_to_decimal(110)
    assert abs(over_325 - 4.25) < 1e-9
    store = PliveStore()
    eid = "199298371"
    store.spread_markets = (6, 7, 8)
    store.apply_message(
        {
            "isDiff": False,
            "payload": {
                "c": {
                    "m": {
                        "3": {"o": {"1": {"1": astros_ml}, "2": {"1": sox_ml}}},
                        "6": {
                            "o": {
                                "1.5": {0: astros_plus15, 1: sox_minus15},
                            }
                        },
                        "7": {"o": {"2.5": {0: over_325, 1: 1.144928}}},
                        "8": {"o": {"2.5": {0: 3.12, 1: 1.319489}}},
                    }
                }
            },
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventCoefficients.{eid}",
    )
    by_name = {m["name"]: m for m in store.markets_for_event(eid)}
    assert "Spread" in by_name
    rows = by_name["Spread"]["odds"]
    assert all(abs(float(r["hdp"]) + 1.5) > 1e-9 for r in rows)
    plus15 = next(r for r in rows if abs(float(r["hdp"]) - 1.5) < 1e-9)
    assert abs(float(plus15["home"]) - astros_plus15) < 1e-6
    assert abs(float(plus15["away"]) - sox_minus15) < 1e-6
    assert plus15["plive_market"] == 6
    assert all(r.get("plive_market") == 6 for r in rows)
    assert all(abs(float(r.get("home") or 0) - over_325) > 1e-6 for r in rows)
    assert all(abs(float(r.get("away") or 0) - over_325) > 1e-6 for r in rows)
    assert decimal_to_american(plus15["away"]) == 266
    assert decimal_to_american(plus15["home"]) == -392

    odds_home, odds_away = "Houston Astros", "Chicago White Sox"
    _h, h_qual, h_line = _pick_qualifier_line_for_side(
        odds_home, odds_away, "Spread", "home", plus15
    )
    a_pick, a_qual, a_line = _pick_qualifier_line_for_side(
        odds_home, odds_away, "Spread", "away", plus15
    )
    assert h_line == 1.5 and h_qual == "+1.5"
    assert a_pick == odds_away
    assert a_line == -1.5
    assert a_qual == "-1.5"

    leaked = {
        "name": "Spread",
        "odds": [
            {"hdp": -1.5, "home": over_325, "away": 1.14, "plive_market": 7, "market_type": "team_total"},
            plus15,
        ],
    }
    assert _pick_matching_odds_row(leaked, "Spread", {"hdp": -1.5}) == {}
    assert _pick_matching_odds_row(leaked, "Spread", {"hdp": 1.5})["home"] == plus15["home"]

    bks = {
        "Kalshi": [{"name": "Spread", "odds": [{"hdp": -1.5, "home": 1.80, "away": 2.10}]}],
        "PLive": store.markets_for_event(eid),
    }
    painted = _build_display_books_payload(
        "Houston Astros",
        bks,
        "Spread",
        "home",
        ["Kalshi", "PLive"],
        -110,
        {"hdp": -1.5, "home": 1.80, "away": 2.10},
        take_book="Kalshi",
    )
    plive_tiles = [r for r in painted["Houston Astros"] if str(r.get("book")) == "PLive"]
    assert plive_tiles == []
    assert all(int(r.get("odds") or 0) != 325 for r in painted["Houston Astros"])


def test_stl_lad_market3_game_winner_is_176_not_289():
    """2026-09-02 ~11:30 CT: public UI Cardinals +176 / Dodgers −242.

    idx0 +289 and market-10 first-5 must not become the ML take. Live market 3
    replaces a stale Odds-API PLive ML that still says +289.
    """
    from ev_calculator import american_to_decimal, decimal_to_american

    store = PliveStore()
    eid = "199312002"
    lad = american_to_decimal(-242)
    stl = american_to_decimal(176)
    fake_289 = american_to_decimal(289)
    store.apply_message(
        {
            "isDiff": False,
            "payload": {
                "s": {
                    "1": {
                        "2": {
                            "8": {
                                eid: [
                                    ["Los Angeles Dodgers", "", "", 2],
                                    ["St. Louis Cardinals", "", "", 1],
                                    1788392100,
                                    None,
                                    {"ip": True},
                                ]
                            }
                        }
                    }
                }
            },
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventData",
    )
    store.apply_message(
        {
            "isDiff": False,
            "payload": {
                "c": {
                    "m": {
                        "3": {
                            "o": {
                                "1": {0: 1.55, 1: lad},
                                "2": {0: fake_289, 1: stl},
                            }
                        },
                        "10": {"o": {"1": {"1": 1.80}, "2": {"1": fake_289}}},
                        "5": {"o": {"9.5": [1.91, 1.91]}},
                        "6": {"o": {"1.5": {0: 1.80, 1: 2.00}}},
                        "7": {"o": {"4.5": {0: 1.90, 1: 1.90}}},
                    }
                }
            },
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventCoefficients.{eid}",
    )
    ev = store.events[eid]
    assert ev["home"] == "Los Angeles Dodgers"
    assert ev["away"] == "St. Louis Cardinals"
    ml = next(m for m in store.markets_for_event(eid) if m["name"] == "ML")
    assert decimal_to_american(ml["odds"][0]["home"]) == -242
    assert decimal_to_american(ml["odds"][0]["away"]) == 176
    assert decimal_to_american(ml["odds"][0]["away"]) != 289
    assert ml["odds"][0].get("plive_market") == 3

    stale = [{"name": "ML", "odds": [{"home": american_to_decimal(-150), "away": fake_289}]}]
    merged = merge_plive_market_lists(stale, store.markets_for_event(eid))
    take_away = decimal_to_american(next(m for m in merged if m["name"] == "ML")["odds"][0]["away"])
    assert take_away == 176
    assert take_away != 289

    # Later patch: idx1 moves with the board. idx0 staying at 3.89 must not win.
    store.apply_message(
        {
            "isDiff": True,
            "payload": [
                {"op": "replace", "path": "/c/m/3/o/2/1", "value": stl},
                {"op": "replace", "path": "/c/m/3/o/2/0", "value": fake_289},
            ],
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventCoefficients.{eid}",
    )
    later = next(m for m in store.markets_for_event(eid) if m["name"] == "ML")
    assert decimal_to_american(later["odds"][0]["away"]) == 176
