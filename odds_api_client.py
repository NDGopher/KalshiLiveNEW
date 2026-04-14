"""
Async client for Odds-API.io (https://odds-api.io).

FREE TIER SAFE — default sports focus MLB/NBA via league filtering; tune with ODDS_API_SPORTS.
UPGRADE PATH: add WebSocket here (see https://docs.odds-api.io/guides/websockets) instead of polling.

Env:
  ODDS_API_KEY          — required for authenticated endpoints
  ODDS_API_BASE         — default https://api.odds-api.io/v3
  ODDS_API_BOOKMAKERS   — comma list, e.g. Kalshi,FanDuel (display names for API)
  ODDS_API_SPORTS       — comma sport slugs (API or legacy); see sport_slug_query_for_api()
  ODDS_API_LEAGUE_MLB   — optional /events league slug for MLB (default usa-mlb)
  ODDS_API_LEAGUE_NBA   — optional (default usa-nba)
  ODDS_API_LEAGUE_NHL   — optional (default usa-nhl)
  ODDS_API_LEAGUE_NFL   — optional (default usa-nfl)
  ODDS_API_MAX_REQUESTS_PER_HOUR — soft cap (default 100)
  ODDS_API_VALUE_BETS_TTL_SEC    — cache TTL for /value-bets (default 25)
  ODDS_API_ODDS_TTL_SEC          — cache TTL for /odds and /odds/multi (default 35)
  ODDS_API_EVENTS_TTL_SEC        — cache TTL for /events (default 120)
  ODDS_API_MULTI_PARALLEL_BOOKS  — default true: one /odds/multi per book (parallel) + merge, so all books appear (API truncates when many are listed in one query).
  ODDS_API_MULTI_PARALLEL_LIMIT  — max concurrent multi requests (default 12); cap if your host limits parallel connections.
  ODDS_DEBUG_MODE                — verbose inspection (see odds_ev_monitor)
  ODDS_DEBUG_MAX_EVENTS          — max events per debug poll (default 28)

EV math (Kalshi vs sharps) lives in ev_calculator.py; multi-book aggregation uses
ev_percent_three_methods_multi_sharp when you pass several sharp two-way panels.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
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
load_dotenv(_PROJECT_DIR / ".env.env", override=False, encoding="utf-8-sig")
load_dotenv(Path.cwd() / ".env.env", override=False, encoding="utf-8-sig")


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


def _merge_odds_multi_by_event_id(
    docs: List[Dict[str, Any]], id_order: Optional[List[int]] = None
) -> List[Dict[str, Any]]:
    """Merge partial /odds/multi payloads (same event id, different book slices) into one doc per event."""
    by_id: Dict[int, Dict[str, Any]] = {}
    for doc in docs:
        if not isinstance(doc, dict) or doc.get("id") is None:
            continue
        try:
            eid = int(doc["id"])
        except (TypeError, ValueError):
            continue
        bks_in = doc.get("bookmakers")
        if not isinstance(bks_in, dict):
            bks_in = {}
        if eid not in by_id:
            merged = {k: v for k, v in doc.items() if k != "bookmakers"}
            merged["bookmakers"] = {}
            by_id[eid] = merged
        tgt_bks = by_id[eid]["bookmakers"]
        assert isinstance(tgt_bks, dict)
        for raw_k, v in bks_in.items():
            ck = _canonical_odds_api_bookmaker(str(raw_k))
            tgt_bks[ck] = v
    out = list(by_id.values())
    if id_order:
        pos = {eid: i for i, eid in enumerate(id_order)}
        out.sort(key=lambda d: pos.get(int(d.get("id") or 0), 9999))
    return out


def _norm_book(s: str) -> str:
    s = s.strip()
    if not s:
        return s
    # Kalshi, FanDuel, etc.
    return s[0].upper() + s[1:] if len(s) > 1 else s.upper()


# Odds-API.io `/v3/bookmakers` uses a few names that differ from common sportsbook spellings.
# See https://docs.odds-api.io — invalid names produce HTTP 400 from `/odds/multi`.
_BOOKMAKER_API_ALIASES = {
    "novig": "NoVig",
    "bookmaker": "BookMaker.eu",
    "bookmaker.eu": "BookMaker.eu",
    "betfair": "Betfair Exchange",
}


def _canonical_odds_api_bookmaker(name: str) -> str:
    n = _norm_book(name)
    return _BOOKMAKER_API_ALIASES.get(n.lower(), n)


def _bookmaker_for_odds_request(name: str) -> str:
    """
    Name to send on /odds and /odds/multi ``bookmakers=`` (account "selected" list may differ).
    Betfair is often ``Betfair Sportsbook`` on the wire even when you think of it as Exchange.
    """
    c = _canonical_odds_api_bookmaker(name)
    if c.lower() == "betfair exchange":
        override = (os.getenv("ODDS_API_BETFAIR_REQUEST_NAME") or "").strip()
        if override:
            return override
        return "Betfair Sportsbook"
    return c


def _rekey_bookmakers_to_configured_name(
    docs: List[Dict[str, Any]], configured_book: str
) -> None:
    """If API returns one book under a different label (e.g. Sportsbook vs Exchange), re-key to ``configured_book``."""
    want = _canonical_odds_api_bookmaker(configured_book)
    for doc in docs:
        bks = doc.get("bookmakers")
        if not isinstance(bks, dict) or len(bks) != 1:
            continue
        k = next(iter(bks.keys()))
        if _norm_book(str(k)).lower() != _norm_book(want).lower():
            bks[want] = bks.pop(k)


def _books_from_odds_api_403_error(text: str) -> Optional[List[str]]:
    """Parse ``Allowed: A, B, ...`` from Odds-API.io access-denied JSON body."""
    if not text or "Allowed:" not in text:
        return None
    m = re.search(r"Allowed:\s*([^.]+)\.", text)
    if not m:
        return None
    return [x.strip() for x in m.group(1).split(",") if x.strip()]


def sport_slug_query_for_api(slug: str) -> str:
    """
    Map UI / legacy slugs to Odds-API.io ``sport`` query values (GET /events, GET /events/live).
    See docs Supported Sports: american-football, ice-hockey, football (soccer), etc.
    """
    s = (slug or "").strip().lower().replace("_", "-")
    # Legacy names without hyphens (older dashboard / env)
    legacy = {
        "americanfootball": "american-football",
        "icehockey": "ice-hockey",
        "soccer": "football",
        "mma": "mixed-martial-arts",
        "tabletennis": "table-tennis",
        "beachvolleyball": "beach-volleyball",
        "waterpolo": "water-polo",
        "beachsoccer": "beach-soccer",
        "gaelicfootball": "gaelic-football",
        "aussierules": "aussie-rules",
        "crosscountry": "cross-country",
        "beachhandball": "beach-handball",
    }
    if s in legacy:
        return legacy[s]
    return s


def normalize_sport_slug_key(slug: str) -> str:
    """Normalize for comparing API event.sport.slug to UI selection."""
    return sport_slug_query_for_api(slug).replace("-", "")


def major_league_slug_for_events(sport_api_slug: str, league_focus: str) -> Optional[str]:
    """
    Optional ``league`` for GET /events (docs). Defaults are common USA slugs; override via env
    if your feed uses different league identifiers.
    """
    lf = (league_focus or "all").strip().lower()
    sp = (sport_api_slug or "").strip().lower()
    mlb = (os.getenv("ODDS_API_LEAGUE_MLB") or "usa-mlb").strip()
    nba = (os.getenv("ODDS_API_LEAGUE_NBA") or "usa-nba").strip()
    nhl = (os.getenv("ODDS_API_LEAGUE_NHL") or "usa-nhl").strip()
    nfl = (os.getenv("ODDS_API_LEAGUE_NFL") or "usa-nfl").strip()
    if lf == "mlb" and sp == "baseball":
        return mlb or None
    if lf == "nba" and sp == "basketball":
        return nba or None
    if lf == "nhl" and sp == "ice-hockey":
        return nhl or None
    if lf == "nfl" and sp == "american-football":
        return nfl or None
    return None


def _subset_bookmakers_for_api(requested: List[str], allowed: List[str]) -> List[str]:
    """Preserve ``requested`` order; use Odds-API.io spelling from ``allowed``."""
    amap = {a.strip().lower(): a.strip() for a in allowed if a.strip()}
    out: List[str] = []
    seen: set = set()
    for b in requested:
        c = _canonical_odds_api_bookmaker(b)
        k = c.lower()
        if k not in amap:
            continue
        canon = amap[k]
        lk = canon.lower()
        if lk not in seen:
            seen.add(lk)
            out.append(canon)
    return out


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
            out.append(_canonical_odds_api_bookmaker(t))
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


_ODDS_API_MASTER_BOOKS_LOGGED = False


def log_odds_api_master_bookmakers_locked_once() -> None:
    """One clear startup line: exact parsed list from ODDS_API_BOOKMAKERS (no .env edits here)."""
    global _ODDS_API_MASTER_BOOKS_LOGGED
    if _ODDS_API_MASTER_BOOKS_LOGGED:
        return
    _ODDS_API_MASTER_BOOKS_LOGGED = True
    lst = odds_api_master_bookmakers()
    print(f"[ODDS-API] Locked ODDS_API_BOOKMAKERS ({len(lst)}): {', '.join(lst)}")


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
        raw_sports = _parse_csv("ODDS_API_SPORTS", "baseball,basketball")
        self.sports_slugs = [sport_slug_query_for_api(s) for s in raw_sports if s.strip()]
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

        log_odds_api_master_bookmakers_locked_once()

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

    async def _odds_multi_http(self, ids: str, bms: str, _429_attempt: int = 0) -> Tuple[int, Any]:
        await self._rate_limit()
        sess = await self._ensure_session()
        q: Dict[str, Any] = {"eventIds": ids, "bookmakers": bms, "apiKey": self.api_key}
        url = f"{self.base_url}/odds/multi?{urlencode(q)}"
        self.http_request_count += 1
        async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 429 and _429_attempt < 4:
                await asyncio.sleep(2.0 + _429_attempt)
                return await self._odds_multi_http(ids, bms, _429_attempt + 1)
            text = await resp.text()
            st = resp.status
            if st != 200:
                return st, text
            try:
                return st, json.loads(text)
            except json.JSONDecodeError:
                return st, text

    async def get_value_bets(self, bookmaker: str, include_event_details: bool = True) -> List[Dict[str, Any]]:
        """GET /value-bets — opportunities for a single target bookmaker (e.g. Kalshi)."""
        bm = _bookmaker_for_odds_request(_canonical_odds_api_bookmaker(bookmaker))
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
        bms = ",".join(_bookmaker_for_odds_request(b) for b in books)
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

    async def _get_odds_multi_one_slice(
        self, ids: str, books_slice: List[str]
    ) -> List[Dict[str, Any]]:
        """Single /odds/multi HTTP for one event-id batch and one bookmaker slice (cached per slice)."""
        if not books_slice:
            return []
        books_canon = [_canonical_odds_api_bookmaker(b) for b in books_slice]
        books_http = [_bookmaker_for_odds_request(b) for b in books_canon]
        bms = ",".join(books_http)
        key = f"multi:{ids}:{bms}"
        cached = await self._cache_odds.get_valid(key)
        if cached is not None:
            docs = _as_odds_multi_list(cached)
            if len(books_canon) == 1:
                _rekey_bookmakers_to_configured_name(docs, books_canon[0])
            return docs
        status, data = await self._odds_multi_http(ids, bms)
        if status == 403 and isinstance(data, str):
            allowed = _books_from_odds_api_403_error(data)
            if allowed:
                sub = _subset_bookmakers_for_api(books_http, allowed)
                if sub:
                    bms2 = ",".join(sub)
                    if bms2 != bms:
                        bms = bms2
                        key = f"multi:{ids}:{bms}"
                        status, data = await self._odds_multi_http(ids, bms)
                else:
                    # Requested book(s) not in account's allowed list (e.g. Polymarket not selected).
                    return []
            else:
                return []
        if status != 200:
            if status == 403:
                return []
            preview = data[:500] if isinstance(data, str) else str(data)[:500]
            raise RuntimeError(f"/odds/multi HTTP {status}: {preview}")
        if self._odds_ttl > 0:
            await self._cache_odds.set(key, data, self._odds_ttl)
        docs = _as_odds_multi_list(data)
        if len(books_canon) == 1:
            _rekey_bookmakers_to_configured_name(docs, books_canon[0])
        return docs

    async def get_odds_multi(self, event_ids: List[int], bookmakers: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """GET /odds/multi — up to 10 event ids per HTTP call.

        When ``ODDS_API_MULTI_PARALLEL_BOOKS`` is true (default), fetches **one bookmaker per request**
        in parallel for the same ``eventIds`` and **merges** ``bookmakers`` by event id. Odds-API.io
        often omits most books if you pass all 10 in a single ``bookmakers=`` param; per-book calls
        return full lines for each book so the live grid and devig see all configured books together.
        Set ``ODDS_API_MULTI_PARALLEL_BOOKS=false`` to send one request with every book (legacy).
        """
        if not event_ids:
            return []
        books = [_canonical_odds_api_bookmaker(b) for b in (bookmakers or self.bookmakers)]
        parallel_books = os.getenv("ODDS_API_MULTI_PARALLEL_BOOKS", "true").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        try:
            par_lim = int(os.getenv("ODDS_API_MULTI_PARALLEL_LIMIT", "12"))
        except ValueError:
            par_lim = 12
        par_lim = max(1, min(par_lim, 25))

        out: List[Dict[str, Any]] = []
        for i in range(0, len(event_ids), 10):
            part = [int(x) for x in event_ids[i : i + 10]]
            ids = ",".join(str(x) for x in part)
            if not parallel_books or len(books) <= 1:
                out.extend(await self._get_odds_multi_one_slice(ids, books))
                continue
            sem = asyncio.Semaphore(par_lim)

            async def _one_book(b: str) -> List[Dict[str, Any]]:
                async with sem:
                    try:
                        return await self._get_odds_multi_one_slice(ids, [b])
                    except RuntimeError:
                        return []

            nested = await asyncio.gather(*[_one_book(b) for b in books])
            partials: List[Dict[str, Any]] = []
            for lst in nested:
                partials.extend(lst)
            out.extend(_merge_odds_multi_by_event_id(partials, id_order=part))
        return out

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

    async def list_events_for_sport(
        self,
        sport_slug: str,
        *,
        league: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """GET /events — docs: sport (required), league, status optional."""
        slug = sport_slug_query_for_api(sport_slug)
        st = (status or "").strip() or None
        lg = (league or "").strip() or None
        key = f"events:sport:{slug}:league={lg or '-'}:status={st or '-'}"
        cached = await self._cache_events.get_valid(key)
        if cached is not None:
            return cached
        params: Dict[str, Any] = {"sport": slug}
        if lg:
            params["league"] = lg
        if st:
            params["status"] = st
        data = await self._get_json(
            "/events",
            params,
            cache=self._cache_events,
            cache_key=key,
            ttl=self._ev_ttl,
        )
        return data if isinstance(data, list) else []

    async def list_live_events(self, sport: Optional[str] = None) -> List[Dict[str, Any]]:
        """GET /events/live — docs: optional ``sport`` filter (API sport slug)."""
        api_s: Optional[str] = None
        if sport and str(sport).strip().lower() not in ("", "all"):
            api_s = sport_slug_query_for_api(str(sport))
        key = f"events:live:{api_s or 'all'}"
        cached = await self._cache_events.get_valid(key)
        if cached is not None:
            return cached
        params: Dict[str, Any] = {}
        if api_s:
            params["sport"] = api_s
        data = await self._get_json(
            "/events/live",
            params,
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
