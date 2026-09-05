"""Restart restore for auto-bet ON/OFF and $25 default stake.

Uses the same ``user_filters_state.json`` keys as ``_persist_filters_state``.
Does not change fail-closed execution, PLive take, or ODDS_API_BOOKMAKERS.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest


PERSIST_KEYS = (
    "saved_filters",
    "selected_dashboard_filters",
    "selected_auto_bettor_filters",
    "auto_bet_enabled",
    "auto_bet_amount",
    "auto_bet_ev_min",
    "auto_bet_ev_max",
    "auto_bet_odds_min",
    "auto_bet_odds_max",
    "auto_bet_settings_by_filter",
)


@pytest.fixture
def dash_persist(tmp_path, monkeypatch):
    import dashboard as dash

    snap = {
        "enabled": dash.auto_bet_enabled,
        "amount": dash.auto_bet_amount,
        "ev_min": dash.auto_bet_ev_min,
        "ev_max": dash.auto_bet_ev_max,
        "odds_min": dash.auto_bet_odds_min,
        "odds_max": dash.auto_bet_odds_max,
        "settings": copy.deepcopy(dash.auto_bet_settings_by_filter),
        "auto_sel": list(dash.selected_auto_bettor_filters),
        "dash_sel": list(dash.selected_dashboard_filters),
        "state_file": dash.USER_FILTERS_STATE_FILE,
    }
    path = tmp_path / "user_filters_state.json"
    monkeypatch.setattr(dash, "USER_FILTERS_STATE_FILE", str(path))
    yield dash, path
    dash.auto_bet_enabled = snap["enabled"]
    dash.auto_bet_amount = snap["amount"]
    dash.auto_bet_ev_min = snap["ev_min"]
    dash.auto_bet_ev_max = snap["ev_max"]
    dash.auto_bet_odds_min = snap["odds_min"]
    dash.auto_bet_odds_max = snap["odds_max"]
    dash.auto_bet_settings_by_filter = snap["settings"]
    dash.selected_auto_bettor_filters = snap["auto_sel"]
    dash.selected_dashboard_filters = snap["dash_sel"]
    dash.USER_FILTERS_STATE_FILE = snap["state_file"]


def _reset_process_defaults(dash) -> None:
    """Simulate a fresh process: fail-closed OFF, $25 stake."""
    dash.auto_bet_enabled = False
    dash.auto_bet_amount = dash.DEFAULT_AUTO_BET_AMOUNT
    dash.auto_bet_ev_min = 5.0
    dash.auto_bet_ev_max = 25.0
    dash.auto_bet_odds_min = -200
    dash.auto_bet_odds_max = 200


def test_amount_defaults_to_25_when_unset(dash_persist):
    dash, path = dash_persist
    assert dash.DEFAULT_AUTO_BET_AMOUNT == 25.0
    assert dash._coerce_auto_bet_amount(None) == 25.0
    assert dash._coerce_auto_bet_amount(0) == 25.0
    assert dash._coerce_auto_bet_amount("nope") == 25.0
    _reset_process_defaults(dash)
    path.write_text(json.dumps({"version": 1, "saved_filters": {}}), encoding="utf-8")
    dash._load_filters_state()
    assert dash.auto_bet_enabled is False
    assert dash.auto_bet_amount == 25.0
    src = Path(dash.__file__).read_text(encoding="utf-8")
    assert "auto_bet_amount = 25.0" in src
    assert "'amount': 25.0" in src


def test_restart_loads_enabled_true_when_saved(dash_persist):
    dash, path = dash_persist
    dash.auto_bet_enabled = True
    dash.auto_bet_amount = 40.0
    dash.auto_bet_ev_min = 6.0
    dash.auto_bet_ev_max = 22.0
    dash._persist_filters_state()
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in PERSIST_KEYS:
        assert key in data
    assert data["auto_bet_enabled"] is True
    assert data["auto_bet_amount"] == 40.0
    assert data["auto_bet_ev_min"] == 6.0
    assert data["auto_bet_ev_max"] == 22.0

    _reset_process_defaults(dash)
    assert dash.auto_bet_enabled is False
    dash._load_filters_state()
    assert dash.auto_bet_enabled is True
    assert dash.auto_bet_amount == 40.0
    assert dash.auto_bet_ev_min == 6.0
    assert dash.auto_bet_ev_max == 22.0


def test_restart_enabled_false_stays_off(dash_persist):
    dash, path = dash_persist
    dash.auto_bet_enabled = False
    dash.auto_bet_amount = 25.0
    dash._persist_filters_state()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["auto_bet_enabled"] is False

    dash.auto_bet_enabled = True
    dash._load_filters_state()
    assert dash.auto_bet_enabled is False

    _reset_process_defaults(dash)
    dash._load_filters_state()
    assert dash.auto_bet_enabled is False


def test_set_auto_bet_persists_enabled_for_restart(dash_persist):
    dash, path = dash_persist
    _reset_process_defaults(dash)
    with dash.app.test_client() as client:
        resp = client.post("/api/set_auto_bet", json={"enabled": True, "amount": 25})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["enabled"] is True
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["auto_bet_enabled"] is True
    assert saved["auto_bet_amount"] == 25.0

    _reset_process_defaults(dash)
    dash._load_filters_state()
    assert dash.auto_bet_enabled is True
    assert dash.auto_bet_amount == 25.0


def test_bot_control_persists_enable_and_disable(dash_persist):
    dash, path = dash_persist
    _reset_process_defaults(dash)
    with dash.app.test_client() as client:
        on = client.post("/api/bot_control", json={"action": "enable_auto_bet"})
        assert on.status_code == 200
        assert json.loads(path.read_text(encoding="utf-8"))["auto_bet_enabled"] is True
        off = client.post("/api/bot_control", json={"action": "disable_auto_bet"})
        assert off.status_code == 200
        assert json.loads(path.read_text(encoding="utf-8"))["auto_bet_enabled"] is False

    _reset_process_defaults(dash)
    dash._load_filters_state()
    assert dash.auto_bet_enabled is False


def test_missing_enabled_key_stays_off(dash_persist):
    dash, path = dash_persist
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "saved_filters": {},
                "selected_auto_bettor_filters": [],
                "auto_bet_amount": 25,
            }
        ),
        encoding="utf-8",
    )
    dash.auto_bet_enabled = True
    _reset_process_defaults(dash)
    dash._load_filters_state()
    assert dash.auto_bet_enabled is False
    assert dash.auto_bet_amount == 25.0
