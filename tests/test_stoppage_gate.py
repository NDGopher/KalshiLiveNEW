"""Odds-API stoppage gate. No BookieBeats. No invented baseball clock."""
from __future__ import annotations

from pathlib import Path

from stoppage_gate import (
    clock_fields_for_live_odds,
    is_baseball_event,
    is_timed_sport_event,
    live_take_blocked_by_stoppage,
    stoppage_allows_live_take,
)
from odds_api_ws import OddsWsStore, parse_ws_channels
from odds_ev_monitor import OddsEVMonitor

REPO = Path(__file__).resolve().parents[1]


def test_clock_stopped_allows_timed_sport():
    ev = {"sport": "basketball", "clock": {"running": False, "time": "5:00"}}
    ok, reason = stoppage_allows_live_take(ev)
    assert ok is True
    assert reason == "clock_stopped"
    assert live_take_blocked_by_stoppage(ev, enabled=True) is None


def test_clock_running_blocks_timed_sport():
    ev = {"sport": "soccer", "clock": {"running": True, "time": "67:00"}}
    ok, reason = stoppage_allows_live_take(ev)
    assert ok is False
    assert reason == "clock_running"


def test_omitted_clock_fails_closed():
    ev = {"sport": "americanfootball", "league": "NFL", "status": "live"}
    ok, reason = stoppage_allows_live_take(ev)
    assert ok is False
    assert reason == "clock_omitted"
    assert live_take_blocked_by_stoppage(ev, enabled=True) == "clock_omitted"
    assert live_take_blocked_by_stoppage(ev, enabled=False) is None


def test_halftime_and_break_allow_without_clock():
    for detail in ("Halftime", "Break", "HT"):
        ev = {"sport": "football", "statusDetail": detail}
        ok, reason = stoppage_allows_live_take(ev)
        assert ok is True, detail
        assert reason == "statusDetail_break"


def test_baseball_inning_never_invented_stoppage():
    ev = {"sport": "baseball", "league": "usa-mlb", "statusDetail": "5th inning"}
    assert is_baseball_event(ev) is True
    assert is_timed_sport_event(ev) is False
    ok, reason = stoppage_allows_live_take(ev)
    assert ok is False
    assert reason == "baseball_no_clock"


def test_live_odds_forwards_clock_and_status_detail():
    fields = clock_fields_for_live_odds(
        {"status": "live"},
        {"clock": {"running": False}, "statusDetail": "Halftime"},
    )
    assert fields["clock"] == {"running": False}
    assert fields["clock_running"] is False
    assert fields["statusDetail"] == "Halftime"
    dash = (REPO / "dashboard.py").read_text(encoding="utf-8")
<<<<<<< HEAD
    assert "clock=clock_fields[\"clock\"]" in dash
    assert "status_detail=clock_fields[\"statusDetail\"]" in dash
=======
    assert "clock_fields[\"clock\"]" in dash or "clock=clock_fields[\"clock\"]" in dash
    assert (
        "clock_fields[\"statusDetail\"]" in dash
        or "status_detail=clock_fields[\"statusDetail\"]" in dash
    )
>>>>>>> e9cc1c9 (Keep PLive-take totals listing after the market-key lock.)


def test_ws_persists_raw_clock_and_status_detail():
    store = OddsWsStore()
    store.apply_slate(
        [{"id": 9, "sport": "baseball", "league": "usa-mlb", "statusDetail": "1st inning"}]
    )
    store.apply_message(
        {
            "type": "status",
            "id": 9,
            "status": "live",
            "statusDetail": "3rd inning",
            "clock": {"running": False},
        }
    )
    meta = store.event_meta[9]
    assert meta["statusDetail"] == "3rd inning"
    assert meta["clock"] == {"running": False}


def test_default_ws_channels_include_scores_and_status(monkeypatch):
    monkeypatch.delenv("ODDS_API_WS_CHANNELS", raising=False)
    ch = parse_ws_channels()
    assert "odds" in ch
    assert "scores" in ch
    assert "status" in ch


def test_stoppages_only_default_off_auto_bet_untouched():
    assert OddsEVMonitor.stoppages_only is False
    src = (REPO / "dashboard.py").read_text(encoding="utf-8")
    assert "auto_bet_enabled = False" in src


def test_no_bookiebeats_in_stoppage_module():
    src = (REPO / "stoppage_gate.py").read_text(encoding="utf-8")
    assert "bookiebeats.com" not in src.lower()
    assert "scrape" not in src.lower()


def test_checkbox_disabled_for_baseball_in_ui():
    html = (REPO / "templates" / "dashboard.html").read_text(encoding="utf-8")
    js = (REPO / "static" / "script.js").read_text(encoding="utf-8")
    assert 'id="stoppages-only"' in html
    assert "isBaseballSportSelection" in js
    assert "syncStoppagesOnlyForSport" in js
