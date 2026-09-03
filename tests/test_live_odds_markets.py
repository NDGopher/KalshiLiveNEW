"""/api/live_odds must emit ML + Spread + Totals, not ML-only."""
from __future__ import annotations

from ev_calculator import decimal_to_american
from dashboard import (
    _live_collect_lines,
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
    # 1.892857 is a favorite (−112). (dec−1)×100 = +89 is not American.
    assert abs(decimal_to_american(1.892857) + 112) <= 1
    assert abs(decimal_to_american(1.847458) + 118) <= 1


def test_plive_market5_mil_chc_and_mia_kc():
    store = PliveStore()
    fixtures = (
        ("199295401", "13.5", 1.90, 1.0 + 100.0 / 120.0, -111, -120),
        ("199295402", "15.5", 3.35, 1.0 + 100.0 / 338.0, 235, -338),
    )
    for eid, line, over_d, under_d, over_am, under_am in fixtures:
        store.apply_message(
            {
                "isDiff": False,
                "payload": {
                    "c": {
                        "m": {
                            "5": {"o": {line: {0: over_d, 1: under_d}}},
                            "7": {"o": {"4.5": {0: 1.80, 1: 2.00}}},
                            "8": {"o": {"5.5": {0: 1.75, 1: 2.05}}},
                        }
                    }
                },
            },
            event_name=f"live.main.{PLIVE_LINE_SET}.eventCoefficients.{eid}",
        )
        by_name = {m["name"]: m for m in store.markets_for_event(eid)}
        assert "Totals" in by_name
        assert "Spread" not in by_name
        row = by_name["Totals"]["odds"][0]
        assert abs(float(row["hdp"]) - float(line)) < 1e-9
        assert abs(float(row["over"]) - over_d) < 1e-6
        assert abs(float(row["under"]) - under_d) < 1e-6
        assert abs(decimal_to_american(over_d) - over_am) <= 1
        assert abs(decimal_to_american(under_d) - under_am) <= 1


def test_plive_take_totals_tradable_without_kalshi():
    doc = {
        "bookmakers": {
            "PLive": [
                {
                    "name": "Totals",
                    "odds": [{"hdp": 13.5, "over": 1.90, "under": 1.833333}],
                }
            ],
            "Kalshi": [{"name": "Totals", "odds": [{"match_failed": True}]}],
        }
    }
    assert _odds_doc_has_kalshi_tradable_gameline(doc) is True


def test_live_odds_line_match_skips_unpriced_and_team_total():
    """Kalshi Astros −1.5 must not inherit PLive Sox TT Over 2.5 = +325."""
    from ev_calculator import american_to_decimal

    tt = american_to_decimal(325)
    plus15_home = american_to_decimal(-392)
    plus15_away = american_to_decimal(266)
    bks = {
        "Kalshi": [
            {"name": "Spread", "odds": [{"hdp": -1.5, "home": 1.80, "away": 2.10}]},
        ],
        "PLive": [
            {
                "name": "Spread",
                "odds": [
                    {
                        "hdp": -1.5,
                        "home": tt,
                        "away": 1.14,
                        "plive_market": 7,
                        "market_type": "team_total",
                    },
                    {
                        "hdp": 1.5,
                        "home": plus15_home,
                        "away": plus15_away,
                        "plive_market": 6,
                        "market_type": "run_line",
                    },
                ],
            }
        ],
    }
    lines = _live_collect_lines(bks, ["Kalshi", "PLive"], "Spread", "spread")
    assert any(ln is not None and abs(float(ln) - 1.5) < 1e-9 for ln in lines)
    minus = _live_prices_for_kind(bks, ["Kalshi", "PLive"], "Spread", "spread", line=-1.5)
    assert minus["Kalshi"]["home_am"] is not None
    assert minus["PLive"]["home_am"] is None
    assert minus["PLive"]["away_am"] is None
    plus = _live_prices_for_kind(bks, ["Kalshi", "PLive"], "Spread", "spread", line=1.5)
    assert plus["PLive"]["home_am"] == decimal_to_american(plus15_home)
    assert plus["PLive"]["away_am"] == decimal_to_american(plus15_away)
    assert plus["PLive"]["home_am"] != 325
    assert plus["PLive"]["away_am"] != 325
