"""
Async REST client for Odds-API.io (https://odds-api.io).

Primary live odds path is the WebSocket module ``odds_api_ws.py``
(https://docs.odds-api.io/guides/websockets). This client is for slate
(``/events``, ``/events/live``), REST→WS handoff snapshots (``includeSeq=true``),
resync after ``resync_required``, and REST fallback (prefer ``/odds/updated``).

Env:
  ODDS_API_KEY          — required for authenticated endpoints (read from env; never commit)
  ODDS_API_BASE         — default https://api.odds-api.io/v3
  ODDS_API_BOOKMAKERS   — comma catalog names (default: Growth 10-book set; BookMaker.eu is catalog-inactive)
  ODDS_API_BETFAIR_REQUEST_NAME — override only; default wire name is ``Betfair Exchange``
  ODDS_API_SELECT_BOOKS — if true, PUT /bookmakers/selected/select for ODDS_API_BOOKMAKERS on WS startup
  ODDS_API_SPORTS       — comma sport slugs; unset / empty / ``all`` → MLB/NBA/NHL/NFL+CBB+CFB/soccer only (see LIQUIDITY_DEFAULT_ODDS_API_SPORTS)
  ODDS_API_LEAGUE_MLB   — optional /events league slug for MLB (default usa-mlb)
  ODDS_API_LEAGUE_NBA   — optional (default usa-nba)
  ODDS_API_LEAGUE_NHL   — optional (default usa-nhl)
  ODDS_API_LEAGUE_NFL   — optional (default usa-nfl)
  ODDS_API_MAX_REQUESTS_PER_HOUR — soft cap (default 100)
  ODDS_API_VALUE_BETS_TTL_SEC    — cache TTL for /value-bets (default 25)
  ODDS_API_ODDS_TTL_SEC          — cache TTL for /odds and /odds/multi when not using live-odds TTL (default 35)
  ODDS_API_EVENTS_TTL_SEC        — cache TTL for /events (default 120)
  ODDS_API_LIVE_EVENTS_TTL_SEC   — cache TTL for /events/live only (default 1200s ≈ 20m; slate changes slowly)
  ODDS_API_LIVE_ODDS_TTL_SEC     — cache TTL for /odds/multi when monitors pass live refresh (default 0 = fresh each poll)
  ODDS_API_MAX_REQUESTS_PER_HOUR — soft client throttle (default 5000; lower on free tiers)
  ODDS_API_MULTI_PARALLEL_BOOKS  — default true: one /odds/multi per book (parallel) + merge, so all books appear (API truncates when many are listed in one query).
  ODDS_API_MULTI_PARALLEL_LIMIT  — max concurrent multi requests (default 12); cap if your host limits parallel connections.
  ODDS_DEBUG_MODE                — verbose inspection (see odds_ev_monitor)
  ODDS_DEBUG_MAX_EVENTS          — max events per debug poll (default 28)

EV math (Kalshi vs sharps) lives in ev_calculator.py; multi-book aggregation uses
ev_percent_three_methods_multi_sharp when you pass several sharp two-way panels.

Kalshi identity on Odds-API payloads (docs.odds-api.io/guides/prediction-markets
and websockets) — event-level only, not per-outcome market tickers:

  CARRIES a real KX… (or a URL that contains one):
    bookmakerIds.Kalshi     — Kalshi event ticker, e.g. KXNCAAFGAME-26SEP05NIUIOWA
    urls.Kalshi             — https://kalshi.com/events/KX…
    WS created/updated.url  — same event URL; store keeps it in event_meta.urls[bookie]

  DOES NOT carry (scan defensively; do not invent):
    per-outcome market ticker / market_id / selection id
    bookmakerOdds.href on REST or WS odds rows (href is our attach/enrich field)
    YES/NO side — Odds-API uses home/away/over/under/draw decimals

  Still resolved via public Kalshi markets API:
    market-level KX…-TEAM<ceil(|line|)> (or GAME-TEAM for ML)
    YES-ask overlay / orderbook depth when Odds-API last is stale

An event ticker is enough for handle_alert → find_submarket (fail-closed ceil
line / Under=NO / dog=NO). Public attach remains the market-ticker enricher.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlencode

import aiohttp
from dotenv import load_dotenv

from execution_guard import is_kalshi_ticker

# Load .env before any code reads os.environ (standalone: cwd may differ from package dir).
_PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(_PROJECT_DIR / ".env", override=True, encoding="utf-8-sig")
load_dotenv(Path.cwd() / ".env", override=True, encoding="utf-8-sig")
load_dotenv(_PROJECT_DIR / ".env.env", override=False, encoding="utf-8-sig")
load_dotenv(Path.cwd() / ".env.env", override=False, encoding="utf-8-sig")


def _parse_csv(name: str, default: str) -> List[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


# Last KX… token in a URL or bare id. Event URLs end with the event ticker;
# a nested /markets/… path ends with the more specific market ticker.
_KX_IN_TEXT = re.compile(r"(KX[A-Z0-9]+(?:-[A-Z0-9]+)+)", re.I)
_ODDS_API_ROW_TICKER_KEYS = (
    "ticker",
    "market_ticker",
    "marketTicker",
    "market_id",
    "marketId",
    "href",
    "home_href",
    "away_href",
    "draw_href",
    "over_href",
    "under_href",
    "home_ticker",
    "away_ticker",
    "draw_ticker",
    "over_ticker",
    "under_ticker",
)


def _odds_api_book_map_get(mapping: Any, book: str = "Kalshi") -> Any:
    if not isinstance(mapping, dict):
        return None
    want = str(book).strip().lower()
    for key, val in mapping.items():
        if str(key).strip().lower() == want:
            return val
    return None


def coerce_odds_api_kalshi_ticker(value: Any) -> Optional[str]:
    """Accept a bare KX… ticker or a kalshi.com URL that contains one."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    matches = _KX_IN_TEXT.findall(raw)
    for cand in reversed(matches):
        tok = str(cand).upper()
        if is_kalshi_ticker(tok):
            return tok
    if is_kalshi_ticker(raw):
        return raw.upper()
    return None


def odds_api_kalshi_row_ticker(row: Any) -> Optional[str]:
    """Per-outcome identity if Odds-API ever puts a KX on the odds row.

    Documented Kalshi rows are home/away/draw + lay/depth only. We still
    read href/ticker/market_id so a future payload does not stay paper.
    """
    if not isinstance(row, dict):
        return None
    for key in _ODDS_API_ROW_TICKER_KEYS:
        tok = coerce_odds_api_kalshi_ticker(row.get(key))
        if tok:
            return tok
    return None


def odds_api_kalshi_event_ticker(*sources: Any) -> Optional[str]:
    """Event-level KX from Odds-API ``bookmakerIds`` / ``urls`` (or a stamp)."""
    for src in sources:
        if not isinstance(src, dict):
            continue
        for map_key in ("bookmakerIds", "bookmaker_ids"):
            tok = coerce_odds_api_kalshi_ticker(_odds_api_book_map_get(src.get(map_key)))
            if tok:
                return tok
        tok = coerce_odds_api_kalshi_ticker(_odds_api_book_map_get(src.get("urls")))
        if tok:
            return tok
        tok = coerce_odds_api_kalshi_ticker(src.get("kalshiEventTicker") or src.get("eventTicker"))
        if tok:
            return tok
    return None


def odds_api_kalshi_event_url(*sources: Any) -> Optional[str]:
    for src in sources:
        if not isinstance(src, dict):
            continue
        raw = _odds_api_book_map_get(src.get("urls"))
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        extra = src.get("eventHref")
        if isinstance(extra, str) and extra.strip():
            return extra.strip()
        tok = odds_api_kalshi_event_ticker(src)
        if tok:
            return f"https://kalshi.com/events/{tok}"
    return None


def resolve_kalshi_take_ticker(row: Any = None, *docs: Any) -> Optional[str]:
    """Executable Kalshi identity for a take card.

    Prefer a market-level KX on the odds row (public attach / rare Odds-API
    href). Else the Odds-API event ticker — enough for find_submarket.
    """
    tok = odds_api_kalshi_row_ticker(row)
    if tok:
        return tok
    return odds_api_kalshi_event_ticker(*docs)


def stamp_odds_api_kalshi_event_identity(doc: Dict[str, Any]) -> Optional[str]:
    """Copy event ticker onto ``kalshiEventTicker`` without touching odds rows.

    Row href stays empty so public attach can still paint market-level KX.
    """
    if not isinstance(doc, dict):
        return None
    tok = odds_api_kalshi_event_ticker(doc)
    if tok:
        doc["kalshiEventTicker"] = tok
    return tok


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
            if is_local_only_bookmaker(ck):
                continue
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
    "plive": "PLive",
    # Odds-API WS occasionally suffixes catalog names.
    "bet365 (no latency)": "Bet365",
    "bet365 no latency": "Bet365",
}

# Prefix → catalog name for regional / latency-decorated WS labels
# (e.g. ``Bet365 NJ``, ``DraftKings (no latency)``).
_BOOKMAKER_PREFIX_CANON = (
    ("bet365", "Bet365"),
    ("draftkings", "DraftKings"),
    ("fanduel", "FanDuel"),
    ("betmgm", "BetMGM"),
    ("polymarket", "Polymarket"),
    ("kalshi", "Kalshi"),
    ("caesars", "Caesars"),
    ("circa", "Circa"),
    ("novig", "NoVig"),
)

_unknown_ws_bookies_logged: Set[str] = set()

# Local books that must never be sent on Odds-API.io REST / WS select.
_LOCAL_ONLY_BOOKS = frozenset({"plive"})


def is_local_only_bookmaker(name: str) -> bool:
    return _canonical_odds_api_bookmaker(name).lower() in _LOCAL_ONLY_BOOKS


def api_wire_bookmakers(names: Optional[List[str]] = None) -> List[str]:
    """ODDS_API_BOOKMAKERS minus local-only books (PLive is not an Odds-API catalog name)."""
    src = names if names is not None else parse_odds_api_bookmakers()
    return [b for b in src if not is_local_only_bookmaker(b)]


def _strip_ws_bookie_decorations(low: str, n: str) -> Tuple[str, str]:
    """Remove latency / parenthetical decorations from WS bookie labels."""
    for suffix in (
        " (no latency)",
        " (low latency)",
        " (high latency)",
        " (with latency)",
    ):
        if low.endswith(suffix):
            n = n[: -len(suffix)].strip()
            low = n.lower()
            break
    # Generic parenthetical: ``Bet365 (something)`` → ``Bet365``.
    if "(" in low and low.endswith(")"):
        base = n[: n.rfind("(")].strip()
        if base:
            n = base
            low = n.lower()
    for suffix in (" no latency", " low latency", " high latency"):
        if low.endswith(suffix):
            n = n[: -len(suffix)].strip()
            low = n.lower()
            break
    return low, n


def _prefix_canonical_bookmaker(low: str) -> Optional[str]:
    for prefix, canon in _BOOKMAKER_PREFIX_CANON:
        if low == prefix or low.startswith(prefix + " ") or low.startswith(prefix + "-"):
            return canon
    # Betfair Sportsbook / Exchange-style labels → catalog alias.
    if low == "betfair" or low.startswith("betfair ") or low.startswith("betfair-"):
        return _BOOKMAKER_API_ALIASES["betfair"]
    return None


def _canonical_odds_api_bookmaker(name: str) -> str:
    n = _norm_book(name)
    low = n.lower()
    low, n = _strip_ws_bookie_decorations(low, n)
    aliased = _BOOKMAKER_API_ALIASES.get(low)
    if aliased is not None:
        return aliased
    prefixed = _prefix_canonical_bookmaker(low)
    if prefixed is not None:
        return prefixed
    return n


def note_unknown_ws_bookie(raw: str) -> None:
    """One-shot log when a WS bookie label does not map onto ODDS_API_BOOKMAKERS."""
    raw_s = str(raw or "").strip()
    if not raw_s:
        return
    canon = _canonical_odds_api_bookmaker(raw_s)
    if is_local_only_bookmaker(canon):
        return
    master = {_canonical_odds_api_bookmaker(b).lower() for b in odds_api_master_bookmakers()}
    if canon.lower() in master:
        return
    key = raw_s.lower()
    if key in _unknown_ws_bookies_logged:
        return
    _unknown_ws_bookies_logged.add(key)
    print(
        f"[ODDS-API WS] unknown bookie label {raw_s!r} → {canon!r} "
        f"(not in ODDS_API_BOOKMAKERS; check mapping)"
    )


# Growth 10-book catalog names. BookMaker.eu is catalog-inactive — do not add it.
DEFAULT_ODDS_API_BOOKMAKERS = (
    "DraftKings,FanDuel,BetMGM,Betfair Exchange,Circa,Polymarket,Bet365,Caesars,Kalshi,NoVig"
)


def _bookmaker_for_odds_request(name: str) -> str:
    """
    Name to send on /odds, /odds/multi, and related ``bookmakers=`` params.

    Send ``Betfair Exchange`` on the wire (this plan uses the Exchange catalog name).
    ``ODDS_API_BETFAIR_REQUEST_NAME`` is an override only — set it if your account
    lists a different Betfair label (e.g. ``Betfair Sportsbook``).
    """
    c = _canonical_odds_api_bookmaker(name)
    if c.lower() == "betfair exchange":
        override = (os.getenv("ODDS_API_BETFAIR_REQUEST_NAME") or "").strip()
        if override:
            return override
        return "Betfair Exchange"
    return c


def parse_odds_api_seq_header(headers: Any) -> Optional[int]:
    """Read ``X-OddsAPI-Seq`` from a REST response (REST→WS handoff / resync)."""
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    raw = None
    if callable(getter):
        raw = getter("X-OddsAPI-Seq") or getter("x-oddsapi-seq")
    elif isinstance(headers, dict):
        raw = headers.get("X-OddsAPI-Seq") or headers.get("x-oddsapi-seq")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


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


# GET /odds/updated requires the sport *display name* from GET /sports (e.g. "Baseball"),
# not the slug ("baseball"). Wrong or missing sport → HTTP 400 and a dead REST fallback.
_ODDS_UPDATED_SPORT_NAME_BY_SLUG: Dict[str, str] = {
    "football": "Football",
    "basketball": "Basketball",
    "tennis": "Tennis",
    "baseball": "Baseball",
    "american-football": "American Football",
    "ice-hockey": "Ice Hockey",
    "esports": "Esports",
    "darts": "Darts",
    "mixed-martial-arts": "MMA",
    "boxing": "Boxing",
    "handball": "Handball",
    "volleyball": "Volleyball",
    "snooker": "Snooker",
    "table-tennis": "Table Tennis",
    "rugby": "Rugby",
    "cricket": "Cricket",
    "water-polo": "Waterpolo",
    "futsal": "Futsal",
    "beach-volleyball": "Beach Volley",
    "aussie-rules": "Aussie Rules",
    "floorball": "Floorball",
    "squash": "Squash",
    "beach-soccer": "Beach Soccer",
    "lacrosse": "Lacrosse",
    "curling": "Curling",
    "padel": "Padel",
    "bandy": "Bandy",
    "gaelic-football": "Gaelic Football",
    "beach-handball": "Beach Handball",
    "athletics": "Athletics",
    "badminton": "Badminton",
    "cross-country": "Cross-Country",
    "golf": "Golf",
    "cycling": "Cycling",
}


# Prefer these leagues when the live slate is huge (FA Cup / lower leagues flood
# /events/live). Without this, EPL sits past ODDS_LIVE_SCAN_MAX_EVENTS (55) and
# never enters odds resolve — zero soccer alerts despite priced majors.
_PRIORITY_LIVE_LEAGUE_SLUGS: frozenset = frozenset(
    {
        "england-premier-league",
        "spain-laliga",
        "germany-bundesliga",
        "italy-serie-a",
        "france-ligue-1",
        "portugal-primeira-liga",
        "netherlands-eredivisie",
        "mexico-liga-mx",
        "brazil-serie-a",
        "argentina-liga-profesional",
        "usa-mlb",
        "usa-nba",
        "usa-nfl",
        "usa-nhl",
        "usa-mls",
        "usa-college",  # NCAAF / college football on Odds-API
        "usa-ncaaf",
        "usa-ncaa",
        "usa-ncaab",
        "usa-ncaa-basketball",
        "usa-college-basketball",
        "usa-ncaa-football",
        "uefa-champions-league",
        "uefa-europa-league",
        "uefa-europa-conference-league",
    }
)

# Soccer slugs in the priority set. Odds-API reuses ``germany-bundesliga`` for
# handball; those must not steal majors-only slots from EPL / NCAAF.
_SOCCER_MAJOR_LEAGUE_SLUGS: frozenset = frozenset(
    {
        "england-premier-league",
        "spain-laliga",
        "germany-bundesliga",
        "italy-serie-a",
        "france-ligue-1",
        "portugal-primeira-liga",
        "netherlands-eredivisie",
        "mexico-liga-mx",
        "brazil-serie-a",
        "argentina-liga-profesional",
        "usa-mls",
        "uefa-champions-league",
        "uefa-europa-league",
        "uefa-europa-conference-league",
    }
)

# Prefer liquidity sports so Angola/ATP junk does not crowd out soccer/CFB majors.
_PRIORITY_LIVE_SPORT_SLUGS: frozenset = frozenset(
    {
        "football",  # soccer
        "american-football",
        "baseball",
        "basketball",
        "ice-hockey",
    }
)
# Lower-league / cup / youth floods — and thin markets that never clear minSharp.
_DEPRIORITIZE_LEAGUE_TOKENS: Tuple[str, ...] = (
    "amateur",
    "primavera",
    "liga-iii",
    "liga-3",
    "national-league",
    "u19",
    "u20",
    "u21",
    "youth",
    "reserves",
    "women",
    "fa-cup",  # huge flood; still soccer but not EPL
    "championship-round",
    "qualification",
    "girabola",
    "angola",
    "bahrain",
    "botswana",
    "armenia",
    "azerbaijan",
    "malta",
    "ghana",
    "nigeria",
    "kuwait",
    "divize",
    "kolmonen",
    "druga-nl",
    "nb-iii",
    "ofb-cup",
    "club-friendlies",
    "friendlies",
    "tercer",
    "regionalliga",
    "oberliga",
    "3-liga",
    "ii-liga",
    "iii-liga",
)


def live_event_league_slug(ev: Dict[str, Any]) -> str:
    lg = ev.get("league") if isinstance(ev, dict) else None
    if isinstance(lg, dict):
        return str(lg.get("slug") or "").strip().lower()
    return ""


def live_event_sport_slug(ev: Dict[str, Any]) -> str:
    sp = ev.get("sport") if isinstance(ev, dict) else None
    if isinstance(sp, dict):
        return sport_slug_query_for_api(str(sp.get("slug") or sp.get("name") or ""))
    if sp is not None:
        return sport_slug_query_for_api(str(sp))
    return ""


def is_major_scan_event(ev: Dict[str, Any]) -> bool:
    """True for board-priority events: must-cover majors, never minor soccer.

    Musts: NCAAF/NFL (all american-football), NBA/NCAAB, NHL, MLB, MLS, top soccer.
    Minor soccer / cups stay out unless listed in ``_PRIORITY_LIVE_LEAGUE_SLUGS``.
    """
    slug = live_event_league_slug(ev)
    sport = live_event_sport_slug(ev)
    if slug in _PRIORITY_LIVE_LEAGUE_SLUGS:
        # Soccer names are not unique across sports (handball Bundesliga, etc.).
        if slug in _SOCCER_MAJOR_LEAGUE_SLUGS and sport and sport != "football":
            return False
        return True
    # Every live american-football game (NCAAF/NFL) belongs on the board.
    if sport == "american-football":
        return True
    # US basketball + NHL: league slug variants differ across Odds-API feeds.
    if sport == "basketball" and any(
        tok in slug for tok in ("nba", "ncaa", "college", "ncaab")
    ):
        return True
    if sport == "ice-hockey" and ("nhl" in slug or slug.startswith("usa-")):
        return True
    return False


def live_event_scan_rank(ev: Dict[str, Any]) -> Tuple[int, int, int, str]:
    """Sort key for live scan / WS handoff (lower = earlier).

    0) demote lower-league floods
    1) promote known major slugs (EPL, NCAAF, MLB, …)
    2) prefer liquidity sports (soccer / CFB / majors) over tennis/esports junk
    3) stable slug for determinism
    """
    slug = live_event_league_slug(ev)
    sport = live_event_sport_slug(ev)
    demote = 1 if any(tok in slug for tok in _DEPRIORITIZE_LEAGUE_TOKENS) else 0
    major = 0 if is_major_scan_event(ev) else 1
    sport_tier = 0 if sport in _PRIORITY_LIVE_SPORT_SLUGS else 1
    return (demote, major, sport_tier, slug)


def prioritize_live_events_for_scan(
    events: Sequence[Dict[str, Any]],
    limit: int,
) -> List[Dict[str, Any]]:
    """Return up to ``limit`` live events with majors first (EPL/NCAAF before junk).

    Default ``ODDS_LIVE_SCAN_MAJORS_ONLY=true``: only must-cover majors
    (NCAAF/NFL/NBA/NCAAB/NHL/MLB + top soccer). Do **not** pad with minor
    soccer — that burns odds budget and starves real boards. Set
    ``ODDS_LIVE_SCAN_MAJORS_ONLY=false`` to restore fill-with-rest behavior.
    """
    lim = max(0, int(limit))
    if lim <= 0:
        return []
    indexed = list(enumerate(events or []))
    indexed.sort(key=lambda pair: (live_event_scan_rank(pair[1]), pair[0]))
    ordered = [ev for _i, ev in indexed]
    majors_only = os.getenv("ODDS_LIVE_SCAN_MAJORS_ONLY", "true").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if majors_only:
        majors = [ev for ev in ordered if is_major_scan_event(ev)]
        return majors[:lim]
    return ordered[:lim]


def sport_name_for_odds_updated(sport: Any) -> Optional[str]:
    """Return the Odds-API.io ``sport`` query value for GET /odds/updated.

    Accepts a slug (``baseball``), display name (``Baseball``), or event ``sport``
    object ``{"name":"Baseball","slug":"baseball"}``. Returns None if empty.
    """
    if sport is None:
        return None
    if isinstance(sport, dict):
        name = sport.get("name")
        if name and str(name).strip():
            return str(name).strip()
        slug = sport.get("slug") or sport.get("key") or sport.get("id")
        return sport_name_for_odds_updated(slug)
    raw = str(sport).strip()
    if not raw:
        return None
    # Already a known display name.
    if raw in _ODDS_UPDATED_SPORT_NAME_BY_SLUG.values():
        return raw
    slug = sport_slug_query_for_api(raw)
    if slug in _ODDS_UPDATED_SPORT_NAME_BY_SLUG:
        return _ODDS_UPDATED_SPORT_NAME_BY_SLUG[slug]
    # Last resort: title-case the slug (works for simple names; prefer the map).
    return slug.replace("-", " ").title()


def odds_updated_sport_names(sports: Optional[Sequence[Any]] = None) -> List[str]:
    """Deduped display names for /odds/updated. Defaults to ``odds_api_sports_list()``."""
    src: Sequence[Any]
    if sports is None:
        src = odds_api_sports_list()
    else:
        src = sports
    out: List[str] = []
    seen: set = set()
    for item in src:
        name = sport_name_for_odds_updated(item)
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


# When ``ODDS_API_SPORTS`` is unset, empty, or ``all`` / ``*`` / ``everything``: use this set (Odds-API ``sport`` values).
# ``basketball`` covers NBA and NCAA men's hoops; ``american-football`` covers NFL and NCAA football; ``football`` is soccer.
# Keeping these year-round avoids missing CBB/CFB when seasons start; off-season leagues mostly return empty /events rows (cached).
LIQUIDITY_DEFAULT_ODDS_API_SPORTS: Tuple[str, ...] = (
    "baseball",
    "basketball",
    "ice-hockey",
    "american-football",
    "football",
)


def odds_api_sports_list() -> List[str]:
    """
    Sport slugs for multi-sport pregame ``/events`` fetches and ``OddsAPIClient.sports_slugs``.

    - **Unset or empty** → ``LIQUIDITY_DEFAULT_ODDS_API_SPORTS`` (high-liquidity majors + soccer).
    - **``all`` / ``*`` / ``everything``** → same liquidity set (not the full API catalog), to limit HTTP toward 5k/hr.
      Set an explicit comma-separated list to add tennis, MMA, etc.
    - Otherwise → comma/semicolon list, each passed through ``sport_slug_query_for_api``.
    """
    raw = (os.getenv("ODDS_API_SPORTS") or "").strip()
    if not raw or raw.lower() in ("all", "*", "everything"):
        src: Tuple[str, ...] = LIQUIDITY_DEFAULT_ODDS_API_SPORTS
    else:
        src = tuple(x.strip() for x in raw.replace(";", ",").split(",") if x.strip())
    seen: set = set()
    out: List[str] = []
    for s in src:
        q = sport_slug_query_for_api(s)
        k = q.lower()
        if k not in seen:
            seen.add(k)
            out.append(q)
    return out


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
    Default is the Growth 10-book catalog list (``DEFAULT_ODDS_API_BOOKMAKERS``).
    """
    default = DEFAULT_ODDS_API_BOOKMAKERS
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

    async def peek(self, key: str) -> Optional[Any]:
        """Return the cached value even if expired. Does not evict.

        Used as a fail-open slate when ``/events/live`` 429s. Odds
        recovery must still fail closed — do not use this for prices.
        """
        async with self._lock:
            ent = self._data.get(key)
            if not ent:
                return None
            return ent[1]

    async def get_valid(self, key: str) -> Optional[Any]:
        async with self._lock:
            ent = self._data.get(key)
            if not ent:
                return None
            exp, val = ent
            if exp < time.time():
                # Keep the expired entry for /events/live 429 slate fallback.
                return None
            return val

    async def invalidate(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)


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
        self.sports_slugs = odds_api_sports_list()
        self.max_rph = int(os.getenv("ODDS_API_MAX_REQUESTS_PER_HOUR", "5000"))
        self._vb_ttl = float(os.getenv("ODDS_API_VALUE_BETS_TTL_SEC", "25"))
        self._odds_ttl = float(os.getenv("ODDS_API_ODDS_TTL_SEC", "35"))
        self._ev_ttl = float(os.getenv("ODDS_API_EVENTS_TTL_SEC", "120"))
        _live_ttl_raw = os.getenv("ODDS_API_LIVE_EVENTS_TTL_SEC")
        if _live_ttl_raw is not None and str(_live_ttl_raw).strip() != "":
            self._live_events_ttl = float(_live_ttl_raw)
        else:
            # Live *slate* (which games exist): 15–30 min is fine; decouple from line refresh poll.
            self._live_events_ttl = max(60.0, min(2400.0, 1200.0))
        _lod_raw = os.getenv("ODDS_API_LIVE_ODDS_TTL_SEC")
        if _lod_raw is not None and str(_lod_raw).strip() != "":
            self._live_odds_multi_ttl = float(_lod_raw)
        else:
            # 0 = do not cache /odds/multi for monitor-driven live refreshes (all books same “tick”).
            self._live_odds_multi_ttl = 0.0

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
        self.last_seq: Optional[int] = None  # latest X-OddsAPI-Seq from includeSeq snapshots

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
            self._note_seq_header(resp.headers)
            data = await resp.json()
        if cache and cache_key and ttl > 0:
            await cache.set(cache_key, data, ttl)
        return data

    def _note_seq_header(self, headers: Any) -> Optional[int]:
        seq = parse_odds_api_seq_header(headers)
        if seq is not None:
            prev = self.last_seq
            self.last_seq = seq if prev is None else max(prev, seq)
        return seq

    async def _odds_multi_http(
        self,
        ids: str,
        bms: str,
        _429_attempt: int = 0,
        *,
        include_seq: bool = False,
    ) -> Tuple[int, Any]:
        await self._rate_limit()
        sess = await self._ensure_session()
        q: Dict[str, Any] = {"eventIds": ids, "bookmakers": bms, "apiKey": self.api_key}
        if include_seq:
            q["includeSeq"] = "true"
        url = f"{self.base_url}/odds/multi?{urlencode(q)}"
        self.http_request_count += 1
        async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 429 and _429_attempt < 4:
                await asyncio.sleep(2.0 + _429_attempt)
                return await self._odds_multi_http(
                    ids, bms, _429_attempt + 1, include_seq=include_seq
                )
            self._note_seq_header(resp.headers)
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

    async def get_odds_for_event(
        self,
        event_id: int,
        bookmakers: Optional[List[str]] = None,
        *,
        include_seq: bool = False,
    ) -> Dict[str, Any]:
        """GET /odds — full odds payload for one event.

        ``include_seq=true`` adds ``includeSeq=true`` and records ``X-OddsAPI-Seq``
        on ``self.last_seq`` for WebSocket ``lastSeq`` handoff. Skips TTL cache.
        """
        books = bookmakers or self.bookmakers
        bms = ",".join(_bookmaker_for_odds_request(b) for b in books)
        key = f"odds:{event_id}:{bms}"
        if not include_seq:
            cached = await self._cache_odds.get_valid(key)
            if cached is not None:
                return cached
        params: Dict[str, Any] = {"eventId": event_id, "bookmakers": bms}
        if include_seq:
            params["includeSeq"] = "true"
        data = await self._get_json(
            "/odds",
            params,
            cache=None if include_seq else self._cache_odds,
            cache_key=None if include_seq else key,
            ttl=0.0 if include_seq else self._odds_ttl,
        )
        return data if isinstance(data, dict) else {}

    async def _get_odds_multi_one_slice(
        self,
        ids: str,
        books_slice: List[str],
        *,
        cache_ttl: Optional[float] = None,
        include_seq: bool = False,
    ) -> List[Dict[str, Any]]:
        """Single /odds/multi HTTP for one event-id batch and one bookmaker slice (cached per slice)."""
        if not books_slice:
            return []
        eff_ttl = 0.0 if include_seq else (self._odds_ttl if cache_ttl is None else float(cache_ttl))
        books_canon = [_canonical_odds_api_bookmaker(b) for b in books_slice]
        books_http = [_bookmaker_for_odds_request(b) for b in books_canon]
        bms = ",".join(books_http)
        key = f"multi:{ids}:{bms}"
        if eff_ttl > 0:
            cached = await self._cache_odds.get_valid(key)
            if cached is not None:
                docs = _as_odds_multi_list(cached)
                if len(books_canon) == 1:
                    _rekey_bookmakers_to_configured_name(docs, books_canon[0])
                return docs
        status, data = await self._odds_multi_http(ids, bms, include_seq=include_seq)
        if status == 403 and isinstance(data, str):
            allowed = _books_from_odds_api_403_error(data)
            if allowed:
                sub = _subset_bookmakers_for_api(books_http, allowed)
                if sub:
                    bms2 = ",".join(sub)
                    if bms2 != bms:
                        bms = bms2
                        key = f"multi:{ids}:{bms}"
                        status, data = await self._odds_multi_http(ids, bms, include_seq=include_seq)
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
        if eff_ttl > 0:
            await self._cache_odds.set(key, data, eff_ttl)
        docs = _as_odds_multi_list(data)
        if len(books_canon) == 1:
            _rekey_bookmakers_to_configured_name(docs, books_canon[0])
        return docs

    async def get_odds_multi(
        self,
        event_ids: List[int],
        bookmakers: Optional[List[str]] = None,
        *,
        odds_cache_ttl: Optional[float] = None,
        include_seq: bool = False,
    ) -> List[Dict[str, Any]]:
        """GET /odds/multi — up to 10 event ids per HTTP call.

        When ``ODDS_API_MULTI_PARALLEL_BOOKS`` is true (default), fetches **one bookmaker per request**
        in parallel for the same ``eventIds`` and **merges** ``bookmakers`` by event id. Odds-API.io
        often omits most books if you pass all 10 in a single ``bookmakers=`` param; per-book calls
        return full lines for each book so the live grid and devig see all configured books together.
        Set ``ODDS_API_MULTI_PARALLEL_BOOKS=false`` to send one request with every book (legacy).

        ``odds_cache_ttl``: per-request cache TTL for these slices. ``None`` uses ``ODDS_API_ODDS_TTL_SEC``.
        Monitors pass ``_live_odds_multi_ttl`` (default 0) so consecutive polls do not reuse stale merged books.
        ``include_seq``: REST→WS handoff / resync — send ``includeSeq=true`` and read ``X-OddsAPI-Seq``.
        """
        if not event_ids:
            return []
        eff_multi_ttl = 0.0 if include_seq else (self._odds_ttl if odds_cache_ttl is None else float(odds_cache_ttl))
        books = api_wire_bookmakers(
            [_canonical_odds_api_bookmaker(b) for b in (bookmakers or self.bookmakers)]
        )
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
                out.extend(
                    await self._get_odds_multi_one_slice(
                        ids, books, cache_ttl=eff_multi_ttl, include_seq=include_seq
                    )
                )
                continue
            sem = asyncio.Semaphore(par_lim)

            async def _one_book(b: str) -> List[Dict[str, Any]]:
                async with sem:
                    try:
                        return await self._get_odds_multi_one_slice(
                            ids, [b], cache_ttl=eff_multi_ttl, include_seq=include_seq
                        )
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

    async def list_live_events(
        self,
        sport: Optional[str] = None,
        *,
        force_refresh: bool = False,
    ) -> List[Dict[str, Any]]:
        """GET /events/live — docs: optional ``sport`` filter (API sport slug).

        ``force_refresh``: drop cache entry first (e.g. right before first pitch); default uses
        ``ODDS_API_LIVE_EVENTS_TTL_SEC`` (long slate refresh independent of line poll).
        """
        api_s: Optional[str] = None
        if sport and str(sport).strip().lower() not in ("", "all"):
            api_s = sport_slug_query_for_api(str(sport))
        key = f"events:live:{api_s or 'all'}"
        if force_refresh:
            await self._cache_events.invalidate(key)
        cached = await self._cache_events.get_valid(key)
        if cached is not None:
            return cached
        stale = await self._cache_events.peek(key)
        params: Dict[str, Any] = {}
        if api_s:
            params["sport"] = api_s
        try:
            data = await self._get_json(
                "/events/live",
                params,
                cache=self._cache_events,
                cache_key=key,
                ttl=self._live_events_ttl,
            )
        except Exception as ex:
            if isinstance(stale, list) and stale:
                print(
                    f"[ODDS-API] [WARN] /events/live failed ({ex}); "
                    f"using cached slate n={len(stale)}"
                )
                return stale
            raise
        return data if isinstance(data, list) else []

    async def peek_cached_live_events(
        self,
        sport: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Last ``/events/live`` slate, including an expired cache entry."""
        api_s: Optional[str] = None
        if sport and str(sport).strip().lower() not in ("", "all"):
            api_s = sport_slug_query_for_api(str(sport))
        key = f"events:live:{api_s or 'all'}"
        cached = await self._cache_events.peek(key)
        return list(cached) if isinstance(cached, list) else []

    async def get_odds_updated(
        self,
        since: int,
        bookmaker: str,
        sport: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """GET /odds/updated — odds changed since UNIX ``since`` (must be ≤90s old).

        One bookmaker per call. Prefer this over full ``/odds/multi`` when the
        WebSocket is down and you already have a snapshot to patch.

        ``sport`` is required by Odds-API.io and must be the display name from
        GET /sports (e.g. ``Baseball``), not the slug. Slugs and sport objects
        are coerced via ``sport_name_for_odds_updated``.
        """
        bm = _bookmaker_for_odds_request(_canonical_odds_api_bookmaker(bookmaker))
        sport_name = sport_name_for_odds_updated(sport)
        if not sport_name:
            raise ValueError(
                "GET /odds/updated requires sport (display name from /sports, e.g. 'Baseball')"
            )
        params: Dict[str, Any] = {
            "since": int(since),
            "bookmaker": bm,
            "sport": sport_name,
        }
        data = await self._get_json("/odds/updated", params)
        docs = _as_odds_multi_list(data)
        if docs:
            _rekey_bookmakers_to_configured_name(docs, _canonical_odds_api_bookmaker(bookmaker))
            return docs
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict) and data.get("id") is not None:
            wrapped = [data]
            _rekey_bookmakers_to_configured_name(wrapped, _canonical_odds_api_bookmaker(bookmaker))
            return wrapped
        return []

    async def get_selected_bookmakers(self) -> List[str]:
        """GET /bookmakers/selected — account dashboard / selected list."""
        data = await self._get_json("/bookmakers/selected", {})
        if isinstance(data, list):
            return [_canonical_odds_api_bookmaker(str(x)) for x in data if str(x).strip()]
        if isinstance(data, dict):
            for key in ("bookmakers", "selected", "data", "names"):
                v = data.get(key)
                if isinstance(v, list):
                    return [_canonical_odds_api_bookmaker(str(x)) for x in v if str(x).strip()]
        return []

    async def select_bookmakers(self, names: Optional[List[str]] = None) -> Any:
        """PUT /bookmakers/selected/select — set the account selected list (WS uses this)."""
        if not self.api_key:
            raise RuntimeError("ODDS_API_KEY is not set")
        books = api_wire_bookmakers(names or self.bookmakers)
        bms = ",".join(_bookmaker_for_odds_request(b) for b in books)
        await self._rate_limit()
        sess = await self._ensure_session()
        q = {"bookmakers": bms, "apiKey": self.api_key}
        url = f"{self.base_url}/bookmakers/selected/select?{urlencode(q)}"
        self.http_request_count += 1
        async with sess.put(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 429:
                await asyncio.sleep(2.0)
                return await self.select_bookmakers(names)
            resp.raise_for_status()
            try:
                return await resp.json()
            except Exception:
                return await resp.text()


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
