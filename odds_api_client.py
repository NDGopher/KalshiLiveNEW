"""
Async client for Odds-API.io (https://odds-api.io).

FREE TIER SAFE — default sports focus MLB/NBA via league filtering; tune with ODDS_API_SPORTS.
UPGRADE PATH: add WebSocket here (see https://docs.odds-api.io/guides/websockets) instead of polling.

Env:
  ODDS_API_KEY          — required for authenticated endpoints
  ODDS_API_BASE         — default https://api.odds-api.io/v3
  ODDS_API_BOOKMAKERS   — comma list, e.g. Kalshi,FanDuel (display names for API)
  ODDS_API_SPORTS       — comma sport slugs, e.g. baseball,basketball
  ODDS_API_MAX_REQUESTS_PER_HOUR — soft cap (default 100)
  ODDS_API_VALUE_BETS_TTL_SEC    — cache TTL for /value-bets (default 25)
  ODDS_API_ODDS_TTL_SEC          — cache TTL for /odds and /odds/multi (default 35)
  ODDS_API_EVENTS_TTL_SEC        — cache TTL for /events (default 120)
  ODDS_DEBUG_MODE                — verbose inspection (see odds_ev_monitor)
  ODDS_DEBUG_MAX_EVENTS          — max events per debug poll (default 28)

EV math (Kalshi vs sharps) lives in ev_calculator.py; multi-book aggregation uses
ev_percent_three_methods_multi_sharp when you pass several sharp two-way panels.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import aiohttp
from dotenv import load_dotenv

# Load .env before any code reads os.environ (standalone: cwd may differ from package dir).
_PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(_PROJECT_DIR / ".env", override=True, encoding="utf-8-sig")
load_dotenv(Path.cwd() / ".env", override=True, encoding="utf-8-sig")


def _parse_csv(name: str, default: str) -> List[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _as_odds_multi_list(data: Any) -> List[Dict[str, Any]]:
    """Odds-API /odds/multi usually returns a JSON list; normalize wrapped shapes."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("data", "events", "results", "odds", "items"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _norm_book(s: str) -> str:
    s = s.strip()
    if not s:
        return s
    # Kalshi, FanDuel, etc.
    return s[0].upper() + s[1:] if len(s) > 1 else s.upper()


def parse_odds_api_bookmakers() -> List[str]:
    """
    Comma- or semicolon-separated bookmaker display names for Odds-API.io.
    Strips BOM/whitespace/quotes; de-dupes case-insensitively while preserving order.
    """
    default = "Kalshi,FanDuel"
    raw = os.getenv("ODDS_API_BOOKMAKERS")
    if raw is None or not str(raw).strip():
        raw = default
    s = str(raw).strip().strip('"').strip("'").lstrip("\ufeff")
    out: List[str] = []
    for chunk in s.replace(";", ",").split(","):
        t = chunk.strip()
        if t:
            out.append(_norm_book(t))
    seen: set = set()
    uniq: List[str] = []
    for b in out:
        k = b.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(b)
    return uniq


def odds_api_master_bookmakers() -> List[str]:
    """
    Subscription master list: always exactly what ODDS_API_BOOKMAKERS defines (e.g. 10 books).
    Use this for /odds/multi and the dashboard Odds tab. Per-filter ``displayBooks`` may be a
    smaller subset for alert-card columns only; it must not shrink what we request from the API.
    """
    return parse_odds_api_bookmakers()


class _TTLCache:
    def __init__(self):
        self._data: Dict[str, Tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            ent = self._data.get(key)
            if not ent:
                return None
            return ent[1]

    async def set(self, key: str, value: Any, ttl: float) -> None:
        async with self._lock:
            self._data[key] = (time.time() + ttl, value)

    async def get_valid(self, key: str) -> Optional[Any]:
        async with self._lock:
            ent = self._data.get(key)
            if not ent:
                return None
            exp, val = ent
            if exp < time.time():
                del self._data[key]
                return None
            return val


class OddsAPIClient:
    """Minimal async Odds-API.io v3 client with TTL caches and hourly rate limiting."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.api_key = api_key or os.getenv("ODDS_API_KEY", "")
        self.base_url = (base_url or os.getenv("ODDS_API_BASE", "https://api.odds-api.io/v3")).rstrip("/")
        self.bookmakers = odds_api_master_bookmakers()
        self.sports_slugs = [s.strip().lower() for s in _parse_csv("ODDS_API_SPORTS", "baseball,basketball")]
        self.max_rph = int(os.getenv("ODDS_API_MAX_REQUESTS_PER_HOUR", "100"))
        self._vb_ttl = float(os.getenv("ODDS_API_VALUE_BETS_TTL_SEC", "25"))
        self._odds_ttl = float(os.getenv("ODDS_API_ODDS_TTL_SEC", "35"))
        self._ev_ttl = float(os.getenv("ODDS_API_EVENTS_TTL_SEC", "120"))

        self._session_owner = session is None
        self._session = session

        self._cache_vb = _TTLCache()
        self._cache_odds = _TTLCache()
        self._cache_events = _TTLCache()
        self._cache_event_one = _TTLCache()

        self._rl_lock = asyncio.Lock()
        self._req_times: List[float] = []
        self.http_request_count = 0  # actual HTTP GETs (not cache hits); for standalone test summary

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._session_owner = True
        return self._session

    async def close(self) -> None:
        if self._session and self._session_owner:
            await self._session.close()
        self._session = None

    async def _rate_limit(self) -> None:
        async with self._rl_lock:
            now = time.time()
            window = 3600.0
            self._req_times = [t for t in self._req_times if now - t < window]
            if len(self._req_times) >= self.max_rph:
                sleep_for = window - (now - self._req_times[0]) + 0.05
                if sleep_for > 0:
                    await asyncio.sleep(min(sleep_for, window))
                now = time.time()
                self._req_times = [t for t in self._req_times if now - t < window]
            self._req_times.append(time.time())

    async def _get_json(
        self,
        path: str,
        params: Dict[str, Any],
        cache: Optional[_TTLCache] = None,
        cache_key: Optional[str] = None,
        ttl: float = 0.0,
        _429_attempt: int = 0,
    ) -> Any:
        if not self.api_key and path not in ("/sports", "/bookmakers"):
            raise RuntimeError("ODDS_API_KEY is not set")
        if cache and cache_key:
            hit = await cache.get_valid(cache_key)
            if hit is not None:
                return hit
        await self._rate_limit()
        sess = await self._ensure_session()
        q = dict(params)
        if path not in ("/sports", "/bookmakers"):
            q.setdefault("apiKey", self.api_key)
        url = f"{self.base_url}{path}?{urlencode(q)}"
        self.http_request_count += 1
        async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 429 and _429_attempt < 4:
                await asyncio.sleep(2.0 + _429_attempt)
                return await self._get_json(path, params, cache, cache_key, ttl, _429_attempt + 1)
            resp.raise_for_status()
            data = await resp.json()
        if cache and cache_key and ttl > 0:
            await cache.set(cache_key, data, ttl)
        return data

    async def get_value_bets(self, bookmaker: str, include_event_details: bool = True) -> List[Dict[str, Any]]:
        """GET /value-bets — opportunities for a single target bookmaker (e.g. Kalshi)."""
        bm = _norm_book(bookmaker)
        key = f"vb:{bm}:{int(include_event_details)}"
        data = await self._get_json(
            "/value-bets",
            {"bookmaker": bm, "includeEventDetails": "true" if include_event_details else "false"},
            cache=self._cache_vb,
            cache_key=key,
            ttl=self._vb_ttl,
        )
        return data if isinstance(data, list) else []

    async def get_odds_for_event(self, event_id: int, bookmakers: Optional[List[str]] = None) -> Dict[str, Any]:
        """GET /odds — full odds payload for one event."""
        books = bookmakers or self.bookmakers
        bms = ",".join(_norm_book(b) for b in books)
        key = f"odds:{event_id}:{bms}"
        cached = await self._cache_odds.get_valid(key)
        if cached is not None:
            return cached
        data = await self._get_json(
            "/odds",
            {"eventId": event_id, "bookmakers": bms},
            cache=self._cache_odds,
            cache_key=key,
            ttl=self._odds_ttl,
        )
        return data if isinstance(data, dict) else {}

    async def get_odds_multi(self, event_ids: List[int], bookmakers: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """GET /odds/multi — batch up to 10 events, counts as one request."""
        if not event_ids:
            return []
        books = bookmakers or self.bookmakers
        bms = ",".join(_norm_book(b) for b in books)
        chunk: List[Dict[str, Any]] = []
        for i in range(0, len(event_ids), 10):
            part = event_ids[i : i + 10]
            ids = ",".join(str(x) for x in part)
            key = f"multi:{ids}:{bms}"
            cached = await self._cache_odds.get_valid(key)
            if cached is not None:
                chunk.extend(cached)
                continue
            data = await self._get_json(
                "/odds/multi",
                {"eventIds": ids, "bookmakers": bms},
                cache=self._cache_odds,
                cache_key=key,
                ttl=self._odds_ttl,
            )
            chunk.extend(_as_odds_multi_list(data))
        return chunk

    async def search_events(self, query: str, limit: int = 25) -> List[Dict[str, Any]]:
        """GET /events/search"""
        if not query:
            return []
        key = f"search:{query}:{limit}"
        cached = await self._cache_events.get_valid(key)
        if cached is not None:
            return cached
        data = await self._get_json(
            "/events/search",
            {"query": query, "limit": limit},
            cache=self._cache_events,
            cache_key=key,
            ttl=self._ev_ttl,
        )
        return data if isinstance(data, list) else []

    async def get_market_details(self, event_id: int) -> Dict[str, Any]:
        """GET /events/{id} — event metadata (same as 'event details')."""
        key = f"event:{event_id}"
        cached = await self._cache_event_one.get_valid(key)
        if cached is not None:
            return cached
        data = await self._get_json(
            f"/events/{event_id}",
            {},
            cache=self._cache_event_one,
            cache_key=key,
            ttl=self._ev_ttl,
        )
        return data if isinstance(data, dict) else {}

    async def list_events_for_sport(self, sport_slug: str) -> List[Dict[str, Any]]:
        """GET /events?sport=slug — used sparingly; cached."""
        slug = sport_slug.strip().lower()
        key = f"events:sport:{slug}"
        cached = await self._cache_events.get_valid(key)
        if cached is not None:
            return cached
        data = await self._get_json(
            "/events",
            {"sport": slug},
            cache=self._cache_events,
            cache_key=key,
            ttl=self._ev_ttl,
        )
        return data if isinstance(data, list) else []

    async def list_live_events(self) -> List[Dict[str, Any]]:
        """GET /events/live — optional focus for in-play."""
        key = "events:live"
        cached = await self._cache_events.get_valid(key)
        if cached is not None:
            return cached
        data = await self._get_json(
            "/events/live",
            {},
            cache=self._cache_events,
            cache_key=key,
            ttl=min(self._ev_ttl, 30.0),
        )
        return data if isinstance(data, list) else []


_shared_client: Optional[OddsAPIClient] = None
_shared_lock = asyncio.Lock()


async def get_shared_odds_client() -> OddsAPIClient:
    """Process-wide shared client so multiple OddsEVMonitor filters reuse caches."""
    global _shared_client
    async with _shared_lock:
        key = os.getenv("ODDS_API_KEY", "").strip()
        if _shared_client is None:
            _shared_client = OddsAPIClient()
        else:
            cur = (_shared_client.api_key or "").strip()
            if key != cur:
                await _shared_client.close()
                _shared_client = OddsAPIClient()
        return _shared_client


async def reset_shared_odds_client() -> None:
    """Close and drop singleton (e.g. after reloading .env in a long-lived process)."""
    global _shared_client
    async with _shared_lock:
        if _shared_client is not None:
            await _shared_client.close()
            _shared_client = None
