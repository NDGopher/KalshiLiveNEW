"""REST /events/live 429 must not empty the live scan when the WS store has events.

Price recovery stays fail-closed (PR #7): no stale /odds/multi hammer.
"""
from __future__ import annotations

import asyncio

import odds_api_ws as ows
from ev_calculator import american_to_decimal
from odds_api_client import OddsAPIClient
from odds_api_ws import OddsApiWsFeed, OddsWsStore, live_events_from_ws_store
from odds_ev_monitor import (
    OddsEVMonitor,
    _resolve_live_events_slate,
)


class _DummyRest:
    last_seq = None

    async def list_live_events(self, sport=None, force_refresh=False):
        return []

    async def select_bookmakers(self, names=None):
        return None

    async def get_odds_multi(self, *args, **kwargs):
        return []

    async def get_odds_updated(self, since, bookmaker, sport=None):
        return []


def _install_shared_feed(feed: OddsApiWsFeed) -> None:
    ows._shared_feed = feed
    ows._recovery_lock = None


SOCCER_EVENT = {
    "id": 9001,
    "home": "Al-Fayha FC",
    "away": "Al-Kholood",
    "sport": {"slug": "football"},
    # Must be a majors-scan league: ODDS_LIVE_SCAN_MAJORS_ONLY drops minor soccer
    # before resolve_odds_docs, which would empty WS-first recovery for this fixture.
    "league": {
        "name": "England - Premier League",
        "slug": "england-premier-league",
    },
    "live": True,
}

UNDER_25 = american_to_decimal(186)
OVER_25 = 1.52
PLIVE_UNDER_25 = american_to_decimal(205)

# Odds-API WS only keeps catalog books. PLive is Pandora-local and is attached via
# merge_plive_into_docs — never via the Odds-API WS bookmakers map.
_KALSHI_TOTALS = {
    "Kalshi": [
        {
            "name": "Totals",
            "odds": [
                {
                    "hdp": 2.5,
                    "max": 2.5,
                    "line": 2.5,
                    "over": OVER_25,
                    "under": UNDER_25,
                }
            ],
        }
    ],
    "[REDACTED]": [
        {"name": "Totals", "odds": [{"max": 2.5, "over": 1.55, "under": 2.40}]}
    ],
    "Bet365": [
        {"name": "Totals", "odds": [{"max": 2.5, "over": 1.56, "under": 2.38}]}
    ],
    "FanDuel": [
        {"name": "Totals", "odds": [{"max": 2.5, "over": 1.54, "under": 2.42}]}
    ],
}

_PLIVE_TOTALS = [
    {
        "name": "Totals",
        "odds": [
            {
                "hdp": 2.5,
                "max": 2.5,
                "line": 2.5,
                "over": OVER_25,
                "under": PLIVE_UNDER_25,
                # Pandora live markers — required by is_live_plive_side / is_live_plive_row.
                "plive_live": True,
                "plive_market": 5,
            }
        ],
    }
]


class _Raise429Live:
    _live_events_ttl = 1200.0
    _live_odds_multi_ttl = 0.0

    async def list_live_events(self, sport=None, force_refresh=False):
        err = Exception("429 Too Many Requests: /v3/events/live")
        err.status = 429
        raise err

    async def peek_cached_live_events(self, sport=None):
        return []

    async def get_odds_multi(self, *args, **kwargs):
        raise AssertionError("fail-closed must not fall through to /odds/multi")


def _healthy_feed(store: OddsWsStore) -> OddsApiWsFeed:
    feed = OddsApiWsFeed(_DummyRest(), api_key="test-not-a-real-key")
    feed.store = store
    feed.connected = True
    feed.welcome_ok = True
    feed._running = True
    feed.resyncing = False
    return feed


def test_list_live_events_429_returns_cached_slate():
    async def run() -> None:
        client = OddsAPIClient(api_key="test-not-a-real-key")
        await client._cache_events.set("events:live:all", [dict(SOCCER_EVENT)], ttl=0.001)
        await asyncio.sleep(0.02)
        assert await client._cache_events.get_valid("events:live:all") is None
        stale = await client.peek_cached_live_events(None)
        assert stale and int(stale[0]["id"]) == 9001

        async def boom(*_a, **_k):
            err = Exception("429 Too Many Requests: /v3/events/live")
            err.status = 429
            raise err

        client._get_json = boom  # type: ignore[method-assign]
        rows = await client.list_live_events(None)
        assert int(rows[0]["id"]) == 9001

    asyncio.run(run())


def test_resolve_live_slate_uses_ws_store_on_429():
    store = OddsWsStore()
    store.apply_slate([dict(SOCCER_EVENT)])
    store.apply_rest_docs(
        [
            {
                **SOCCER_EVENT,
                "bookmakers": {
                    "FanDuel": [{"name": "Totals", "odds": [{"max": 2.5, "over": 1.54, "under": 2.42}]}],
                },
            }
        ]
    )
    feed = _healthy_feed(store)
    _install_shared_feed(feed)
    try:
        rows = asyncio.run(_resolve_live_events_slate(_Raise429Live(), None))
        ids = {int(r["id"]) for r in rows}
        assert 9001 in ids
        assert live_events_from_ws_store()
    finally:
        ows._shared_feed = None
        ows._recovery_lock = None


def test_list_live_events_429_does_not_empty_scan_when_ws_store_has_events(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("ODDS_API_WS", "true")
    monkeypatch.delenv("ODDS_API_SPORTS", raising=False)
    monkeypatch.delenv("ODDS_API_WS_LEAGUES", raising=False)

    store = OddsWsStore()
    store.apply_slate([dict(SOCCER_EVENT)])
    store.apply_rest_docs(
        [
            {
                **SOCCER_EVENT,
                "bookmakers": dict(_KALSHI_TOTALS),
            }
        ]
    )
    feed = _healthy_feed(store)
    _install_shared_feed(feed)

    resolved_ids = []

    orig_resolve = ows.resolve_odds_docs

    async def spy_resolve(rest_client, event_ids, bookmakers=None, **kwargs):
        resolved_ids.extend(int(x) for x in event_ids)
        return await orig_resolve(rest_client, event_ids, bookmakers, **kwargs)

    monkeypatch.setattr("odds_ev_monitor.resolve_odds_docs", spy_resolve)

    async def no_kalshi(docs):
        return 0

    monkeypatch.setattr("odds_ev_monitor.attach_public_kalshi_to_docs", no_kalshi)

    def attach_plive(docs):
        """Simulate a healthy Pandora feed matching this WS event."""
        n = 0
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            bks = doc.setdefault("bookmakers", {})
            if not isinstance(bks, dict):
                continue
            bks.pop("PLive", None)
            bks["PLive"] = [dict(m) for m in _PLIVE_TOTALS]
            n += 1
        return n

    monkeypatch.setattr("odds_ev_monitor.merge_plive_into_docs", attach_plive)

    mon = OddsEVMonitor(auth_token=None)
    mon.set_filter(
        {
            "betTypes": ["GAMELINES"],
            "leagues": ["SOCCER_ALL"],
            "minRoi": 0,
            "devigFilter": {
                "sharps": ["[REDACTED]", "Bet365", "FanDuel"],
                "method": "POWER",
                "type": "AVERAGE",
                "minEv": 0,
                "minSharpBooks": 2,
                "hold": [{"book": "Any", "max": 20}],
            },
            "oddsRanges": [{"book": "Any", "min": -500, "max": 500}],
            "displayBooks": ["PLive", "Kalshi", "[REDACTED]", "Bet365", "FanDuel"],
            "bettingBooks": ["Kalshi", "PLive"],
        }
    )

    try:
        alerts = asyncio.run(mon._fetch_alerts_live_broad_scan(_Raise429Live()))
        assert 9001 in resolved_ids
        plive_unders = [
            a
            for a in alerts
            if str(getattr(a, "take_book", "")).lower() == "plive"
            and str(getattr(a, "pick", "")).lower() == "under"
        ]
        assert plive_unders, f"expected PLive under cards, got {len(alerts)} alerts"
        assert any(abs(float(a.qualifier) - 2.5) < 1e-9 for a in plive_unders)
        assert any(str(a.odds) in ("+205", "205") for a in plive_unders)
    finally:
        ows._shared_feed = None
        ows._recovery_lock = None
