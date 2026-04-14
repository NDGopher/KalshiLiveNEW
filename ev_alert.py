"""
Normalized live EV alert for Kalshi matching and dashboard display.

Populated by OddsEVMonitor (Odds-API.io) or legacy tooling; same shape as the
historical alert record used across the codebase.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict


class EvAlert:
    """Single +EV opportunity: sportsbook consensus vs Kalshi."""

    def __init__(self, data: Dict[str, Any]):
        self.market_type = data.get("market_type", "")
        self.teams = data.get("teams", "")
        self.ev_percent = data.get("ev_percent", 0.0)
        self.expected_profit = data.get("expected_profit", 0.0)
        self.pick = data.get("pick", "")
        self.qualifier = data.get("qualifier", "")
        self.odds = data.get("odds", "")
        self.liquidity = data.get("liquidity", 0.0)
        self.book_price = data.get("book_price", "")
        self.fair_odds = data.get("fair_odds", "")
        self.display_books = data.get("display_books", {})
        self.devig_books = data.get("devig_books", [])
        self.market_url = data.get("market_url", "")
        self.ticker = data.get("ticker", "")
        self.timestamp = datetime.now()
        self.raw_html = data.get("raw_html", "")
        self.price_cents = None
        self.line = None
        # False = diagnostic display candidate (failed strict minRoi/minEv but passed relaxed gates)
        self.strict_pass = bool(data.get("strict_pass", True))
        # How the opportunity was discovered: e.g. odds_api_value_bets vs local_odds_scan (future).
        self.ev_source = str(data.get("ev_source") or "odds_api_value_bets")

    def extract_ticker_from_url(self):
        """Extract Kalshi ticker from market URL."""
        if not self.market_url:
            return None
        parts = self.market_url.rstrip("/").split("/")
        if parts:
            return parts[-1].upper()
        return None

    def to_dict(self):
        return {
            "market_type": self.market_type,
            "teams": self.teams,
            "ev_percent": self.ev_percent,
            "expected_profit": self.expected_profit,
            "pick": self.pick,
            "qualifier": self.qualifier,
            "odds": self.odds,
            "liquidity": self.liquidity,
            "book_price": self.book_price,
            "fair_odds": self.fair_odds,
            "market_url": self.market_url,
            "ticker": self.ticker or self.extract_ticker_from_url(),
            "timestamp": self.timestamp.isoformat(),
            "strict_pass": self.strict_pass,
            "ev_source": self.ev_source,
        }
