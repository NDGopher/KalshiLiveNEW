"""Pregame scan must skip settled catalog rows and take soonest NCAAF tip-offs."""
from __future__ import annotations

from datetime import datetime, timezone

from odds_ev_monitor import select_pregame_events_for_scan


def _ev(eid, *, status, date, home="Home", away="Away", sport="american-football", league="usa-college"):
    return {
        "id": eid,
        "status": status,
        "date": date,
        "home": home,
        "away": away,
        "sport": {"slug": sport},
        "league": {"slug": league},
    }


def test_pregame_select_skips_settled_and_live_takes_soonest_cfb():
    now = datetime(2026, 9, 5, 19, 0, tzinfo=timezone.utc)
    rows = [
        _ev(1, status="settled", date="2026-09-05T14:00:00Z", home="Settled U", away="Settled A"),
        _ev(2, status="settled", date="2026-09-05T15:00:00Z", home="Settled2"),
        _ev(3, status="live", date="2026-09-05T18:00:00Z", home="Already Live"),
        _ev(4, status="pending", date="2026-09-05T16:00:00Z", home="Stale D3"),  # past
        _ev(
            5,
            status="pending",
            date="2026-09-05T19:30:00Z",
            home="Georgia Bulldogs",
            away="Tennessee State Tigers",
        ),
        _ev(
            6,
            status="pending",
            date="2026-09-05T19:30:00Z",
            home="Penn State Nittany Lions",
            away="Marshall Herd",
        ),
        _ev(
            7,
            status="pending",
            date="2026-09-06T00:00:00Z",
            home="Later Night",
        ),
        _ev(8, status="pending", date="2026-09-05T19:05:00Z", home="Soon D2"),
    ]
    picked = select_pregame_events_for_scan(rows, 3, now=now)
    homes = [e["home"] for e in picked]
    assert "Settled U" not in homes
    assert "Already Live" not in homes
    assert homes[0] == "Soon D2"
    assert "Georgia Bulldogs" in homes
    assert "Penn State Nittany Lions" in homes
    assert "Later Night" not in homes
    assert "Stale D3" not in homes


def test_pregame_select_dedupes_seen_ids():
    now = datetime(2026, 9, 5, 19, 0, tzinfo=timezone.utc)
    rows = [
        _ev(10, status="pending", date="2026-09-05T19:30:00Z", home="Georgia Bulldogs"),
        _ev(11, status="pending", date="2026-09-05T19:31:00Z", home="Oregon Ducks"),
    ]
    picked = select_pregame_events_for_scan(rows, 5, now=now, seen_ids={10})
    assert [e["id"] for e in picked] == [11]
