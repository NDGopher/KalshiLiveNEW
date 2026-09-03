"""Soccer live Kalshi-take cards. Baseball market-3 / idx1 / two-way POWER stay out.

Primary: Lille OSC @ Toulouse ML Draw, Odds-API Kalshi +178, no href/ticker.
"""
from __future__ import annotations

from ev_calculator import american_to_decimal, decimal_to_american, is_plus_print_ev
from execution_guard import (
    is_kalshi_ticker,
    is_paper_kalshi_ticker,
    paper_kalshi_ticker,
    prepare_executable_order,
)
from kalshi_public_feed import (
    attach_public_kalshi_markets,
    book_from_event,
    kalshi_already_priced,
    match_public_event,
    series_for_docs,
    soccer_series_for_docs,
    sport_key_for_doc,
    _ml_row,
)
from odds_ev_monitor import (
    OddsEVMonitor,
    _numeric_close,
    _pick_matching_odds_row,
    _pick_qualifier_line_for_side,
    format_total_qualifier,
    is_synthetic_kxscan_ticker,
)
from plive_pandora import (
    PLIVE_LINE_SET,
    PliveStore,
    is_live_plive_side,
    soccer_totals_identity_rows,
)


LILLE_EID = 220178001
KALSHI_DRAW = american_to_decimal(178)
PACK_POLY = american_to_decimal(163)
PACK_BF = american_to_decimal(160)
PACK_DK = american_to_decimal(160)
PACK_B365 = american_to_decimal(150)
PACK_MGM = american_to_decimal(125)
# Tight live 1X2 (near-even home/away) so three-way POWER vs +160 pack
# lands in the laptop's +5.5% to +7.4% band against Kalshi +178.
HOME_DEC = 3.20
AWAY_DEC = 3.20


def _soccer_mon(*, min_sharp: int = 2) -> OddsEVMonitor:
    mon = OddsEVMonitor(auth_token=None)
    mon.set_filter(
        {
            "betTypes": ["GAMELINES"],
            "minRoi": 0,
            "devigFilter": {
                "sharps": ["Polymarket", "Betfair Exchange", "DraftKings", "Bet365"],
                "method": "POWER",
                "type": "AVERAGE",
                "minEv": 0,
                "minSharpBooks": min_sharp,
                "hold": [{"book": "Any", "max": 20}],
            },
            "oddsRanges": [{"book": "Any", "min": -500, "max": 500}],
            "displayBooks": [
                "Kalshi",
                "PLive",
                "Polymarket",
                "Betfair Exchange",
                "DraftKings",
                "Bet365",
                "BetMGM",
            ],
        }
    )
    return mon


def _lille_doc(*, plive_row=None, kalshi_href=""):
    kalshi_row = {
        "home": HOME_DEC,
        "draw": KALSHI_DRAW,
        "away": AWAY_DEC,
    }
    if kalshi_href:
        kalshi_row["href"] = kalshi_href
    books = {
        "Kalshi": [{"name": "ML", "odds": [kalshi_row]}],
        "Polymarket": [
            {"name": "ML", "odds": [{"home": HOME_DEC, "draw": PACK_POLY, "away": AWAY_DEC}]}
        ],
        "Betfair Exchange": [
            {"name": "ML", "odds": [{"home": HOME_DEC, "draw": PACK_BF, "away": AWAY_DEC}]}
        ],
        "DraftKings": [
            {"name": "ML", "odds": [{"home": HOME_DEC, "draw": PACK_DK, "away": AWAY_DEC}]}
        ],
        "Bet365": [
            {"name": "ML", "odds": [{"home": HOME_DEC, "draw": PACK_B365, "away": AWAY_DEC}]}
        ],
        "BetMGM": [
            {"name": "ML", "odds": [{"home": HOME_DEC, "draw": PACK_MGM, "away": AWAY_DEC}]}
        ],
    }
    if plive_row is not None:
        books["PLive"] = [{"name": "ML", "odds": [plive_row]}]
    return {
        "id": LILLE_EID,
        "home": "Toulouse",
        "away": "Lille OSC",
        "sport": {"slug": "football"},
        "league": {"name": "France Ligue 1"},
        "live": True,
        "bookmakers": books,
    }


def test_sport_key_soccer_not_mlb_nfl():
    assert sport_key_for_doc({"league": "MLB", "sport": "baseball"}) == "mlb"
    assert sport_key_for_doc({"league": "NFL", "sport": "american-football"}) == "nfl"
    assert sport_key_for_doc({"league": "NCAAF", "sport": "american-football"}) == "ncaaf"
    ligue = {"league": {"name": "France Ligue 1"}, "sport": {"slug": "football"}}
    assert sport_key_for_doc(ligue) == "soccer"
    scotland = {"league": "Scotland Premiership", "sport": "football"}
    assert sport_key_for_doc(scotland) == "soccer"
    epl = {"league": "English Premier League", "sport_slug": "football"}
    assert sport_key_for_doc(epl) == "soccer"
    # No sport slug, league hint only.
    assert sport_key_for_doc({"league": "LaLiga"}) == "soccer"
    # Do not guess soccer series for MLB.
    assert series_for_docs({1: {"league": "MLB", "sport": "baseball"}}) == [
        "KXMLBGAME",
        "KXMLBSPREAD",
        "KXMLBTOTAL",
    ]
    # Empty catalog: soccer does not invent KXEPL / KXLIGUE1.
    assert series_for_docs({2: ligue}) == []


def test_soccer_series_discovered_not_guessed():
    catalog = [
        {"ticker": "KXLIGUE1GAME", "title": "Ligue 1 Game", "tags": ["Soccer"]},
        {"ticker": "KXLIGUE1TOTAL", "title": "Ligue 1 Total", "tags": ["Soccer"]},
        {"ticker": "KXEPLGAME", "title": "English Premier League Game", "tags": ["Soccer"]},
        {"ticker": "KXEPLTOTAL", "title": "English Premier League Total Goals", "tags": ["Soccer"]},
        {"ticker": "KXSCOTTISHPREMGAME", "title": "Scottish Premiership Game", "tags": ["Soccer"]},
        {"ticker": "KXNFLGAME", "title": "NFL Game", "tags": ["Football"]},
        {"ticker": "KXFAKEGAME", "title": "Invented", "tags": []},
    ]
    ligue = {"league": {"name": "France Ligue 1"}, "sport": {"slug": "football"}}
    got = soccer_series_for_docs({1: ligue}, catalog)
    assert "KXLIGUE1GAME" in got
    assert "KXLIGUE1TOTAL" in got
    assert "KXEPLGAME" not in got
    assert "KXFAKEGAME" not in got
    assert "KXNFLGAME" not in got

    # Ambiguous "Premier League" matches English + Israeli equally → fail-closed.
    ambig_catalog = [
        {"ticker": "KXEPLGAME", "title": "English Premier League Game", "tags": ["Soccer"]},
        {"ticker": "KXISRPLGAME", "title": "Israeli Premier League Game", "tags": ["Soccer"]},
    ]
    ambig = {"league": "Premier League", "sport": {"slug": "football"}}
    assert soccer_series_for_docs({1: ambig}, ambig_catalog) == []

    epl = {"league": "English Premier League", "sport": {"slug": "football"}}
    epl_got = soccer_series_for_docs({1: epl}, catalog)
    assert epl_got == ["KXEPLGAME", "KXEPLTOTAL"]


def test_public_ml_row_includes_draw_and_keeps_priced_odds_api():
    home_m = {
        "ticker": "KXLIGUE1GAME-26SEP03LILTOU-TOU",
        "yes_sub_title": "Toulouse",
        "yes_ask_dollars": "0.4878",
        "no_ask_dollars": "0.5200",
    }
    away_m = {
        "ticker": "KXLIGUE1GAME-26SEP03LILTOU-LIL",
        "yes_sub_title": "Lille OSC",
        "yes_ask_dollars": "0.2439",
        "no_ask_dollars": "0.7600",
    }
    draw_m = {
        "ticker": "KXLIGUE1GAME-26SEP03LILTOU-TIE",
        "yes_sub_title": "Draw",
        "yes_ask_dollars": f"{1.0 / KALSHI_DRAW:.4f}",
        "no_ask_dollars": "0.6400",
    }
    row = _ml_row([home_m, away_m, draw_m], "Toulouse", "Lille OSC")
    assert row is not None
    assert row.get("draw") and abs(float(row["draw"]) - KALSHI_DRAW) < 1e-3
    assert "TIE" in str(row.get("draw_ticker") or "")
    assert "draw_href" in row
    book = book_from_event({"moneyline": [home_m, away_m, draw_m]}, "Toulouse", "Lille OSC")
    assert book and book[0]["odds"][0].get("draw")

    grouped = {
        "KXLIGUE1GAME-26SEP03LILTOU": {
            "moneyline": [home_m, away_m, draw_m],
            "spread": [],
            "total": [],
        }
    }
    doc = _lille_doc()
    assert match_public_event(doc, grouped) == "KXLIGUE1GAME-26SEP03LILTOU"
    wrong = {**doc, "home": "Paris Saint-Germain", "away": "Marseille"}
    assert match_public_event(wrong, grouped) is None

    priced = _lille_doc()
    assert kalshi_already_priced(priced) is True
    n = attach_public_kalshi_markets({LILLE_EID: priced}, [home_m, away_m, draw_m])
    assert n == 0
    assert priced["bookmakers"]["Kalshi"][0]["odds"][0]["draw"] == KALSHI_DRAW

    missing = _lille_doc()
    del missing["bookmakers"]["Kalshi"]
    assert kalshi_already_priced(missing) is False
    public = []
    for m, series in (
        (home_m, "KXLIGUE1GAME"),
        (away_m, "KXLIGUE1GAME"),
        (draw_m, "KXLIGUE1GAME"),
    ):
        public.append(
            {
                **m,
                "event_ticker": "KXLIGUE1GAME-26SEP03LILTOU",
                "series_ticker": series,
                "status": "open",
            }
        )
    n2 = attach_public_kalshi_markets({LILLE_EID: missing}, public)
    assert n2 == 1
    attached = missing["bookmakers"]["Kalshi"][0]["odds"][0]
    assert attached.get("draw")
    assert attached.get("draw_ticker")


def test_lille_draw_no_href_prints_kalshi_take_not_plive():
    """PRIMARY: Lille Draw +178, rec pack ~+160, no ticker href.

    Live laptop dropped the row after EV preview with no EV-gates line.
    Missing ticker must not kill a priced Kalshi live-scan card.
    Missing PLive → no PLive tile. Kalshi decimal is never a PLive-take.
    """
    mon = _soccer_mon(min_sharp=2)
    doc = _lille_doc(kalshi_href="")
    assert "PLive" not in doc["bookmakers"]
    href = (doc["bookmakers"]["Kalshi"][0]["odds"][0].get("href") or "")
    assert href == ""

    rows = mon.live_scan_value_bets_from_docs({LILLE_EID: doc})
    draw_rows = [
        r
        for r in rows
        if r.get("betSide") == "draw" and r.get("_scan_mname") == "ML"
    ]
    assert draw_rows
    kalshi_rows = [r for r in draw_rows if r.get("_take_only") == "Kalshi"]
    plive_rows = [r for r in draw_rows if r.get("_take_only") == "PLive"]
    assert kalshi_rows
    assert plive_rows == []
    bo = kalshi_rows[0]["bookmakerOdds"]
    assert abs(float(bo["draw"]) - KALSHI_DRAW) < 1e-9
    assert (bo.get("href") or "") == ""

    built = mon._value_bet_to_normalized_bet(kalshi_rows[0], doc, take_book="Kalshi")
    assert built is not None
    assert built["take_book"] == "Kalshi"
    assert int(built["odds"]) == 178
    assert float(built["ev"]) > 0
    assert is_plus_print_ev(built["ev"])
    assert built["autobet_allow"] is False
    assert is_paper_kalshi_ticker(built["ticker"])
    assert not is_kalshi_ticker(built["ticker"])
    assert not is_synthetic_kxscan_ticker(built["ticker"])
    tiles = [t["book"] for t in built["displayBooks"][built["selection"]]]
    assert tiles[0] == "Kalshi"
    assert "PLive" not in tiles

    assert mon._value_bet_to_normalized_bet(kalshi_rows[0], doc, take_book="PLive") is None

    alerts = mon.alerts_from_live_scan_docs({LILLE_EID: doc})
    plus = [a for a in alerts if is_plus_print_ev(getattr(a, "ev_percent", None))]
    kalshi = [a for a in plus if str(getattr(a, "take_book", "")).lower() == "kalshi"]
    plive = [a for a in plus if str(getattr(a, "take_book", "")).lower() == "plive"]
    assert len(kalshi) == 1
    assert plive == []
    assert int(str(kalshi[0].odds).replace("+", "")) == 178
    assert kalshi[0].autobet_allow is False
    assert is_paper_kalshi_ticker(kalshi[0].ticker)

    denied = prepare_executable_order(
        {
            "ticker": kalshi[0].ticker,
            "side": "yes",
            "price_cents": kalshi[0].price_cents,
            "market_type": kalshi[0].market_type,
            "pick": kalshi[0].pick,
            "teams": kalshi[0].teams,
            "take_book": "Kalshi",
        },
        require_credentials=False,
    )
    assert denied.ok is False
    assert "missing_or_invalid_ticker" in denied.reasons or "plive_not_executable" in denied.reasons


def test_lille_draw_soccer_and_all_sports_collapse_to_one_kalshi_card():
    from dashboard import (
        DEFAULT_FILTER_NAME,
        SOCCER_FILTER_NAME,
        create_alert_id,
        dedupe_listed_alert_rows,
    )

    soccer = _soccer_mon(min_sharp=2)
    allsports = _soccer_mon(min_sharp=3)
    doc = _lille_doc()
    vb = soccer.live_scan_value_bets_from_docs({LILLE_EID: doc})
    draw = next(r for r in vb if r.get("betSide") == "draw")
    s_built = soccer._value_bet_to_normalized_bet(draw, doc, take_book="Kalshi")
    a_built = allsports._value_bet_to_normalized_bet(draw, doc, take_book="Kalshi")
    assert s_built and a_built
    s_alert = soccer.parse_bet_to_alert(s_built, draw["event"])
    a_alert = allsports.parse_bet_to_alert(a_built, draw["event"])
    s_alert.filter_name = SOCCER_FILTER_NAME
    a_alert.filter_name = DEFAULT_FILTER_NAME
    assert create_alert_id(s_alert) == create_alert_id(a_alert)
    listed = dedupe_listed_alert_rows(
        [
            {
                "id": create_alert_id(s_alert),
                "teams": s_alert.teams,
                "market_type": s_alert.market_type,
                "pick": s_alert.pick,
                "qualifier": s_alert.qualifier,
                "line": s_alert.line,
                "take_book": s_alert.take_book,
                "ev_percent": s_alert.ev_percent,
                "filter_name": SOCCER_FILTER_NAME,
                "odds": s_alert.odds,
                "match_failed": False,
            },
            {
                "id": create_alert_id(a_alert),
                "teams": a_alert.teams,
                "market_type": a_alert.market_type,
                "pick": a_alert.pick,
                "qualifier": a_alert.qualifier,
                "line": a_alert.line,
                "take_book": a_alert.take_book,
                "ev_percent": a_alert.ev_percent,
                "filter_name": DEFAULT_FILTER_NAME,
                "odds": a_alert.odds,
                "match_failed": False,
            },
        ]
    )
    assert len(listed) == 1
    assert listed[0]["take_book"] == "Kalshi"


def test_soccer_plive_draw_requires_live_1x2_not_market_3():
    mon = _soccer_mon()
    doc = _lille_doc(
        plive_row={
            "home": 1.40,
            "away": 8.00,
            "draw": KALSHI_DRAW,
            "plive_live": True,
            "plive_market": 3,
            "market_type": "game_winner",
        }
    )
    vb = {
        "event": {
            "home": "Toulouse",
            "away": "Lille OSC",
            "league": "France Ligue 1",
            "sport": {"slug": "football"},
        },
        "market": {"name": "ML", "home": HOME_DEC, "draw": KALSHI_DRAW, "away": AWAY_DEC},
        "betSide": "draw",
        "bookmakerOdds": {"draw": KALSHI_DRAW, "home": HOME_DEC, "away": AWAY_DEC, "href": ""},
        "_live_broad_scan": True,
        "_ev_source": "live_event_scan",
        "_take_only": "Kalshi",
        "_canonical_kalshi_row": {"home": HOME_DEC, "draw": KALSHI_DRAW, "away": AWAY_DEC},
    }
    row = doc["bookmakers"]["PLive"][0]["odds"][0]
    assert not is_live_plive_side(row, "draw")
    assert mon._value_bet_to_normalized_bet(vb, doc, take_book="PLive") is None
    kalshi = mon._value_bet_to_normalized_bet(vb, doc, take_book="Kalshi")
    assert kalshi is not None
    assert "PLive" not in [t["book"] for t in kalshi["displayBooks"][kalshi["selection"]]]

    live = {
        "home": 1.45,
        "away": 7.50,
        "draw": american_to_decimal(190),
        "plive_live": True,
        "plive_market": 1,
        "plive_draw_market": 1,
        "market_type": "game_winner",
    }
    assert is_live_plive_side(live, "draw")
    live_doc = _lille_doc(plive_row=live)
    plive_vb = {
        **vb,
        "_take_only": "PLive",
        "_ev_source": "plive_take",
        "_canonical_kalshi_row": dict(live),
        "bookmakerOdds": {"draw": live["draw"], "href": ""},
    }
    plive = mon._value_bet_to_normalized_bet(plive_vb, live_doc, take_book="PLive")
    assert plive is not None
    assert plive["take_book"] == "PLive"
    assert int(plive["odds"]) == 190
    assert int(plive["odds"]) != 178


def test_soccer_under_25_does_not_attach_to_35_45_or_425():
    store = PliveStore()
    store.apply_meta(
        "220986541",
        {"sportId": 5, "home": "Toulouse", "away": "Lille OSC", "ip": True},
    )
    under25 = american_to_decimal(186)
    store.apply_message(
        {
            "isDiff": False,
            "payload": {
                "c": {
                    "m": {
                        "5": {
                            "o": {
                                "over_2.5": {0: 1.52, 1: 1.61},
                                "under_2.5": {0: under25, 1: american_to_decimal(245)},
                                "over_3.5": {0: 2.20, 1: 2.45},
                                "under_3.5": {0: 1.72, 1: 2.05},
                                "over_4.25": {0: 2.80, 1: 3.10},
                                "under_4.25": {0: 1.45, 1: 1.60},
                                "over_4.5": {0: 3.60, 1: 4.10},
                                "under_4.5": {0: 1.28, 1: 1.50},
                            }
                        }
                    }
                }
            },
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventCoefficients.220986541",
    )
    ident = soccer_totals_identity_rows(store.markets_for_event("220986541"))
    by_line = {round(r["line"], 2): r for r in ident}
    assert 2.5 in by_line
    assert by_line[2.5]["under_am"] == 186
    for lf in (3.5, 4.25, 4.5):
        assert lf in by_line
        assert by_line[lf]["under_am"] != 186
        assert by_line[lf]["over_am"] != 186
    owners = [r["line"] for r in ident if r.get("under_am") == 186]
    assert owners == [2.5]


def test_kortrijk_under_175_prints_175_not_18():
    """Kortrijk @ Anderlecht Under 1.75 stays 1.75. Never .1f → 1.8. Never join 1.8/2.0."""
    store = PliveStore()
    store.apply_meta(
        "2201001",
        {
            "sportId": 5,
            "home": "RSC Anderlecht",
            "away": "KV Kortrijk",
            "leagueName": "Belgium Jupiler League",
            "ip": True,
        },
    )
    under175 = american_to_decimal(186)
    store.apply_message(
        {
            "isDiff": False,
            "payload": {
                "c": {
                    "m": {
                        "5": {
                            "o": {
                                "over_1.75": {0: 1.80, 1: 1.90},
                                "under_1.75": {0: under175, 1: 2.10},
                                "over_1.8": {0: 1.70, 1: 1.85},
                                "under_1.8": {0: 2.20, 1: 2.40},
                                "over_2.0": {0: 2.05, 1: 2.20},
                                "under_2.0": {0: 1.75, 1: 1.90},
                            }
                        }
                    }
                }
            },
        },
        event_name=f"live.main.{PLIVE_LINE_SET}.eventCoefficients.2201001",
    )
    ident = soccer_totals_identity_rows(store.markets_for_event("2201001"))
    by_line = {round(float(r["line"]), 2): r for r in ident}
    assert 1.75 in by_line
    assert 1.8 not in by_line
    assert by_line[1.75]["under_am"] == 186
    owners = [r["line"] for r in ident if r.get("under_am") == 186]
    assert owners == [1.75]

    row_175 = {
        "hdp": 1.75,
        "max": 1.75,
        "line": 1.75,
        "over": 1.80,
        "under": under175,
        "plive_live": True,
        "plive_market": 5,
        "market_type": "game_total",
    }
    pick, qual, line_val = _pick_qualifier_line_for_side(
        "RSC Anderlecht", "KV Kortrijk", "Totals", "under", row_175
    )
    assert pick == "Under"
    assert qual == "1.75"
    assert qual != "1.8"
    assert float(line_val) == 1.75
    assert format_total_qualifier(1.75) == "1.75"
    assert format_total_qualifier(1.75) != "1.8"
    assert format_total_qualifier(8.5) == "8.5"
    assert format_total_qualifier(-3.5) == "-3.5" or format_total_qualifier(3.5) == "3.5"
    assert _numeric_close(1.75, 1.8) is False
    assert _numeric_close(1.75, 2.0) is False
    mk = {
        "name": "Totals",
        "odds": [
            row_175,
            {"hdp": 1.8, "max": 1.8, "over": 1.70, "under": 2.20},
            {"hdp": 2.0, "max": 2.0, "over": 2.05, "under": 1.75},
        ],
    }
    hit = _pick_matching_odds_row(mk, "Totals", {"hdp": 1.75, "max": 1.75, "under": under175})
    assert abs(float(hit["hdp"]) - 1.75) < 1e-9
    assert _pick_matching_odds_row(mk, "Totals", {"hdp": 1.8, "max": 1.8}) != hit

    mon = _soccer_mon()
    # Same-sign plus pack as Al-Kholood Under 2.5 +186. A minus 1.72 pack
    # is a sign-flip vs +186 and gets junked — that is not this bug.
    # FanDuel 1.8 must not join the 1.75 take.
    pack_over, pack_under = 1.55, 2.40
    doc = {
        "home": "RSC Anderlecht",
        "away": "KV Kortrijk",
        "sport": {"slug": "football"},
        "league": {"name": "Belgium Jupiler League"},
        "bookmakers": {
            "PLive": [{"name": "Totals", "odds": [row_175]}],
            "Betfair Exchange": [
                {"name": "Totals", "odds": [{"max": 1.75, "over": pack_over, "under": pack_under}]}
            ],
            "Bet365": [
                {"name": "Totals", "odds": [{"max": 1.75, "over": 1.56, "under": 2.38}]}
            ],
            "DraftKings": [
                {"name": "Totals", "odds": [{"max": 1.75, "over": 1.54, "under": 2.42}]}
            ],
            "Polymarket": [
                {"name": "Totals", "odds": [{"max": 1.75, "over": 1.57, "under": 2.36}]}
            ],
            "FanDuel": [{"name": "Totals", "odds": [{"max": 1.8, "over": 1.70, "under": 2.20}]}],
        },
    }
    vb = {
        "event": {
            "home": "RSC Anderlecht",
            "away": "KV Kortrijk",
            "league": "Belgium Jupiler League",
            "sport": {"slug": "football"},
        },
        "market": {"name": "Totals", **row_175},
        "betSide": "under",
        "bookmakerOdds": {"under": under175, "over": 1.80},
        "_live_broad_scan": True,
        "_ev_source": "plive_take",
        "_take_only": "PLive",
        "_canonical_kalshi_row": dict(row_175),
    }
    built = mon._value_bet_to_normalized_bet(vb, doc, take_book="PLive")
    assert built is not None
    assert built["selection"] == "Under"
    assert built["qualifier"] == "1.75"
    assert built["qualifier"] != "1.8"
    assert float(built["line"]) == 1.75
    assert built["autobet_allow"] is False


def test_paper_kalshi_ticker_is_not_kxscan_and_not_executable():
    tok = paper_kalshi_ticker("Lille OSC @ Toulouse", "Draw", None)
    assert is_paper_kalshi_ticker(tok)
    assert not is_kalshi_ticker(tok)
    assert not is_synthetic_kxscan_ticker(tok)
    check = prepare_executable_order(
        {
            "ticker": tok,
            "side": "yes",
            "price_cents": 36,
            "market_type": "Moneyline",
            "pick": "Draw",
            "teams": "Lille OSC @ Toulouse",
            "take_book": "Kalshi",
        },
        require_credentials=False,
    )
    assert check.ok is False


def test_auto_bet_stays_off_and_paper_handler_exists():
    from pathlib import Path

    dash = (Path(__file__).resolve().parents[1] / "dashboard.py").read_text(encoding="utf-8")
    src = (Path(__file__).resolve().parents[1] / "odds_ev_monitor.py").read_text(encoding="utf-8")
    assert "auto_bet_enabled = False" in dash
    assert "handle_kalshi_paper_display_alert" in dash
    assert "href=\"\" synthetic scan rows are not Kalshi-take cards" not in src
