"""Executable Kalshi cards: real market ticker + yes/no. No fuzzy lines.

Live desk (2026-09-05): /api/alerts shipped autobet_allow=true with
side=None and GAME event tickers (KXMLBGAME-… / KXNCAAFGAME-…) on
spread/total takes. find_submarket then Invalid submarket key — no order.

Contract lives on KalshiClient.find_submarket. Auto-bet stays OFF here.
"""
from __future__ import annotations

from pathlib import Path

from execution_guard import (
    expected_side_for_alert,
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
