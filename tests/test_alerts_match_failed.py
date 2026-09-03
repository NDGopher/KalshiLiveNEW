"""Unmatched / match_failed rows never appear on the live alert list."""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_script_bet365_tile_not_betfair_exchange():
    js = (REPO / "static" / "script.js").read_text(encoding="utf-8")
    assert "substring(0, 2)" not in js
    assert "bookName.substring(0, 2)" not in js
    assert "uniqueBookAbbrev" in js
    assert "resolveBookLogoPaths" in js
    assert "/logos/Bet365.png" in js
    assert "/logos/Betfair.png" in js
    assert "/logos/BetMGM.png" in js
    assert "/logos/Caesars.png" in js
    assert "/logos/NV.png" in js
    assert "/logos/Circa.png" in js
    # BetMGM may try MGM.png; it must never use BookMaker's BM.png.
    mgm_block = js[js.index("'BetMGM':"): js.index("'Caesars':")]
    assert "/logos/BM.png" not in mgm_block
    assert "return 'B365'" in js
    assert "return 'BFX'" in js
    assert js.index("return 'B365'") != js.index("return 'BFX'")
    assert "isCircaBook" in js
    assert "handleBookLogoError" in js


def test_listed_alerts_drop_match_failed():
    try:
        from dashboard import is_unlisted_match_failed, listed_active_alerts
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"dashboard import failed: {exc}")

    rows = {
        "bad": {
            "id": "bad",
            "match_failed": True,
            "ticker": None,
            "match_failure_reason": "Could not find matching submarket",
            "ev_percent": 18.39,
        },
        "also": {
            "id": "also",
            "ticker": None,
            "match_failure_reason": "Could not find matching submarket",
            "ev_percent": 2.57,
        },
        "good": {
            "id": "good",
            "ticker": "KXTEST-YES",
            "match_failed": False,
            "ev_percent": 2.33,
        },
    }
    assert is_unlisted_match_failed(rows["bad"]) is True
    assert is_unlisted_match_failed(rows["also"]) is True
    assert is_unlisted_match_failed(rows["good"]) is False
    visible = listed_active_alerts(rows)
    assert [r["id"] for r in visible] == ["good"]


def test_listed_alerts_honor_min_ev_floor(monkeypatch):
    try:
        import dashboard as dash
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"dashboard import failed: {exc}")

    monkeypatch.setattr(dash, "dashboard_min_ev", 3.0)
    rows = {
        "low": {"id": "low", "ticker": "KXTEST-LOW", "match_failed": False, "ev_percent": 2.33},
        "ok": {"id": "ok", "ticker": "KXTEST-OK", "match_failed": False, "ev_percent": 3.10},
    }
    visible = dash.listed_active_alerts(rows)
    assert [r["id"] for r in visible] == ["ok"]


def test_get_alerts_omits_match_failed(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    try:
        import dashboard as dash
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"dashboard import failed: {exc}")

    dash.active_alerts.clear()
    dash.active_alerts["mf"] = {
        "id": "mf",
        "match_failed": True,
        "ticker": None,
        "match_failure_reason": "Could not find matching submarket",
        "ev_percent": 18.39,
        "teams": "Away @ Home",
    }
    dash.active_alerts["ok"] = {
        "id": "ok",
        "ticker": "KXTEST-YES",
        "match_failed": False,
        "ev_percent": 2.33,
        "teams": "Away @ Home",
    }
    with dash.app.test_client() as client:
        resp = client.get("/api/alerts")
        assert resp.status_code == 200
        body = resp.get_json()
        ids = [a.get("id") for a in body.get("alerts") or []]
        assert "mf" not in ids
        assert "ok" in ids
        assert body.get("count") == 1


def test_unmatched_handle_does_not_emit_new_alert():
    try:
        from dashboard import fanout_unmatched_alert, unmatched_alert_should_emit_new_alert
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"dashboard import failed: {exc}")

    assert unmatched_alert_should_emit_new_alert({"match_failed": True}) is False
    emitted = []

    def _emit(name, payload):
        emitted.append(name)

    result = fanout_unmatched_alert(
        _emit,
        {
            "id": "x",
            "teams": "A @ B",
            "pick": "B",
            "market_type": "Moneyline",
            "match_failed": True,
            "match_failure_reason": "Could not find matching submarket",
            "ticker": None,
            "ev_percent": 18.39,
        },
    )
    assert result["emit_new_alert"] is False
    assert result["stored"] is False
    assert "new_alert" not in emitted
    assert emitted == ["alert_match_failed"]
