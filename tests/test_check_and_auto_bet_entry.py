"""Regression: check_and_auto_bet must bind on entry (zero-fill crash).

Nested wrappers named store_failed_auto_bet / store_successful_auto_bet
made those names locals, so the capture line UnboundLocalError'd every
auto-bet asyncio task. Auto-bet stays OFF. No live orders.
"""
from __future__ import annotations

import asyncio

import pytest


def test_check_and_auto_bet_helpers_are_not_locals():
    """Shadowing those names is the UnboundLocalError. Keep them globals."""
    import dashboard as dash

    names = dash.check_and_auto_bet.__code__.co_varnames
    assert "store_failed_auto_bet" not in names
    assert "store_successful_auto_bet" not in names


def test_check_and_auto_bet_entry_does_not_unbound_local(monkeypatch):
    """Import + call the entry path; must not UnboundLocalError."""
    import dashboard as dash

    monkeypatch.setattr(dash, "write_auto_bet_to_sheets", lambda *_a, **_k: None)
    monkeypatch.setattr(dash, "write_auto_bet_to_csv", lambda *_a, **_k: None)
    before = len(dash.failed_auto_bets)

    async def run():
        await dash.check_and_auto_bet("entry-unbound-regression", {}, None)

    try:
        asyncio.run(run())
    except UnboundLocalError as exc:
        pytest.fail(f"check_and_auto_bet crashed on entry: {exc}")

    assert len(dash.failed_auto_bets) >= before + 1
    last = dash.failed_auto_bets[-1]
    assert last["alert_id"] == "entry-unbound-regression"
    assert "autobet_allow" in str(last.get("error") or "")


def test_strict_pass_false_does_not_skip_when_autobet_allow(monkeypatch):
    """Product lock is autobet_allow. strict_pass=False must not be a terminal skip."""
    import dashboard as dash

    monkeypatch.setattr(dash, "write_auto_bet_to_sheets", lambda *_a, **_k: None)
    monkeypatch.setattr(dash, "write_auto_bet_to_csv", lambda *_a, **_k: None)
    before = len(dash.failed_auto_bets)

    async def run():
        await dash.check_and_auto_bet(
            "strict-pass-not-terminal",
            {
                "autobet_allow": True,
                "strict_pass": False,
                "ev_percent": 0.1,
                "teams": "Wyoming vs CSU",
                "pick": "Wyoming",
                "ticker": "KXNCAAFGAME-26SEP05WYOCSU-WYO",
                "side": "yes",
            },
            None,
        )

    asyncio.run(run())
    assert len(dash.failed_auto_bets) >= before + 1
    last = dash.failed_auto_bets[-1]
    assert last["alert_id"] == "strict-pass-not-terminal"
    err = str(last.get("error") or "")
    assert "strict_pass" not in err
    assert "below filter minimum" in err
