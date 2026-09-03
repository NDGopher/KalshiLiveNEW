"""PLive Pandora client tests — no live Socket.IO connection."""
from __future__ import annotations

from plive_pandora import (
    PLIVE_BOOK_NAME,
    PliveStore,
    event_id_from_channel,
    match_plive_event_to_odds_doc,
    parse_coeff_path,
    plive_wanted,
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
            {"op": "replace", "path": "/c/m/2/o/-1.5/1", "value": 1.95},
            {"op": "replace", "path": "/c/m/2/o/1.5/1", "value": 1.87},
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


def test_sport_1_filter_drops_other_sports():
    store = PliveStore()
    store.apply_meta("mlb", {"sportId": 1, "home": "A", "away": "B"})
    store.apply_meta("nba", {"sportId": 2, "home": "C", "away": "D"})
    assert "mlb" in store.mlb_events()
    assert "nba" not in store.mlb_events()
