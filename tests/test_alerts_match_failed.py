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
    plive = {
        "id": "plive-ou",
        "ticker": "PLIVE|Detroit Tigers @ Minnesota Twins|Over|11.5",
        "take_book": "PLive",
        "match_failed": False,
        "ev_percent": 4.2,
        "pick": "Over",
        "qualifier": "11.5",
    }
    assert is_unlisted_match_failed(plive) is False
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


def test_get_alerts_lists_href_less_plive_over_under(monkeypatch):
    """DET@MIN 11.5 PLive-take O/U list on /api/alerts with no Kalshi ticker."""
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    try:
        import asyncio

        import dashboard as dash
        from ev_alert import EvAlert
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"dashboard import failed: {exc}")

    dash.active_alerts.clear()
    monkeypatch.setattr(dash, "dashboard_min_ev", 0.0)
    monkeypatch.setattr(dash, "selected_dashboard_filters", set())

    def _ou(pick: str) -> EvAlert:
        alert = EvAlert(
            {
                "market_type": "Total Runs",
                "teams": "Detroit Tigers @ Minnesota Twins",
                "pick": pick,
                "qualifier": "11.5",
                "ev_percent": 4.0,
                "odds": "-112",
                "take_book": "PLive",
                "ev_source": "plive_take",
                "ticker": "",
                "market_url": "",
            }
        )
        alert.price_cents = 53
        return alert

    over = _ou("Over")
    under = _ou("Under")
    assert dash._is_plive_take_alert(over) is True
    assert dash._is_plive_take_alert(under) is True
    kalshi_blank = EvAlert(
        {
            "market_type": "Total Runs",
            "teams": "Detroit Tigers @ Minnesota Twins",
            "pick": "Over",
            "qualifier": "11.5",
            "ev_percent": 4.0,
            "ticker": "",
            "market_url": "",
            "take_book": "Kalshi",
        }
    )
    assert dash._is_plive_take_alert(kalshi_blank) is False

    asyncio.run(dash.handle_new_alert(over))
    asyncio.run(dash.handle_new_alert(under))
    with dash.app.test_client() as client:
        resp = client.get("/api/alerts")
        assert resp.status_code == 200
        body = resp.get_json()
        picks = {(a.get("pick"), str(a.get("qualifier"))) for a in body.get("alerts") or []}
        assert ("Over", "11.5") in picks
        assert ("Under", "11.5") in picks
        assert body.get("count") >= 2
        for a in body.get("alerts") or []:
            if a.get("pick") in ("Over", "Under") and str(a.get("qualifier")) == "11.5":
                assert a.get("take_book") == "PLive"
                assert a.get("match_failed") is False
                assert str(a.get("ticker") or "").startswith("PLIVE|")
                assert "KXMLB" not in str(a.get("ticker") or "")
                assert dash.is_unlisted_match_failed(a) is False


def test_href_less_plive_not_hidden_as_match_failed():
    try:
        from dashboard import is_unlisted_match_failed
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"dashboard import failed: {exc}")

    href_less = {
        "id": "plive-ou-hrefless",
        "ticker": "PLIVE|Detroit Tigers @ Minnesota Twins|Over|11.5",
        "ev_source": "plive_take",
        "take_book": "PLive",
        "match_failed": True,
        "match_failure_reason": "Could not find matching submarket",
        "ev_percent": 4.0,
        "pick": "Over",
        "qualifier": "11.5",
    }
    assert is_unlisted_match_failed(href_less) is False
    kalshi_blank = {
        "id": "kalshi-blank",
        "ticker": None,
        "take_book": "Kalshi",
        "match_failed": True,
        "match_failure_reason": "Could not find matching submarket",
        "ev_percent": 4.0,
    }
    assert is_unlisted_match_failed(kalshi_blank) is True


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


def test_blank_kalshi_ticker_routes_to_paper_display():
    try:
        from dashboard import _is_paper_kalshi_alert, _is_plive_take_alert
        from ev_alert import EvAlert
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"dashboard import failed: {exc}")

    blank = EvAlert(
        {
            "market_type": "Moneyline",
            "teams": "A @ B",
            "pick": "Draw",
            "qualifier": "",
            "ev_percent": 5.0,
            "odds": "+150",
            "ticker": "",
            "take_book": "Kalshi",
            "ev_source": "live_event_scan",
        }
    )
    assert _is_plive_take_alert(blank) is False
    assert _is_paper_kalshi_alert(blank) is True


def test_match_failed_kalshi_falls_back_to_paper_display(monkeypatch):
    """Backend-fired alert with unmatchable KX ticker must still reach /api/alerts."""
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    try:
        import asyncio

        import dashboard as dash
        from ev_alert import EvAlert
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"dashboard import failed: {exc}")

    dash.active_alerts.clear()
    monkeypatch.setattr(dash, "dashboard_min_ev", 0.0)
    monkeypatch.setattr(dash, "selected_dashboard_filters", [])

    async def _no_submarket(*_a, **_k):
        return None

    class _FakeKalshi:
        async def find_submarket(self, *a, **k):
            return None

        async def build_market_ticker(self, *a, **k):
            return None

        async def subscribe_orderbook(self, *_a, **_k):
            return None

        warm_cache_tickers = set()

    monkeypatch.setattr(dash, "kalshi_client", _FakeKalshi())

    alert = EvAlert(
        {
            "market_type": "Moneyline",
            "teams": "Away FC @ Home FC",
            "pick": "Draw",
            "qualifier": "",
            "ev_percent": 6.5,
            "odds": "+180",
            "ticker": "KXSOCCERGAME-26SEP04AWYHOM",
            "market_url": "https://kalshi.com/markets/kxsoccer/game",
            "take_book": "Kalshi",
            "ev_source": "odds_api_value_bets",
            "filter_name": "",
        }
    )
    alert.price_cents = 36
    alert.filter_name = ""

    emitted = []

    def _emit(event, data=None, **_kwargs):
        emitted.append(event)

    monkeypatch.setattr(dash.socketio, "emit", _emit)

    asyncio.run(dash.handle_new_alert(alert))
    assert "new_alert" in emitted
    assert "alert_match_failed" in emitted
    with dash.app.test_client() as client:
        body = client.get("/api/alerts").get_json()
        assert body.get("count", 0) >= 1
        row = (body.get("alerts") or [])[0]
        assert row.get("take_book") == "Kalshi"
        assert row.get("match_failed") is False
        assert float(row.get("ev_percent") or 0) >= 6.0


def test_keepalive_invokes_updated_callbacks_for_unchanged_alerts():
    """Stable odds must still hit updated callbacks (dashboard refreshes last_seen)."""
    try:
        import asyncio

        from ev_alert import EvAlert
        from odds_ev_monitor import OddsEVMonitor
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"import failed: {exc}")

    alert = EvAlert(
        {
            "market_type": "Moneyline",
            "teams": "A @ B",
            "pick": "A",
            "qualifier": "",
            "ev_percent": 4.0,
            "odds": "+120",
            "ticker": "KALSHI|A @ B|A|",
            "take_book": "Kalshi",
            "ev_source": "live_event_scan",
        }
    )
    mon = OddsEVMonitor(auth_token=None)
    touched = []

    async def _on_upd(a):
        touched.append(a.pick)

    mon.updated_alert_callbacks.append(_on_upd)
    h = f"{alert.ticker}|{alert.pick}|{alert.qualifier}|{alert.odds}"
    mon._seen_alerts.add(h)
    mon._previous_alert_values[h] = {
        "ev_percent": alert.ev_percent,
        "liquidity": getattr(alert, "liquidity", 0),
        "odds": alert.odds,
    }

    async def fake_fetch():
        return [alert]

    mon.fetch_alerts = fake_fetch  # type: ignore[method-assign]
    asyncio.run(mon.check_for_new_alerts())
    assert touched == ["A"]
