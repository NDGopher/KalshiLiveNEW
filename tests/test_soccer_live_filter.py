"""Soccer Live (2 Sharps) product locks. Do not weaken DEFAULT minSharp 3."""
from __future__ import annotations

from odds_ev_monitor import _league_matches_filter


def test_soccer_filter_min_sharp_two_default_stays_three():
    from dashboard import (
        CBB_FILTER_PAYLOAD,
        DEFAULT_FILTER_PAYLOAD,
        SOCCER_FILTER_NAME,
        SOCCER_FILTER_PAYLOAD,
        auto_bet_settings_by_filter,
        selected_auto_bettor_filters,
    )

    assert SOCCER_FILTER_NAME == "Soccer Live (2 Sharps)"
    assert DEFAULT_FILTER_PAYLOAD["devigFilter"]["minSharpBooks"] == 3
    assert CBB_FILTER_PAYLOAD["devigFilter"]["minSharpBooks"] == 2
    assert SOCCER_FILTER_PAYLOAD["devigFilter"]["minSharpBooks"] == 2
    assert SOCCER_FILTER_PAYLOAD["devigFilter"]["method"] == "POWER"
    assert SOCCER_FILTER_PAYLOAD["devigFilter"]["type"] == "AVERAGE"
    assert SOCCER_FILTER_PAYLOAD["betTypes"] == ["GAMELINES"]
    assert SOCCER_FILTER_PAYLOAD["leagues"] == ["SOCCER_ALL"]
    assert SOCCER_FILTER_PAYLOAD["bettingBooks"] == ["Kalshi"]


def test_soccer_sharps_drop_betmgm_keep_display_and_core_books():
    from dashboard import SOCCER_FILTER_PAYLOAD, display_books_list

    sharps = [str(x).strip().lower() for x in (SOCCER_FILTER_PAYLOAD["devigFilter"]["sharps"] or [])]
    display = [str(x).strip().lower() for x in (SOCCER_FILTER_PAYLOAD["displayBooks"] or [])]
    assert "betmgm" not in sharps
    assert "kalshi" not in sharps
    assert "plive" not in sharps
    assert "lowvig" not in sharps
    assert "lowvig" not in display
    # BetMGM may remain on tiles / account list when configured.
    assert "betmgm" in [str(x).strip().lower() for x in display_books_list] or "betmgm" in display
    for core in ("bet365", "betfair exchange", "draftkings", "fanduel"):
        if core in [str(x).strip().lower() for x in display_books_list]:
            assert core in sharps


def test_soccer_excludes_half_and_team_totals():
    from dashboard import SOCCER_FILTER_PAYLOAD

    excluded = {str(x) for x in (SOCCER_FILTER_PAYLOAD.get("excludedCategories") or [])}
    assert "1st Half" in excluded
    assert "2nd Half" in excluded
    assert "Team Total" in excluded or "Team Totals" in excluded


def test_soccer_auto_bet_off_not_in_auto_bettor_selection():
    from dashboard import (
        SOCCER_FILTER_NAME,
        auto_bet_settings_by_filter,
        selected_auto_bettor_filters,
    )

    settings = auto_bet_settings_by_filter[SOCCER_FILTER_NAME]
    assert settings["enabled"] is False
    assert settings["ev_min"] == 5.0
    assert SOCCER_FILTER_NAME not in selected_auto_bettor_filters


def test_soccer_all_accepts_football_slug_eredivisie():
    assert _league_matches_filter("Eredivisie", ["SOCCER_ALL"], "football") is True
    assert _league_matches_filter("Eredivisie", ["SOCCER_ALL"], "") is False
    assert _league_matches_filter("English Premier League", ["SOCCER_ALL"], "") is True


def test_football_all_includes_cfb_nfl_not_soccer():
    assert _league_matches_filter("NFL", ["FOOTBALL_ALL"], "american-football") is True
    assert _league_matches_filter("NCAA Football", ["FOOTBALL_ALL"], "american-football") is True
    assert _league_matches_filter("NCAAF", ["FOOTBALL_ALL"], "") is True
    assert _league_matches_filter("NFL", ["NFL"], "american-football") is True
    assert _league_matches_filter("NCAA Football", ["NFL"], "american-football") is False
    assert _league_matches_filter("Eredivisie", ["FOOTBALL_ALL"], "football") is False
    assert _league_matches_filter("NCAA Men's Basketball", ["FOOTBALL_ALL"], "basketball") is False
