"""Cards follow monitor presence — not a 30/45s wall-clock TTL."""
from __future__ import annotations

import os

import dashboard as dash
from ev_alert import EvAlert, alert_presence_key, presence_key_from_row


def test_presence_key_stable_across_odds_ticks():
    a = EvAlert(
        {
            "ticker": "KXTEST-YES",
            "pick": "Over 8.5",
            "qualifier": "total",
            "market_type": "total",
            "odds": "-110",
            "take_book": "Kalshi",
        }
    )
    b = EvAlert(
        {
            "ticker": "KXTEST-YES",
            "pick": "Over 8.5",
            "qualifier": "total",
            "market_type": "total",
            "odds": "-105",  # price moved
            "take_book": "Kalshi",
        }
    )
    assert alert_presence_key(a) == alert_presence_key(b)
    assert "−110" not in alert_presence_key(a)
    assert "-110" not in alert_presence_key(a)


def test_presence_key_from_row_roundtrip():
    alert = EvAlert(
        {
            "ticker": "KXABC",
            "pick": "Lakers",
            "qualifier": "moneyline",
            "market_type": "moneyline",
            "take_book": "PLive",
        }
    )
    key = alert_presence_key(alert)
    row = {
        "ticker": "KXABC",
        "pick": "Lakers",
        "qualifier": "moneyline",
        "market_type": "moneyline",
        "take_book": "PLive",
        "presence_key": key,
    }
    assert presence_key_from_row(row) == key


def test_orphan_safety_net_is_minutes_not_poll_race():
    os.environ.pop("ALERT_ORPHAN_SEC", None)
    os.environ.pop("ALERT_STALE_SEC", None)
    os.environ.pop("ALERT_TTL_SEC", None)
    # Must not race a 1s WS eval or 45s REST fallback.
    assert dash.alert_orphan_sec() >= 120


def test_touch_liveness_refreshes_last_seen():
    row: dict = {"ticker": "KX", "pick": "A", "qualifier": "", "market_type": "ml"}
    dash.touch_alert_liveness(row, now=5_000.0)
    assert row["last_seen"] == 5_000.0
    assert row.get("presence_key")
