"""Odds-API.io WebSocket unit tests — no live ODDS_API_KEY required."""
from __future__ import annotations

import asyncio
import os

import pytest

import odds_api_ws as ows

from odds_api_client import (
    DEFAULT_ODDS_API_BOOKMAKERS,
    _bookmaker_for_odds_request,
    _canonical_odds_api_bookmaker,
    api_wire_bookmakers,
    parse_odds_api_bookmakers,
    parse_odds_api_seq_header,
)
from odds_api_ws import (
    OddsApiWsFeed,
    OddsWsStore,
    WsFilterError,
    bookmaker_list_mismatch,
    build_ws_url,
    is_odds_api_rate_limit_error,
    mlb_ws_slice_active,
    odds_api_ws_wanted,
    redact_ws_url,
    resolve_odds_docs,
    ws_close_code,
    ws_filters_from_env,
    ws_reconnect_delay_sec,
)


def test_default_ten_books_excludes_bookmaker_eu(monkeypatch):
    monkeypatch.delenv("ODDS_API_BOOKMAKERS", raising=False)
    books = parse_odds_api_bookmakers()
    assert books == [x.strip() for x in DEFAULT_ODDS_API_BOOKMAKERS.split(",")]
    assert len(books) == 10
    assert "BookMaker.eu" not in books
    assert "Betfair Exchange" in books
    assert "Kalshi" in books
    assert "PLive" not in books


def test_betfair_exchange_on_the_wire(monkeypatch):
    monkeypatch.delenv("ODDS_API_BETFAIR_REQUEST_NAME", raising=False)
    assert _bookmaker_for_odds_request("Betfair Exchange") == "Betfair Exchange"
    assert _bookmaker_for_odds_request("betfair") == "Betfair Exchange"


def test_betfair_request_name_is_override_only(monkeypatch):
    monkeypatch.setenv("ODDS_API_BETFAIR_REQUEST_NAME", "Betfair Sportsbook")
    assert _bookmaker_for_odds_request("Betfair Exchange") == "Betfair Sportsbook"


def test_plive_never_on_api_wire():
    assert api_wire_bookmakers(["Kalshi", "PLive", "FanDuel"]) == ["Kalshi", "FanDuel"]


def test_seq_header_parser():
    assert parse_odds_api_seq_header({"X-OddsAPI-Seq": "482900"}) == 482900
    assert parse_odds_api_seq_header({"x-oddsapi-seq": "7"}) == 7
    assert parse_odds_api_seq_header({}) is None


def test_ws_wanted_defaults_true_when_key(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.delenv("ODDS_API_WS", raising=False)
    assert odds_api_ws_wanted() is True
    monkeypatch.setenv("ODDS_API_WS", "false")
    assert odds_api_ws_wanted() is False
    monkeypatch.delenv("ODDS_API_KEY")
    monkeypatch.setenv("ODDS_API_WS", "true")
    assert odds_api_ws_wanted() is False


def test_unset_sports_is_multi_sport_no_league_pin(monkeypatch):
    monkeypatch.delenv("ODDS_API_SPORTS", raising=False)
    monkeypatch.delenv("ODDS_API_WS_SPORT", raising=False)
    monkeypatch.delenv("ODDS_API_WS_SPORTS", raising=False)
    monkeypatch.delenv("ODDS_API_WS_LEAGUES", raising=False)
    monkeypatch.delenv("ODDS_API_WS_EVENT_IDS", raising=False)
    monkeypatch.delenv("ODDS_API_WS_STATUS", raising=False)
    assert mlb_ws_slice_active() is False
    f = ws_filters_from_env()
    assert f["sport"] == ["baseball", "football", "american-football"]
    assert f["leagues"] == []
    assert f["eventIds"] == []
    assert f["status"] is None
    assert f["markets"] == ["ML", "Spread", "Totals"]


def test_mlb_ws_filters_prematch_and_live(monkeypatch):
    monkeypatch.setenv("ODDS_API_SPORTS", "baseball")
    monkeypatch.setenv("ODDS_API_LEAGUE_MLB", "usa-mlb")
    monkeypatch.delenv("ODDS_API_WS_STATUS", raising=False)
    monkeypatch.delenv("ODDS_API_WS_LEAGUES", raising=False)
    monkeypatch.delenv("ODDS_API_WS_SPORT", raising=False)
    monkeypatch.delenv("ODDS_API_WS_EVENT_IDS", raising=False)
    assert mlb_ws_slice_active() is True
    f = ws_filters_from_env()
    assert f["sport"] == ["baseball"]
    assert f["leagues"] == ["usa-mlb"]
    assert f["eventIds"] == []
    assert f["status"] is None  # omit = prematch + live
    assert f["markets"] == ["ML", "Spread", "Totals"]
    assert "odds" in f["channels"]


def test_build_url_leagues_xor_event_ids():
    url = build_ws_url(
        "k",
        markets=["ML", "Spread", "Totals"],
        sport=["baseball"],
        leagues=["usa-mlb"],
        last_seq=99,
    )
    assert "apiKey=k" in url
    assert "sport=baseball" in url
    assert "leagues=usa-mlb" in url
    assert "markets=ML%2CSpread%2CTotals" in url or "markets=ML,Spread,Totals" in url
    assert "lastSeq=99" in url
    assert "status=" not in url.split("?")[1] or "status" not in url
    with pytest.raises(WsFilterError):
        build_ws_url("k", leagues=["usa-mlb"], event_ids=[1, 2])


def test_redact_api_key_in_url():
    url = build_ws_url("super-secret-key", markets=["ML"], sport=["baseball"])
    red = redact_ws_url(url)
    assert "super-secret-key" not in red
    assert "apiKey=%2A%2A%2A" in red or "apiKey=***" in red


def test_store_ml_only_update_keeps_totals():
    """Merge-by-market-name: created/updated must not replace the whole list.

    Subscribe ML,Spread,Totals is not this test. An ML-only updated payload
    must leave the stored Totals/Spread blocks (same over/under) in the store.
    """
    store = OddsWsStore()
    totals_odds = [{"max": 11.5, "hdp": 11.5, "over": 1.892857, "under": 1.847458}]
    store.apply_message(
        {
            "type": "created",
            "seq": 10,
            "id": "100",
            "bookie": "FanDuel",
            "markets": [
                {"name": "ML", "odds": [{"home": "1.9", "away": "2.0"}]},
                {
                    "name": "Spread",
                    "odds": [{"hdp": -1.5, "home": 1.91, "away": 1.91}],
                },
                {"name": "Totals", "odds": list(totals_odds)},
            ],
        }
    )
    store.apply_message(
        {
            "type": "updated",
            "seq": 11,
            "id": "100",
            "bookie": "FanDuel",
            "markets": [{"name": "ML", "odds": [{"home": "1.8", "away": "2.1"}]}],
        }
    )
    doc = store.merged_doc(100)
    fd = doc["bookmakers"]["FanDuel"]
    names = [m["name"] for m in fd]
    assert names.count("Totals") == 1
    assert "ML" in names
    assert "Totals" in names
    assert "Spread" in names
    ml = next(m for m in fd if m["name"] == "ML")
    tot = next(m for m in fd if m["name"] == "Totals")
    assert ml["odds"][0]["home"] == "1.8"
    assert tot["odds"][0]["over"] == 1.892857
    assert tot["odds"][0]["under"] == 1.847458
    counts = store.market_family_counts()
    assert counts["ml"] == 1
    assert counts["totals"] == 1
    assert counts["spread"] == 1
    assert store.last_seq == 11
    assert "FanDuel" in doc["book_updated_at"]
    assert store.book_updated_at[(100, "FanDuel")] == doc["book_updated_at"]["FanDuel"]


def test_same_family_alias_does_not_wipe_totals_name():
    """Family merge would drop Totals when a later payload says Total Runs."""
    store = OddsWsStore()
    store.apply_message(
        {
            "type": "created",
            "seq": 1,
            "id": 44,
            "bookie": "DraftKings",
            "markets": [
                {"name": "ML", "odds": [{"home": 1.9, "away": 2.0}]},
                {"name": "Totals", "odds": [{"max": 8.5, "over": 1.91, "under": 1.91}]},
            ],
        }
    )
    store.apply_message(
        {
            "type": "updated",
            "seq": 2,
            "id": 44,
            "bookie": "DraftKings",
            "markets": [
                {"name": "Total Runs", "odds": [{"max": 9.5, "over": 1.80, "under": 2.00}]},
            ],
        }
    )
    names = [m["name"] for m in store.merged_doc(44)["bookmakers"]["DraftKings"]]
    assert "Totals" in names
    assert "Total Runs" in names
    tot = next(m for m in store.merged_doc(44)["bookmakers"]["DraftKings"] if m["name"] == "Totals")
    assert tot["odds"][0]["max"] == 8.5


def test_rest_ml_only_snapshot_keeps_totals():
    """REST /odds ML-only row must not wipe Spread+Totals (same merge as WS)."""
    store = OddsWsStore()
    store.apply_rest_docs(
        [
            {
                "id": 200,
                "home": "Minnesota Twins",
                "away": "Detroit Tigers",
                "bookmakers": {
                    "FanDuel": [
                        {"name": "ML", "odds": [{"home": 1.9, "away": 2.0}]},
                        {"name": "Spread", "odds": [{"hdp": -1.5, "home": 1.91, "away": 1.91}]},
                        {"name": "Totals", "odds": [{"max": 11.5, "over": 1.91, "under": 1.91}]},
                    ]
                },
            }
        ]
    )
    store.apply_rest_docs(
        [
            {
                "id": 200,
                "bookmakers": {
                    "FanDuel": [{"name": "ML", "odds": [{"home": 1.85, "away": 2.05}]}],
                },
            }
        ]
    )
    names = [m["name"] for m in store.merged_doc(200)["bookmakers"]["FanDuel"]]
    assert "ML" in names
    assert "Totals" in names
    assert "Spread" in names


def test_totals_name_in_payload_replaces_totals_only():
    """Payload that includes Totals replaces Totals; ML/Spread stay."""
    store = OddsWsStore()
    store.apply_message(
        {
            "type": "created",
            "seq": 1,
            "id": 7,
            "bookie": "Caesars",
            "markets": [
                {"name": "ML", "odds": [{"home": 1.9, "away": 2.0}]},
                {"name": "Spread", "odds": [{"hdp": -1.5, "home": 1.91, "away": 1.91}]},
                {"name": "Totals", "odds": [{"max": 8.5, "over": 1.91, "under": 1.91}]},
            ],
        }
    )
    store.apply_message(
        {
            "type": "updated",
            "seq": 2,
            "id": 7,
            "bookie": "Caesars",
            "markets": [
                {"name": "Totals", "odds": [{"max": 9.5, "over": 1.80, "under": 2.05}]},
            ],
        }
    )
    names = [m["name"] for m in store.merged_doc(7)["bookmakers"]["Caesars"]]
    assert names.count("Totals") == 1
    assert "ML" in names and "Spread" in names
    tot = next(m for m in store.merged_doc(7)["bookmakers"]["Caesars"] if m["name"] == "Totals")
    assert tot["odds"][0]["max"] == 9.5


def test_slate_and_rest_preserve_kalshi_event_identity():
    """Odds-API urls / bookmakerIds must survive WS store merge (not just home/away)."""
    from odds_api_client import odds_api_kalshi_event_ticker

    store = OddsWsStore()
    store.apply_slate(
        [
            {
                "id": 260905027,
                "home": "Iowa Hawkeyes",
                "away": "Northern Illinois Huskies",
                "league": {"slug": "usa-college"},
                "urls": {"Kalshi": "https://kalshi.com/events/KXNCAAFGAME-26SEP05NIUIOWA"},
                "bookmakerIds": {"Kalshi": "KXNCAAFGAME-26SEP05NIUIOWA"},
            }
        ]
    )
    store.apply_rest_docs(
        [
            {
                "id": 260905027,
                "home": "Iowa Hawkeyes",
                "bookmakers": {
                    "Kalshi": [{"name": "Spread", "odds": [{"hdp": -27.5, "home": 1.45, "away": 2.80}]}],
                },
            }
        ]
    )
    merged = store.merged_doc(260905027)
    assert merged["urls"]["Kalshi"].endswith("KXNCAAFGAME-26SEP05NIUIOWA")
    assert merged["bookmakerIds"]["Kalshi"] == "KXNCAAFGAME-26SEP05NIUIOWA"
    assert odds_api_kalshi_event_ticker(merged) == "KXNCAAFGAME-26SEP05NIUIOWA"
    assert merged["kalshiEventTicker"] == "KXNCAAFGAME-26SEP05NIUIOWA"
    row = merged["bookmakers"]["Kalshi"][0]["odds"][0]
    assert not row.get("href")


def test_ws_updated_kalshi_url_becomes_event_ticker():
    store = OddsWsStore()
    store.apply_message(
        {
            "type": "updated",
            "seq": 1,
            "id": 99,
            "bookie": "Kalshi",
            "url": "https://kalshi.com/events/KXNCAAFGAME-26SEP05NIUIOWA",
            "markets": [{"name": "ML", "odds": [{"home": 1.2, "away": 4.5}]}],
        }
    )
    merged = store.merged_doc(99)
    assert merged["urls"]["Kalshi"].endswith("KXNCAAFGAME-26SEP05NIUIOWA")
    assert merged["kalshiEventTicker"] == "KXNCAAFGAME-26SEP05NIUIOWA"


def test_store_deleted_and_no_markets():
    store = OddsWsStore()
    store.apply_message(
        {"type": "created", "seq": 1, "id": 5, "bookie": "Kalshi", "markets": [{"name": "ML"}]}
    )
    store.apply_message({"type": "no_markets", "seq": 2, "id": 5, "bookie": "Kalshi", "markets": []})
    assert store.merged_doc(5)["bookmakers"]["Kalshi"] == []
    store.apply_message({"type": "deleted", "seq": 3, "id": 5, "bookie": "Kalshi"})
    assert "Kalshi" not in store.merged_doc(5)["bookmakers"]


def test_resync_required_and_welcome_mismatch():
    store = OddsWsStore()
    applied = store.apply_message({"type": "resync_required", "reason": "replay_limit_exceeded"})
    assert applied.type == "resync_required"
    missing, extra = bookmaker_list_mismatch(["FanDuel"], ["FanDuel", "Kalshi"])
    assert missing == ["Kalshi"]
    assert extra == []


def test_rest_docs_merge_by_name_per_book():
    """REST snapshot upserts by market name. A Spread-only row must not wipe ML."""
    store = OddsWsStore()
    store.apply_rest_docs(
        [
            {
                "id": 9,
                "home": "Yankees",
                "away": "Red Sox",
                "bookmakers": {"Kalshi": [{"name": "ML"}]},
            }
        ]
    )
    store.apply_rest_docs(
        [
            {
                "id": 9,
                "bookmakers": {"Kalshi": [{"name": "Spread"}]},
            }
        ]
    )
    names = [m["name"] for m in store.merged_doc(9)["bookmakers"]["Kalshi"]]
    assert "Spread" in names
    assert "ML" in names
    assert store.merged_doc(9)["home"] == "Yankees"


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


class _TryAgainLater(Exception):
    code = 1013

    def __str__(self) -> str:
        return "received 1013 (Try again later)"


class _Raise1013Connect:
    def __init__(self, url):
        raise _TryAgainLater()


class _Immediate1013Socket:
    close_code = 1013

    def __init__(self, url):
        self.url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def test_ws_close_code_and_429_detection():
    class Frame:
        code = 1013

    class Closed(Exception):
        rcvd = Frame()

    assert ws_close_code(Closed()) == 1013
    assert ws_close_code(_Immediate1013Socket("wss://x")) == 1013
    err = Exception("429 Too Many Requests")
    err.status = 429
    assert is_odds_api_rate_limit_error(err) is True
    assert is_odds_api_rate_limit_error(Exception("timeout")) is False


def test_1013_reconnect_delay_grows_with_jitter_bounds(monkeypatch):
    monkeypatch.setenv("ODDS_API_WS_1013_BASE_SEC", "8")
    monkeypatch.setenv("ODDS_API_WS_1013_MAX_SEC", "120")
    monkeypatch.setenv("ODDS_API_WS_RECONNECT_JITTER", "0.25")
    mid = [ws_reconnect_delay_sec(i, close_code=1013, rng=lambda: 0.5) for i in range(1, 6)]
    assert mid == [8.0, 16.0, 32.0, 64.0, 120.0]
    lo = ws_reconnect_delay_sec(1, close_code=1013, rng=lambda: 0.0)
    hi = ws_reconnect_delay_sec(1, close_code=1013, rng=lambda: 1.0)
    assert lo == pytest.approx(6.0)
    assert hi == pytest.approx(10.0)
    # Non-1013 uses the normal 2s base, not the 8s 1013 floor.
    monkeypatch.setenv("ODDS_API_WS_RECONNECT_BASE_SEC", "2")
    monkeypatch.setenv("ODDS_API_WS_RECONNECT_MAX_SEC", "60")
    assert ws_reconnect_delay_sec(1, close_code=None, rng=lambda: 0.5) == 2.0


def test_1013_reconnect_storm_does_not_reset_backoff(monkeypatch):
    """Brief 1013 closes must grow delay instead of 1s-looping with lastSeq."""
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("ODDS_API_WS_RECONNECT_JITTER", "0")
    monkeypatch.setenv("ODDS_API_WS_1013_BASE_SEC", "8")
    monkeypatch.setenv("ODDS_API_WS_1013_MAX_SEC", "120")
    monkeypatch.setenv("ODDS_API_WS_HEALTHY_RESET_SEC", "15")
    delays = []

    async def run() -> None:
        feed = OddsApiWsFeed(
            _DummyRest(),
            api_key="test-not-a-real-key",
            connect_fn=_Immediate1013Socket,
        )
        feed.store.last_seq = 99
        feed._running = True

        async def fake_sleep(delay):
            delays.append(float(delay))
            if len(delays) >= 5:
                feed._running = False

        monkeypatch.setattr(ows.asyncio, "sleep", fake_sleep)
        await feed._run_loop()
        assert feed.last_close_code == 1013
        assert feed._reconnect_attempts == 5

    asyncio.run(run())
    assert delays == [8.0, 16.0, 32.0, 64.0, 120.0]


def test_1013_exception_reconnect_storm(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("ODDS_API_WS_RECONNECT_JITTER", "0")
    monkeypatch.setenv("ODDS_API_WS_1013_BASE_SEC", "8")
    monkeypatch.setenv("ODDS_API_WS_1013_MAX_SEC", "120")
    delays = []

    async def run() -> None:
        feed = OddsApiWsFeed(
            _DummyRest(),
            api_key="test-not-a-real-key",
            connect_fn=_Raise1013Connect,
        )
        feed._running = True

        async def fake_sleep(delay):
            delays.append(float(delay))
            if len(delays) >= 4:
                feed._running = False

        monkeypatch.setattr(ows.asyncio, "sleep", fake_sleep)
        await feed._run_loop()
        assert feed.last_close_code == 1013

    asyncio.run(run())
    assert delays == [8.0, 16.0, 32.0, 64.0]


def _install_shared_feed(feed: OddsApiWsFeed) -> None:
    ows._shared_feed = feed
    ows._recovery_lock = None
    ows._ws_rest_backfill_lock = None
    ows._ws_rest_backfill_until = 0.0


def _mark_feed_healthy(feed: OddsApiWsFeed) -> None:
    feed.connected = True
    feed.welcome_ok = True
    feed._running = True
    feed.resyncing = False
    feed._unhealthy_since = None


def test_bet365_latency_suffix_canonicalizes():
    assert _canonical_odds_api_bookmaker("Bet365 (no latency)") == "Bet365"
    assert _canonical_odds_api_bookmaker("Bet365 (low latency)") == "Bet365"
    assert _canonical_odds_api_bookmaker("bet365 no latency") == "Bet365"
    assert _canonical_odds_api_bookmaker("Bet365 NJ") == "Bet365"
    assert _canonical_odds_api_bookmaker("DraftKings (no latency)") == "DraftKings"
    assert _canonical_odds_api_bookmaker("FanDuel - NJ") == "FanDuel"
    assert _canonical_odds_api_bookmaker("betfair sportsbook") == _canonical_odds_api_bookmaker(
        "betfair"
    )


def test_ws_bet365_latency_label_stores_as_bet365():
    store = OddsWsStore()
    store.apply_message(
        {
            "type": "updated",
            "seq": 1,
            "id": 55,
            "bookie": "Bet365 (no latency)",
            "markets": [{"name": "ML", "odds": [{"home": 2.1, "away": 1.7, "draw": 3.4}]}],
        }
    )
    assert "Bet365" in store.merged_doc(55)["bookmakers"]
    assert "Bet365 (no latency)" not in store.merged_doc(55)["bookmakers"]


def test_thin_ws_store_skips_rest_when_backfill_disabled(monkeypatch):
    """WS-first default: thin Polymarket-only store is NOT REST-filled every poll."""
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("ODDS_API_WS", "true")
    monkeypatch.setenv("ODDS_API_WS_REST_BACKFILL", "false")
    monkeypatch.setenv("ODDS_API_WS_COLD_SEED", "false")

    class SpyRest(_DummyRest):
        def __init__(self):
            self.multi_calls = 0

        async def get_odds_multi(self, *args, **kwargs):
            self.multi_calls += 1
            raise AssertionError("thin backfill disabled — must not call REST")

    async def run() -> None:
        rest = SpyRest()
        feed = OddsApiWsFeed(rest, api_key="test-not-a-real-key")
        _mark_feed_healthy(feed)
        feed.store.apply_rest_docs(
            [
                {
                    "id": 42,
                    "home": "Arsenal",
                    "away": "Chelsea",
                    "bookmakers": {
                        "Polymarket": [{"name": "ML", "odds": [{"home": 2.1, "away": 3.3, "draw": 3.0}]}],
                    },
                }
            ]
        )
        _install_shared_feed(feed)
        docs, src = await resolve_odds_docs(rest, [42], ["DraftKings", "Polymarket"])
        assert src == "websocket"
        assert rest.multi_calls == 0
        assert list((docs[0].get("bookmakers") or {}).keys()) == ["Polymarket"]

    try:
        asyncio.run(run())
    finally:
        ows._shared_feed = None
        ows._recovery_lock = None
        ows._ws_rest_backfill_lock = None
        ows._ws_rest_backfill_until = 0.0



def test_handoff_runs_rest_odds_by_default(monkeypatch):
    """REST→WS handoff seeds the store by default (then WS keeps books live)."""
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.delenv("ODDS_API_WS_HANDOFF_REST_ODDS", raising=False)

    class SpyRest(_DummyRest):
        def __init__(self):
            self.live_calls = 0
            self.multi_calls = 0
            self.last_seq = 42

        async def list_live_events(self, *args, **kwargs):
            self.live_calls += 1
            return [{"id": 1, "league": {"slug": "england-premier-league"}, "home": "A", "away": "B"}]

        async def get_odds_multi(self, event_ids, bookmakers, **kwargs):
            self.multi_calls += 1
            return [
                {
                    "id": 1,
                    "home": "A",
                    "away": "B",
                    "bookmakers": {
                        "DraftKings": [{"name": "ML", "odds": [{"home": 2.0, "away": 1.8}]}],
                        "Kalshi": [{"name": "ML", "odds": [{"home": 2.1, "away": 1.7}]}],
                    },
                }
            ]

    async def run() -> None:
        rest = SpyRest()
        feed = OddsApiWsFeed(rest, api_key="test-not-a-real-key")
        await feed._handoff_snapshot()
        assert rest.live_calls == 1
        assert rest.multi_calls == 1
        doc = feed.store.merged_doc(1)
        assert "DraftKings" in (doc.get("bookmakers") or {})
        assert "Kalshi" in (doc.get("bookmakers") or {})

    asyncio.run(run())


def test_handoff_seeds_soonest_pregame_ncaaf(monkeypatch):
    """REST handoff must include pending NCAAF tip-offs, not only /events/live."""
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.delenv("ODDS_API_WS_HANDOFF_REST_ODDS", raising=False)
    monkeypatch.setenv("ODDS_PREGAME_EVENTS_PER_SPORT", "8")

    class SpyRest(_DummyRest):
        def __init__(self):
            self.snap_ids: list = []
            self.last_seq = 7

        async def list_live_events(self, *args, **kwargs):
            return [
                {
                    "id": 1,
                    "sport": {"slug": "football"},
                    "league": {"slug": "england-premier-league"},
                    "home": "Arsenal",
                    "away": "Chelsea",
                }
            ]

        async def list_events_for_sport(self, sport_slug, league=None, status=None):
            from odds_api_client import sport_slug_query_for_api

            if sport_slug_query_for_api(str(sport_slug)) != "american-football":
                return []
            return [
                {
                    "id": 88,
                    "status": "settled",
                    "date": "2026-09-05T14:00:00Z",
                    "sport": {"slug": "american-football"},
                    "league": {"slug": "usa-college"},
                    "home": "Settled",
                    "away": "Done",
                },
                {
                    "id": 99,
                    "status": "pending",
                    "date": "2026-09-05T19:30:00Z",
                    "sport": {"slug": "american-football"},
                    "league": {"slug": "usa-college"},
                    "home": "Georgia Bulldogs",
                    "away": "Tennessee State Tigers",
                },
            ]

        async def get_odds_multi(self, event_ids, bookmakers, **kwargs):
            self.snap_ids = [int(x) for x in event_ids]
            return [
                {
                    "id": int(eid),
                    "bookmakers": {
                        "DraftKings": [{"name": "ML", "odds": [{"home": 2.0, "away": 1.8}]}],
                        "FanDuel": [{"name": "ML", "odds": [{"home": 2.0, "away": 1.8}]}],
                        "Bet365": [{"name": "ML", "odds": [{"home": 2.0, "away": 1.8}]}],
                        "Kalshi": [{"name": "ML", "odds": [{"home": 2.1, "away": 1.7}]}],
                    },
                }
                for eid in event_ids
            ]

    async def run() -> None:
        rest = SpyRest()
        feed = OddsApiWsFeed(rest, api_key="test-not-a-real-key")
        await feed._handoff_snapshot()
        assert 99 in rest.snap_ids
        assert 88 not in rest.snap_ids
        doc = feed.store.merged_doc(99)
        bks = doc.get("bookmakers") or {}
        assert "DraftKings" in bks
        assert "Kalshi" in bks

    asyncio.run(run())


def test_handoff_retries_after_transient_rest_error(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.delenv("ODDS_API_WS_HANDOFF_REST_ODDS", raising=False)

    class FlakyRest(_DummyRest):
        def __init__(self):
            self.live_calls = 0
            self.last_seq = 3

        async def list_live_events(self, *args, **kwargs):
            self.live_calls += 1
            if self.live_calls < 2:
                raise RuntimeError("ssl boom")
            return [
                {
                    "id": 1,
                    "sport": {"slug": "football"},
                    "league": {"slug": "england-premier-league"},
                }
            ]

        async def get_odds_multi(self, event_ids, bookmakers, **kwargs):
            return [
                {
                    "id": 1,
                    "bookmakers": {
                        "DraftKings": [{"name": "ML", "odds": [{"home": 2.0, "away": 1.8}]}],
                    },
                }
            ]

    async def run() -> None:
        rest = FlakyRest()
        feed = OddsApiWsFeed(rest, api_key="test-not-a-real-key")
        await feed._handoff_snapshot()
        assert rest.live_calls == 2
        assert "DraftKings" in (feed.store.merged_doc(1).get("bookmakers") or {})

    async def _fast_sleep(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    asyncio.run(run())


def test_handoff_does_not_retry_on_429(monkeypatch):
    """A 429 during includeSeq handoff must not re-blast /events/live + /odds/multi."""
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.delenv("ODDS_API_WS_HANDOFF_REST_ODDS", raising=False)
    monkeypatch.setenv("ODDS_API_REST_429_BASE_SEC", "60")

    class RateLimitedRest(_DummyRest):
        def __init__(self):
            self.live_calls = 0
            self.multi_calls = 0

        async def list_live_events(self, *args, **kwargs):
            self.live_calls += 1
            err = Exception("429 Too Many Requests: /v3/events/live")
            err.status = 429
            raise err

        async def get_odds_multi(self, *args, **kwargs):
            self.multi_calls += 1
            raise AssertionError("handoff 429 must not continue to /odds/multi")

    async def run() -> None:
        rest = RateLimitedRest()
        feed = OddsApiWsFeed(rest, api_key="test-not-a-real-key")
        await feed._handoff_snapshot()
        assert rest.live_calls == 1
        assert rest.multi_calls == 0

    async def _fast_sleep(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    try:
        asyncio.run(run())
    finally:
        from odds_api_client import reset_odds_api_429_backoff

        reset_odds_api_429_backoff()


def test_handoff_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("ODDS_API_WS_HANDOFF_REST_ODDS", "false")

    class SpyRest(_DummyRest):
        def __init__(self):
            self.multi_calls = 0

        async def list_live_events(self, *args, **kwargs):
            return [{"id": 1}]

        async def get_odds_multi(self, *args, **kwargs):
            self.multi_calls += 1
            raise AssertionError("disabled handoff must not call get_odds_multi")

    async def run() -> None:
        rest = SpyRest()
        feed = OddsApiWsFeed(rest, api_key="test-not-a-real-key")
        await feed._handoff_snapshot()
        assert rest.multi_calls == 0

    asyncio.run(run())


def test_cold_ws_store_does_not_rest_fill_while_healthy(monkeypatch):
    """WS-primary: healthy socket + empty event must not fire /odds/multi."""
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("ODDS_API_WS", "true")
    monkeypatch.setenv("ODDS_API_WS_REST_BACKFILL", "true")
    monkeypatch.setenv("ODDS_API_WS_COLD_SEED", "true")
    monkeypatch.setenv("ODDS_API_WS_REST_BACKFILL_COOLDOWN_SEC", "30")

    class SeedRest(_DummyRest):
        def __init__(self):
            self.multi_calls = 0

        async def get_odds_multi(self, event_ids, bookmakers, **kwargs):
            self.multi_calls += 1
            raise AssertionError("WS healthy must not cold-seed via REST")

    async def run() -> None:
        rest = SeedRest()
        feed = OddsApiWsFeed(rest, api_key="test-not-a-real-key")
        _mark_feed_healthy(feed)
        feed.store.apply_slate([{"id": 42, "home": "Arsenal", "away": "Chelsea"}])
        _install_shared_feed(feed)
        docs, src = await resolve_odds_docs(rest, [42], ["DraftKings", "FanDuel", "Bet365"])
        assert src == "websocket"
        assert rest.multi_calls == 0
        assert docs and int(docs[0]["id"]) == 42
        assert (docs[0].get("bookmakers") or {}) == {}

    try:
        asyncio.run(run())
    finally:
        ows._shared_feed = None
        ows._recovery_lock = None
        ows._ws_rest_backfill_lock = None
        ows._ws_rest_backfill_until = 0.0


def test_thin_ws_store_does_not_rest_backfill_while_healthy(monkeypatch):
    """WS-primary: thin store while healthy must not flood /odds/multi."""
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("ODDS_API_WS", "true")
    monkeypatch.setenv("ODDS_API_WS_REST_BACKFILL", "true")
    monkeypatch.setenv("ODDS_API_WS_REST_BACKFILL_MIN_BOOKS", "4")
    monkeypatch.setenv("ODDS_API_WS_REST_BACKFILL_COOLDOWN_SEC", "30")

    class FatRest(_DummyRest):
        def __init__(self):
            self.multi_calls = 0

        async def get_odds_multi(self, event_ids, bookmakers, **kwargs):
            self.multi_calls += 1
            raise AssertionError("WS healthy must not REST-backfill a thin store")

    async def run() -> None:
        rest = FatRest()
        feed = OddsApiWsFeed(rest, api_key="test-not-a-real-key")
        _mark_feed_healthy(feed)
        feed.store.apply_rest_docs(
            [
                {
                    "id": 42,
                    "home": "Arsenal",
                    "away": "Chelsea",
                    "bookmakers": {
                        "Polymarket": [{"name": "ML", "odds": [{"home": 2.1, "away": 3.3, "draw": 3.0}]}],
                    },
                }
            ]
        )
        _install_shared_feed(feed)
        books = ["DraftKings", "FanDuel", "Bet365", "Polymarket", "BetMGM", "Kalshi"]
        docs, src = await resolve_odds_docs(rest, [42], books)
        assert src == "websocket"
        assert rest.multi_calls == 0
        assert list((docs[0].get("bookmakers") or {}).keys()) == ["Polymarket"]
        docs2, src2 = await resolve_odds_docs(rest, [42], books)
        assert rest.multi_calls == 0
        assert src2 == "websocket"

    try:
        asyncio.run(run())
    finally:
        ows._shared_feed = None
        ows._recovery_lock = None
        ows._ws_rest_backfill_lock = None
        ows._ws_rest_backfill_until = 0.0


def test_ws_rest_backfill_skipped_when_store_already_fat(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("ODDS_API_WS", "true")
    monkeypatch.setenv("ODDS_API_WS_REST_BACKFILL", "true")
    monkeypatch.setenv("ODDS_API_WS_REST_BACKFILL_MIN_BOOKS", "4")

    class SpyRest(_DummyRest):
        def __init__(self):
            self.multi_calls = 0

        async def get_odds_multi(self, *args, **kwargs):
            self.multi_calls += 1
            raise AssertionError("fat WS store must not REST backfill")

    async def run() -> None:
        rest = SpyRest()
        feed = OddsApiWsFeed(rest, api_key="test-not-a-real-key")
        _mark_feed_healthy(feed)
        feed.store.apply_rest_docs(
            [
                {
                    "id": 9,
                    "home": "A",
                    "away": "B",
                    "bookmakers": {
                        "DraftKings": [{"name": "ML", "odds": [{"home": 2.0, "away": 1.8}]}],
                        "FanDuel": [{"name": "ML", "odds": [{"home": 2.0, "away": 1.8}]}],
                        "Bet365": [{"name": "ML", "odds": [{"home": 2.0, "away": 1.8}]}],
                        "Polymarket": [{"name": "ML", "odds": [{"home": 2.0, "away": 1.8}]}],
                    },
                }
            ]
        )
        _install_shared_feed(feed)
        docs, src = await resolve_odds_docs(
            rest, [9], ["DraftKings", "FanDuel", "Bet365", "Polymarket"]
        )
        assert src == "websocket"
        assert rest.multi_calls == 0
        assert len(docs[0]["bookmakers"]) == 4

    try:
        asyncio.run(run())
    finally:
        ows._shared_feed = None
        ows._recovery_lock = None
        ows._ws_rest_backfill_lock = None
        ows._ws_rest_backfill_until = 0.0


def test_429_fallback_suppression(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("ODDS_API_WS", "true")
    monkeypatch.setenv("ODDS_API_REST_UPDATED_FALLBACK", "true")
    monkeypatch.setenv("ODDS_API_REST_FALLBACK_429_COOLDOWN_SEC", "60")
    monkeypatch.setenv("ODDS_API_SPORTS", "baseball")

    class RateLimitedRest(_DummyRest):
        def __init__(self):
            self.calls = []

        async def get_odds_updated(self, since, bookmaker, sport=None):
            self.calls.append(bookmaker)
            err = Exception("429 Too Many Requests")
            err.status = 429
            raise err

        async def get_odds_multi(self, *args, **kwargs):
            raise AssertionError("fail-closed must not fall through to /odds/multi")

    async def run() -> None:
        rest = RateLimitedRest()
        feed = OddsApiWsFeed(rest, api_key="test-not-a-real-key")
        feed.store.event_meta[1] = {"id": 1, "home": "A", "away": "B"}
        feed.store.apply_rest_docs(
            [
                {
                    "id": 1,
                    "home": "A",
                    "away": "B",
                    "bookmakers": {"FanDuel": [{"name": "ML", "odds": [{"home": 1.9, "away": 2.0}]}]},
                }
            ]
        )
        feed._reconnect_attempts = 2
        feed.last_error = "1013 try again later"
        _install_shared_feed(feed)
        books = ["DraftKings", "FanDuel", "BetMGM", "Kalshi"]
        docs, src = await resolve_odds_docs(rest, [1], books)
        assert src == "unavailable"
        assert docs == []
        assert rest.calls == ["DraftKings"]
        assert feed.fallback_cooling_down() is True
        docs2, src2 = await resolve_odds_docs(rest, [1], books)
        assert src2 == "unavailable"
        assert docs2 == []
        assert rest.calls == ["DraftKings"]

    try:
        asyncio.run(run())
    finally:
        ows._shared_feed = None
        ows._recovery_lock = None
        from odds_api_client import reset_odds_api_429_backoff

        reset_odds_api_429_backoff()


def test_no_concurrent_recovery_calls(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("ODDS_API_WS", "true")
    monkeypatch.setenv("ODDS_API_REST_UPDATED_FALLBACK", "true")
    monkeypatch.setenv("ODDS_API_REST_FALLBACK_COOLDOWN_SEC", "30")
    monkeypatch.setenv("ODDS_API_SPORTS", "baseball")

    class SlowRest(_DummyRest):
        def __init__(self):
            self.calls = []
            self.inflight = 0
            self.max_inflight = 0

        async def get_odds_updated(self, since, bookmaker, sport=None):
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            self.calls.append(bookmaker)
            self.sports = getattr(self, "sports", [])
            self.sports.append(sport)
            await asyncio.sleep(0.05)
            self.inflight -= 1
            return []

    async def run() -> None:
        rest = SlowRest()
        feed = OddsApiWsFeed(rest, api_key="test-not-a-real-key")
        feed.store.event_meta[7] = {"id": 7}
        feed._reconnect_attempts = 1
        feed.last_error = "disconnected"
        _install_shared_feed(feed)
        books = ["DraftKings", "FanDuel", "BetMGM"]
        results = await asyncio.gather(
            resolve_odds_docs(rest, [7], books),
            resolve_odds_docs(rest, [7], books),
            resolve_odds_docs(rest, [7], books),
            feed.rest_updated_fallback(1, books),
        )
        assert rest.max_inflight == 1
        # One fallback pass (3 books × 1 pinned sport). Extra callers wait then hit cooldown.
        assert rest.calls == ["DraftKings", "FanDuel", "BetMGM"]
        assert rest.sports == ["Baseball", "Baseball", "Baseball"]
        sources = [r[1] if isinstance(r, tuple) else "direct" for r in results]
        assert sources.count("unavailable") >= 2
        assert "rest_multi" not in sources

    try:
        asyncio.run(run())
    finally:
        ows._shared_feed = None
        ows._recovery_lock = None


def test_ws_recovery_fail_closed_does_not_serve_stale(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("ODDS_API_WS", "true")
    monkeypatch.setenv("ODDS_API_REST_UPDATED_FALLBACK", "true")

    async def run() -> None:
        rest = _DummyRest()
        feed = OddsApiWsFeed(rest, api_key="test-not-a-real-key")
        feed.store.apply_rest_docs(
            [
                {
                    "id": 3,
                    "bookmakers": {"Kalshi": [{"name": "ML", "odds": [{"home": 1.5, "away": 2.5}]}]},
                }
            ]
        )
        feed._fallback_cooldown_until = 1e12
        feed._reconnect_attempts = 3
        feed.last_error = "1013"
        _install_shared_feed(feed)
        docs, src = await resolve_odds_docs(rest, [3], ["Kalshi"])
        assert src == "unavailable"
        assert docs == []
        assert feed.healthy is False

    try:
        asyncio.run(run())
    finally:
        ows._shared_feed = None
        ows._recovery_lock = None


def test_sport_name_for_odds_updated_maps_slug_and_object():
    from odds_api_client import sport_name_for_odds_updated, odds_updated_sport_names

    assert sport_name_for_odds_updated("baseball") == "Baseball"
    assert sport_name_for_odds_updated("american-football") == "American Football"
    assert sport_name_for_odds_updated({"name": "Ice Hockey", "slug": "ice-hockey"}) == "Ice Hockey"
    assert sport_name_for_odds_updated(None) is None
    assert odds_updated_sport_names(["baseball", "Baseball", "soccer"]) == ["Baseball", "Football"]


def test_prioritize_live_events_promotes_epl_over_fa_cup_flood(monkeypatch):
    from odds_api_client import prioritize_live_events_for_scan

    monkeypatch.setenv("ODDS_LIVE_SCAN_MAJORS_ONLY", "true")
    # Mimic /events/live: FA Cup / amateur flood first, EPL buried past scan max.
    events = []
    for i in range(100):
        events.append(
            {
                "id": 1000 + i,
                "sport": {"slug": "football"},
                "league": {"slug": "fa-cup" if i % 2 == 0 else "amateur-foo"},
            }
        )
    for i, slug in enumerate(
        [
            "england-premier-league",
            "england-premier-league",
            "spain-laliga",
            "usa-mlb",
            "germany-bundesliga",
        ]
    ):
        sport = "baseball" if slug == "usa-mlb" else "football"
        events.append({"id": 2000 + i, "sport": {"slug": sport}, "league": {"slug": slug}})

    raw_top = [e["league"]["slug"] for e in events[:80]]
    assert "england-premier-league" not in raw_top

    picked = prioritize_live_events_for_scan(events, 80)
    slugs = [e["league"]["slug"] for e in picked]
    # Majors-only: FA Cup / amateur must not pad the window.
    assert "fa-cup" not in slugs
    assert "amateur-foo" not in slugs
    assert slugs.count("england-premier-league") == 2
    assert "spain-laliga" in slugs
    assert "usa-mlb" in slugs
    assert picked[0]["league"]["slug"] in {
        "england-premier-league",
        "spain-laliga",
        "usa-mlb",
        "germany-bundesliga",
    }


def test_prioritize_majors_only_keeps_all_ncaaf_drops_bahrain(monkeypatch):
    from odds_api_client import prioritize_live_events_for_scan

    monkeypatch.setenv("ODDS_LIVE_SCAN_MAJORS_ONLY", "true")
    events = [
        {"id": 1, "sport": {"slug": "football"}, "league": {"slug": "bahrain-premier-league"}},
        {"id": 2, "sport": {"slug": "football"}, "league": {"slug": "angola-girabola"}},
        {"id": 3, "sport": {"slug": "american-football"}, "league": {"slug": "usa-college"}},
        {"id": 4, "sport": {"slug": "american-football"}, "league": {"slug": "usa-college"}},
        {"id": 5, "sport": {"slug": "football"}, "league": {"slug": "france-ligue-1"}},
        {"id": 6, "sport": {"slug": "cycling"}, "league": {"slug": "cycling-tour-of-britain-stage-4"}},
    ]
    picked = prioritize_live_events_for_scan(events, 80)
    slugs = [e["league"]["slug"] for e in picked]
    assert slugs.count("usa-college") == 2
    assert "france-ligue-1" in slugs
    assert "bahrain-premier-league" not in slugs
    assert "angola-girabola" not in slugs
    assert "cycling-tour-of-britain-stage-4" not in slugs
    assert len(picked) == 3



def test_prioritize_majors_keeps_ncaab_nhl_drops_minor_soccer(monkeypatch):
    from odds_api_client import prioritize_live_events_for_scan

    monkeypatch.setenv("ODDS_LIVE_SCAN_MAJORS_ONLY", "true")
    events = [
        {"id": 1, "sport": {"slug": "football"}, "league": {"slug": "bahrain-premier-league"}},
        {"id": 2, "sport": {"slug": "basketball"}, "league": {"slug": "usa-ncaa-basketball"}},
        {"id": 3, "sport": {"slug": "ice-hockey"}, "league": {"slug": "usa-nhl"}},
        {"id": 4, "sport": {"slug": "basketball"}, "league": {"slug": "usa-nba"}},
        {"id": 5, "sport": {"slug": "american-football"}, "league": {"slug": "usa-college"}},
        {"id": 6, "sport": {"slug": "football"}, "league": {"slug": "england-premier-league"}},
    ]
    picked = prioritize_live_events_for_scan(events, 80)
    slugs = [e["league"]["slug"] for e in picked]
    assert "usa-ncaa-basketball" in slugs
    assert "usa-nhl" in slugs
    assert "usa-nba" in slugs
    assert "usa-college" in slugs
    assert "england-premier-league" in slugs
    assert "bahrain-premier-league" not in slugs
    assert len(picked) == 5


def test_majors_only_drops_handball_bundesliga_keeps_soccer(monkeypatch):
    from odds_api_client import is_major_scan_event, prioritize_live_events_for_scan

    monkeypatch.setenv("ODDS_LIVE_SCAN_MAJORS_ONLY", "true")
    handball = {
        "id": 1,
        "sport": {"slug": "handball"},
        "league": {"slug": "germany-bundesliga"},
        "home": "VfL Gummersbach",
        "away": "MT Melsungen",
    }
    soccer = {
        "id": 2,
        "sport": {"slug": "football"},
        "league": {"slug": "germany-bundesliga"},
        "home": "Bayern Munich",
        "away": "Dortmund",
    }
    cfb = {
        "id": 3,
        "sport": {"slug": "american-football"},
        "league": {"slug": "usa-college"},
        "home": "Georgia Bulldogs",
        "away": "Tennessee State Tigers",
    }
    assert is_major_scan_event(handball) is False
    assert is_major_scan_event(soccer) is True
    assert is_major_scan_event(cfb) is True
    picked = prioritize_live_events_for_scan([handball, soccer, cfb], 80)
    assert [e["id"] for e in picked] == [2, 3]


def test_rest_updated_fallback_requires_sport_display_name(monkeypatch):
    monkeypatch.setenv("ODDS_API_SPORTS", "baseball")
    monkeypatch.setenv("ODDS_API_REST_UPDATED_FALLBACK", "true")

    class CaptureRest(_DummyRest):
        def __init__(self):
            self.params = []

        async def get_odds_updated(self, since, bookmaker, sport=None):
            self.params.append((bookmaker, sport))
            return []

    async def run() -> None:
        rest = CaptureRest()
        feed = OddsApiWsFeed(rest, api_key="test-not-a-real-key")
        feed.store.event_meta[1] = {"id": 1, "sport": {"name": "Baseball", "slug": "baseball"}}
        feed._reconnect_attempts = 1
        feed.last_error = "disconnected"
        n = await feed.rest_updated_fallback(1, ["DraftKings"], sports=[{"slug": "baseball"}])
        assert n == 0
        assert rest.params == [("DraftKings", "Baseball")]

    asyncio.run(run())


def test_prioritize_promotes_usa_college_and_sport_tier():
    from odds_api_client import prioritize_live_events_for_scan

    events = [
        {"id": 1, "sport": {"slug": "tennis"}, "league": {"slug": "atp-us-open"}},
        {"id": 2, "sport": {"slug": "football"}, "league": {"slug": "angola-girabola"}},
        {"id": 3, "sport": {"slug": "american-football"}, "league": {"slug": "usa-college"}},
        {"id": 4, "sport": {"slug": "football"}, "league": {"slug": "england-premier-league"}},
    ]
    picked = prioritize_live_events_for_scan(events, 3)
    slugs = [e["league"]["slug"] for e in picked]
    assert slugs[0] == "england-premier-league"
    assert slugs[1] == "usa-college"
