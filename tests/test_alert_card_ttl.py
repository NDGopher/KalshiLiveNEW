"""Presence-based cards: orphan safety-net is minutes, not a poll race."""
from __future__ import annotations

import os

import dashboard as dash


def test_orphan_floor_above_ws_and_rest_cadence():
    os.environ.pop("ALERT_ORPHAN_SEC", None)
    os.environ.pop("ALERT_STALE_SEC", None)
    os.environ.pop("ALERT_TTL_SEC", None)
    os.environ.pop("ALERT_TTL", None)
    sec = dash.alert_orphan_sec()
    assert sec >= 120
    assert sec > 45  # must not race REST fallback poll
    assert sec > 30  # must not race the old hard expiry


def test_touch_alert_liveness_extends_last_seen():
    row: dict = {}
    dash.touch_alert_liveness(row, now=1_000.0)
    assert row["last_seen"] == 1_000.0
    assert row["expiry"] == 1_000.0 + dash.alert_orphan_sec()
    dash.touch_alert_liveness(row, now=1_050.0)
    assert row["last_seen"] == 1_050.0


def test_alert_card_ttl_alias_matches_orphan():
    assert dash.alert_card_ttl_sec() == dash.alert_orphan_sec()
