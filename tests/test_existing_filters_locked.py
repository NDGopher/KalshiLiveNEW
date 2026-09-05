"""Lock Stephen's existing filter names, knobs, and dollar sizes.

Do not invent new filters or bet amounts. Auto-bet stays OFF at startup.
"""
from __future__ import annotations

from pathlib import Path

DASH = Path(__file__).resolve().parents[1] / "dashboard.py"


def _src() -> str:
    return DASH.read_text(encoding="utf-8")


def test_default_and_cbb_filter_payloads_unchanged():
    src = _src()
    assert 'DEFAULT_FILTER_NAME = "Kalshi All Sports (3 Sharps Live)"' in src
    assert 'CBB_FILTER_NAME = "CBB EV Filter (Live - Kalshi)"' in src
    assert '"bettingBooks": ["Kalshi"]' in src
    assert '"betTypes": ["GAMELINES"]' in src
    assert '"method": "POWER"' in src
    assert '"type": "AVERAGE"' in src
    assert '"minSharpBooks": 3' in src
    assert '"method": "WORST_CASE"' in src
    assert '"minSharpBooks": 2' in src
    assert 'USER_FILTERS_STATE_FILE' in src
    assert "user_filters_state.json" in src
    assert "auto_bet_settings_by_filter" in src
    assert "_persist_filters_state" in src
    assert "_load_filters_state" in src
    assert "_overlay_auto_bet_persist" in src
    assert '"auto_bet_enabled"' in src


def test_dollar_rules_and_auto_bet_off():
    src = _src()
    assert "auto_bet_enabled = False" in src
    assert "auto_bet_amount = 25.0" in src
    assert "DEFAULT_AUTO_BET_AMOUNT = 25.0" in src
    assert "moneyline_bet_amount = 151.0" in src
    assert "total_bet_amount = 101.0" in src
    assert "spread_bet_amount = 75.0" in src
    assert "nhl_over_bet_amount = 202.0" in src
    assert "per_event_max_bet = 404.0" in src
    assert "user_max_bet_amount = 100.0" in src
    assert "px_novig_multiplier = 2.0" in src
    assert "auto_bet_ev_min = 2.0" in src
    assert "auto_bet_ev_max = 25.0" in src
    assert "auto_bet_odds_min = -200" in src
    assert "auto_bet_odds_max = 200" in src
    # Per-filter auto-bet: All Sports 2-25%, CBB 10-25%, soccer 5-25%, all disabled.
    assert "'ev_min': 2.0" in src
    assert "'ev_min': 5.0" in src
    assert "'ev_min': 10.0" in src
    assert "'ev_max': 25.0" in src
    assert "'enabled': False" in src


def test_ten_odds_api_books_include_bet365_not_bookmaker_eu():
    from odds_api_client import DEFAULT_ODDS_API_BOOKMAKERS

    books = [b.strip() for b in DEFAULT_ODDS_API_BOOKMAKERS.split(",") if b.strip()]
    assert books == [
        "DraftKings",
        "FanDuel",
        "BetMGM",
        "Betfair Exchange",
        "Circa",
        "Polymarket",
        "Bet365",
        "Caesars",
        "Kalshi",
        "NoVig",
    ]
    assert "BookMaker.eu" not in books


def test_payload_leftovers_use_canonical_book_names():
    src = _src()
    assert '"book": "Betfair"' not in src
    assert '"book": "Novig"' not in src
    assert '"Betfair Exchange"' in src
    assert '"NoVig"' in src
    assert "auto_bet_enabled = False" in src
    assert "show_matched = is_plus_print_ev(alert.ev_percent, dashboard_min_ev)" in src
    assert "show_update = is_plus_print_ev(alert.ev_percent, dashboard_min_ev)" in src
    assert "or not getattr(alert, 'strict_pass', True)" not in src
    assert "sharps_list.append(_extra_bk)" not in src
    assert 'if _dnorm(b) not in ("kalshi", "plive")' in src
    assert 'SOCCER_FILTER_NAME = "Soccer Live (2 Sharps)"' in src
    assert 'soccer_sharps = [b for b in sharps_list if _dnorm(b) != "betmgm"]' in src
    assert 'selected_auto_bettor_filters = []' in src
