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


# --- Busy Saturday: tickerless Odds-API last + alt strikes (Iowa -27.5) ---

IOWA_HOME = "Iowa Hawkeyes"
NIU_AWAY = "Northern Illinois Huskies"
IOWA_EID = 260905027
IOWA_EVENT = "KXNCAAFGAME-26SEP05NIUIOWA"
IOWA_ML_HOME = "KXNCAAFGAME-26SEP05NIUIOWA-IOWA"
IOWA_ML_AWAY = "KXNCAAFGAME-26SEP05NIUIOWA-NIU"
IOWA_SPR_28 = "KXNCAAFSPREAD-26SEP05NIUIOWA-IOWA28"
IOWA_SPR_27 = "KXNCAAFSPREAD-26SEP05NIUIOWA-IOWA27"
IOWA_SPR_4 = "KXNCAAFSPREAD-26SEP05NIUIOWA-IOWA4"
WYO_EVENT = "KXNCAAFGAME-26SEP05WYOCSU"
WYO_ML_HOME = "KXNCAAFGAME-26SEP05WYOCSU-CSU"
WYO_ML_AWAY = "KXNCAAFGAME-26SEP05WYOCSU-WYO"
SEP12_BAMA = "KXNCAAFGAME-26SEP12ALAUG"
# Saturday 7:30 PM ET 2026-09-05 (23:30 UTC) — 18h-from-midnight-UTC used to reject this.
SAT_NIGHT_ET = "2026-09-05T23:30:00Z"


def _iowa_doc(*, href="", age_sec=8.0, now=None, hdp=-27.5):
    import time as _time

    now = float(now if now is not None else _time.time())
    row = {
        "home": 1.45,
        "away": 2.80,
        "hdp": hdp,
    }
    if href:
        row["href"] = href
    return {
        "id": IOWA_EID,
        "home": IOWA_HOME,
        "away": NIU_AWAY,
        "sport": {"name": "American Football", "slug": "american-football"},
        "league": {"name": "USA - College", "slug": "usa-college"},
        "live": True,
        "startTime": SAT_NIGHT_ET,
        "book_updated_at": {"Kalshi": now - float(age_sec), "FanDuel": now - 2.0},
        "bookmakers": {
            "Kalshi": [{"name": "Spread", "odds": [row]}],
            "FanDuel": [{"name": "Spread", "odds": [{"home": 1.50, "away": 2.60, "hdp": hdp}]}],
            "DraftKings": [{"name": "Spread", "odds": [{"home": 1.52, "away": 2.55, "hdp": hdp}]}],
            "NoVig": [{"name": "Spread", "odds": [{"home": 1.51, "away": 2.58, "hdp": hdp}]}],
        },
    }


def _iowa_public_markets():
    return [
        {
            "ticker": IOWA_ML_HOME,
            "event_ticker": IOWA_EVENT,
            "series_ticker": "KXNCAAFGAME",
            "status": "open",
            "yes_sub_title": "Iowa",
            "yes_ask_dollars": "0.6900",
            "no_ask_dollars": "0.3300",
        },
        {
            "ticker": IOWA_ML_AWAY,
            "event_ticker": IOWA_EVENT,
            "series_ticker": "KXNCAAFGAME",
            "status": "open",
            "yes_sub_title": "Northern Illinois",
            "yes_ask_dollars": "0.3300",
            "no_ask_dollars": "0.6900",
        },
        {
            "ticker": IOWA_SPR_28,
            "event_ticker": "KXNCAAFSPREAD-26SEP05NIUIOWA",
            "series_ticker": "KXNCAAFSPREAD",
            "status": "open",
            "yes_sub_title": "Iowa wins by over 27.5 points",
            "floor_strike": 27.5,
            "yes_ask_dollars": "0.5200",
            "no_ask_dollars": "0.5000",
        },
        {
            "ticker": IOWA_SPR_27,
            "event_ticker": "KXNCAAFSPREAD-26SEP05NIUIOWA",
            "series_ticker": "KXNCAAFSPREAD",
            "status": "open",
            "yes_sub_title": "Iowa wins by over 26.5 points",
            "floor_strike": 26.5,
            "yes_ask_dollars": "0.4800",
            "no_ask_dollars": "0.5400",
        },
        {
            "ticker": IOWA_SPR_4,
            "event_ticker": "KXNCAAFSPREAD-26SEP05NIUIOWA",
            "series_ticker": "KXNCAAFSPREAD",
            "status": "open",
            "yes_sub_title": "Iowa wins by over 3.5 points",
            "floor_strike": 3.5,
            "yes_ask_dollars": "0.6100",
            "no_ask_dollars": "0.4100",
        },
        {
            "ticker": WYO_ML_HOME,
            "event_ticker": WYO_EVENT,
            "series_ticker": "KXNCAAFGAME",
            "status": "open",
            "yes_sub_title": "Colorado St.",
            "yes_ask_dollars": "0.5500",
            "no_ask_dollars": "0.4700",
        },
        {
            "ticker": WYO_ML_AWAY,
            "event_ticker": WYO_EVENT,
            "series_ticker": "KXNCAAFGAME",
            "status": "open",
            "yes_sub_title": "Wyoming",
            "yes_ask_dollars": "0.4700",
            "no_ask_dollars": "0.5500",
        },
        {
            "ticker": "KXNCAAFGAME-26SEP12ALAUG-ALA",
            "event_ticker": SEP12_BAMA,
            "series_ticker": "KXNCAAFGAME",
            "status": "open",
            "yes_sub_title": "Alabama",
            "yes_ask_dollars": "0.7000",
            "no_ask_dollars": "0.3200",
        },
        {
            "ticker": "KXNCAAFGAME-26SEP12ALAUG-UGA",
            "event_ticker": SEP12_BAMA,
            "series_ticker": "KXNCAAFGAME",
            "status": "open",
            "yes_sub_title": "Georgia",
            "yes_ask_dollars": "0.3200",
            "no_ask_dollars": "0.7000",
        },
    ]


def test_tickerless_fresh_odds_api_kalshi_gets_kxncaaf_href():
    """Desk: fresh Kalshi decimal, href null → paper card. Enrich, don't skip."""
    import time as _time

    from kalshi_public_feed import kalshi_already_priced, kalshi_doc_has_real_kx

    now = _time.time()
    doc = _iowa_doc(href="", age_sec=8.0, now=now)
    assert kalshi_already_priced(doc, now=now) is True
    assert kalshi_doc_has_real_kx(doc) is False
    n = attach_public_kalshi_markets({IOWA_EID: doc}, _iowa_public_markets(), now=now)
    assert n == 1
    row = doc["bookmakers"]["Kalshi"][0]["odds"][0]
    assert row["home"] == 1.45
    assert row["hdp"] == -27.5
    assert IOWA_SPR_28 in str(row.get("href") or "")
    assert IOWA_SPR_28 in str(row.get("ticker") or "")
    assert IOWA_SPR_27 not in str(row.get("href") or "")
    assert IOWA_SPR_4 not in str(row.get("href") or "")
    assert is_kalshi_ticker(row["ticker"])
    assert not is_paper_kalshi_ticker(row["ticker"])


def test_wrong_spread_line_is_not_painted():
    """+3.5 / neighbor 26.5 must not land on a -27.5 row (ceil fail-closed)."""
    import time as _time

    now = _time.time()
    doc = _iowa_doc(href="", hdp=-27.5, now=now)
    only_alts = [
        m
        for m in _iowa_public_markets()
        if m["ticker"] in (IOWA_SPR_27, IOWA_SPR_4, IOWA_ML_HOME, IOWA_ML_AWAY)
    ]
    n = attach_public_kalshi_markets({IOWA_EID: doc}, only_alts, now=now)
    row = doc["bookmakers"]["Kalshi"][0]["odds"][0]
    href = str(row.get("href") or "")
    assert IOWA_SPR_27 not in href
    assert IOWA_SPR_4 not in href
    assert IOWA_SPR_28 not in href
    assert row["home"] == 1.45
    assert n == 0 or not href


def test_fresh_odds_api_with_real_kx_keeps_href():
    import time as _time

    now = _time.time()
    keep = "KXNCAAFSPREAD-26SEP05NIUIOWA-IOWA28"
    doc = _iowa_doc(href=f"https://kalshi.com/markets/{keep}", now=now)
    n = attach_public_kalshi_markets({IOWA_EID: doc}, _iowa_public_markets(), now=now)
    href = doc["bookmakers"]["Kalshi"][0]["odds"][0]["href"]
    assert keep in href
    assert n in (0, 1)


def test_state_and_nested_names_do_not_cross_attach():
    from kalshi_public_feed import match_public_event, _group_events, tokens_match_team

    assert tokens_match_team("Texas Longhorns", "Texas") is True
    assert tokens_match_team("Texas Longhorns", "Texas St.") is False
    assert tokens_match_team("Michigan Wolverines", "Michigan") is True
    assert tokens_match_team("Michigan Wolverines", "Western Michigan") is False
    assert tokens_match_team("Iowa Hawkeyes", "Iowa") is True
    assert tokens_match_team("Iowa Hawkeyes", "Colorado St.") is False

    grouped = _group_events(_iowa_public_markets())
    iowa = _iowa_doc()
    assert match_public_event(iowa, grouped) == IOWA_EVENT
    wyo = {
        "home": "Colorado State Rams",
        "away": "Wyoming Cowboys",
        "startTime": SAT_NIGHT_ET,
    }
    assert match_public_event(wyo, grouped) == WYO_EVENT
    next_week = {
        "home": "Alabama Crimson Tide",
        "away": "Georgia Bulldogs",
        "startTime": SAT_NIGHT_ET,
    }
    assert match_public_event(next_week, grouped) is None


def test_date_only_cfb_suffix_allows_saturday_night_et():
    from kalshi_public_feed import _timing_ok

    ev = IOWA_EVENT
    night = {"startTime": SAT_NIGHT_ET}
    noon = {"startTime": "2026-09-05T16:00:00Z"}
    next_sat = {"startTime": "2026-09-12T19:00:00Z"}
    assert _timing_ok(ev, night) is True
    assert _timing_ok(ev, noon) is True
    assert _timing_ok(ev, next_sat) is False
    soccer_clock = "KXLALIGAGAME-26SEP032140CELSOC"
    too_far = {"startTime": "2026-09-05T21:40:00Z"}
    assert _timing_ok(soccer_clock, {"startTime": "2026-09-03T21:40:00Z"}) is True
    assert _timing_ok(soccer_clock, too_far) is False


def test_live_scan_copies_enriched_href_as_market_ticker():
    """Public attach still paints market-level href; scan copies it for side."""
    import time as _time

    from odds_ev_monitor import OddsEVMonitor, extract_kalshi_ticker_from_href

    now = _time.time()
    doc = _iowa_doc(href="", now=now)
    assert attach_public_kalshi_markets({IOWA_EID: doc}, _iowa_public_markets(), now=now) == 1
    mon = OddsEVMonitor(auth_token=None)
    mon.set_filter(
        {
            "betTypes": ["GAMELINES"],
            "minRoi": 0,
            "devigFilter": {
                "sharps": ["FanDuel", "DraftKings", "NoVig"],
                "method": "POWER",
                "type": "AVERAGE",
                "minEv": 0,
                "minSharpBooks": 3,
            },
        }
    )
    rows = mon.live_scan_value_bets_from_docs({IOWA_EID: doc})
    spr = [
        r
        for r in rows
        if str(r.get("_scan_mname") or "").lower() == "spread"
        and str(r.get("_take_only") or "").lower() == "kalshi"
    ]
    assert spr
    href = (spr[0].get("bookmakerOdds") or {}).get("href") or ""
    assert extract_kalshi_ticker_from_href(href) == IOWA_SPR_28


def _iowa_doc_with_odds_api_event_ids(**kwargs):
    doc = _iowa_doc(**kwargs)
    doc["urls"] = {"Kalshi": f"https://kalshi.com/events/{IOWA_EVENT}"}
    doc["bookmakerIds"] = {"Kalshi": IOWA_EVENT}
    return doc


def test_odds_api_event_ticker_fields_are_documented_and_parsed():
    """bookmakerIds / urls carry the event KX. Odds rows do not (documented)."""
    from odds_api_client import (
        coerce_odds_api_kalshi_ticker,
        odds_api_kalshi_event_ticker,
        odds_api_kalshi_row_ticker,
        resolve_kalshi_take_ticker,
    )

    doc = _iowa_doc_with_odds_api_event_ids(href="")
    assert odds_api_kalshi_event_ticker(doc) == IOWA_EVENT
    assert coerce_odds_api_kalshi_ticker(doc["urls"]["Kalshi"]) == IOWA_EVENT
    assert odds_api_kalshi_row_ticker(doc["bookmakers"]["Kalshi"][0]["odds"][0]) is None
    assert resolve_kalshi_take_ticker(doc["bookmakers"]["Kalshi"][0]["odds"][0], doc) == IOWA_EVENT
    assert coerce_odds_api_kalshi_ticker("KXSCAN-NOT-REAL") is None


def test_odds_api_event_ticker_paints_take_card_without_attach():
    """attach=0 must not leave every card paper when Odds-API named the event."""
    import time as _time

    from ev_calculator import is_plus_print_ev
    from odds_ev_monitor import OddsEVMonitor

    now = _time.time()
    doc = _iowa_doc_with_odds_api_event_ids(href="", now=now)
    mon = OddsEVMonitor(auth_token=None)
    mon.set_filter(
        {
            "betTypes": ["GAMELINES"],
            "minRoi": 0,
            "devigFilter": {
                "sharps": ["FanDuel", "DraftKings", "NoVig"],
                "method": "POWER",
                "type": "AVERAGE",
                "minEv": 0,
                "minSharpBooks": 3,
            },
        }
    )
    rows = mon.live_scan_value_bets_from_docs({IOWA_EID: doc})
    spr = [
        r
        for r in rows
        if str(r.get("_scan_mname") or "").lower() == "spread"
        and str(r.get("_take_only") or "").lower() == "kalshi"
        and r.get("betSide") == "home"
    ]
    assert spr
    bo = spr[0]["bookmakerOdds"]
    assert not (bo.get("href") or "")
    assert bo.get("eventTicker") == IOWA_EVENT
    built = mon._value_bet_to_normalized_bet(spr[0], doc, take_book="Kalshi")
    assert built is not None
    assert built["ticker"] == IOWA_EVENT
    assert is_kalshi_ticker(built["ticker"])
    assert not is_paper_kalshi_ticker(built["ticker"])
    assert IOWA_EVENT in str(built.get("link") or "")
    if is_plus_print_ev(built.get("ev")) and built.get("strict_pass"):
        assert built["autobet_allow"] is True
    alert = mon.parse_bet_to_alert(built, doc)
    if alert is not None:
        assert alert.ticker == IOWA_EVENT
        assert alert.autobet_allow is True or not built["autobet_allow"]


def test_public_attach_market_href_wins_over_odds_api_event_ticker():
    """Market-level KX from public attach is preferred over the event ticker."""
    import time as _time

    from odds_ev_monitor import OddsEVMonitor, extract_kalshi_ticker_from_href

    now = _time.time()
    doc = _iowa_doc_with_odds_api_event_ids(href="", now=now)
    assert attach_public_kalshi_markets({IOWA_EID: doc}, _iowa_public_markets(), now=now) == 1
    mon = OddsEVMonitor(auth_token=None)
    mon.set_filter(
        {
            "betTypes": ["GAMELINES"],
            "minRoi": 0,
            "devigFilter": {
                "sharps": ["FanDuel", "DraftKings", "NoVig"],
                "method": "POWER",
                "type": "AVERAGE",
                "minEv": 0,
                "minSharpBooks": 3,
            },
        }
    )
    rows = mon.live_scan_value_bets_from_docs({IOWA_EID: doc})
    spr = [
        r
        for r in rows
        if str(r.get("_scan_mname") or "").lower() == "spread"
        and str(r.get("_take_only") or "").lower() == "kalshi"
    ]
    href = (spr[0].get("bookmakerOdds") or {}).get("href") or ""
    assert extract_kalshi_ticker_from_href(href) == IOWA_SPR_28
    built = mon._value_bet_to_normalized_bet(spr[0], doc, take_book="Kalshi")
    assert built is not None
    assert built["ticker"] == IOWA_SPR_28


def test_odds_api_event_ticker_does_not_block_public_market_enrich():
    """Stamping event identity on the doc must leave tickerless rows enrichable."""
    import time as _time

    from kalshi_public_feed import kalshi_doc_has_real_kx, kalshi_row_has_real_kx
    from odds_api_client import stamp_odds_api_kalshi_event_identity

    now = _time.time()
    doc = _iowa_doc_with_odds_api_event_ids(href="", now=now)
    assert stamp_odds_api_kalshi_event_identity(doc) == IOWA_EVENT
    row = doc["bookmakers"]["Kalshi"][0]["odds"][0]
    assert kalshi_row_has_real_kx(row) is False
    assert kalshi_doc_has_real_kx(doc) is False
    n = attach_public_kalshi_markets({IOWA_EID: doc}, _iowa_public_markets(), now=now)
    assert n == 1
    assert IOWA_SPR_28 in str(row.get("href") or "")


def test_empty_public_fetch_keeps_last_good_cache():
    from kalshi_public_feed import _cache, commit_fetched_markets

    prev = dict(_cache)
    try:
        _cache.clear()
        _cache.update({"ts": 1.0, "key": "", "markets": []})
        first = commit_fetched_markets("KXNCAAFGAME", [{"ticker": IOWA_ML_HOME}])
        assert first and first[0]["ticker"] == IOWA_ML_HOME
        kept = commit_fetched_markets("KXNCAAFGAME", [])
        assert kept and kept[0]["ticker"] == IOWA_ML_HOME
    finally:
        _cache.clear()
        _cache.update(prev)
