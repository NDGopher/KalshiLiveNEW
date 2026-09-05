"""WS-primary Odds-API usage: healthy socket must not poll REST event/odds loops."""
from __future__ import annotations

import asyncio

import odds_api_ws as ows
from odds_api_client import reset_odds_api_429_backoff
from odds_api_ws import OddsApiWsFeed, resolve_odds_docs
from odds_ev_monitor import _resolve_live_events_slate


class _SpyRest:
    last_seq = None

    def __init__(self):
        self.live_calls = 0
        self.multi_calls = 0
        self.updated_calls = 0

    async def list_live_events(self, sport=None, force_refresh=False):
        self.live_calls += 1
        raise AssertionError("WS healthy + warm slate must not call /events/live")

    async def peek_cached_live_events(self, sport=None):
        return []

    async def select_bookmakers(self, names=None):
        return None

    async def get_odds_multi(self, *args, **kwargs):
        self.multi_calls += 1
        raise AssertionError("WS healthy must not call /odds/multi")

    async def get_odds_updated(self, since, bookmaker, sport=None):
        self.updated_calls += 1
        raise AssertionError("WS healthy must not call /odds/updated")


class _FailoverRest(_SpyRest):
    async def list_live_events(self, sport=None, force_refresh=False):
        self.live_calls += 1
        return [
            {
                "id": 7,
                "home": "A",
                "away": "B",
                "sport": {"slug": "baseball"},
                "league": {"slug": "usa-mlb"},
            }
        ]

    async def get_odds_multi(self, *args, **kwargs):
        self.multi_calls += 1
        raise AssertionError("WS-down failover must use /odds/updated, not /odds/multi")

    async def get_odds_updated(self, since, bookmaker, sport=None):
        self.updated_calls += 1
        return [
            {
                "id": 7,
                "home": "A",
                "away": "B",
                "sport": "Baseball",
                "bookmakers": {
                    bookmaker: [{"name": "ML", "odds": [{"home": 1.9, "away": 2.0}]}],
                },
            }
        ]


SOCCER = {
    "id": 42,
    "home": "Arsenal",
    "away": "Chelsea",
    "sport": {"slug": "football"},
    "league": {"slug": "england-premier-league", "name": "England - Premier League"},
    "live": True,
}


def _install(feed: OddsApiWsFeed) -> None:
    ows._shared_feed = feed
    ows._recovery_lock = None
    ows._ws_rest_backfill_lock = None
    ows._ws_rest_backfill_until = 0.0


def _healthy(rest) -> OddsApiWsFeed:
    feed = OddsApiWsFeed(rest, api_key="test-not-a-real-key")
    feed.connected = True
    feed.welcome_ok = True
    feed._running = True
    feed.resyncing = False
    feed._unhealthy_since = None
    return feed


def _cleanup() -> None:
    ows._shared_feed = None
    ows._recovery_lock = None
    ows._ws_rest_backfill_lock = None
    ows._ws_rest_backfill_until = 0.0
    reset_odds_api_429_backoff()


def test_ws_healthy_warm_slate_skips_events_live(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("ODDS_API_WS", "true")
    rest = _SpyRest()
    feed = _healthy(rest)
    feed.store.apply_slate([dict(SOCCER)])
    _install(feed)
    try:

        async def run() -> None:
            for _ in range(5):
                rows = await _resolve_live_events_slate(rest, None)
                assert {int(r["id"]) for r in rows} == {42}
            assert rest.live_calls == 0

        asyncio.run(run())
    finally:
        _cleanup()


def test_ws_healthy_skips_periodic_odds_multi(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("ODDS_API_WS", "true")
    monkeypatch.setenv("ODDS_API_WS_COLD_SEED", "true")
    monkeypatch.setenv("ODDS_API_WS_REST_BACKFILL", "true")
    rest = _SpyRest()
    feed = _healthy(rest)
    feed.store.apply_slate([dict(SOCCER)])
    feed.store.apply_rest_docs(
        [
            {
                **SOCCER,
                "bookmakers": {
                    "FanDuel": [{"name": "ML", "odds": [{"home": 2.0, "away": 1.8}]}],
                    "DraftKings": [{"name": "ML", "odds": [{"home": 2.05, "away": 1.75}]}],
                },
            }
        ]
    )
    _install(feed)
    try:

        async def run() -> None:
            books = [
                "DraftKings",
                "FanDuel",
                "BetMGM",
                "Bet365",
                "Kalshi",
                "Circa",
                "Polymarket",
                "Caesars",
                "NoVig",
                "[REDACTED]",
            ]
            for _ in range(8):
                docs, src = await resolve_odds_docs(rest, [42], books)
                assert src == "websocket"
                assert rest.multi_calls == 0
                assert rest.updated_calls == 0
                assert docs and int(docs[0]["id"]) == 42

        asyncio.run(run())
    finally:
        _cleanup()


def test_ws_down_allows_rate_limited_rest_updated(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("ODDS_API_WS", "true")
    monkeypatch.setenv("ODDS_API_REST_UPDATED_FALLBACK", "true")
    monkeypatch.setenv("ODDS_API_REST_FALLBACK_COOLDOWN_SEC", "30")
    monkeypatch.setenv("ODDS_API_SPORTS", "baseball")
    rest = _FailoverRest()
    feed = OddsApiWsFeed(rest, api_key="test-not-a-real-key")
    feed.connected = False
    feed.welcome_ok = False
    feed._running = True
    feed.resyncing = False
    feed._unhealthy_since = 0.0
    feed._reconnect_attempts = 2
    feed.last_error = "connection ended"
    feed.store.event_meta[7] = {
        "id": 7,
        "home": "A",
        "away": "B",
        "sport": "Baseball",
    }
    _install(feed)
    try:

        async def run() -> None:
            docs, src = await resolve_odds_docs(rest, [7], ["FanDuel", "DraftKings"])
            assert src == "rest_updated"
            assert rest.updated_calls >= 1
            assert rest.multi_calls == 0
            assert docs and int(docs[0]["id"]) == 7
            # Cooldown: second resolve must not hammer /odds/updated.
            docs2, src2 = await resolve_odds_docs(rest, [7], ["FanDuel", "DraftKings"])
            assert src2 == "unavailable"
            assert docs2 == []
            assert rest.updated_calls == 2  # FanDuel + DraftKings on first pass only

        asyncio.run(run())
    finally:
        _cleanup()


def test_ws_down_slate_may_use_rest(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("ODDS_API_WS", "true")
    rest = _FailoverRest()
    feed = OddsApiWsFeed(rest, api_key="test-not-a-real-key")
    feed.connected = False
    feed.welcome_ok = False
    feed._running = True
    feed.resyncing = False
    _install(feed)
    try:

        async def run() -> None:
            rows = await _resolve_live_events_slate(rest, None)
            assert rest.live_calls == 1
            assert {int(r["id"]) for r in rows} == {7}

        asyncio.run(run())
    finally:
        _cleanup()
