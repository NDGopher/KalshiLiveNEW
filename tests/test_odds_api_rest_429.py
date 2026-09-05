"""Odds-API REST 429: exponential backoff, no tight-retry loops."""
from __future__ import annotations

import asyncio
import time

import pytest

from odds_api_client import (
    OddsAPIClient,
    OddsAPIRateLimitError,
    note_odds_api_429,
    odds_api_rest_429_blocked,
    reset_odds_api_429_backoff,
)


class _FakeResp:
    def __init__(self, status, payload=None, headers=None, text=""):
        self.status = status
        self.headers = headers or {}
        self._payload = payload if payload is not None else {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return self._text or ""

    def raise_for_status(self):
        if int(self.status) >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class _FakeSess:
    def __init__(self):
        self.calls = 0
        self.closed = False
        self.urls: list = []

    def get(self, url, timeout=None):
        self.calls += 1
        self.urls.append(url)
        return _FakeResp(429, text="Too Many Requests", headers={"Retry-After": "12"})

    def put(self, url, timeout=None):
        self.calls += 1
        self.urls.append(url)
        return _FakeResp(429, text="Too Many Requests")


@pytest.fixture(autouse=True)
def _clear_429():
    reset_odds_api_429_backoff()
    yield
    reset_odds_api_429_backoff()


def test_429_backoff_doubles_and_caps(monkeypatch):
    monkeypatch.setenv("ODDS_API_REST_429_BASE_SEC", "10")
    monkeypatch.setenv("ODDS_API_REST_429_MAX_SEC", "40")
    t0 = time.time()
    d1 = note_odds_api_429()
    d2 = note_odds_api_429()
    d3 = note_odds_api_429()
    assert d1 == 10.0
    assert d2 == 20.0
    assert d3 == 40.0
    assert odds_api_rest_429_blocked() is True
    assert time.time() - t0 < 1.0


def test_get_json_429_does_not_retry(monkeypatch):
    monkeypatch.setenv("ODDS_API_REST_429_BASE_SEC", "30")

    async def run() -> None:
        client = OddsAPIClient(api_key="test-not-a-real-key")
        sess = _FakeSess()
        client._session = sess  # type: ignore[assignment]
        client._session_owner = False
        with pytest.raises(OddsAPIRateLimitError):
            await client._get_json("/events/live", {})
        assert sess.calls == 1
        with pytest.raises(OddsAPIRateLimitError):
            await client._get_json("/events/live", {})
        # Backoff: second call must not hit the network.
        assert sess.calls == 1
        with pytest.raises(OddsAPIRateLimitError):
            await client.get_odds_multi([1, 2, 3], ["FanDuel", "DraftKings"])
        assert sess.calls == 1

    asyncio.run(run())


def test_odds_multi_http_429_does_not_retry(monkeypatch):
    monkeypatch.setenv("ODDS_API_REST_429_BASE_SEC", "30")

    async def run() -> None:
        client = OddsAPIClient(api_key="test-not-a-real-key")
        sess = _FakeSess()
        client._session = sess  # type: ignore[assignment]
        client._session_owner = False
        with pytest.raises(OddsAPIRateLimitError):
            await client._odds_multi_http("1,2", "FanDuel")
        assert sess.calls == 1
        with pytest.raises(OddsAPIRateLimitError):
            await client._odds_multi_http("1,2", "DraftKings")
        assert sess.calls == 1

    asyncio.run(run())
