"""Odds-API stoppage gate. No BookieBeats. No invented baseball clock."""
from __future__ import annotations

from pathlib import Path

from stoppage_gate import (
    clock_fields_for_live_odds,
    format_game_status_line,
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
    assert "clock=clock_fields[\"clock\"]" in dash
    assert "status_detail=clock_fields[\"statusDetail\"]" in dash


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
    assert "isTimedBoardSportSelection" in js
    assert "syncStoppagesOnlyForSport" in js
    assert "el.disabled = !timed" in js
    assert 'value="americanfootball|ncaaf"' in html
    assert "alert-game-status" in js
    assert "formatAlertGameStatus" in js


def test_nba_game_status_line_stopped():
    ev = {
        "sport": "basketball",
        "league": "NBA",
        "scores": {"home": 14, "away": 11},
        "clock": {"playedSeconds": 252, "period": 3, "running": False},
    }
    assert format_game_status_line(ev) == "14-11 · Q3 4:12 · STOPPED"
    fields = clock_fields_for_live_odds(ev)
    assert fields["score"] == "14-11"
    assert fields["scores"] == {"home": 14, "away": 11}
    assert fields["game_status"] == "14-11 · Q3 4:12 · STOPPED"
    assert fields["clock_running"] is False


def test_soccer_game_status_line():
    ev = {
        "sport": "football",
        "scores": {"home": 1, "away": 1},
        "clock": {"minute": 67, "period": 2, "running": True},
    }
    assert format_game_status_line(ev) == "1-1 · 67' · 2nd half"
    assert "STOPPED" not in format_game_status_line(ev)


def test_mlb_game_status_display_only_never_stopped():
    ev = {
        "sport": "baseball",
        "league": "usa-mlb",
        "scores": {"home": 3, "away": 2},
        "statusDetail": "7th inning",
        "clock": {"running": False},
    }
    assert format_game_status_line(ev) == "3-2 · 7th"
    assert "STOPPED" not in format_game_status_line(ev)


def test_mlb_never_invents_inning():
    ev = {
        "sport": "baseball",
        "scores": {"home": 1, "away": 0},
        "clock": {"period": 5, "running": False},
    }
    assert format_game_status_line(ev) == "1-0"


def test_missing_clock_and_score_omits_status_line():
    assert format_game_status_line({"sport": "basketball"}) == ""
    assert clock_fields_for_live_odds({"sport": "nba"})["game_status"] == ""


def test_cfb_is_timed_and_stoppages_stay_default_off():
    ev = {"sport": "americanfootball", "league": "NCAAF"}
    assert is_timed_sport_event(ev) is True
    assert OddsEVMonitor.stoppages_only is False


def test_ws_score_message_persists_scores():
    store = OddsWsStore()
    store.apply_slate([{"id": 11, "sport": "basketball", "league": "NBA"}])
    store.apply_message(
        {
            "type": "score",
            "id": 11,
            "scores": {"home": 14, "away": 11, "periods": [4, 3, 7]},
            "clock": {"period": 3, "playedSeconds": 252, "running": False},
        }
    )
    meta = store.event_meta[11]
    assert meta["scores"]["home"] == 14
    assert meta["clock"]["running"] is False
    fields = clock_fields_for_live_odds({"sport": "basketball"}, meta)
    assert fields["game_status"] == "14-11 · Q3 4:12 · STOPPED"


def test_parse_bet_to_alert_wires_clock_fields():
    from odds_ev_monitor import OddsEVMonitor

    mon = OddsEVMonitor.__new__(OddsEVMonitor)
    built = {
        "market": "Moneyline",
        "teams": "Away @ Home",
        "selection": "Home",
        "ev": 3.5,
        "limit": 10,
        "link": "https://kalshi.com/markets/KXTEST",
        "displayBooks": {},
        "devigBooks": [],
        "take_book": "Kalshi",
    }
    ev = {
        "sport": "basketball",
        "league": "NBA",
        "live": True,
        "scores": {"home": 14, "away": 11},
        "clock": {"playedSeconds": 252, "period": 3, "running": False},
    }
    alert = mon.parse_bet_to_alert(built, ev)
    assert alert is not None
    assert alert.game_status == "14-11 · Q3 4:12 · STOPPED"
    assert alert.score == "14-11"
    assert alert.clock_running is False
    assert alert.scores == {"home": 14, "away": 11}
