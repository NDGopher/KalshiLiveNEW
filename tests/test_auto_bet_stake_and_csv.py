"""P0: auto-bet stake respects filter amount; executed fills write CSV.

HCU@Rice Over 47.5 (2026-09-05 17:30 CT): filter $25, place used ~198
contracts / ~$97 at 49¢. BET_PLACED landed in jsonl; auto_bets.csv did not
get an executed row. Does not change ODDS_API_BOOKMAKERS, PLive auto,
ticker/side, or Order V2.
"""
from __future__ import annotations

import csv
from pathlib import Path

from auto_bet_sheet import build_auto_bet_sheet_record
from auto_bet_sizing import (
    auto_bet_stake_respecting_filter,
    contracts_for_stake_dollars,
    size_auto_bet_order,
)
from market_matcher import MarketMatcher


def test_twenty_five_dollars_at_49_cents_is_51_contracts_not_198():
    """$25 at 49¢ → floor(25/0.49)=51 contracts, cost $24.99 — not ~198 / $97."""
    stake, contracts, cost = size_auto_bet_order(25.0, 49, proposed_dollars=101.0)
    assert stake == 25.0
    assert contracts == 51
    assert contracts == contracts_for_stake_dollars(25.0, 49)
    assert abs(cost - 51 * 0.49) < 1e-9
    assert cost <= 25.0 + 1e-9
    assert contracts != 198
    assert cost < 90.0
    # Legacy total_bet_amount $101 at 51¢ was the silent 4x (198 contracts).
    assert contracts_for_stake_dollars(101.0, 51) == 198
    assert auto_bet_stake_respecting_filter(25.0, 101.0) == 25.0


def test_filter_amount_clamps_market_type_and_px_novig():
    assert auto_bet_stake_respecting_filter(25.0, 151.0) == 25.0
    assert auto_bet_stake_respecting_filter(25.0, 75.0) == 25.0
    assert auto_bet_stake_respecting_filter(25.0, 202.0) == 25.0
    assert auto_bet_stake_respecting_filter(25.0, 101.0 * 2.0) == 25.0
    # Prefer spending the configured amount, not a smaller legacy size.
    assert auto_bet_stake_respecting_filter(200.0, 75.0) == 200.0


def test_stake_sizing_fail_closed():
    assert contracts_for_stake_dollars(0, 49) == 0
    assert contracts_for_stake_dollars(25.0, 0) == 0
    assert contracts_for_stake_dollars(25.0, -5) == 0
    assert contracts_for_stake_dollars(None, 49) == 0
    assert contracts_for_stake_dollars(0.25, 49) == 0
    stake, contracts, cost = size_auto_bet_order(25.0, 0)
    assert contracts == 0 and cost == 0.0
    matcher = MarketMatcher(kalshi_client=None)
    assert matcher.calculate_contracts_from_dollars(25.0, 49) == 51
    assert matcher.calculate_contracts_from_dollars(25.0, 0) == 0


def test_executed_path_writes_csv_fields(tmp_path, monkeypatch):
    """Successful place appends executed CSV with order_id, prices, contracts, cost."""
    import dashboard as dash

    csv_path = tmp_path / "auto_bets.csv"
    monkeypatch.setattr(dash, "AUTO_BET_CSV_FILE", str(csv_path))

    class _WS:
        def __init__(self):
            self.rows = []

        def row_values(self, _n):
            return []

        def update(self, *_a, **_k):
            return None

        def append_row(self, row):
            self.rows.append(row)

    class _SS:
        def worksheet(self, _name):
            return _WS()

    class _Client:
        def open_by_key(self, _key):
            return _SS()

    # Sheets "success" used to return without writing CSV.
    monkeypatch.setattr(dash, "google_sheets_client", _Client())
    monkeypatch.setattr(dash, "GOOGLE_SHEETS_SPREADSHEET_ID", "sheet-id")

    rec = build_auto_bet_sheet_record(
        fill={
            "timestamp": "2026-09-05T17:30:00",
            "order_id": "ord-hcu-rice-475",
            "ticker": "KXNCAAFGAME-26SEP05HCURICE-TOTAL-475",
            "side": "yes",
            "teams": "Houston Christian @ Rice",
            "market_type": "Total Points",
            "pick": "Over",
            "qualifier": "47.5",
            "ev_percent": "3.10",
            "expected_price_cents": "49",
            "executed_price_cents": "49",
            "american_odds": "-105",
            "contracts": "51",
            "cost": "24.99",
            "payout": "51.00",
            "win_amount": "26.01",
            "sport": "NCAAF",
            "status": "executed",
            "result": "OPEN",
            "pnl": "0.00",
            "settled": "FALSE",
            "filter_name": "Kalshi All Sports (3 Sharps Live)",
        }
    )
    dash.write_auto_bet_to_sheets(rec)

    assert csv_path.exists()
    with csv_path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "executed"
    assert row["order_id"] == "ord-hcu-rice-475"
    assert row["executed_price_cents"] == "49"
    assert row["expected_price_cents"] == "49"
    assert row["contracts"] == "51"
    assert row["cost"] == "24.99"
    assert row["ticker"].endswith("TOTAL-475")


def test_dashboard_uses_filter_capped_sizer():
    src = Path(__file__).resolve().parents[1] / "dashboard.py"
    text = src.read_text(encoding="utf-8")
    assert "from auto_bet_sizing import size_auto_bet_order" in text
    assert "size_auto_bet_order(" in text
    assert "write_auto_bet_to_csv(bet_data)" in text
    assert "Clamped stake" in text
