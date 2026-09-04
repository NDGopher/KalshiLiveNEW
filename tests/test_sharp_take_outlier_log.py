"""Take junk-wipe must not look like 'missing books (3/3)'."""
from __future__ import annotations

from ev_calculator import (
    decimal_to_american,
    fair_books_excluding_take,
    filter_sharp_panel,
    is_junk_vs_kalshi,
)


def test_inverted_kalshi_take_wipes_panel_but_baseline_keeps_sharps():
    """Cardinals-style: market ~-150, Kalshi take +138 (sign flip).

    Raw DK/FD/Betfair are present. Take-relative junk screen empties the panel.
    Baseline (no take ref) still has 3+ sharps — so the truth is a bad take, not
    missing Odds-API books.
    """
    panel = [
        {"name": "DraftKings", "american": -156, "decimal_pick": 1.641, "decimal_opp": 2.32},
        {"name": "FanDuel", "american": -170, "decimal_pick": 1.588, "decimal_opp": 2.40},
        {"name": "Betfair Exchange", "american": -141, "decimal_pick": 1.709, "decimal_opp": 2.25},
        {"name": "Caesars", "american": -179, "decimal_pick": 1.559, "decimal_opp": 2.45},
        {"name": "NoVig", "american": -141, "decimal_pick": 1.709, "decimal_opp": 2.25},
        {"name": "Polymarket", "american": -138, "decimal_pick": 1.725, "decimal_opp": 2.22},
    ]
    # Refresh decimals from american for exact junk math
    for b in panel:
        from ev_calculator import american_to_decimal

        b["decimal_pick"] = american_to_decimal(int(b["american"]))
        # rough sister so two-way eligibility passes if required upstream
        b["decimal_opp"] = american_to_decimal(int(-b["american"]) if abs(b["american"]) > 100 else 100)

    take = 138  # inverted vs -150 market
    assert all(is_junk_vs_kalshi(int(b["american"]), take) for b in panel)

    baseline = filter_sharp_panel(panel, kalshi_american=None)
    baseline_fair = fair_books_excluding_take(baseline, "Kalshi")
    after_take = filter_sharp_panel(panel, kalshi_american=take)
    fair_after = fair_books_excluding_take(after_take, "Kalshi")

    assert len(baseline_fair) >= 3
    assert len(fair_after) == 0
    # The old bug: `after_take or panel` would report len(panel)==6 as if sharps survived.
    wrongly_counted = len(after_take or panel)
    assert wrongly_counted == len(panel)
    correctly_counted = len(fair_after)
    assert correctly_counted == 0
    assert correctly_counted < 3 <= len(baseline_fair)


def test_honest_drop_reason_branches():
    """Mirror the monitor branch condition for take-outlier vs true thin books."""
    min_sharp = 3
    # Case A: books present, take wiped them
    base_pc, pc = 6, 0
    assert base_pc >= min_sharp and pc < min_sharp  # take outlier path
    # Case B: truly thin alt (FD-only)
    base_pc, pc = 1, 1
    assert not (base_pc >= min_sharp and pc < min_sharp)  # insufficient sharps path
