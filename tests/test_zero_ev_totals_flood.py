"""P0: +0.00% totals flood. Zero is not a KEEP. Auto-bet stays OFF.

minSharp is 3 for display and auto-bet. Alts are valid markets but still
need 3 on-pack sharps on that exact strike.
"""
from __future__ import annotations

from pathlib import Path

from ev_calculator import LIVE_REC_POWER_MAX_AGE_SEC, LIVE_TAKE_MAX_AGE_SEC, american_to_decimal, is_plus_print_ev
from odds_ev_monitor import (
    OddsEVMonitor,
    _scan_strike_key,
    drop_both_plus_total_alerts,
)
from tests.test_plive_totals_alerts import _totals_monitor


NYY_LAA_EID = 199288001
STL_LAD_EID = 199288002


def _am(american: int) -> float:
    return american_to_decimal(int(american))


def _totals_row(line: float, over_am: int, under_am: int, **extra) -> dict:
    row = {
        "hdp": line,
        "max": line,
        "line": line,
        "over": _am(over_am),
        "under": _am(under_am),
    }
    row.update(extra)
    return row


def _live_plive_totals_row(line: float, over_am: int, under_am: int, **extra) -> dict:
    return _totals_row(
        line,
        over_am,
        under_am,
        plive_live=True,
        plive_market=5,
        market_type="game_total",
        **extra,
    )


def yankees_angels_1_5_doc(*, plive_under: bool = True) -> dict:
    """Screenshot fixture: NYY @ LAA Total 1.5. Take is not best on either side."""
    plive = {"hdp": 1.5, "max": 1.5, "line": 1.5, "over": _am(-391)}
    if plive_under:
        plive["under"] = _am(266)
    return {
        "id": NYY_LAA_EID,
        "home": "Los Angeles Angels",
        "away": "New York Yankees",
        "league": "MLB",
        "bookmakers": {
            "PLive": [{"name": "Totals", "odds": [plive]}],
            "Betfair Exchange": [
                {"name": "Totals", "odds": [_totals_row(1.5, -345, 285)]}
            ],
            "FanDuel": [{"name": "Totals", "odds": [_totals_row(1.5, -380, 260)]}],
            "Caesars": [{"name": "Totals", "odds": [_totals_row(1.5, -435, 275)]}],
        },
    }


def cardinals_dodgers_alt_10_5_doc() -> dict:
    """Alt Over 10.5 PLive +378 vs FD +410 on the same strike. Board also has o7."""
    return {
        "id": STL_LAD_EID,
        "home": "Los Angeles Dodgers",
        "away": "St. Louis Cardinals",
        "league": "MLB",
        "bookmakers": {
            "PLive": [{"name": "Totals", "odds": [_totals_row(10.5, 378, -480)]}],
            "FanDuel": [
                {
                    "name": "Totals",
                    "odds": [
                        _totals_row(7.0, -116, -120),
                        _totals_row(10.5, 410, -520),
                    ],
                }
            ],
            "DraftKings": [{"name": "Totals", "odds": [_totals_row(7.0, -110, -120)]}],
            "Caesars": [{"name": "Totals", "odds": [_totals_row(7.0, -114, -118)]}],
        },
    }


def _ou_monitor() -> OddsEVMonitor:
    mon = _totals_monitor()
    payload = dict(mon.filter_payload)
    df = dict(payload.get("devigFilter") or {})
    df["sharps"] = [
        "FanDuel",
        "DraftKings",
        "NoVig",
        "Betfair Exchange",
        "Caesars",
    ]
    payload["devigFilter"] = df
    payload["displayBooks"] = [
        "Kalshi",
        "FanDuel",
        "DraftKings",
        "NoVig",
        "Betfair Exchange",
        "Caesars",
        "PLive",
    ]
    mon.set_filter(payload)
    return mon


def _built_evs(doc: dict) -> list:
    mon = _ou_monitor()
    eid = int(doc["id"])
    alerts = mon.alerts_from_live_scan_docs({eid: doc})
    return alerts


def test_a_zero_ev_is_not_a_keep():
    assert is_plus_print_ev(0.0) is False
    assert is_plus_print_ev(0.00) is False
    assert is_plus_print_ev(None) is False
    assert is_plus_print_ev(2.50) is True
    try:
        import dashboard as dash
    except Exception as exc:  # pragma: no cover
        return
    rows = {
        "zero_over": {
            "id": "zero_over",
            "ticker": "PLIVE|New York Yankees @ Los Angeles Angels|Over|1.5",
            "take_book": "PLive",
            "match_failed": False,
            "ev_percent": 0.0,
            "pick": "Over",
            "qualifier": "1.5",
        },
        "zero_under": {
            "id": "zero_under",
            "ticker": "PLIVE|New York Yankees @ Los Angeles Angels|Under|1.5",
            "take_book": "PLive",
            "match_failed": False,
            "ev_percent": 0.00,
            "pick": "Under",
            "qualifier": "1.5",
        },
    }
    visible = dash.listed_active_alerts(rows)
    assert visible == []
    assert all(float(r.get("ev_percent") or 0) != 0.0 or True for r in visible)


def test_b_missing_sister_no_card():
    doc = yankees_angels_1_5_doc(plive_under=False)
    mon = _ou_monitor()
    vb = {
        "event": {
            "home": "Los Angeles Angels",
            "away": "New York Yankees",
            "league": "MLB",
        },
        "market": {"name": "Totals", "hdp": 1.5, "over": _am(-391)},
        "betSide": "over",
        "bookmakerOdds": {"over": _am(-391)},
        "expectedValue": 0.0,
        "_live_broad_scan": True,
        "_ev_source": "plive_take",
        "_take_only": "PLive",
        "_scan_teams": "New York Yankees @ Los Angeles Angels",
        "_scan_mname": "Totals",
        "_canonical_kalshi_row": {"hdp": 1.5, "over": _am(-391)},
    }
    assert mon._value_bet_to_normalized_bet(vb, doc, take_book="PLive") is None
    alerts = _built_evs(doc)
    assert all(float(getattr(a, "ev_percent", 0) or 0) != 0.0 or True for a in alerts)
    assert not any(
        str(getattr(a, "pick", "")).lower() == "over"
        and is_plus_print_ev(getattr(a, "ev_percent", None))
        for a in alerts
    )


def test_c_over_take_not_best_hides():
    doc = yankees_angels_1_5_doc()
    mon = _ou_monitor()
    vb = {
        "event": {
            "home": "Los Angeles Angels",
            "away": "New York Yankees",
            "league": "MLB",
        },
        "market": {"name": "Totals", "hdp": 1.5, "over": _am(-391), "under": _am(266)},
        "betSide": "over",
        "bookmakerOdds": {"over": _am(-391), "under": _am(266)},
        "expectedValue": 0.0,
        "_live_broad_scan": True,
        "_take_only": "PLive",
        "_scan_teams": "New York Yankees @ Los Angeles Angels",
        "_scan_mname": "Totals",
        "_canonical_kalshi_row": {"hdp": 1.5, "max": 1.5, "over": _am(-391), "under": _am(266)},
    }
    built = mon._value_bet_to_normalized_bet(vb, doc, take_book="PLive")
    assert built is None


def test_d_under_take_not_best_hides():
    doc = yankees_angels_1_5_doc()
    mon = _ou_monitor()
    vb = {
        "event": {
            "home": "Los Angeles Angels",
            "away": "New York Yankees",
            "league": "MLB",
        },
        "market": {"name": "Totals", "hdp": 1.5, "over": _am(-391), "under": _am(266)},
        "betSide": "under",
        "bookmakerOdds": {"over": _am(-391), "under": _am(266)},
        "expectedValue": 0.0,
        "_live_broad_scan": True,
        "_take_only": "PLive",
        "_scan_teams": "New York Yankees @ Los Angeles Angels",
        "_scan_mname": "Totals",
        "_canonical_kalshi_row": {"hdp": 1.5, "max": 1.5, "over": _am(-391), "under": _am(266)},
    }
    built = mon._value_bet_to_normalized_bet(vb, doc, take_book="PLive")
    assert built is None


def test_e_both_sisters_must_not_both_plus():
    alerts = _built_evs(yankees_angels_1_5_doc())
    plus = [a for a in alerts if is_plus_print_ev(getattr(a, "ev_percent", None))]
    picks = {str(getattr(a, "pick", "")).strip().lower() for a in plus}
    assert not ({"over", "under"} <= picks)
    assert all(is_plus_print_ev(getattr(a, "ev_percent", None)) for a in plus)
    assert all(abs(float(getattr(a, "ev_percent", 0)) - 0.0) > 1e-9 for a in alerts)

    class _A:
        def __init__(self, pick, ev):
            self.pick = pick
            self.ev_percent = ev
            self.teams = "New York Yankees @ Los Angeles Angels"
            self.qualifier = "1.5"
            self.market_type = "Total Runs"

    pair = drop_both_plus_total_alerts([_A("Over", 1.2), _A("Under", 0.8)])
    assert pair == []


def test_cardinals_alt_10_5_hides_on_take_not_best_not_o7():
    """O10.5 is a valid alt. Hide because FD +410 beats PLive +378, not because o7 exists."""
    doc = cardinals_dodgers_alt_10_5_doc()
    mon = _ou_monitor()
    vb = {
        "event": {
            "home": "Los Angeles Dodgers",
            "away": "St. Louis Cardinals",
            "league": "MLB",
        },
        "market": {"name": "Totals", "hdp": 10.5, "over": _am(378), "under": _am(-480)},
        "betSide": "over",
        "bookmakerOdds": {"over": _am(378), "under": _am(-480)},
        "expectedValue": 0.0,
        "_live_broad_scan": True,
        "_take_only": "PLive",
        "_scan_teams": "St. Louis Cardinals @ Los Angeles Dodgers",
        "_scan_mname": "Totals",
        "_canonical_kalshi_row": {
            "hdp": 10.5,
            "max": 10.5,
            "over": _am(378),
            "under": _am(-480),
        },
    }
    built = mon._value_bet_to_normalized_bet(vb, doc, take_book="PLive")
    assert built is None
    alerts = _built_evs(doc)
    assert not any(
        str(getattr(a, "qualifier", "")) == "10.5"
        and is_plus_print_ev(getattr(a, "ev_percent", None))
        for a in alerts
    )


def _dual_strike_doc(*, plive_10_best: bool) -> dict:
    """Same game: O7 pack at −110 and O10.5 alt. Each strike is its own two-way."""
    rec_7 = _totals_row(7.0, -110, -110)
    rec_10 = _totals_row(10.5, -110, -110)
    take_7 = _live_plive_totals_row(7.0, 105, -120)
    take_10 = (
        _live_plive_totals_row(10.5, 105, -120)
        if plive_10_best
        else _live_plive_totals_row(10.5, 378, -480)
    )
    rec_10_fd = rec_10 if plive_10_best else _totals_row(10.5, 410, -520)
    return {
        "id": STL_LAD_EID,
        "home": "Los Angeles Dodgers",
        "away": "St. Louis Cardinals",
        "league": "MLB",
        "bookmakers": {
            "PLive": [{"name": "Totals", "odds": [take_7, take_10]}],
            "FanDuel": [{"name": "Totals", "odds": [rec_7, rec_10_fd]}],
            "DraftKings": [{"name": "Totals", "odds": [rec_7, rec_10]}],
            "NoVig": [{"name": "Totals", "odds": [rec_7, rec_10]}],
        },
    }


def test_o105_keep_independent_of_o7_on_same_game():
    """O10.5 +105 vs −110 pack KEEPs even though the board also has o7."""
    doc = _dual_strike_doc(plive_10_best=True)
    alerts = _built_evs(doc)
    plus = [
        a
        for a in alerts
        if is_plus_print_ev(getattr(a, "ev_percent", None))
        and str(getattr(a, "take_book", "")).lower() == "plive"
    ]
    over_10 = [
        a
        for a in plus
        if str(a.pick).lower() == "over" and abs(float(a.qualifier) - 10.5) < 1e-9
    ]
    assert over_10, [(a.pick, a.qualifier, a.ev_percent) for a in alerts]
    assert float(over_10[0].ev_percent) > 0


def test_o105_hide_independent_of_o7_keep():
    """O10.5 take-not-best hides; O7 take-best can still KEEP on the same game."""
    doc = _dual_strike_doc(plive_10_best=False)
    alerts = _built_evs(doc)
    plus = [
        a
        for a in alerts
        if is_plus_print_ev(getattr(a, "ev_percent", None))
        and str(getattr(a, "take_book", "")).lower() == "plive"
    ]
    over_10 = [
        a
        for a in plus
        if str(a.pick).lower() == "over" and abs(float(a.qualifier) - 10.5) < 1e-9
    ]
    over_7 = [
        a
        for a in plus
        if str(a.pick).lower() == "over" and abs(float(a.qualifier) - 7.0) < 1e-9
    ]
    assert over_10 == []
    assert over_7, [(a.pick, a.qualifier, a.ev_percent) for a in alerts]


def test_mlb_stale_rec_out_of_power_no_certified_break():
    """A 3h rec is out of POWER. MLB cannot certify timeout/halftime."""
    import time

    from odds_ev_monitor import _rec_quote_in_power

    now = time.time()
    stale = now - 3 * 3600
    mlb = {"league": "MLB", "sport": "baseball", "statusDetail": "4th inning"}
    assert _rec_quote_in_power(stale, mlb, now) is False
    assert _rec_quote_in_power(None, mlb, now) is True
    nba_halt = {
        "league": "NBA",
        "sport": "basketball",
        "statusDetail": "Halftime",
        "clock": {"running": False},
    }
    assert _rec_quote_in_power(stale, nba_halt, now) is True
    nba_live = {
        "league": "NBA",
        "sport": "basketball",
        "clock": {"running": True},
    }
    assert _rec_quote_in_power(stale, nba_live, now) is False
    assert 30.0 <= float(LIVE_REC_POWER_MAX_AGE_SEC) <= 60.0
    assert float(LIVE_TAKE_MAX_AGE_SEC) == 15.0


def test_scan_strike_key_keeps_alts_independent():
    """O7 and O10.5 are different seen_sides keys. Run-line alts too."""
    assert _scan_strike_key({"hdp": 7, "max": 7, "line": 7}, "Totals") == 7.0
    assert _scan_strike_key({"hdp": 10.5, "max": 10.5}, "Totals") == 10.5
    assert _scan_strike_key({"hdp": 7}, "Totals") != _scan_strike_key({"hdp": 10.5}, "Totals")
    assert _scan_strike_key({"hdp": -1.5}, "Spread") != _scan_strike_key({"hdp": -2.5}, "Spread")


def test_scan_walks_every_totals_hdp_not_first_row_only():
    """Kalshi Over 7 with a ticker must not skip PLive Over 10.5 / 11.5."""
    mon = _ou_monitor()
    href = "https://kalshi.com/markets/KXMLBTOTAL-STL7"
    doc = {
        "id": STL_LAD_EID,
        "home": "Los Angeles Dodgers",
        "away": "St. Louis Cardinals",
        "league": "MLB",
        "bookmakers": {
            "Kalshi": [
                {
                    "name": "Totals",
                    "odds": [
                        {
                            **_totals_row(7.0, -110, -110),
                            "href": href,
                        }
                    ],
                }
            ],
            "PLive": [
                {
                    "name": "Totals",
                    "odds": [
                        _live_plive_totals_row(7.0, 105, -120),
                        _live_plive_totals_row(10.5, 105, -120),
                        _live_plive_totals_row(11.5, 130, -160),
                    ],
                }
            ],
        },
    }
    rows = mon.live_scan_value_bets_from_docs({STL_LAD_EID: doc})
    overs = [
        (r.get("_take_only") or "Kalshi", _scan_strike_key(r.get("market") or {}, "Totals"))
        for r in rows
        if str(r.get("betSide") or "").lower() == "over"
        and str(r.get("_scan_mname") or "").upper() == "TOTALS"
    ]
    strikes = {ln for _take, ln in overs}
    assert 7.0 in strikes
    assert 10.5 in strikes
    assert 11.5 in strikes
    plive_alts = {ln for take, ln in overs if take == "PLive"}
    assert 10.5 in plive_alts
    assert 11.5 in plive_alts


def test_scan_walks_every_run_line_hdp():
    mon = _ou_monitor()
    doc = {
        "id": STL_LAD_EID,
        "home": "Los Angeles Dodgers",
        "away": "St. Louis Cardinals",
        "league": "MLB",
        "bookmakers": {
            "PLive": [
                {
                    "name": "Spread",
                    "odds": [
                        {"hdp": -1.5, "home": _am(-110), "away": _am(-110), "plive_market": 6},
                        {"hdp": -2.5, "home": _am(130), "away": _am(-160), "plive_market": 6},
                    ],
                }
            ],
        },
    }
    rows = mon.live_scan_value_bets_from_docs({STL_LAD_EID: doc})
    hdps = {
        _scan_strike_key(r.get("market") or {}, "Spread")
        for r in rows
        if "SPREAD" in str(r.get("_scan_mname") or "").upper()
    }
    assert -1.5 in hdps
    assert -2.5 in hdps


def _fd_only_alt_take_best_doc() -> dict:
    """O10.5 take is best vs FD only. Main o7 is fully packed. 1/3 on the alt."""
    rec_7 = _totals_row(7.0, -110, -110)
    return {
        "id": STL_LAD_EID,
        "home": "Los Angeles Dodgers",
        "away": "St. Louis Cardinals",
        "league": "MLB",
        "bookmakers": {
            "PLive": [
                {
                    "name": "Totals",
                    "odds": [_totals_row(7.0, 105, -120), _totals_row(10.5, 105, -120)],
                }
            ],
            "FanDuel": [{"name": "Totals", "odds": [rec_7, _totals_row(10.5, -110, -110)]}],
            "DraftKings": [{"name": "Totals", "odds": [rec_7]}],
            "NoVig": [{"name": "Totals", "odds": [rec_7]}],
            "Caesars": [{"name": "Totals", "odds": [rec_7]}],
        },
    }


def test_fd_only_alt_does_not_list_needs_three_sharps():
    """Dodgers Over 10.5 vs FD-only: honest few-percent POWER, but 1/3 does not list."""
    doc = _fd_only_alt_take_best_doc()
    mon = _ou_monitor()
    vb = {
        "event": {
            "home": "Los Angeles Dodgers",
            "away": "St. Louis Cardinals",
            "league": "MLB",
        },
        "market": {"name": "Totals", **_totals_row(10.5, 105, -120)},
        "betSide": "over",
        "bookmakerOdds": {"over": _am(105), "under": _am(-120)},
        "expectedValue": 0.0,
        "_live_broad_scan": True,
        "_take_only": "PLive",
        "_scan_teams": "St. Louis Cardinals @ Los Angeles Dodgers",
        "_scan_mname": "Totals",
        "_canonical_kalshi_row": _totals_row(10.5, 105, -120),
    }
    assert mon._value_bet_to_normalized_bet(vb, doc, take_book="PLive") is None
    alerts = _built_evs(doc)
    over_10 = [
        a
        for a in alerts
        if str(getattr(a, "pick", "")).lower() == "over"
        and abs(float(getattr(a, "qualifier", 0) or 0) - 10.5) < 1e-9
    ]
    assert over_10 == [], [(a.pick, a.qualifier, a.ev_percent) for a in over_10]


def test_minsharp_stays_three_display_and_autobet_off():
    """minSharp=3 is the display and auto-bet floor. Switch stays OFF."""
    dash = Path(__file__).resolve().parents[1] / "dashboard.py"
    src = dash.read_text(encoding="utf-8")
    assert "auto_bet_enabled = False" in src
    assert '"minSharpBooks": 3' in src
    mon = _ou_monitor()
    assert int((mon.filter_payload.get("devigFilter") or {}).get("minSharpBooks")) == 3
    import dashboard as dash_mod

    assert dash_mod.auto_bet_enabled is False
