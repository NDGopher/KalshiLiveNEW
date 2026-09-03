"""PLive eventCoefficients transport: subscribe, getCache, reconnect, stale health."""
from __future__ import annotations

import asyncio
import gzip
import json
import time

from ev_calculator import american_to_decimal

from plive_pandora import (
    PLIVE_LINE_SET,
    PlivePandoraFeed,
    PliveStore,
    coeff_room_for_event,
    merge_plive_into_docs,
    peek_shared_plive_feed,
    reset_shared_plive_feed,
)


EID_MLB = "199298371"
EID_SOC = "555001"
EID_TOP = "220001"
ROOM_MLB = coeff_room_for_event(EID_MLB)
ROOM_SOC = coeff_room_for_event(EID_SOC)


class _FakeSio:
    def __init__(self) -> None:
        self.emits: list = []
        self.handlers: dict = {}

    async def emit(self, event, payload=None):
        self.emits.append((event, payload))

    def on(self, name):
        def _deco(fn):
            self.handlers[name] = fn
            return fn

        return _deco


def _meta(store: PliveStore, eid: str, **fields) -> None:
    base = {"ip": True, "finished": False}
    base.update(fields)
    store.apply_meta(eid, base)


def _feed() -> PlivePandoraFeed:
    return PlivePandoraFeed(connect_fn=lambda _f: None)


def test_initial_coeff_subscription_sports_1_5_220():
    feed = _feed()
    _meta(feed.store, EID_MLB, sportId=1, leagueId=8, home="Astros", away="Sox")
    _meta(feed.store, EID_SOC, sportId=5, home="Al-Fayha FC", away="Al-Kholood")
    _meta(feed.store, EID_TOP, sportId=220, home="Barcelona", away="Real Madrid")
    sio = _FakeSio()
    asyncio.run(feed._subscribe_mlb_coefficients(sio))
    subs = [p for e, p in sio.emits if e == "subscribe"]
    caches = [p for e, p in sio.emits if e == "getCache"]
    assert subs and caches
    rooms = subs[0]
    assert ROOM_MLB in rooms
    assert ROOM_SOC in rooms
    assert coeff_room_for_event(EID_TOP) in rooms
    assert caches[0] == rooms
    assert feed._last_subscribe_at > 0
    assert ROOM_MLB in feed._coeff_subscribed
    assert ROOM_SOC in sio.handlers


def test_new_event_triggers_coeff_subscription():
    feed = _feed()
    _meta(feed.store, EID_MLB, sportId=1, leagueId=8, home="Astros", away="Sox")
    sio = _FakeSio()
    asyncio.run(feed._subscribe_mlb_coefficients(sio))
    sio.emits.clear()
    _meta(feed.store, EID_SOC, sportId=5, home="Al-Fayha FC", away="Al-Kholood")
    asyncio.run(feed._subscribe_mlb_coefficients(sio))
    rooms = [r for e, p in sio.emits if e == "subscribe" for r in (p or [])]
    assert ROOM_SOC in rooms
    assert ROOM_MLB not in rooms  # already subscribed
    assert any(e == "getCache" and ROOM_SOC in (p or []) for e, p in sio.emits)


def test_reconnect_resubscribes_coeff_rooms():
    feed = _feed()
    _meta(feed.store, EID_MLB, sportId=1, leagueId=8, home="Astros", away="Sox")
    _meta(feed.store, EID_SOC, sportId=5, home="Al-Fayha FC", away="Al-Kholood")
    sio1 = _FakeSio()
    asyncio.run(feed._subscribe_mlb_coefficients(sio1))
    assert ROOM_MLB in feed._coeff_subscribed
    feed.reset_socket_bindings()
    assert feed._coeff_subscribed == set()
    sio2 = _FakeSio()
    asyncio.run(feed._subscribe_mlb_coefficients(sio2))
    rooms = [r for e, p in sio2.emits if e == "subscribe" for r in (p or [])]
    assert ROOM_MLB in rooms
    assert ROOM_SOC in rooms
    assert any(e == "getCache" and ROOM_MLB in (p or []) for e, p in sio2.emits)


def test_getcache_refresh_when_subscribed_but_unpriced():
    feed = _feed()
    _meta(feed.store, EID_SOC, sportId=5, home="Al-Fayha FC", away="Al-Kholood")
    sio = _FakeSio()
    asyncio.run(feed._subscribe_mlb_coefficients(sio))
    sio.emits.clear()
    feed._last_getcache_refresh_at = 0.0
    asyncio.run(feed._subscribe_mlb_coefficients(sio))
    assert any(e == "getCache" and ROOM_SOC in (p or []) for e, p in sio.emits)
    assert not any(e == "subscribe" for e, _p in sio.emits)


def test_stale_rooms_resubscribe_and_getcache(monkeypatch):
    monkeypatch.setenv("PLIVE_STALE_SEC", "5")
    feed = _feed()
    _meta(feed.store, EID_SOC, sportId=5, home="Al-Fayha FC", away="Al-Kholood")
    sio = _FakeSio()
    asyncio.run(feed._subscribe_mlb_coefficients(sio))
    feed.store.set_coeff(EID_SOC, 5, "under_2.5", 0, 2.86)
    feed.store.events[EID_SOC]["coeff_updated_at"] = time.time() - 30
    sio.emits.clear()
    feed._last_getcache_refresh_at = 0.0
    asyncio.run(feed._subscribe_mlb_coefficients(sio))
    rooms_sub = [r for e, p in sio.emits if e == "subscribe" for r in (p or [])]
    rooms_cache = [r for e, p in sio.emits if e == "getCache" for r in (p or [])]
    assert ROOM_SOC in rooms_sub
    assert ROOM_SOC in rooms_cache


def test_coeff_patch_list_is_not_split_as_catalog():
    """getCache often delivers a raw JSON-patch list on the room name."""
    feed = _feed()
    _meta(feed.store, EID_SOC, sportId=5, home="Al-Fayha FC", away="Al-Kholood")
    ops = [
        {"op": "replace", "path": "/c/m/3/o/1/1", "value": 1.85},
        {"op": "replace", "path": "/c/m/3/o/2/1", "value": 2.05},
        {"op": "replace", "path": "/c/m/5/o/over_2.5/0", "value": 1.52},
        {"op": "replace", "path": "/c/m/5/o/under_2.5/0", "value": american_to_decimal(186)},
    ]
    feed.ingest_raw(ops, ROOM_SOC)
    mk = {m["name"]: m for m in feed.store.markets_for_event(EID_SOC)}
    assert "ML" in mk
    assert mk["ML"]["odds"][0]["home"] == 1.85
    snap = feed.status_snapshot()
    assert snap["receiving_prices"] is True
    assert snap["soccer_with_prices"] >= 1
    assert snap["price_feed_ok"] is True


def test_room_handler_two_args_keeps_room_identity():
    feed = _feed()
    _meta(feed.store, EID_MLB, sportId=1, leagueId=8, home="Astros", away="Sox")
    sio = _FakeSio()
    asyncio.run(feed._subscribe_mlb_coefficients(sio))
    handler = sio.handlers[ROOM_MLB]
    payload = {
        "isDiff": False,
        "payload": {"c": {"m": {"3": {"o": {"1": {"1": 2.10}, "2": {"1": 1.70}}}}}},
    }
    handler(ROOM_MLB, payload)
    ml = next(m for m in feed.store.markets_for_event(EID_MLB) if m["name"] == "ML")
    assert ml["odds"][0]["home"] == 2.10
    assert feed._last_coeff_at > 0


def test_isdiff_wrapper_on_coeff_room():
    feed = _feed()
    _meta(feed.store, EID_SOC, sportId=5, home="Al-Fayha FC", away="Al-Kholood")
    feed.ingest_raw(
        {
            "isDiff": True,
            "payload": [
                {"op": "replace", "path": "/c/m/5/o/over_2.5/0", "value": 1.52},
                {"op": "replace", "path": "/c/m/5/o/under_2.5/0", "value": 2.86},
            ],
        },
        ROOM_SOC,
    )
    tot = next(m for m in feed.store.markets_for_event(EID_SOC) if m["name"] == "Totals")
    row = next(r for r in tot["odds"] if abs(float(r["hdp"]) - 2.5) < 1e-9)
    assert abs(float(row["under"]) - 2.86) < 1e-9
    assert abs(float(row["over"]) - 1.52) < 1e-9


def test_gzip_binary_on_coeff_room():
    feed = _feed()
    _meta(feed.store, EID_MLB, sportId=1, leagueId=8, home="Astros", away="Sox")
    body = {
        "isDiff": False,
        "payload": {"c": {"m": {"3": {"o": {"1": {"1": 1.91}, "2": {"1": 1.91}}}}}},
    }
    raw = gzip.compress(json.dumps(body).encode("utf-8"))
    feed.ingest_raw(raw, ROOM_MLB)
    ml = next(m for m in feed.store.markets_for_event(EID_MLB) if m["name"] == "ML")
    assert ml["odds"][0]["home"] == 1.91


def test_stale_price_health_fail_closed(monkeypatch):
    monkeypatch.setenv("PLIVE_STALE_SEC", "5")
    feed = _feed()
    feed.connected = True
    feed._running = True
    _meta(feed.store, EID_SOC, sportId=5, home="Al-Fayha FC", away="Al-Kholood")
    assert feed.price_feed_ok() is False
    assert feed.healthy is False
    feed.store.set_coeff(EID_SOC, 3, "1", 1, 1.80)
    feed.store.set_coeff(EID_SOC, 3, "2", 1, 2.10)
    feed._last_coeff_at = time.time()
    assert feed.price_feed_ok() is True
    feed.store.events[EID_SOC]["coeff_updated_at"] = time.time() - 30
    feed._last_coeff_at = time.time() - 30
    assert feed.price_feed_ok() is False
    assert feed.healthy is False
    snap = feed.status_snapshot()
    assert snap["last_coeff_at"] is not None
    assert str(snap["last_coeff_at"]).endswith("Z")
    assert snap["last_coeff_unix"] is not None
    assert snap["coeff_age_sec"] is not None and snap["coeff_age_sec"] >= 30
    assert snap["price_feed_ok"] is False


def test_merge_strips_plive_when_prices_stale(monkeypatch):
    monkeypatch.setenv("PLIVE_STALE_SEC", "5")

    async def _run():
        await reset_shared_plive_feed()
        feed = _feed()
        feed.connected = True
        feed._running = True
        _meta(feed.store, EID_SOC, sportId=5, home="Al-Fayha FC", away="Al-Kholood")
        import plive_pandora as pp

        pp._shared_plive = feed
        doc = {
            "home": "Al-Fayha FC",
            "away": "Al-Kholood",
            "sport": {"slug": "football"},
            "bookmakers": {"PLive": [{"name": "Totals", "odds": [{"hdp": 2.5, "over": 1.5, "under": 2.8}]}]},
        }
        assert feed.price_feed_ok() is False
        n = merge_plive_into_docs([doc])
        assert n == 0
        assert "PLive" not in doc["bookmakers"]
        await reset_shared_plive_feed()

    asyncio.run(_run())
    assert peek_shared_plive_feed() is None
