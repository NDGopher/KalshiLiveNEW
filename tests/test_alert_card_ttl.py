"""Alert cards must outlive the monitor poll so the FE is not wiped empty."""
from __future__ import annotations

import os

import dashboard as dash


def test_alert_card_ttl_above_default_poll():
    os.environ.pop("ALERT_TTL_SEC", None)
    os.environ.pop("ALERT_TTL", None)
    os.environ["ALERT_STALE_SEC"] = "45"
    ttl = dash.alert_card_ttl_sec()
    assert ttl >= 60
    assert ttl >= 45 + 15  # stale floor + buffer
    assert ttl > 30  # old hard TTL that emptied the board


def test_touch_alert_liveness_extends_expiry_with_last_seen():
    row: dict = {}
    dash.touch_alert_liveness(row, now=1_000.0)
    assert row["last_seen"] == 1_000.0
    assert row["expiry"] == 1_000.0 + dash.alert_card_ttl_sec()

    dash.touch_alert_liveness(row, now=1_050.0)
    assert row["last_seen"] == 1_050.0
    assert row["expiry"] == 1_050.0 + dash.alert_card_ttl_sec()
    # Keepalive after 50s must still leave expiry in the future relative to "now"
    assert row["expiry"] > 1_050.0


def test_old_30s_expiry_would_die_before_45s_poll():
    """Document the race this fix closes."""
    created = 0.0
    old_expiry = created + 30.0
    next_poll = created + 45.0
    assert old_expiry < next_poll
    new_expiry = created + dash.alert_card_ttl_sec()
    assert new_expiry > next_poll
