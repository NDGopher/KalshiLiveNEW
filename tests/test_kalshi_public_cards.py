"""Kalshi +EV cards from public/read-only listing — no trading credentials."""
from __future__ import annotations

from pathlib import Path

from ev_calculator import american_to_decimal, is_plus_print_ev
from execution_guard import has_trading_credentials
from kalshi_public_feed import (
    attach_public_kalshi_markets,
    attach_public_kalshi_to_docs,
    decimal_from_ask,
    kalshi_already_priced,
    match_public_event,
    series_for_docs,
)
from odds_ev_monitor import (
    OddsEVMonitor,
    _odds_doc_has_take_tradable_gameline,
    extract_kalshi_ticker_from_href,
)

DET_MIN_EID = 199295331
HOME_TICKER = "KXMLBGAME-26SEP03DETMINE-MIN"
AWAY_TICKER = "KXMLBGAME-26SEP03DETMINE-DET"
EVENT_TICKER = "KXMLBGAME-26SEP03DETMINE"
SPREAD_TICKER = "KXMLBSPREAD-26SEP03DETMINE-MIN1"
TOTAL_TICKER = "KXMLBTOTAL-26SEP03DETMINE-85"

# Same-sign favorite as the rec pack (-142/-141/-139). -133 is +EV and
# inside the 10¢ junk screen used by filter_sharp_panel.
KALSHI_HOME_DEC = american_to_decimal(-133)
KALSHI_AWAY_DEC = american_to_decimal(125)
KALSHI_HOME_ASK = f"{1.0 / KALSHI_HOME_DEC:.4f}"
KALSHI_AWAY_ASK = f"{1.0 / KALSHI_AWAY_DEC:.4f}"


def _public_mlb_markets() -> list:
    return [
        {
            "ticker": HOME_TICKER,
            "event_ticker": EVENT_TICKER,
            "series_ticker": "KXMLBGAME",
            "status": "open",
            "yes_sub_title": "Minnesota",
            "yes_ask_dollars": KALSHI_HOME_ASK,
            "no_ask_dollars": KALSHI_AWAY_ASK,
        },
        {
            "ticker": AWAY_TICKER,
            "event_ticker": EVENT_TICKER,
            "series_ticker": "KXMLBGAME",
            "status": "open",
            "yes_sub_title": "Detroit",
            "yes_ask_dollars": KALSHI_AWAY_ASK,
            "no_ask_dollars": KALSHI_HOME_ASK,
        },
        {
            "ticker": SPREAD_TICKER,
            "event_ticker": "KXMLBSPREAD-26SEP03DETMINE",
            "series_ticker": "KXMLBSPREAD",
            "status": "open",
            "yes_sub_title": "Minnesota",
            "floor_strike": 1.5,
            "yes_ask_dollars": "0.5200",
            "no_ask_dollars": "0.5000",
        },
        {
            "ticker": TOTAL_TICKER,
            "event_ticker": "KXMLBTOTAL-26SEP03DETMINE",
            "series_ticker": "KXMLBTOTAL",
            "status": "open",
            "yes_sub_title": "Over",
            "floor_strike": 8.5,
            "yes_ask_dollars": "0.4800",
            "no_ask_dollars": "0.5400",
        },
    ]


def _rec_ml(dec: float, opp: float) -> list:
    return [{"name": "ML", "odds": [{"home": dec, "away": opp}]}]


def _odds_doc_no_take(*, home="Minnesota Twins", away="Detroit Tigers") -> dict:
    fd = american_to_decimal(-142)
    dk = american_to_decimal(-141)
    nv = american_to_decimal(-139)
    opp = american_to_decimal(125)
    return {
        "id": DET_MIN_EID,
        "home": home,
        "away": away,
        "league": "MLB",
        "sport": "baseball",
        "bookmakers": {
            "FanDuel": _rec_ml(fd, opp),
            "DraftKings": _rec_ml(dk, opp),
            "NoVig": _rec_ml(nv, opp),
        },
    }


def _cards_monitor() -> OddsEVMonitor:
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
                "hold": [{"book": "Any", "max": 20}],
            },
            "oddsRanges": [{"book": "Any", "min": -500, "max": 500}],
            "minLimits": [{"book": "Kalshi", "min": 0}],
            "minSharpLimits": [],
            "displayBooks": ["Kalshi", "FanDuel", "DraftKings", "NoVig", "PLive"],
        }
    )
    return mon


def test_decimal_from_public_ask():
    assert abs(decimal_from_ask(0.455) - (1.0 / 0.455)) < 1e-6
    assert decimal_from_ask(0.0) is None
    assert decimal_from_ask(1.0) is None


def test_series_for_mlb_doc():
    series = series_for_docs({1: _odds_doc_no_take()})
    assert "KXMLBGAME" in series
    assert "KXMLBSPREAD" in series
    assert "KXMLBTOTAL" in series


def test_wrong_teams_do_not_attach():
    doc = _odds_doc_no_take(home="New York Yankees", away="Boston Red Sox")
    assert match_public_event(doc, {}) is None
    n = attach_public_kalshi_markets({1: doc}, _public_mlb_markets())
    assert n == 0
    assert "Kalshi" not in (doc.get("bookmakers") or {})
    assert _odds_doc_has_take_tradable_gameline(doc) is False


def test_does_not_overwrite_priced_odds_api_kalshi():
    doc = _odds_doc_no_take()
    doc["bookmakers"]["Kalshi"] = [
        {"name": "ML", "odds": [{"home": 1.80, "away": 2.10, "href": "https://kalshi.com/markets/KXKEEP"}]}
    ]
    assert kalshi_already_priced(doc) is True
    n = attach_public_kalshi_markets({1: doc}, _public_mlb_markets())
    assert n == 0
    href = doc["bookmakers"]["Kalshi"][0]["odds"][0]["href"]
    assert "KXKEEP" in href


def test_plive_only_stays_tradable_without_public_kalshi():
    doc = {
        "id": 7,
        "home": "Minnesota Twins",
        "away": "Detroit Tigers",
        "league": "MLB",
        "bookmakers": {
            "PLive": [
                {"name": "Totals", "odds": [{"hdp": 11.5, "over": 1.892857, "under": 1.847458}]}
            ]
        },
    }
    assert _odds_doc_has_take_tradable_gameline(doc) is True
    n = attach_public_kalshi_markets({7: doc}, [])
    assert n == 0
    assert _odds_doc_has_take_tradable_gameline(doc) is True


def test_public_attach_makes_doc_tradable_without_credentials():
    assert has_trading_credentials(None, None) is False
    doc = _odds_doc_no_take()
    assert _odds_doc_has_take_tradable_gameline(doc) is False
    n = attach_public_kalshi_markets({DET_MIN_EID: doc}, _public_mlb_markets())
    assert n == 1
    assert _odds_doc_has_take_tradable_gameline(doc) is True
    kal = {m["name"]: m for m in doc["bookmakers"]["Kalshi"]}
    assert "ML" in kal
    home_href = kal["ML"]["odds"][0]["home_href"]
    away_href = kal["ML"]["odds"][0]["away_href"]
    assert extract_kalshi_ticker_from_href(home_href) == HOME_TICKER
    assert extract_kalshi_ticker_from_href(away_href) == AWAY_TICKER
    assert kal["Spread"]["odds"][0]["hdp"] == -1.5
    assert kal["Totals"]["odds"][0]["line"] == 8.5


def test_card_generation_without_trading_credentials():
    """Odds-API recs + public Kalshi listing → +EV Kalshi card. No private key."""
    from kalshi_client import KalshiClient

    client = KalshiClient()
    client.auth.priv = None
    client.auth.kid = None
    assert client.has_trading_credentials() is False
    assert has_trading_credentials(None, None) is False

    doc = _odds_doc_no_take()
    odds_by_id = {DET_MIN_EID: doc}
    assert _odds_doc_has_take_tradable_gameline(doc) is False

    attached = attach_public_kalshi_markets(odds_by_id, _public_mlb_markets())
    assert attached == 1
    assert _odds_doc_has_take_tradable_gameline(doc) is True

    mon = _cards_monitor()
    vbs = mon.live_scan_value_bets_from_docs(odds_by_id)
    kalshi_vbs = [
        r for r in vbs if not r.get("_take_only") or r.get("_take_only") == "Kalshi"
    ]
    home_rows = [r for r in kalshi_vbs if r.get("betSide") == "home" and r.get("_scan_mname") == "ML"]
    assert home_rows, [ (r.get("_scan_mname"), r.get("betSide")) for r in kalshi_vbs ]
    href = (home_rows[0].get("bookmakerOdds") or {}).get("href")
    assert extract_kalshi_ticker_from_href(href) == HOME_TICKER

    built = mon._value_bet_to_normalized_bet(home_rows[0], doc, take_book="Kalshi")
    assert built is not None
    assert built["take_book"] == "Kalshi"
    assert built["ticker"] == HOME_TICKER
    assert built["ev"] > 0
    assert "KALSHI-ACCESS" not in str(built)

    alerts = mon.alerts_from_live_scan_docs(odds_by_id)
    plus = [a for a in alerts if is_plus_print_ev(getattr(a, "ev_percent", None))]
    kalshi = [
        a
        for a in plus
        if str(getattr(a, "take_book", "")).lower() == "kalshi"
        and str(a.ticker or "") == HOME_TICKER
    ]
    assert kalshi, [(a.take_book, a.ticker, a.pick, a.ev_percent) for a in alerts]
    card = kalshi[0]
    assert card.price_cents and 1 <= int(card.price_cents) <= 99
    assert HOME_TICKER in str(card.ticker)


def test_listing_gets_are_public_ok():
    client = (Path(__file__).resolve().parents[1] / "kalshi_client.py").read_text(encoding="utf-8")
    assert 'async def search_markets' in client
    assert 'async def get_event_by_ticker' in client
    assert 'async def search_markets_by_event' in client
    # Unsigned listing when no private key — same helper as get_market_by_ticker.
    assert client.count('_headers_for("GET", path, public_ok=True)') >= 5


def test_async_attach_uses_injected_markets_not_network():
    import asyncio

    doc = _odds_doc_no_take()

    async def _run():
        return await attach_public_kalshi_to_docs({DET_MIN_EID: doc}, _public_mlb_markets())

    n = asyncio.run(_run())
    assert n == 1
    assert _odds_doc_has_take_tradable_gameline(doc) is True
