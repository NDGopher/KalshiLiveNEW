"""Auto-Bets sheet record: 23 locked columns + appended analysis fields.

Auto-bet stays OFF. minSharp stays 3. BetMGM is not dropped.
"""
from __future__ import annotations

from pathlib import Path

from auto_bet_sheet import (
    AUTO_BET_CSV_BASE_FIELDS,
    AUTO_BET_CSV_FIELDNAMES,
    AUTO_BET_SHEET_BASE_HEADERS,
    AUTO_BET_SHEET_HEADERS,
    auto_bet_sheet_row,
    build_auto_bet_sheet_record,
    ensure_sheet_extra_headers,
    format_devig_books_named,
    format_power_books,
    live_context_from_event,
    sheet_line_value,
)
from ev_alert import EvAlert


LOCKED_23 = [
    "Timestamp",
    "Order ID",
    "Ticker",
    "Side",
    "Teams",
    "Market Type",
    "Pick",
    "Qualifier",
    "EV %",
    "Expected Price (¢)",
    "Executed Price (¢)",
    "American Odds",
    "Contracts",
    "Cost ($)",
    "Payout ($)",
    "Win Amount ($)",
    "Sport",
    "Status",
    "Result",
    "PNL ($)",
    "Settled",
    "Filter Name",
    "Devig Books",
]


def _display(pick, books):
    return {pick: [{"book": name, "odds": odds} for name, odds in books]}


def test_locked_23_headers_in_order():
    assert AUTO_BET_SHEET_BASE_HEADERS == LOCKED_23
    assert AUTO_BET_SHEET_HEADERS[:23] == LOCKED_23
    assert AUTO_BET_SHEET_HEADERS[23:] == [
        "take_book",
        "power_books",
        "sharp_count",
        "line",
        "clock_running",
        "status_detail",
        "score",
        "live",
        "skip_reason",
    ]
    assert AUTO_BET_CSV_FIELDNAMES[:23] == AUTO_BET_CSV_BASE_FIELDS
    assert AUTO_BET_CSV_FIELDNAMES[23:] == [
        "take_book",
        "power_books",
        "sharp_count",
        "line",
        "clock_running",
        "status_detail",
        "score",
        "live",
        "skip_reason",
    ]


def test_ensure_headers_append_only():
    existing = list(LOCKED_23)
    out = ensure_sheet_extra_headers(existing)
    assert out[:23] == LOCKED_23
    assert out[-1] == "skip_reason"
    assert ensure_sheet_extra_headers(out) == out


def test_devig_books_named_list_not_blob():
    named = format_devig_books_named(
        ["Circa", "NoVig", "DraftKings", "FanDuel"],
        _display("Over", [("Circa", -110), ("NoVig", -108), ("DraftKings", -112), ("FanDuel", -115)]),
        "Over",
    )
    assert named == "Circa -110 | NoVig -108 | DK -112 | FD -115"


def test_devig_books_keeps_mgm_in_named_list():
    named = format_devig_books_named(
        ["BetMGM", "FanDuel", "DraftKings"],
        _display("Home", [("BetMGM", -105), ("FanDuel", -110), ("DraftKings", -108)]),
        "Home",
    )
    assert "MGM -105" in named
    assert "FD -110" in named


def test_power_books_are_pack_not_full_tile():
    pack = ["Circa", "NoVig", "DraftKings"]
    tile = _display(
        "Over",
        [("Circa", -110), ("NoVig", -108), ("DraftKings", -112), ("FanDuel", -115), ("Bet365", 120)],
    )
    rec = build_auto_bet_sheet_record(
        fill={
            "pick": "Over",
            "market_type": "Total Runs",
            "qualifier": "10.5",
            "line": 10.5,
            "devig_books_names": pack,
            "display_books": tile,
            "take_book": "Kalshi",
            "status": "executed",
        }
    )
    assert rec["power_books"] == "Circa, NoVig, DraftKings"
    assert rec["sharp_count"] == 3
    assert "Bet365" not in rec["power_books"]
    assert "FanDuel" not in rec["power_books"]
    assert rec["devig_books"] == "Circa -110 | NoVig -108 | DK -112"
    assert rec["skip_reason"] == ""
    assert rec["status"] == "executed"


def test_fill_record_has_new_keys():
    rec = build_auto_bet_sheet_record(
        fill={
            "order_id": "ord-1",
            "ticker": "KXMLB-FOO",
            "side": "yes",
            "teams": "Cardinals @ Dodgers",
            "market_type": "Total Runs",
            "pick": "Over",
            "qualifier": "10.5",
            "line": 10.5,
            "ev_percent": "4.78",
            "take_book": "Kalshi",
            "devig_books_names": ["FanDuel", "DraftKings", "NoVig"],
            "display_books": _display(
                "Over",
                [("FanDuel", -110), ("DraftKings", -110), ("NoVig", -110)],
            ),
            "clock_running": False,
            "status_detail": "4th inning",
            "score": "3-2",
            "live": True,
            "status": "executed",
        }
    )
    for key in (
        "take_book",
        "power_books",
        "sharp_count",
        "line",
        "clock_running",
        "status_detail",
        "score",
        "live",
        "skip_reason",
        "devig_books",
        "filter_name",
        "status",
    ):
        assert key in rec
    assert rec["take_book"] == "Kalshi"
    assert rec["line"] == "10.5"
    assert rec["clock_running"] == "false"
    assert rec["status_detail"] == "4th inning"
    assert rec["score"] == "3-2"
    assert rec["live"] == "true"
    assert rec["skip_reason"] == ""
    assert rec["sharp_count"] == 3
    row = auto_bet_sheet_row(rec)
    assert len(row) == 32
    assert row[0] == rec["timestamp"]
    assert row[22] == rec["devig_books"]
    assert row[23] == "Kalshi"
    assert row[31] == ""


def test_ml_line_empty_total_line_kept():
    assert sheet_line_value("Moneyline", 1.5, "-110") == ""
    assert sheet_line_value("ML", None, "foo") == ""
    assert sheet_line_value("Total Runs", 10.5, "10.5") == "10.5"
    assert sheet_line_value("Point Spread", -1.5, "-1.5") == "-1.5"


def test_skipped_record_fills_skip_reason():
    alert = EvAlert(
        {
            "teams": "Yankees @ Angels",
            "pick": "Over",
            "qualifier": "1.5",
            "market_type": "Total Runs",
            "ev_percent": 2.5,
            "take_book": "PLive",
            "devig_books": ["FanDuel"],
            "display_books": _display("Over", [("FanDuel", -345)]),
            "line": 1.5,
        }
    )
    rec = build_auto_bet_sheet_record(
        alert=alert,
        skipped=True,
        skip_reason="insufficient sharp quotes (1/3)",
        fill={"status": "SKIPPED"},
    )
    assert rec["status"] == "SKIPPED"
    assert rec["skip_reason"] == "insufficient sharp quotes (1/3)"
    assert rec["take_book"] == "PLive"
    assert rec["line"] == "1.5"
    assert rec["result"] == ""


def test_mlb_clock_running_blank_if_missing():
    ctx = live_context_from_event({"league": "MLB", "live": True, "statusDetail": "4th inning"})
    assert ctx["clock_running"] is None
    rec = build_auto_bet_sheet_record(
        fill={"market_type": "Moneyline", "pick": "Yankees", "live": True, "status_detail": "4th inning"}
    )
    assert rec["clock_running"] == ""
    assert rec["live"] == "true"
    assert rec["line"] == ""


def test_nba_clock_running_from_event():
    ctx = live_context_from_event({"clock": {"running": False}, "statusDetail": "Halftime", "scores": {"home": 55, "away": 52}})
    assert ctx["clock_running"] is False
    assert ctx["score"] == "55-52"
    rec = build_auto_bet_sheet_record(
        fill={
            "market_type": "Moneyline",
            "pick": "Lakers",
            "clock_running": False,
            "status_detail": "Halftime",
            "score": "55-52",
            "live": True,
        }
    )
    assert rec["clock_running"] == "false"
    assert rec["score"] == "55-52"


def test_auto_bet_off_minsharp_three_mgm_stays():
    dash = Path(__file__).resolve().parents[1] / "dashboard.py"
    src = dash.read_text(encoding="utf-8")
    assert "auto_bet_enabled = False" in src
    assert '"minSharpBooks": 3' in src
    from odds_api_client import DEFAULT_ODDS_API_BOOKMAKERS

    assert "BetMGM" in DEFAULT_ODDS_API_BOOKMAKERS
    assert "from auto_bet_sheet import" in src
    assert "build_auto_bet_sheet_record(" in src
    assert "from auto_bet_sizing import size_auto_bet_order" in src
    import dashboard as dash_mod

    assert dash_mod.auto_bet_enabled is False
