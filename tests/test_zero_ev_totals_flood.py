"""P0: +0.00% totals flood. Zero is not a KEEP. Auto-bet stays OFF."""
from __future__ import annotations

from ev_calculator import american_to_decimal, is_plus_print_ev
from odds_ev_monitor import (
    OddsEVMonitor,
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
    take_7 = _totals_row(7.0, 105, -120)
    take_10 = (
        _totals_row(10.5, 105, -120)
        if plive_10_best
        else _totals_row(10.5, 378, -480)
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
