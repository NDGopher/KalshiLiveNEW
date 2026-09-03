"""/api/live_odds must emit ML + Spread + Totals, not ML-only."""
from __future__ import annotations

from dashboard import (
    _live_market_has_any_price,
    _live_pick_kind_name,
    _live_prices_for_kind,
)
from odds_ev_monitor import _odds_doc_has_kalshi_tradable_gameline
from plive_pandora import PliveStore, PLIVE_LINE_SET


def test_live_odds_helpers_emit_three_markets():
    bks = {
        "Kalshi": [
            {"name": "ML", "odds": [{"home": 1.90, "away": 2.00}]},
            {"name": "Spread", "odds": [{"hdp": -1.5, "home": 1.80, "away": 2.10}]},
            {"name": "Totals", "odds": [{"hdp": 8.5, "over": 1.91, "under": 1.91}]},
        ]
    }
    assert _live_pick_kind_name(bks, "ml") == "ML"
    assert _live_pick_kind_name(bks, "spread") == "Spread"
    assert _live_pick_kind_name(bks, "total") == "Totals"
    tot = _live_prices_for_kind(bks, ["Kalshi"], "Totals", "total")
    assert _live_market_has_any_price(tot)
    assert tot["Kalshi"]["away_am"] is not None
    assert tot["Kalshi"]["home_am"] is not None
    spr = _live_prices_for_kind(bks, ["Kalshi"], "Spread", "spread")
    assert _live_market_has_any_price(spr)


def test_plive_only_totals_doc_is_tradable():
    doc = {
        "bookmakers": {
            "PLive": [
                {
                    "name": "Totals",
                    "odds": [{"hdp": 11.5, "over": 1.892857, "under": 1.847458}],
                }
            ]
        }
    }
    assert _odds_doc_has_kalshi_tradable_gameline(doc) is True


def test_plive_market5_game_total_det_min():
    store = PliveStore()
    eid = "199295331"
    store.apply_message(
        {
            "isDiff": False,
            "payload": {
                "c": {
                    "m": {
                        "5": {"o": {"11.5": {0: 1.892857, 1: 1.847458}}},
                    }
                }
            },
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventCoefficients.{eid}",
    )
    by_name = {m["name"]: m for m in store.markets_for_event(eid)}
    assert "Totals" in by_name
    row = by_name["Totals"]["odds"][0]
    assert abs(float(row["hdp"]) - 11.5) < 1e-9
    assert abs(float(row["over"]) - 1.892857) < 1e-6
    assert abs(float(row["under"]) - 1.847458) < 1e-6
    assert "Spread" not in by_name
