"""Executable Kalshi cards: real market ticker + yes/no. No fuzzy lines.

Live desk (2026-09-05): /api/alerts shipped autobet_allow=true with
side=None and GAME event tickers (KXMLBGAME-… / KXNCAAFGAME-…) on
spread/total takes. find_submarket then Invalid submarket key — no order.

Sheet contract (1382 executed): market tickers like KXNCAAMB*/KXNBATOTAL*/
KXMLBSPREAD* — never bare GAME event + side=null. All fills EV≥5%
(median 10.6%). Auto path stays ≥2–20%; do not chase sub-2% crumbs.

Contract lives on KalshiClient.find_submarket. Auto-bet stays OFF here.
"""
from __future__ import annotations

from pathlib import Path

from execution_guard import (
    expected_side_for_alert,
    is_bare_game_event_ticker,
    is_executable_market_ticker,
    is_kalshi_ticker,
    parse_kalshi_ticker,
)
from kalshi_client import KalshiClient

REPO = Path(__file__).resolve().parents[1]

# MLB 2026-09-05 live shape: date + HHMM + team codes.
ATL_PHI_EVENT = "KXMLBGAME-26SEP051805ATLPHI"
ATL_PHI_SPREAD_PHI2 = "KXMLBSPREAD-26SEP051805ATLPHI-PHI2"
ATL_PHI_TOTAL_9 = "KXMLBTOTAL-26SEP051805ATLPHI-9"
ATL_PHI_ML_PHI = "KXMLBGAME-26SEP051805ATLPHI-PHI"
BAL_EVENT = "KXMLBGAME-26SEP051805BOSBAL"
BAL_ML = "KXMLBGAME-26SEP051805BOSBAL-BAL"

# CFB 2026-09-05 live shape (date-only suffix, no kickoff time).
DUQ_EVENT = "KXNCAAFGAME-26SEP05DUQAFA"
DUQ_TOTAL_39 = "KXNCAAFTOTAL-26SEP05DUQAFA-39"
DUQ_SPREAD_AFA10 = "KXNCAAFSPREAD-26SEP05DUQAFA-AFA10"
DUQ_ML_AFA = "KXNCAAFGAME-26SEP05DUQAFA-AFA"
BALL_EVENT = "KXNCAAFGAME-26SEP05BALLOSU"
BALL_TOTAL_60 = "KXNCAAFTOTAL-26SEP05BALLOSU-60"

# 2026-09-05 17:19 CT live skips (auto_bets.csv).
DET_CLE_EVENT = "KXMLBGAME-26SEP051810DETCLE"
DET_CLE_SPREAD_25 = "KXMLBSPREAD-26SEP051810DETCLE-DET3"
DET_CLE_SPREAD_15 = "KXMLBSPREAD-26SEP051810DETCLE-DET2"
DET_CLE_TEAMS = "Detroit Tigers @ Cleveland Guardians"
PHI_ML_EVENT = "KXMLBGAME-26SEP051805ATLPHI"
PHI_ML_MARKET = "KXMLBGAME-26SEP051805ATLPHI-PHI"
PHI_ML_TEAMS = "Atlanta Braves @ Philadelphia Phillies"
HCU_RICE_SPREAD = "KXNCAAFSPREAD-26SEP05HCURICE-RICE24"
HCU_RICE_EVENT = "KXNCAAFGAME-26SEP05HCURICE"
HCU_RICE_TEAMS = "Houston Christian @ Rice"


def _client() -> KalshiClient:
    return KalshiClient()


def test_find_submarket_contract_is_documented():
    doc = KalshiClient.find_submarket.__doc__ or ""
    for needle in (
        "ceil(|line|)",
        "Over=YES",
        "Under=NO",
        "Favorite",
        "dog",
        "GAME-",
        "floor_strike",
        "fail-closed",
    ):
        assert needle in doc, needle


def test_mlb_timed_suffix_extracts_atl_phi_not_1805():
    client = _client()
    c1, c2 = client._extract_team_codes_from_event_ticker(
        ATL_PHI_EVENT, "Atlanta Braves @ Philadelphia Phillies", "Phillies"
    )
    assert {c1, c2} == {"ATL", "PHI"}
    assert "1805" not in (c1 or "") and "1805" not in (c2 or "")


def test_mlb_spread_favorite_is_spread_series_ceil_not_game_event():
    client = _client()
    ident = client.resolve_executable_market_identity(
        ATL_PHI_EVENT,
        "Point Spread",
        -1.5,
        "Philadelphia Phillies",
        "Atlanta Braves @ Philadelphia Phillies",
    )
    assert ident is not None
    assert ident["ticker"] == ATL_PHI_SPREAD_PHI2
    assert ident["side"] == "yes"
    parsed = parse_kalshi_ticker(ident["ticker"])
    assert parsed is not None
    assert parsed.family == "spread"
    assert parsed.is_market is True
    assert parsed.line_int == 2
    assert is_executable_market_ticker(ident["ticker"], "Point Spread", -1.5)
    assert not is_executable_market_ticker(ATL_PHI_EVENT, "Point Spread", -1.5)


def test_mlb_spread_dog_is_no_on_fav_ticker():
    client = _client()
    ident = client.resolve_executable_market_identity(
        ATL_PHI_EVENT,
        "Point Spread",
        1.5,
        "Atlanta Braves",
        "Atlanta Braves @ Philadelphia Phillies",
    )
    assert ident is not None
    assert ident["ticker"] == ATL_PHI_SPREAD_PHI2
    assert ident["side"] == "no"


def test_mlb_total_and_ml_fixtures():
    client = _client()
    over = client.resolve_executable_market_identity(
        ATL_PHI_EVENT,
        "Total Runs",
        8.5,
        "Over",
        "Atlanta Braves @ Philadelphia Phillies",
    )
    under = client.resolve_executable_market_identity(
        ATL_PHI_EVENT,
        "Total Runs",
        8.5,
        "Under",
        "Atlanta Braves @ Philadelphia Phillies",
    )
    ml = client.resolve_executable_market_identity(
        BAL_EVENT,
        "Moneyline",
        None,
        "Baltimore Orioles",
        "Boston Red Sox @ Baltimore Orioles",
    )
    assert over is not None and over["ticker"] == ATL_PHI_TOTAL_9 and over["side"] == "yes"
    assert under is not None and under["ticker"] == ATL_PHI_TOTAL_9 and under["side"] == "no"
    assert ml is not None and ml["ticker"] == BAL_ML and ml["side"] == "yes"
    assert parse_kalshi_ticker(over["ticker"]).family == "total"
    assert parse_kalshi_ticker(ml["ticker"]).family == "moneyline"
    assert parse_kalshi_ticker(ml["ticker"]).is_market is True


def test_cfb_ml_spread_total_fixtures():
    client = _client()
    total = client.resolve_executable_market_identity(
        DUQ_EVENT, "Total Points", 38.5, "Under", "Duquesne @ Air Force"
    )
    spread = client.resolve_executable_market_identity(
        DUQ_EVENT, "Point Spread", -9.5, "Air Force", "Duquesne @ Air Force"
    )
    ml = client.resolve_executable_market_identity(
        DUQ_EVENT, "Moneyline", None, "Air Force", "Duquesne @ Air Force"
    )
    ball = client.resolve_executable_market_identity(
        BALL_EVENT, "Total Points", 59.5, "Over", "Ball State @ Ohio State"
    )
    assert total == {"ticker": DUQ_TOTAL_39, "side": "no", "event_ticker": DUQ_EVENT}
    assert spread == {
        "ticker": DUQ_SPREAD_AFA10,
        "side": "yes",
        "event_ticker": DUQ_EVENT,
    }
    assert ml == {"ticker": DUQ_ML_AFA, "side": "yes", "event_ticker": DUQ_EVENT}
    assert ball == {"ticker": BALL_TOTAL_60, "side": "yes", "event_ticker": BALL_EVENT}


def test_spread_allow_true_never_ships_side_none():
    """Invariant: executable allow requires yes/no. GAME event is not enough."""
    client = _client()
    ident = client.resolve_executable_market_identity(
        ATL_PHI_EVENT,
        "Point Spread",
        -1.5,
        "Philadelphia Phillies",
        "Atlanta Braves @ Philadelphia Phillies",
    )
    assert ident is not None
    allow = bool(
        ident["side"] in ("yes", "no")
        and is_executable_market_ticker(ident["ticker"], "Point Spread", -1.5)
    )
    assert allow is True
    assert ident["side"] is not None

    event_only_allow = bool(
        is_executable_market_ticker(ATL_PHI_EVENT, "Point Spread", -1.5)
        and expected_side_for_alert(
            market_type="Point Spread",
            pick="Philadelphia Phillies",
            line=-1.5,
            ticker=ATL_PHI_EVENT,
            teams="Atlanta Braves @ Philadelphia Phillies",
        )
        in ("yes", "no")
        and is_executable_market_ticker(ATL_PHI_EVENT, "Point Spread", -1.5)
    )
    assert event_only_allow is False


def test_expected_side_locked_conventions():
    assert expected_side_for_alert(
        market_type="Total Points", pick="Under", line=38.5, ticker=DUQ_TOTAL_39
    ) == "no"
    assert expected_side_for_alert(
        market_type="Total Points", pick="Over", line=38.5, ticker=DUQ_TOTAL_39
    ) == "yes"
    assert expected_side_for_alert(
        market_type="Point Spread",
        pick="Philadelphia Phillies",
        line=-1.5,
        ticker=ATL_PHI_SPREAD_PHI2,
        teams="Atlanta Braves @ Philadelphia Phillies",
    ) == "yes"
    assert expected_side_for_alert(
        market_type="Point Spread",
        pick="Atlanta Braves",
        line=1.5,
        ticker=ATL_PHI_SPREAD_PHI2,
        teams="Atlanta Braves @ Philadelphia Phillies",
    ) == "no"
    # Event-only still resolves (fav=YES, dog=NO) so cards are not side=None.
    assert expected_side_for_alert(
        market_type="Point Spread",
        pick="Philadelphia Phillies",
        line=-1.5,
        ticker=ATL_PHI_EVENT,
        teams="Atlanta Braves @ Philadelphia Phillies",
    ) == "yes"
    assert expected_side_for_alert(
        market_type="Moneyline",
        pick="Baltimore Orioles",
        line=None,
        ticker=BAL_ML,
        teams="Boston Red Sox @ Baltimore Orioles",
    ) == "yes"


def test_det_live_spread_game_event_resolves_or_fail_closed():
    """DET −2.5 / −1.5 on KXMLBGAME-…DETCLE → SPREAD-DET{ceil} YES, never a GAME event."""
    client = _client()
    for line, want in ((-2.5, DET_CLE_SPREAD_25), (-1.5, DET_CLE_SPREAD_15)):
        ident = client.resolve_executable_market_identity(
            DET_CLE_EVENT, "Point Spread", line, "Detroit Tigers", DET_CLE_TEAMS
        )
        assert ident is not None, f"DET {line} must resolve or fail-closed (not a GAME event)"
        assert ident["ticker"] == want
        assert ident["side"] == "yes"
        assert ident["event_ticker"] == DET_CLE_EVENT
        assert is_bare_game_event_ticker(DET_CLE_EVENT) is True
        assert is_bare_game_event_ticker(ident["ticker"]) is False
        assert is_executable_market_ticker(ident["ticker"], "Point Spread", line)
        assert not is_executable_market_ticker(DET_CLE_EVENT, "Point Spread", line)


def test_phi_ml_game_event_resolves_market_and_side():
    """Phillies ML on KXMLBGAME-…ATLPHI → GAME-PHI YES."""
    client = _client()
    ident = client.resolve_executable_market_identity(
        PHI_ML_EVENT, "Moneyline", None, "Phillies", PHI_ML_TEAMS
    )
    assert ident is not None
    assert ident["ticker"] == PHI_ML_MARKET
    assert ident["side"] == "yes"
    assert ident["event_ticker"] == PHI_ML_EVENT
    assert is_executable_market_ticker(ident["ticker"], "Moneyline")
    assert not is_executable_market_ticker(PHI_ML_EVENT, "Moneyline")
    assert expected_side_for_alert(
        market_type="Moneyline",
        pick="Philadelphia Phillies",
        line=None,
        ticker=PHI_ML_MARKET,
        teams=PHI_ML_TEAMS,
    ) == "yes"


def test_hcu_plus_hdp_on_fav_spread_ticker_is_no():
    """HCU +23.5 on …-RICE24 is dog → NO on the favorite ticker."""
    client = _client()
    ident = client.resolve_executable_market_identity(
        HCU_RICE_SPREAD, "Point Spread", 23.5, "Houston Christian", HCU_RICE_TEAMS
    )
    assert ident is not None
    assert ident["ticker"] == HCU_RICE_SPREAD
    assert ident["side"] == "no"
    # Qualifier-only (line missing) must still map dog → NO.
    ident_q = client.resolve_executable_market_identity(
        HCU_RICE_SPREAD,
        "Point Spread",
        None,
        "Houston Christian",
        HCU_RICE_TEAMS,
        "+23.5",
    )
    assert ident_q == ident
    assert expected_side_for_alert(
        market_type="Point Spread",
        pick="Houston Christian",
        line=None,
        ticker=HCU_RICE_SPREAD,
        teams=HCU_RICE_TEAMS,
        qualifier="+23.5",
    ) == "no"
    from_event = client.resolve_executable_market_identity(
        HCU_RICE_EVENT, "Point Spread", 23.5, "Houston Christian", HCU_RICE_TEAMS
    )
    assert from_event is not None
    assert from_event["ticker"] == HCU_RICE_SPREAD
    assert from_event["side"] == "no"


def test_paper_ticker_reason_not_applied_to_kx_event_ids():
    from odds_ev_monitor import _autobet_card_reasons

    assert _autobet_card_reasons([], ticker=ATL_PHI_EVENT) == []
    assert _autobet_card_reasons([], ticker="KALSHI|ATL|PHI|-1.5") == ["paper_ticker"]
    assert is_kalshi_ticker(ATL_PHI_EVENT) is True


def test_monitor_phillies_spread_card_has_market_ticker_and_side():
    """Odds-API event KX must not ship as allow=true / side=None / GAME event."""
    import time as _time

    from ev_calculator import american_to_decimal
    from odds_ev_monitor import OddsEVMonitor

    now = _time.time()
    phi_dec = american_to_decimal(130)
    atl_dec = american_to_decimal(-160)
    doc = {
        "id": 2609051805,
        "home": "Philadelphia Phillies",
        "away": "Atlanta Braves",
        "sport": {"name": "Baseball", "slug": "baseball"},
        "league": {"name": "MLB", "slug": "usa-mlb"},
        "live": True,
        "urls": {"Kalshi": f"https://kalshi.com/events/{ATL_PHI_EVENT}"},
        "bookmakerIds": {"Kalshi": ATL_PHI_EVENT},
        "book_updated_at": {"Kalshi": now - 4.0, "FanDuel": now - 2.0},
        "bookmakers": {
            "Kalshi": [
                {
                    "name": "Spread",
                    "odds": [{"home": phi_dec, "away": atl_dec, "hdp": -1.5}],
                }
            ],
            "FanDuel": [
                {"name": "Spread", "odds": [{"home": 1.91, "away": 1.91, "hdp": -1.5}]}
            ],
            "DraftKings": [
                {"name": "Spread", "odds": [{"home": 1.90, "away": 1.92, "hdp": -1.5}]}
            ],
            "NoVig": [
                {"name": "Spread", "odds": [{"home": 1.90, "away": 1.91, "hdp": -1.5}]}
            ],
        },
    }
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
    rows = mon.live_scan_value_bets_from_docs({2609051805: doc})
    spr = [
        r
        for r in rows
        if str(r.get("_scan_mname") or "").lower() == "spread"
        and str(r.get("_take_only") or "").lower() == "kalshi"
        and r.get("betSide") == "home"
    ]
    assert spr
    built = mon._value_bet_to_normalized_bet(spr[0], doc, take_book="Kalshi")
    assert built is not None
    assert built["ticker"] == ATL_PHI_SPREAD_PHI2
    assert built.get("side") in ("yes", "no")
    if built.get("autobet_allow") is True:
        assert built.get("side") in ("yes", "no")
        assert "GAME" not in str(built["ticker"]).split("-")[0] or "SPREAD" in built["ticker"]
        parsed = parse_kalshi_ticker(built["ticker"])
        assert parsed is not None and parsed.family == "spread"


def test_strict_pass_does_not_block_order_when_allow_true():
    dash = (REPO / "dashboard.py").read_text(encoding="utf-8")
    assert "Alert failed strict_pass gate" not in dash
    assert "strict_pass is display-only" in dash
    assert "autobet_allow=False" in dash
    js = (REPO / "static" / "script.js").read_text(encoding="utf-8")
    assert "const autobetAllow = alert.autobet_allow === true;" in js
    assert "alert.autobet_allow === true && strictOk" not in js


def test_check_and_auto_bet_allow_true_ignores_strict_pass(monkeypatch):
    """Volume path: shape already passed. strict_pass=False must not be the skip."""
    import asyncio

    import dashboard as dash

    monkeypatch.setattr(dash, "write_auto_bet_to_sheets", lambda *_a, **_k: None)
    monkeypatch.setattr(dash, "write_auto_bet_to_csv", lambda *_a, **_k: None)
    monkeypatch.setattr(dash, "auto_bet_enabled", False)

    async def run():
        await dash.check_and_auto_bet(
            "allow-true-strict-false",
            {
                "strict_pass": False,
                "autobet_allow": True,
                "ticker": ATL_PHI_SPREAD_PHI2,
                "side": "yes",
                "ev_percent": 6.0,
                "market_type": "Point Spread",
                "pick": "Philadelphia Phillies",
                "teams": "Atlanta Braves @ Philadelphia Phillies",
                "line": -1.5,
            },
            None,
        )

    asyncio.run(run())
    last = dash.failed_auto_bets[-1]
    assert last["alert_id"] == "allow-true-strict-false"
    err = str(last.get("error") or "")
    assert "strict_pass" not in err
    assert "Auto-bet disabled" in err or "autobet_allow" not in err


def test_auto_path_is_real_ev_not_sub2_crumbs(monkeypatch):
    """Sheet: 1382 fills, all EV≥5% (median 10.6%). Desk floor stays 2–20, not crumbs."""
    import asyncio

    import dashboard as dash
    from ev_calculator import AUTOBET_MAX_EV_PCT, autobet_product_shape

    assert dash.auto_bet_ev_min == 2.0
    assert AUTOBET_MAX_EV_PCT == 20.0
    books = [
        {"name": "DraftKings", "american": 134, "decimal_pick": 2.34, "decimal_opp": 1.65},
        {"name": "FanDuel", "american": 116, "decimal_pick": 2.16, "decimal_opp": 1.74},
        {"name": "Caesars", "american": 110, "decimal_pick": 2.10, "decimal_opp": 1.77},
    ]
    mid = autobet_product_shape(books, 163, take_book="Kalshi", ev_percent=10.6, plus_alert=True)
    crumb = autobet_product_shape(books, 163, take_book="Kalshi", ev_percent=1.0, plus_alert=True)
    assert mid["allow"] is True
    assert crumb["allow"] is True  # shape is plus + ≤20; order path still floors at 2%
    assert autobet_product_shape(
        books, 163, take_book="Kalshi", ev_percent=20.01, plus_alert=True
    )["allow"] is False

    monkeypatch.setattr(dash, "write_auto_bet_to_sheets", lambda *_a, **_k: None)
    monkeypatch.setattr(dash, "write_auto_bet_to_csv", lambda *_a, **_k: None)
    monkeypatch.setattr(dash, "auto_bet_enabled", True)
    monkeypatch.setattr(dash, "auto_bet_ev_min", 2.0)

    async def run():
        await dash.check_and_auto_bet(
            "crumb-1pct-not-a-fill",
            {
                "autobet_allow": True,
                "strict_pass": True,
                "ticker": ATL_PHI_SPREAD_PHI2,
                "side": "yes",
                "ev_percent": 1.0,
                "market_type": "Point Spread",
                "pick": "Philadelphia Phillies",
                "teams": "Atlanta Braves @ Philadelphia Phillies",
                "line": -1.5,
            },
            None,
        )

    asyncio.run(run())
    last = dash.failed_auto_bets[-1]
    assert last["alert_id"] == "crumb-1pct-not-a-fill"
    assert "below filter minimum" in str(last.get("error") or "")


def test_handle_new_alert_resolves_det_phi_hcu_when_find_submarket_misses(monkeypatch):
    """GET miss must not skip a resolved live MLB/CFB identity (2026-09-05 CSV)."""
    import asyncio

    import dashboard as dash
    from ev_alert import EvAlert

    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    dash.active_alerts.clear()
    monkeypatch.setattr(dash, "dashboard_min_ev", 0.0)
    monkeypatch.setattr(dash, "selected_dashboard_filters", set())
    monkeypatch.setattr(dash, "auto_bet_enabled", False)

    real = dash.KalshiClient()

    async def _no_get(*_a, **_k):
        return None

    async def _no_ob(*_a, **_k):
        return None

    monkeypatch.setattr(dash, "kalshi_client", real)
    monkeypatch.setattr(real, "find_submarket", _no_get)
    monkeypatch.setattr(real, "fetch_orderbook", _no_ob)
    monkeypatch.setattr(dash.socketio, "emit", lambda *_a, **_k: None)

    cases = [
        {
            "market_type": "Point Spread",
            "teams": DET_CLE_TEAMS,
            "pick": "Detroit Tigers",
            "qualifier": "-2.5",
            "line": -2.5,
            "ticker": DET_CLE_EVENT,
            "want_ticker": DET_CLE_SPREAD_25,
            "want_side": "yes",
            "ev": 15.59,
        },
        {
            "market_type": "Point Spread",
            "teams": DET_CLE_TEAMS,
            "pick": "Detroit Tigers",
            "qualifier": "-1.5",
            "line": -1.5,
            "ticker": DET_CLE_EVENT,
            "want_ticker": DET_CLE_SPREAD_15,
            "want_side": "yes",
            "ev": 14.72,
        },
        {
            "market_type": "Moneyline",
            "teams": PHI_ML_TEAMS,
            "pick": "Phillies",
            "qualifier": "",
            "line": None,
            "ticker": PHI_ML_EVENT,
            "want_ticker": PHI_ML_MARKET,
            "want_side": "yes",
            "ev": 5.28,
        },
        {
            "market_type": "Point Spread",
            "teams": HCU_RICE_TEAMS,
            "pick": "Houston Christian",
            "qualifier": "+23.5",
            "line": 23.5,
            "ticker": HCU_RICE_SPREAD,
            "want_ticker": HCU_RICE_SPREAD,
            "want_side": "no",
            "ev": 3.97,
        },
    ]

    async def _run():
        for row in cases:
            alert = EvAlert(
                {
                    "market_type": row["market_type"],
                    "teams": row["teams"],
                    "pick": row["pick"],
                    "qualifier": row["qualifier"],
                    "ev_percent": row["ev"],
                    "odds": "+120",
                    "ticker": row["ticker"],
                    "market_url": f"https://kalshi.com/events/{row['ticker']}",
                    "take_book": "Kalshi",
                    "ev_source": "odds_api_value_bets",
                    "autobet_allow": True,
                }
            )
            alert.price_cents = 45
            alert.line = row["line"]
            alert.autobet_allow = True
            await dash.handle_new_alert(alert)

    asyncio.run(_run())
    stored = list(dash.active_alerts.values())
    by_qual = {(str(r.get("pick")), str(r.get("qualifier") or "")): r for r in stored}
    det25 = by_qual.get(("Detroit Tigers", "-2.5"))
    det15 = by_qual.get(("Detroit Tigers", "-1.5"))
    phi = by_qual.get(("Phillies", ""))
    hcu = by_qual.get(("Houston Christian", "+23.5"))
    assert det25 is not None and det25.get("ticker") == DET_CLE_SPREAD_25
    assert det25.get("side") == "yes" and det25.get("match_failed") is not True
    assert det15 is not None and det15.get("ticker") == DET_CLE_SPREAD_15
    assert det15.get("side") == "yes" and det15.get("match_failed") is not True
    assert phi is not None and phi.get("ticker") == PHI_ML_MARKET
    assert phi.get("side") == "yes"
    assert hcu is not None and hcu.get("ticker") == HCU_RICE_SPREAD
    assert hcu.get("side") == "no"


def test_check_and_auto_bet_game_event_empty_side_resolves_before_place(monkeypatch):
    """Allow-path with GAME ticker + side='' must resolve, never place empty side."""
    import asyncio

    import dashboard as dash

    monkeypatch.setattr(dash, "write_auto_bet_to_sheets", lambda *_a, **_k: None)
    monkeypatch.setattr(dash, "write_auto_bet_to_csv", lambda *_a, **_k: None)
    monkeypatch.setattr(dash, "auto_bet_enabled", False)
    monkeypatch.setattr(dash, "auto_bet_ev_min", 2.0)
    placed = []

    async def _forbid_place(*_a, **_k):
        placed.append(True)
        raise AssertionError("must not place with unresolved GAME event / empty side")

    client = dash.kalshi_client or dash.KalshiClient()
    monkeypatch.setattr(dash, "kalshi_client", client)
    monkeypatch.setattr(client, "place_order", _forbid_place)

    async def run():
        await dash.check_and_auto_bet(
            "det-game-empty-side",
            {
                "autobet_allow": True,
                "strict_pass": False,
                "ticker": DET_CLE_EVENT,
                "side": "",
                "ev_percent": 15.59,
                "market_type": "Point Spread",
                "pick": "Detroit Tigers",
                "teams": DET_CLE_TEAMS,
                "line": -2.5,
                "qualifier": "-2.5",
            },
            None,
        )
        await dash.check_and_auto_bet(
            "det-game-empty-side-15",
            {
                "autobet_allow": True,
                "ticker": DET_CLE_EVENT,
                "side": "",
                "ev_percent": 14.72,
                "market_type": "Point Spread",
                "pick": "Detroit Tigers",
                "teams": DET_CLE_TEAMS,
                "line": -1.5,
                "qualifier": "-1.5",
            },
            None,
        )
        await dash.check_and_auto_bet(
            "phi-game-empty-side",
            {
                "autobet_allow": True,
                "ticker": PHI_ML_EVENT,
                "side": "",
                "ev_percent": 5.28,
                "market_type": "Moneyline",
                "pick": "Phillies",
                "teams": PHI_ML_TEAMS,
            },
            None,
        )
        await dash.check_and_auto_bet(
            "hcu-rice24-empty-side",
            {
                "autobet_allow": True,
                "ticker": HCU_RICE_SPREAD,
                "side": "",
                "ev_percent": 3.97,
                "market_type": "Point Spread",
                "pick": "Houston Christian",
                "teams": HCU_RICE_TEAMS,
                "qualifier": "+23.5",
            },
            None,
        )

    asyncio.run(run())
    assert placed == []
    by_id = {r["alert_id"]: r for r in dash.failed_auto_bets}
    det = by_id["det-game-empty-side"]
    det15 = by_id["det-game-empty-side-15"]
    phi = by_id["phi-game-empty-side"]
    hcu = by_id["hcu-rice24-empty-side"]
    assert det.get("ticker") == DET_CLE_SPREAD_25
    assert str(det.get("side") or "").lower() == "yes"
    assert det15.get("ticker") == DET_CLE_SPREAD_15
    assert str(det15.get("side") or "").lower() == "yes"
    assert "matching failed" not in str(det.get("error") or "").lower()
    assert "side determination failed" not in str(det.get("error") or "").lower()
    assert phi.get("ticker") == PHI_ML_MARKET
    assert str(phi.get("side") or "").lower() == "yes"
    assert hcu.get("ticker") == HCU_RICE_SPREAD
    assert str(hcu.get("side") or "").lower() == "no"
    for row in (det, det15, phi, hcu):
        err = str(row.get("error") or "")
        assert "Auto-bet disabled" in err or "unresolved" not in err.lower()


def test_check_and_auto_bet_skips_allow_not_strict_when_allow_false(monkeypatch):
    import asyncio

    import dashboard as dash

    monkeypatch.setattr(dash, "write_auto_bet_to_sheets", lambda *_a, **_k: None)
    monkeypatch.setattr(dash, "write_auto_bet_to_csv", lambda *_a, **_k: None)
    before = len(dash.failed_auto_bets)

    async def run():
        await dash.check_and_auto_bet(
            "strict-pass-not-order-gate",
            {
                "strict_pass": False,
                "autobet_allow": False,
                "ticker": ATL_PHI_SPREAD_PHI2,
                "side": "yes",
                "ev_percent": 6.0,
            },
            None,
        )

    asyncio.run(run())
    last = dash.failed_auto_bets[-1]
    assert last["alert_id"] == "strict-pass-not-order-gate"
    assert "autobet_allow" in str(last.get("error") or "")
    assert "strict_pass" not in str(last.get("error") or "")
    assert len(dash.failed_auto_bets) >= before + 1
