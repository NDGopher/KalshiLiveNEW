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
                {"op": "replace", "path": "/c/m/10/o/1/1", "value": 1.85},
                {"op": "replace", "path": "/c/m/10/o/2/1", "value": 2.05},
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
    store.set_coeff("7", 10, "1", 1, 1.9)
    store.set_coeff("7", 10, "2", 1, 2.0)
    assert store.markets_for_event("7")[0]["odds"][0]["home"] == 1.9
    store.set_coeff("7", 10, "1", 1, 1.7)
    assert store.markets_for_event("7")[0]["odds"][0]["home"] == 1.7


def test_team_match_to_odds_doc():
    store = PliveStore()
    store.apply_meta("99", {"home": "New York Yankees", "away": "Boston Red Sox", "sportId": 1})
    store.set_coeff("99", 10, "1", 1, 1.8)
    store.set_coeff("99", 10, "2", 1, 2.1)
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
    assert f"live.main.{PLIVE_LINE_SET}.eventCoefficients" in topics
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
    assert any(t.endswith(".eventData") for t in topics)
    assert any(t.endswith(".eventCoefficients") for t in topics)
    assert "live.events" not in topics
    assert coeff_room_for_event("199298371").endswith(".eventCoefficients.199298371")


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
    assert mlb["199992971"]["away"] == "Chicago Cubs"
    assert mlb["199992971"]["home"] == "Milwaukee Brewers"
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
                        "10": {"o": {"1": 1.29, "2": 3.45}},
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
    feed.store.set_coeff("99", 10, "1", 1, 1.8)
    feed.store.set_coeff("99", 10, "2", 1, 2.1)
    snap = feed.status_snapshot()
    assert snap["connected"] is True
    assert snap["partner_id"] == 113
    assert snap["flavor"] == "live"
    assert snap["sport_id"] == 1
    assert snap["mlb_events"] == 1
    assert snap["mlb_with_prices"] == 1
    assert snap["receiving_prices"] is True
    assert snap["samples"]


def test_market6_nested_pair_is_run_line_not_dead_scalars():
    """Dan dump: slot-1 [1.446, 2.65] is the live ±1.5; 8.86 / 1.045 are dead."""
    store = PliveStore()
    eid = "199298371"
    store.set_coeff(eid, 6, "-1.5", 0, 8.86)
    store.set_coeff(eid, 6, "-1.5", 1, [1.446, 2.65])
    store.set_coeff(eid, 6, "2.5", 1, [1.17, 4.6])
    store.set_coeff(eid, 5, "4.5", 1, 3.79)
    store.set_coeff(eid, 7, "2.5", 1, 4.25)
    mk = {m["name"]: m for m in store.markets_for_event(eid)}
    assert "Spread" in mk
    rows = mk["Spread"]["odds"]
    live = next(r for r in rows if abs(float(r["hdp"])) == 1.5)
    assert live["home"] == 1.446
    assert live["away"] == 2.65
    assert all(r["home"] != 8.86 for r in rows)
    assert all(r["home"] != 3.79 for r in rows)
    names = set(mk)
    assert "Spread" in names
    # Team totals (7) must not become a spread row.
    assert not any(abs(float(r.get("hdp") or 0) - 4.5) < 1e-9 for r in rows)


def test_unpriced_run_line_omits_plive_spread_tile():
    store = PliveStore()
    store.set_coeff("1", 6, "-1.5", 0, 8.86)
    store.set_coeff("1", 6, "-1.5", 1, 1.045)
    store.set_coeff("1", 5, "4.5", 0, 3.79)
    store.set_coeff("1", 5, "4.5", 1, 1.30)
    names = {m["name"] for m in store.markets_for_event("1")}
    assert "Spread" not in names
    assert "Totals" in names


def test_market3_is_not_ml_column():
    store = PliveStore()
    store.set_coeff("e", 3, "1", 1, 2.61)
    store.set_coeff("e", 3, "2", 1, 1.463)
    store.set_coeff("e", 10, "1", 1, 1.69)
    store.set_coeff("e", 10, "2", 1, 2.10)
    ml = next(m for m in store.markets_for_event("e") if m["name"] == "ML")
    assert ml["odds"][0]["home"] == 1.69
    assert ml["odds"][0]["away"] == 2.10


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


def test_event_199298371_pair_parse_and_away_label():
    """Dump 199298371: nested [home, away] on market 6; Odds-API Sox @ Astros."""
    from odds_ev_monitor import _decimal_for_side, _pick_qualifier_line_for_side

    store = PliveStore()
    eid = "199298371"
    store.apply_message(
        {
            "isDiff": False,
            "payload": {
                "c": {
                    "m": {
                        "3": {"o": {"1": 2.61, "2": 1.462963}},
                        "5": {"o": {"4.5": [3.79, 1.240385]}},
                        "6": {
                            "o": {
                                "-1.5": {0: 8.86, 1: 1.045065},
                                "1": [1.847458, 1.892857],
                                "1.5": {1: [1.446429, 2.65]},
                                "2.5": {0: 1.172712, 1: 4.6},
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
    # Alt +2.5 from click-in stays. Dead 8.86/−1.5 scalars do not.
    assert any(abs(float(r["hdp"]) - 2.5) < 1e-9 for r in rows)
    live15 = next(r for r in rows if abs(float(r["hdp"]) - 1.5) < 1e-9)
    assert live15["home"] == 1.446429
    assert live15["away"] == 2.65
    assert all(r.get("home") != 8.86 for r in rows)
    assert all(r.get("home") != 3.79 for r in rows)
    assert all(r.get("home") != 4.97 for r in rows)
    assert "ML" not in by_name or by_name["ML"]["odds"][0]["home"] != 2.61

    # Odds-API fixture: White Sox @ Astros. Away label is −1.5, not +1.5.
    odds_home, odds_away = "Houston Astros", "Chicago White Sox"
    home_pick, home_qual, home_line = _pick_qualifier_line_for_side(
        odds_home, odds_away, "Spread", "home", live15
    )
    away_pick, away_qual, away_line = _pick_qualifier_line_for_side(
        odds_home, odds_away, "Spread", "away", live15
    )
    assert home_pick == odds_home
    assert home_line == 1.5
    assert home_qual == "+1.5"
    assert away_pick == odds_away
    assert away_line == -1.5
    assert away_qual == "-1.5"
    assert _decimal_for_side(live15, "home") == 1.446429
    assert _decimal_for_side(live15, "away") == 2.65


def test_merge_keeps_odds_api_plive_ml():
    existing = [{"name": "ML", "odds": [{"home": 1.69, "away": 2.10}]}]
    incoming = [{"name": "Spread", "odds": [{"hdp": 1.5, "home": 1.45, "away": 2.65}]}]
    merged = merge_plive_market_lists(existing, incoming)
    ml = next(m for m in merged if m["name"] == "ML")
    assert ml["odds"][0]["home"] == 1.69
    assert any(m["name"] == "Spread" for m in merged)
