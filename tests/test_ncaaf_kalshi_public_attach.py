"""Odds-API college football must attach KXNCAAF* — not KXNFL*.

Live desk symptom: USA - College / usa-college + american-football was
classified as nfl, so public attach fetched NFL markets, matched 0, and
alerts kept paper KALSHI|… tickers (autobet_allow false).
"""
from __future__ import annotations

from ev_calculator import american_to_decimal
from execution_guard import is_kalshi_ticker, is_paper_kalshi_ticker
from kalshi_public_feed import (
    _SERIES_BY_SPORT,
    _soccer_series_cache,
    attach_public_kalshi_markets,
    kalshi_tickers_from_doc,
    series_for_docs,
    soccer_series_for_docs,
    sport_key_for_doc,
)

NCAAF_SERIES = list(_SERIES_BY_SPORT["ncaaf"])
NFL_SERIES = list(_SERIES_BY_SPORT["nfl"])

OHIO_HOME = "Ohio State Buckeyes"
MICH_AWAY = "Michigan Wolverines"
NCAAF_EID = 260906001
HOME_TICKER = "KXNCAAFGAME-26SEP06MICHOSU-OSU"
AWAY_TICKER = "KXNCAAFGAME-26SEP06MICHOSU-MICH"
EVENT_TICKER = "KXNCAAFGAME-26SEP06MICHOSU"
NFL_HOME_TICKER = "KXNFLGAME-26SEP06KCANE-KC"
NFL_AWAY_TICKER = "KXNFLGAME-26SEP06KCANE-NE"
NFL_EVENT = "KXNFLGAME-26SEP06KCANE"

OSU_DEC = american_to_decimal(-150)
MICH_DEC = american_to_decimal(130)
OSU_ASK = f"{1.0 / OSU_DEC:.4f}"
MICH_ASK = f"{1.0 / MICH_DEC:.4f}"


def _usa_college_cfb_doc(**overrides) -> dict:
    """Live Odds-API shape: league USA - College / usa-college, sport american-football."""
    doc = {
        "id": NCAAF_EID,
        "home": OHIO_HOME,
        "away": MICH_AWAY,
        "sport": {"name": "American Football", "slug": "american-football"},
        "league": {"name": "USA - College", "slug": "usa-college"},
        "live": True,
        "bookmakers": {
            "FanDuel": [{"name": "ML", "odds": [{"home": OSU_DEC, "away": MICH_DEC}]}],
            "DraftKings": [{"name": "ML", "odds": [{"home": OSU_DEC, "away": MICH_DEC}]}],
            "NoVig": [{"name": "ML", "odds": [{"home": OSU_DEC, "away": MICH_DEC}]}],
        },
    }
    doc.update(overrides)
    return doc


def _ncaaf_public_markets() -> list:
    return [
        {
            "ticker": HOME_TICKER,
            "event_ticker": EVENT_TICKER,
            "series_ticker": "KXNCAAFGAME",
            "status": "open",
            "yes_sub_title": "Ohio State",
            "yes_ask_dollars": OSU_ASK,
            "no_ask_dollars": MICH_ASK,
        },
        {
            "ticker": AWAY_TICKER,
            "event_ticker": EVENT_TICKER,
            "series_ticker": "KXNCAAFGAME",
            "status": "open",
            "yes_sub_title": "Michigan",
            "yes_ask_dollars": MICH_ASK,
            "no_ask_dollars": OSU_ASK,
        },
    ]


def _nfl_public_markets() -> list:
    return [
        {
            "ticker": NFL_HOME_TICKER,
            "event_ticker": NFL_EVENT,
            "series_ticker": "KXNFLGAME",
            "status": "open",
            "yes_sub_title": "Kansas City Chiefs",
            "yes_ask_dollars": "0.6200",
            "no_ask_dollars": "0.4000",
        },
        {
            "ticker": NFL_AWAY_TICKER,
            "event_ticker": NFL_EVENT,
            "series_ticker": "KXNFLGAME",
            "status": "open",
            "yes_sub_title": "New England Patriots",
            "yes_ask_dollars": "0.4000",
            "no_ask_dollars": "0.6200",
        },
    ]


def test_usa_college_american_football_is_ncaaf_not_nfl():
    named = _usa_college_cfb_doc()
    assert sport_key_for_doc(named) == "ncaaf"
    slug_only = {
        "league": {"slug": "usa-college"},
        "sport": {"slug": "american-football"},
        "home": OHIO_HOME,
        "away": MICH_AWAY,
    }
    assert sport_key_for_doc(slug_only) == "ncaaf"
    name_only = {
        "league": {"name": "USA - College"},
        "sport": "american-football",
    }
    assert sport_key_for_doc(name_only) == "ncaaf"
    assert sport_key_for_doc({"league": {"slug": "usa-ncaaf"}, "sport": {"slug": "american-football"}}) == "ncaaf"
    assert sport_key_for_doc({"league": "College Football", "sport": "american-football"}) == "ncaaf"
    assert sport_key_for_doc({"league": "NCAAF", "sport": {"slug": "american-football"}}) == "ncaaf"
    assert sport_key_for_doc({"league": "CFB", "sport": "american-football"}) == "ncaaf"


def test_true_nfl_stays_nfl():
    assert sport_key_for_doc({"league": "NFL", "sport": "american-football"}) == "nfl"
    assert (
        sport_key_for_doc(
            {
                "league": {"name": "NFL", "slug": "usa-nfl"},
                "sport": {"slug": "american-football"},
            }
        )
        == "nfl"
    )
    assert sport_key_for_doc({"league": {"slug": "usa-nfl"}, "sport": {"slug": "american-football"}}) == "nfl"


def test_ncaab_and_soccer_not_swallowed_by_football_branch():
    assert (
        sport_key_for_doc(
            {
                "league": {"name": "USA - College", "slug": "usa-college"},
                "sport": {"slug": "basketball"},
            }
        )
        == "ncaab"
    )
    assert sport_key_for_doc({"league": "NCAAB", "sport": "basketball"}) == "ncaab"
    assert sport_key_for_doc({"league": "NBA", "sport": "basketball"}) == "nba"
    ligue = {"league": {"name": "France Ligue 1"}, "sport": {"slug": "football"}}
    assert sport_key_for_doc(ligue) == "soccer"
    epl = {"league": "English Premier League", "sport_slug": "football"}
    assert sport_key_for_doc(epl) == "soccer"


def test_series_for_usa_college_docs_are_kxncaaf_not_kxnfl():
    docs = {NCAAF_EID: _usa_college_cfb_doc()}
    series = series_for_docs(docs)
    assert series == sorted(NCAAF_SERIES)
    assert "KXNCAAFGAME" in series
    assert "KXNCAAFSPREAD" in series
    assert "KXNCAAFTOTAL" in series
    for tok in NFL_SERIES:
        assert tok not in series


def test_mixed_nfl_ncaaf_soccer_series_selection():
    nfl = {
        "league": {"name": "NFL", "slug": "usa-nfl"},
        "sport": {"slug": "american-football"},
        "home": "Kansas City Chiefs",
        "away": "New England Patriots",
    }
    cfb = _usa_college_cfb_doc()
    ligue = {
        "league": {"name": "France Ligue 1"},
        "sport": {"slug": "football"},
        "home": "Toulouse",
        "away": "Lille OSC",
    }
    mixed = {1: nfl, 2: cfb, 3: ligue}
    without_catalog = series_for_docs(mixed)
    assert set(NCAAF_SERIES) <= set(without_catalog)
    assert set(NFL_SERIES) <= set(without_catalog)
    # Empty soccer catalog: do not invent KXEPL / KXLIGUE1.
    assert "KXLIGUE1GAME" not in without_catalog

    catalog = [
        {"ticker": "KXLIGUE1GAME", "title": "Ligue 1 Game", "tags": ["Soccer"]},
        {"ticker": "KXLIGUE1TOTAL", "title": "Ligue 1 Total", "tags": ["Soccer"]},
        {"ticker": "KXEPLGAME", "title": "English Premier League Game", "tags": ["Soccer"]},
    ]
    assert soccer_series_for_docs({3: ligue}, catalog) == ["KXLIGUE1GAME", "KXLIGUE1TOTAL"]

    prev = dict(_soccer_series_cache)
    try:
        _soccer_series_cache["series"] = catalog
        _soccer_series_cache["ts"] = 1.0
        with_catalog = series_for_docs(mixed)
    finally:
        _soccer_series_cache.clear()
        _soccer_series_cache.update(prev)
    assert set(NCAAF_SERIES) <= set(with_catalog)
    assert set(NFL_SERIES) <= set(with_catalog)
    assert "KXLIGUE1GAME" in with_catalog
    assert "KXLIGUE1TOTAL" in with_catalog
    assert "KXEPLGAME" not in with_catalog


def test_cfb_attaches_kxncaaf_and_rejects_nfl_only_catalog():
    """Dry attach: right series → real KX ticker. NFL-only catalog → 0 (old bug)."""
    cfb = _usa_college_cfb_doc()
    assert attach_public_kalshi_markets({NCAAF_EID: cfb}, _nfl_public_markets()) == 0
    assert "Kalshi" not in (cfb.get("bookmakers") or {})

    n = attach_public_kalshi_markets({NCAAF_EID: cfb}, _ncaaf_public_markets())
    assert n == 1
    tickers = kalshi_tickers_from_doc(cfb)
    assert HOME_TICKER in tickers
    assert AWAY_TICKER in tickers
    assert all(is_kalshi_ticker(t) for t in tickers)
    assert not any(is_paper_kalshi_ticker(t) for t in tickers)
    href = cfb["bookmakers"]["Kalshi"][0]["odds"][0]["href"]
    assert href.endswith(HOME_TICKER)
    assert is_kalshi_ticker(HOME_TICKER)
