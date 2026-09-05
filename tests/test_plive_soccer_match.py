"""Conservative PLive soccer join (sport 5 + Top Soccer 220).

Odds-API event IDs join Odds-API books only. PLive is a separate
Pandora-id join. Fail closed on swap, stale, ambiguous, or team-only.
"""
from __future__ import annotations

import asyncio
import time

from plive_pandora import (
    PLIVE_LINE_SET,
    PLIVE_SPORT_CATALOG_FALLBACK,
    PlivePandoraFeed,
    PliveStore,
    coeff_room_for_event,
    match_plive_event_to_odds_doc,
    match_plive_soccer_to_odds_doc,
    plive_football_sport_ids,
    plive_soccer_sport_ids,
    plive_sport_id,
    plive_sport_ids,
    teams_same_orientation,
)


KICKOFF = 1_780_000_000
NOW = 1_780_000_050.0


def _odds_doc(**overrides):
    doc = {
        "sport": {"slug": "football"},
        "home": "Arsenal",
        "away": "Chelsea",
        "league": {"name": "English Premier League"},
        "startTime": KICKOFF,
        "live": True,
    }
    doc.update(overrides)
    return doc


def _plive_ev(eid: str = "5001", **overrides):
    ev = {
        "id": eid,
        "sport_id": 5,
        "home": "Arsenal FC",
        "away": "Chelsea",
        "league_name": "Premier League",
        "start": KICKOFF,
        "ip": True,
        "finished": False,
        "coeff_updated_at": NOW,
    }
    ev.update(overrides)
    return ev


def test_default_plive_sport_ids_include_mlb_football_and_soccer(monkeypatch):
    monkeypatch.delenv("PLIVE_SPORT_IDS", raising=False)
    monkeypatch.delenv("PLIVE_SPORT_ID", raising=False)
    monkeypatch.delenv("PLIVE_PAGE", raising=False)
    monkeypatch.delenv("PLIVE_HASH", raising=False)
    assert plive_sport_id() == 1
    assert plive_sport_ids() == [1, 3, 5, 220]
    assert plive_soccer_sport_ids() == (5, 220)
    assert plive_football_sport_ids() == (3,)
    assert PLIVE_SPORT_CATALOG_FALLBACK[3] == "Football"
    assert PLIVE_SPORT_CATALOG_FALLBACK[5] == "Soccer"
    assert PLIVE_SPORT_CATALOG_FALLBACK[220] == "Top Soccer"


def test_event_data_tree_keeps_sport_5_and_220():
    store = PliveStore()
    store.apply_message(
        {
            "isDiff": False,
            "payload": {
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
                            }
                        }
                    },
                    "5": {
                        "1": {
                            "39": {
                                "555001": [
                                    ["Arsenal", "", ""],
                                    ["Chelsea", "", ""],
                                    KICKOFF,
                                    None,
                                    {"ip": True, "leagueName": "Premier League"},
                                ]
                            }
                        }
                    },
                    "220": {
                        "1": {
                            "9": {
                                "220001": [
                                    ["Barcelona", "", ""],
                                    ["Real Madrid", "", ""],
                                    KICKOFF,
                                    None,
                                    {"ip": True, "leagueName": "La Liga"},
                                ]
                            }
                        }
                    },
                }
            },
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventData",
    )
    mlb = store.mlb_events()
    soccer = store.soccer_events()
    assert "199992971" in mlb
    assert "555001" not in mlb
    assert "220001" not in mlb
    assert "555001" in soccer
    assert soccer["555001"]["sport_id"] == 5
    assert soccer["555001"]["start"] == KICKOFF
    assert soccer["555001"]["ip"] is True
    assert "220001" in soccer
    assert soccer["220001"]["sport_id"] == 220
    assert store.wants_mlb_coeff(mlb["199992971"]) is True
    assert store.wants_coeff(soccer["555001"]) is True
    assert store.wants_coeff(soccer["220001"]) is True
    assert store.wants_mlb_coeff(soccer["555001"]) is False


def test_same_orientation_match_sport_5_and_220():
    events = {
        "5001": _plive_ev("5001", sport_id=5),
        "2201": _plive_ev(
            "2201",
            sport_id=220,
            home="Barcelona",
            away="Real Madrid",
            league_name="La Liga",
        ),
    }
    assert match_plive_soccer_to_odds_doc(events, _odds_doc(), now=NOW) == "5001"
    barca = _odds_doc(
        home="Barcelona",
        away="Real Madrid",
        league={"name": "La Liga"},
    )
    assert match_plive_soccer_to_odds_doc(events, barca, now=NOW) == "2201"


def test_swapped_home_away_is_rejected():
    events = {"5001": _plive_ev()}
    swapped = _odds_doc(home="Chelsea", away="Arsenal")
    assert teams_same_orientation("Chelsea", "Arsenal", "Arsenal FC", "Chelsea") is False
    assert match_plive_soccer_to_odds_doc(events, swapped, now=NOW) is None


def test_team_name_only_without_league_or_time_is_rejected():
    events = {
        "5001": _plive_ev(
            league_name="",
            start=None,
            ip=False,
            live=False,
        )
    }
    doc = _odds_doc(league="", startTime=None, live=False)
    doc.pop("league")
    assert match_plive_soccer_to_odds_doc(events, doc, now=NOW) is None


def test_conflicting_leagues_rejected():
    events = {"5001": _plive_ev(league_name="Premier League")}
    doc = _odds_doc(league={"name": "Championship"})
    assert match_plive_soccer_to_odds_doc(events, doc, now=NOW) is None


def test_ambiguous_two_plive_ids_rejected():
    events = {
        "5001": _plive_ev("5001", sport_id=5),
        "2201": _plive_ev("2201", sport_id=220),
    }
    assert match_plive_soccer_to_odds_doc(events, _odds_doc(), now=NOW) is None


def test_stale_coeff_rejected(monkeypatch):
    monkeypatch.setenv("PLIVE_STALE_SEC", "90")
    events = {"5001": _plive_ev(coeff_updated_at=NOW - 120)}
    assert match_plive_soccer_to_odds_doc(events, _odds_doc(), now=NOW) is None


def test_start_time_tolerance(monkeypatch):
    monkeypatch.setenv("PLIVE_START_TOLERANCE_SEC", "900")
    events = {"5001": _plive_ev(start=KICKOFF + 901, ip=False)}
    doc = _odds_doc(startTime=KICKOFF, live=False)
    assert match_plive_soccer_to_odds_doc(events, doc, now=NOW) is None
    events["5001"]["start"] = KICKOFF + 400
    assert match_plive_soccer_to_odds_doc(events, doc, now=NOW) == "5001"


def test_nyc_vs_new_york_city_does_not_match():
    """Conservative token identity — abbreviations are not expanded."""
    assert teams_same_orientation(
        "New York City", "Atlanta United", "NYC", "Atlanta United"
    ) is False


def test_mlb_swap_tolerant_matcher_unchanged():
    store = PliveStore()
    store.apply_meta(
        "99",
        {"home": "Boston Red Sox", "away": "New York Yankees", "sportId": 1},
    )
    eid = match_plive_event_to_odds_doc(
        store.mlb_events(), "New York Yankees", "Boston Red Sox"
    )
    assert eid == "99"


def test_markets_for_odds_event_soccer_fail_closed():
    feed = PlivePandoraFeed(connect_fn=lambda _f: None)
    feed.store.apply_meta(
        "5001",
        {
            "sportId": 5,
            "home": "Arsenal FC",
            "away": "Chelsea",
            "leagueName": "Premier League",
            "start": KICKOFF,
            "ip": True,
        },
    )
    feed.store.set_coeff("5001", 3, "1", 1, 1.80)
    feed.store.set_coeff("5001", 3, "2", 1, 2.10)
    ok = feed.markets_for_odds_event(_odds_doc())
    assert any(m.get("name") == "ML" for m in ok)
    swapped = feed.markets_for_odds_event(_odds_doc(home="Chelsea", away="Arsenal"))
    assert swapped == []
    feed.store.events["5001"]["coeff_updated_at"] = time.time() - 10_000
    assert feed.markets_for_odds_event(_odds_doc()) == []


def test_subscribe_coeffs_for_soccer_5_and_220():
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
        "555001",
        {"sportId": 5, "home": "Arsenal", "away": "Chelsea", "ip": True},
    )
    feed.store.apply_meta(
        "220001",
        {"sportId": 220, "home": "Barcelona", "away": "Real Madrid", "ip": True},
    )
    sio = _FakeSio()
    asyncio.run(feed._subscribe_mlb_coefficients(sio))
    r5 = coeff_room_for_event("555001")
    r220 = coeff_room_for_event("220001")
    assert r5 in feed._coeff_subscribed
    assert r220 in feed._coeff_subscribed
    feed.store.apply_meta("555001", {"finished": True})
    asyncio.run(feed._subscribe_mlb_coefficients(sio))
    assert r5 not in feed._coeff_subscribed
    assert r220 in feed._coeff_subscribed
