"""Public Kalshi market listing → Odds-API take-book attach.

Odds-API supplies the rec pack and, for Kalshi, an **event** ticker
(``bookmakerIds.Kalshi`` / ``urls.Kalshi`` / WS ``url``). That is enough
for handle_alert → find_submarket. This module is the fallback / enricher
for **market-level** KX… (ceil suffix) and public YES-ask depth when the
Odds-API row has no href. Attach=0 must not leave every card as paper
``KALSHI|…`` if Odds-API already named the event.

Private-key credentials are not used here. Orders still require a key.
Fail-closed: zero or two-plus event matches, swapped/ambiguous teams,
or a missing ask → no attach. Fresh Odds-API Kalshi **with a real KX
ticker** is kept. Tickerless / paper Odds-API Kalshi is not "already
priced" — public attach may paint executable tickers. Stale Odds-API
Kalshi is overwritten and stamped now. PLive is untouched.

Date-only CFB suffixes (``26SEP05MICHOSU``, midnight UTC) are matched
by US slate day, not the 18h clock window used when the suffix has a
kickoff time. Team identity is subset + school-qualifier leftover, not
any shared token (``State`` / ``Texas`` / ``Michigan``).
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from ev_calculator import LIVE_REC_POWER_MAX_AGE_SEC, LIVE_TAKE_MAX_AGE_SEC
from execution_guard import (
    event_ticker_from_any,
    kalshi_line_int,
    market_floor_strike_matches_alert,
    parse_kalshi_ticker,
)
from plive_pandora import _TEAM_STOPWORDS, _norm_team, _team_identity_tokens, odds_event_start_unix

KALSHI_BASE = "https://api.elections.kalshi.com"
KALSHI_MARKETS_PATH = "/trade-api/v2/markets"
KALSHI_SERIES_PATH = "/trade-api/v2/series"
KALSHI_HREF = "https://kalshi.com/markets/{ticker}"

# Open take cards need the executable YES ask every poll. 45s froze Celta -179.
CACHE_TTL_SEC = float(os.getenv("KALSHI_PUBLIC_FEED_TTL_SEC", "2") or "2")
SOCCER_SERIES_TTL_SEC = float(os.getenv("KALSHI_SOCCER_SERIES_TTL_SEC", "300") or "300")
START_TOLERANCE_SEC = int(os.getenv("KALSHI_PUBLIC_START_TOLERANCE_SEC", "64800") or "64800")
MAX_PAGES = 8
PAGE_LIMIT = 200

_SOCCER_SERIES_STOP = frozenset(
    {
        "game",
        "total",
        "totals",
        "goals",
        "point",
        "the",
        "of",
        "and",
        "fc",
        "soccer",
        "football",
        "winner",
    }
)
_SOCCER_LEAGUE_HINTS = (
    "soccer",
    "premier league",
    "la liga",
    "laliga",
    "ligue 1",
    "ligue 2",
    "serie a",
    "bundesliga",
    "scotland",
    "scottish",
    "eredivisie",
    "mls",
    "uefa",
    "champions league",
    "europa league",
    "conference league",
    "liga 1",
    "serie b",
    "jupiler",
    "pro league",
    "primeira",
    "brasileiro",
    "ligue1",
)

_MONTH = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

_SERIES_BY_SPORT = {
    "mlb": ("KXMLBGAME", "KXMLBSPREAD", "KXMLBTOTAL"),
    "nba": ("KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL"),
    "nhl": ("KXNHLGAME", "KXNHLSPREAD", "KXNHLTOTAL"),
    "nfl": ("KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL"),
    "ncaab": ("KXNCAAMBGAME", "KXNCAAMBSPREAD", "KXNCAAMBTOTAL"),
    "ncaaf": ("KXNCAAFGAME", "KXNCAAFSPREAD", "KXNCAAFTOTAL"),
    "wnba": ("KXWNBAGAME", "KXWNBASPREAD", "KXWNBATOTAL"),
}

_SUFFIX_RE = re.compile(
    r"^(?P<date>\d{2}[A-Z]{3}\d{2})(?P<time>\d{4})?(?P<codes>[A-Z]+)$"
)

# School tokens that distinguish Texas vs Texas State, Michigan vs
# Western Michigan. Leftover qualifier → different school, not a match.
_SCHOOL_QUALIFIERS = frozenset(
    {
        "state",
        "tech",
        "university",
        "univ",
        "college",
        "international",
        "atlantic",
        "southern",
        "northern",
        "eastern",
        "western",
        "central",
        "am",
        "a&m",
        "poly",
        "christian",
        "baptist",
        "a",
        "m",
    }
)

_cache_lock: Optional[asyncio.Lock] = None
_cache: Dict[str, Any] = {"ts": 0.0, "key": "", "markets": []}
_soccer_series_cache: Dict[str, Any] = {"ts": 0.0, "series": []}


def _lock() -> asyncio.Lock:
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


def commit_fetched_markets(key: str, markets: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Write the public-market cache. Empty refresh keeps the last good list."""
    got = [m for m in (markets or []) if isinstance(m, dict)]
    if got:
        _cache["ts"] = time.time()
        _cache["key"] = key
        _cache["markets"] = list(got)
        return list(got)
    prev = list(_cache.get("markets") or [])
    if prev:
        print(
            f"[KALSHI PUBLIC] empty refresh kept {len(prev)} cached market(s) "
            f"(failed or empty list for {key})"
        )
        _cache["ts"] = time.time()
        _cache["key"] = key
        return prev
    _cache["ts"] = time.time()
    _cache["key"] = key
    _cache["markets"] = []
    return []


def market_href(ticker: str) -> str:
    return KALSHI_HREF.format(ticker=str(ticker or "").strip().upper())


def _as_docs(docs: Union[Dict[Any, Dict[str, Any]], Sequence[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if isinstance(docs, dict):
        return [d for d in docs.values() if isinstance(d, dict)]
    return [d for d in (docs or []) if isinstance(d, dict)]


def _league_blob(doc: Dict[str, Any]) -> str:
    """League + sport text. Include name and slug so usa-college is visible."""
    parts: List[str] = []
    for key in ("league", "league_slug", "sport", "sport_slug"):
        val = doc.get(key)
        if isinstance(val, dict):
            parts.extend(str(val.get(x) or "") for x in ("name", "slug"))
        elif val:
            parts.append(str(val))
    return " ".join(parts).lower()


def _sport_slug_from_doc(doc: Dict[str, Any]) -> str:
    for key in ("sport", "sport_slug"):
        sp = doc.get(key)
        if isinstance(sp, dict):
            return str(sp.get("slug") or sp.get("name") or "").lower()
        if sp:
            return str(sp).lower()
    return ""


def _compact_blob(blob: str) -> str:
    return re.sub(r"[\s_\-]+", " ", str(blob or "").lower()).strip()


def _is_american_football(doc: Dict[str, Any], blob: str = "") -> bool:
    blob = blob or _league_blob(doc)
    if any(x in blob for x in ("american-football", "american football", "americanfootball")):
        return True
    slug = _sport_slug_from_doc(doc).replace("_", "-")
    return slug in ("american-football", "americanfootball")


def _college_hint(blob: str) -> bool:
    """Odds-API CFB/CBB often says USA - College / usa-college, not NCAAF."""
    compact = _compact_blob(blob)
    if any(tok in compact for tok in ("ncaaf", "ncaab", "ncaamb", "usa ncaaf", "usa ncaab")):
        return True
    if re.search(r"\bcfb\b", compact):
        return True
    if re.search(r"\bncaa\b", compact):
        return True
    if "usa college" in compact:
        return True
    return bool(re.search(r"\bcollege\b", compact))


def _doc_is_soccer(doc: Dict[str, Any], blob: str = "") -> bool:
    """Odds-API soccer is sport=football. Never classify MLB/NFL as soccer."""
    blob = blob or _league_blob(doc)
    if any(
        x in blob
        for x in (
            "american-football",
            "american football",
            "ncaaf",
            "ncaab",
            "college football",
            "college basketball",
            "cfb",
        )
    ):
        return False
    if _is_american_football(doc, blob):
        return False
    if any(x in blob for x in ("mlb", "baseball", "nba", "nhl", "wnba")):
        return False
    # Bare "nfl" after american-football check. "nfl" is not in soccer league names.
    if re.search(r"\bnfl\b", blob):
        return False
    slug = _sport_slug_from_doc(doc).replace("_", "-")
    if slug in ("american-football", "americanfootball"):
        return False
    if slug in ("football", "soccer"):
        return True
    return any(h in blob for h in _SOCCER_LEAGUE_HINTS)


def sport_key_for_doc(doc: Dict[str, Any]) -> Optional[str]:
    """Map an Odds-API event onto a Kalshi series family.

    ``american-football`` alone is NFL. College (usa-college / NCAAF / CFB)
    must win first or CFB docs pull KXNFL* and public attach stays 0.
    """
    blob = _league_blob(doc)
    college = _college_hint(blob)
    am_fb = _is_american_football(doc, blob)
    if "ncaab" in blob or "ncaamb" in blob or "college basketball" in blob:
        return "ncaab"
    if college and "basketball" in blob and not am_fb:
        return "ncaab"
    if "ncaaf" in blob or "college football" in blob or re.search(r"\bcfb\b", _compact_blob(blob)):
        return "ncaaf"
    if college and am_fb:
        return "ncaaf"
    if "wnba" in blob:
        return "wnba"
    if "mlb" in blob or "baseball" in blob:
        return "mlb"
    if "nba" in blob or ("basketball" in blob and "college" not in blob):
        return "nba"
    if "nhl" in blob or "hockey" in blob:
        return "nhl"
    if "nfl" in blob or am_fb:
        return "nfl"
    if _doc_is_soccer(doc, blob):
        return "soccer"
    return None


def series_for_docs(docs: Union[Dict[Any, Dict[str, Any]], Sequence[Dict[str, Any]]]) -> List[str]:
    needed: Set[str] = set()
    soccer_docs: List[Dict[str, Any]] = []
    for doc in _as_docs(docs):
        key = sport_key_for_doc(doc)
        if key == "soccer":
            soccer_docs.append(doc)
        elif key and key in _SERIES_BY_SPORT:
            needed.update(_SERIES_BY_SPORT[key])
    if soccer_docs:
        cached = list(_soccer_series_cache.get("series") or [])
        if cached:
            needed.update(soccer_series_for_docs(soccer_docs, cached))
    return sorted(needed)


def _series_match_tokens(text: str) -> Set[str]:
    raw = re.findall(r"[a-z0-9]+", str(text or "").lower())
    return {t for t in raw if t not in _SOCCER_SERIES_STOP and len(t) >= 2}


def _tok_hit(left: str, right: str) -> bool:
    if left == right:
        return True
    if len(left) >= 4 and len(right) >= 4:
        if left.startswith(right) or right.startswith(left) or left[:4] == right[:4]:
            return True
    return False


def _token_overlap_score(league_tokens: Set[str], series_tokens: Set[str]) -> int:
    score = 0
    used_b: Set[str] = set()
    for a in league_tokens:
        for b in series_tokens:
            if b in used_b:
                continue
            if _tok_hit(a, b):
                score += 1
                used_b.add(b)
                break
    return score


def is_soccer_gameline_series(ticker: str, tags: Any = None) -> bool:
    """Public unsigned catalog: Soccer tag + GAME/TOTAL. No guessed tickers."""
    t = str(ticker or "").strip().upper()
    tag_list = tags if isinstance(tags, (list, tuple, set)) else ([tags] if tags else [])
    tags_l = [str(x).lower() for x in tag_list]
    if "soccer" not in tags_l:
        return False
    if any(bad in t for bad in ("TEAMTOTAL", "1HTOTAL", "2HTOTAL", "BTTS", "FTTS")):
        return False
    if "1H" in t or "2H" in t:
        return False
    return t.endswith("GAME") or t.endswith("TOTAL")


def filter_soccer_gameline_catalog(series: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for s in series or []:
        if not isinstance(s, dict):
            continue
        ticker = str(s.get("ticker") or s.get("series_ticker") or "").strip().upper()
        if not ticker or ticker in seen:
            continue
        if not is_soccer_gameline_series(ticker, s.get("tags")):
            continue
        seen.add(ticker)
        out.append({**s, "ticker": ticker})
    return out


def soccer_series_for_docs(
    docs: Union[Dict[Any, Dict[str, Any]], Sequence[Dict[str, Any]]],
    catalog: Sequence[Dict[str, Any]],
) -> List[str]:
    """League-matched GAME + TOTAL from a discovered catalog. Ambiguous → omit."""
    filtered = filter_soccer_gameline_catalog(catalog)
    if not filtered:
        return []
    needed: Set[str] = set()
    for doc in _as_docs(docs):
        if sport_key_for_doc(doc) != "soccer":
            continue
        league = ""
        lg = doc.get("league")
        if isinstance(lg, dict):
            league = str(lg.get("name") or lg.get("slug") or "")
        else:
            league = str(lg or "")
        picked = _unique_soccer_series_for_league(league, filtered)
        needed.update(picked)
    return sorted(needed)


def _unique_soccer_series_for_league(
    league: str, catalog: Sequence[Dict[str, Any]]
) -> List[str]:
    """Fail-closed: 0 or 2+ equally good GAME matches → no series for this league."""
    league_tokens = _series_match_tokens(league)
    if not league_tokens:
        return []
    scored: List[Tuple[int, str, str]] = []
    for s in catalog:
        ticker = str(s.get("ticker") or "").upper()
        if not ticker.endswith("GAME"):
            continue
        title = str(s.get("title") or "")
        series_tokens = _series_match_tokens(f"{ticker} {title}")
        score = _token_overlap_score(league_tokens, series_tokens)
        if score <= 0:
            continue
        if score == 1:
            # Single short token (e.g. "liga") is not enough.
            distinctive = any(len(t) >= 5 for t in league_tokens)
            if not distinctive:
                continue
        scored.append((score, ticker, title))
    if not scored:
        return []
    best = max(s[0] for s in scored)
    winners = [t for sc, t, _title in scored if sc == best]
    if len(set(winners)) != 1:
        return []
    game = winners[0]
    out = [game]
    prefix = game[: -len("GAME")] if game.endswith("GAME") else game
    total = f"{prefix}TOTAL"
    if any(str(s.get("ticker") or "").upper() == total for s in catalog):
        out.append(total)
    return out


def _ask_prob(market: Dict[str, Any], side: str) -> Optional[float]:
    side = "yes" if str(side).lower() == "yes" else "no"
    dollars = market.get(f"{side}_ask_dollars")
    if dollars is not None:
        try:
            f = float(dollars)
            if 0.0 < f < 1.0:
                return f
        except (TypeError, ValueError):
            pass
    cents = market.get(f"{side}_ask")
    if cents is not None:
        try:
            f = float(cents)
            if 1.0 <= f <= 99.0:
                return f / 100.0
            if 0.0 < f < 1.0:
                return f
        except (TypeError, ValueError):
            pass
    return None


def decimal_from_ask(prob: Optional[float]) -> Optional[float]:
    if prob is None:
        return None
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return None
    if p <= 0.0 or p >= 1.0:
        return None
    return round(1.0 / p, 6)


def _floor_strike(market: Dict[str, Any]) -> Optional[float]:
    for key in ("floor_strike", "floor_strike_dollars", "strike", "yes_strike"):
        raw = market.get(key)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    ticker = str(market.get("ticker") or "").upper()
    parts = ticker.split("-")
    if len(parts) >= 3:
        m = re.search(r"(\d+)$", parts[-1])
        if m:
            n = int(m.group(1))
            # Suffix is ceil(|line|) (#29): 39 → 38.5. Never invent 1.75 / 1.25.
            return float(n) - 0.5
    return None


_WINS_BY_RE = re.compile(r"^(?P<team>.+?)\s+wins by over\b", re.I)


def _title(market: Dict[str, Any]) -> str:
    for key in ("yes_sub_title", "yes_subtitle", "subtitle", "title"):
        val = market.get(key)
        if val:
            return str(val)
    return ""


def _team_from_title(title: str) -> str:
    """'Iowa wins by over 27.5 points' → 'Iowa'. Bare 'Iowa' stays."""
    raw = str(title or "").strip()
    m = _WINS_BY_RE.match(raw)
    if m:
        return m.group("team").strip()
    return raw


def _market_team_title(market: Dict[str, Any]) -> str:
    return _team_from_title(_title(market))


def _family(market: Dict[str, Any]) -> str:
    ticker = str(market.get("ticker") or market.get("event_ticker") or "").upper()
    series = str(market.get("series_ticker") or "").upper()
    blob = f"{ticker} {series}"
    if "SPREAD" in blob:
        return "spread"
    if "TOTAL" in blob:
        return "total"
    return "moneyline"


def _event_ticker(market: Dict[str, Any]) -> str:
    """Normalize GAME/SPREAD/TOTAL tickers onto one GAME event key."""
    for raw in (market.get("event_ticker"), market.get("ticker")):
        ev = event_ticker_from_any(raw)
        if ev:
            return ev
    ev = str(market.get("event_ticker") or "").strip().upper()
    if ev:
        return ev
    ticker = str(market.get("ticker") or "").strip().upper()
    parts = ticker.split("-")
    if len(parts) >= 2:
        return f"{parts[0]}-{parts[1]}"
    return ticker


def parse_event_suffix(event_ticker: str) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """Return (start_unix, code_a, code_b) from ``SERIES-26SEP03DETMINE`` / ``…2140ATHSEA``."""
    parts = str(event_ticker or "").upper().split("-")
    if len(parts) < 2:
        return None, None, None
    m = _SUFFIX_RE.match(parts[1])
    if not m:
        return None, None, None
    date_s = m.group("date")
    time_s = m.group("time") or ""
    codes = m.group("codes") or ""
    start = None
    try:
        yy = int(date_s[:2])
        mon = _MONTH.get(date_s[2:5])
        dd = int(date_s[5:7])
        if mon and dd:
            from datetime import date, datetime, timedelta, timezone

            hh = int(time_s[:2]) if len(time_s) == 4 else 0
            mm = int(time_s[2:4]) if len(time_s) == 4 else 0
            year = 2000 + yy if yy < 80 else 1900 + yy
            start = int(datetime(year, mon, dd, hh, mm, tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError):
        start = None
    code_a = code_b = None
    if len(codes) >= 6 and len(codes) % 2 == 0:
        half = len(codes) // 2
        code_a, code_b = codes[:half], codes[half:]
    elif len(codes) == 6:
        code_a, code_b = codes[:3], codes[3:]
    return start, code_a, code_b


def _code_matches_team(code: str, team: str) -> bool:
    c = str(code or "").lower()
    if len(c) < 2:
        return False
    compact = _norm_team(team).replace(" ", "")
    if c in compact:
        return True
    tokens = _team_identity_tokens(team)
    return any(t.startswith(c) or c.startswith(t[: len(c)]) for t in tokens if t)


def _kalshi_norm_phrase(s: str) -> str:
    """Expand St./St → state so Kalshi 'Penn St.' matches 'Penn State'."""
    t = _norm_team(s)
    t = re.sub(r"\bst\.?\b", "state", t)
    t = t.replace("&", " ")
    return " ".join(t.split())


def _kalshi_team_tokens(s: str) -> Set[str]:
    return {
        w
        for w in _kalshi_norm_phrase(s).split()
        if w and len(w) >= 2 and w not in _TEAM_STOPWORDS
    }


def _team_match_score(odds_name: str, kalshi_name: str) -> Optional[int]:
    """Subset identity. Leftover school qualifier (State/Tech/Western) → None.

    Any-token overlap (``State``, ``Texas``, ``Michigan``) is not a match —
    that turned a Saturday NCAAF catalog into 2+ hits and attach 0.
    """
    ot = _kalshi_team_tokens(odds_name)
    kt = _kalshi_team_tokens(kalshi_name)
    if not ot or not kt:
        return None
    if ot == kt:
        return 200 + len(ot)
    if kt <= ot:
        leftover = ot - kt
        if leftover & _SCHOOL_QUALIFIERS:
            return None
        return 80 + len(kt)
    if ot <= kt:
        leftover = kt - ot
        if leftover & _SCHOOL_QUALIFIERS:
            return None
        return 80 + len(ot)
    return None


def tokens_match_team(odds_name: str, kalshi_name: str) -> bool:
    return _team_match_score(odds_name, kalshi_name) is not None


def _is_draw_title(title: str) -> bool:
    t = re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()
    if not t:
        return False
    return t in {"draw", "tie", "x", "draw tie", "tie draw"} or t.endswith(" draw")


def assign_kalshi_title_side(title: str, odds_home: str, odds_away: str) -> Optional[str]:
    """Map a Kalshi yes-title onto odds home/away/draw. Ambiguous → None."""
    if _is_draw_title(title):
        return "draw"
    hit_h = _team_match_score(odds_home, title)
    hit_a = _team_match_score(odds_away, title)
    if hit_h and hit_a:
        if hit_h == hit_a:
            return None
        return "home" if hit_h > hit_a else "away"
    if hit_h:
        return "home"
    if hit_a:
        return "away"
    return None


def _stamp_epoch_seconds(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v != v or v <= 0:
            return None
        return v / 1000.0 if v > 1e12 else v
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return float(parsed.timestamp())


def _kalshi_book_list(doc: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    bks = doc.get("bookmakers")
    if not isinstance(bks, dict):
        return None
    for key, val in bks.items():
        if str(key).strip().lower() == "kalshi":
            return val if isinstance(val, list) else None
    return None


def kalshi_has_priced_decimal(doc: Dict[str, Any]) -> bool:
    raw = _kalshi_book_list(doc)
    if not raw:
        return False
    for mk in raw:
        if not isinstance(mk, dict):
            continue
        for row in mk.get("odds") or []:
            if not isinstance(row, dict):
                continue
            for side in ("home", "away", "draw", "over", "under"):
                try:
                    if float(row.get(side)) > 1.0:
                        return True
                except (TypeError, ValueError):
                    continue
    return False


def kalshi_book_stamp(doc: Dict[str, Any]) -> Any:
    stamps = doc.get("book_updated_at")
    if isinstance(stamps, dict):
        for key, val in stamps.items():
            if str(key).strip().lower() == "kalshi" and val is not None and val != "":
                return val
    raw = _kalshi_book_list(doc) or []
    for mk in raw:
        if not isinstance(mk, dict):
            continue
        for row in mk.get("odds") or []:
            if not isinstance(row, dict):
                continue
            ts = row.get("book_updated_at") or row.get("updated_at")
            if ts is not None and ts != "":
                return ts
    return None


def kalshi_row_has_real_kx(row: Dict[str, Any]) -> bool:
    """True when an odds row already carries an executable KX ticker or href."""
    if not isinstance(row, dict):
        return False
    keys = (
        "ticker",
        "home_ticker",
        "away_ticker",
        "draw_ticker",
        "over_ticker",
        "under_ticker",
    )
    href_keys = ("href", "home_href", "away_href", "draw_href", "over_href", "under_href")
    for key in keys:
        tok = str(row.get(key) or "").strip().upper()
        if tok.startswith("KX"):
            return True
    for key in href_keys:
        tok = _ticker_from_href(row.get(key))
        if tok.startswith("KX"):
            return True
    return False


def kalshi_doc_has_real_kx(doc: Dict[str, Any]) -> bool:
    return bool(kalshi_tickers_from_doc(doc))


def kalshi_already_priced(doc: Dict[str, Any], now: Optional[float] = None) -> bool:
    """True only for a priced Odds-API Kalshi quote that is still fresh.

    Missing price → False (public may attach). A stamp older than the take
    window on soccer live, or the 45s rec window otherwise, is stale and
    does not block the public YES ask. Unstamped fixtures stay priced.

    Fresh + priced does **not** require a KX ticker. Attach must still
    enrich tickerless rows instead of treating this as a skip.
    """
    if not kalshi_has_priced_decimal(doc):
        return False
    ts = kalshi_book_stamp(doc)
    if ts is None:
        return True
    epoch = _stamp_epoch_seconds(ts)
    if epoch is None:
        return False
    clock = float(now if now is not None else time.time())
    soccer_live = sport_key_for_doc(doc) == "soccer" and bool(doc.get("live"))
    max_age = LIVE_TAKE_MAX_AGE_SEC if soccer_live else LIVE_REC_POWER_MAX_AGE_SEC
    return (clock - epoch) <= float(max_age) + 1e-9


def stamp_kalshi_book(doc: Dict[str, Any], when: Optional[float] = None) -> float:
    """Mark the Kalshi take as the quote just written. Tile age follows this."""
    now = float(when if when is not None else time.time())
    stamps = doc.get("book_updated_at")
    if not isinstance(stamps, dict):
        stamps = {}
        doc["book_updated_at"] = stamps
    stamps["Kalshi"] = now
    raw = _kalshi_book_list(doc) or []
    for mk in raw:
        if not isinstance(mk, dict):
            continue
        for row in mk.get("odds") or []:
            if isinstance(row, dict):
                row["book_updated_at"] = now
    return now


def _volume(market: Dict[str, Any]) -> float:
    for key in ("volume_24h", "volume", "open_interest", "liquidity"):
        raw = market.get(key)
        try:
            if raw is not None:
                return float(raw)
        except (TypeError, ValueError):
            continue
    return 0.0


def _yes_midpoint_score(market: Dict[str, Any]) -> float:
    p = _ask_prob(market, "yes")
    if p is None:
        return 1.0
    return abs(p - 0.5)


def _pick_main(markets: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    priced = [m for m in markets if isinstance(m, dict) and _ask_prob(m, "yes") and _ask_prob(m, "no")]
    if not priced:
        return None
    return min(priced, key=lambda m: (_yes_midpoint_score(m), -_volume(m)))


def _group_events(markets: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    grouped: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(
        lambda: {"moneyline": [], "spread": [], "total": []}
    )
    for m in markets or []:
        if not isinstance(m, dict):
            continue
        status = str(m.get("status") or "open").lower()
        if status and status not in ("open", "active", "initialized"):
            continue
        ticker = str(m.get("ticker") or "").upper()
        if any(bad in ticker for bad in ("MULTIGAME", "EXTENDED", "PARLAY", "COMBO")):
            continue
        ev = _event_ticker(m)
        if not ev:
            continue
        grouped[ev][_family(m)].append(m)
    return grouped


def _codes_compatible(event_ticker: str, home: str, away: str) -> bool:
    _start, a, b = parse_event_suffix(event_ticker)
    if not a or not b:
        return True
    home_hit = _code_matches_team(a, home) or _code_matches_team(b, home)
    away_hit = _code_matches_team(a, away) or _code_matches_team(b, away)
    a_known = _code_matches_team(a, home) or _code_matches_team(a, away)
    b_known = _code_matches_team(b, home) or _code_matches_team(b, away)
    if a_known and b_known:
        return home_hit and away_hit
    return True


def _suffix_re_match(event_ticker: str):
    parts = str(event_ticker or "").upper().split("-")
    if len(parts) < 2:
        return None
    return _SUFFIX_RE.match(parts[1])


def _date_only_slate_ok(k_start: int, o_start: int) -> bool:
    """Date-only Kalshi suffixes are midnight UTC. Compare US slate day ±1.

    The 18h clock window rejects Saturday 3:30/7:00 ET CFB (19–23h after
    00:00 UTC) and was the live 'spike 1–2 then flat' on a busy slate.
    Next week (SEP12 vs SEP05) stays out.
    """
    from datetime import datetime, timezone

    try:
        from zoneinfo import ZoneInfo

        ny = ZoneInfo("America/New_York")
    except Exception:
        from datetime import timedelta

        ny = timezone(timedelta(hours=-4))
    k_date = datetime.fromtimestamp(int(k_start), tz=timezone.utc).date()
    o_date = datetime.fromtimestamp(int(o_start), tz=ny).date()
    return abs((k_date - o_date).days) <= 1


def _timing_ok(event_ticker: str, doc: Dict[str, Any]) -> bool:
    k_start, _a, _b = parse_event_suffix(event_ticker)
    o_start = odds_event_start_unix(doc)
    if k_start and o_start:
        m = _suffix_re_match(event_ticker)
        has_clock = bool(m and m.group("time"))
        if has_clock:
            return abs(int(k_start) - int(o_start)) <= max(0, START_TOLERANCE_SEC)
        return _date_only_slate_ok(int(k_start), int(o_start))
    return True


def _unique_title_sides(
    titles: Sequence[str], home: str, away: str
) -> Optional[Set[str]]:
    assigned: Set[str] = set()
    for title in titles:
        if not title or str(title).lower() in ("over", "under") or _is_draw_title(title):
            continue
        hit_h = _team_match_score(home, title)
        hit_a = _team_match_score(away, title)
        if hit_h and hit_a:
            if hit_h == hit_a:
                return None
            assigned.add("home" if hit_h > hit_a else "away")
        elif hit_h:
            assigned.add("home")
        elif hit_a:
            assigned.add("away")
    return assigned


def match_public_event(
    doc: Dict[str, Any],
    grouped: Dict[str, Dict[str, List[Dict[str, Any]]]],
) -> Optional[str]:
    """Conservative join. Same-orientation team identity. 0 or 2+ → None."""
    home = str(doc.get("home") or "")
    away = str(doc.get("away") or "")
    if not home or not away:
        return None
    hits: List[str] = []
    for ev, buckets in (grouped or {}).items():
        if not _codes_compatible(ev, home, away):
            continue
        if not _timing_ok(ev, doc):
            continue
        ml_titles = [_title(m) for m in (buckets.get("moneyline") or [])]
        spr_titles = [_market_team_title(m) for m in (buckets.get("spread") or [])]
        ml_sides = _unique_title_sides(ml_titles, home, away)
        if ml_sides == {"home", "away"}:
            hits.append(ev)
            continue
        all_sides = _unique_title_sides([*ml_titles, *spr_titles], home, away)
        if all_sides == {"home", "away"}:
            hits.append(ev)
            continue
        _start, code_a, code_b = parse_event_suffix(ev)
        codes_known = bool(
            code_a
            and code_b
            and (_code_matches_team(code_a, home) or _code_matches_team(code_a, away))
            and (_code_matches_team(code_b, home) or _code_matches_team(code_b, away))
            and _codes_compatible(ev, home, away)
        )
        one_side = ml_sides if ml_sides else all_sides
        if codes_known and one_side and len(one_side) == 1:
            hits.append(ev)
    if len(hits) != 1:
        return None
    return hits[0]


def _ml_row(
    markets: Sequence[Dict[str, Any]], home: str, away: str
) -> Optional[Dict[str, Any]]:
    home_m = away_m = draw_m = None
    for m in markets:
        side = assign_kalshi_title_side(_title(m), home, away)
        if side == "home":
            home_m = m
        elif side == "away":
            away_m = m
        elif side == "draw":
            draw_m = m
    home_dec = decimal_from_ask(_ask_prob(home_m, "yes")) if home_m else None
    away_dec = decimal_from_ask(_ask_prob(away_m, "yes")) if away_m else None
    draw_dec = decimal_from_ask(_ask_prob(draw_m, "yes")) if draw_m else None
    if home_dec is not None and away_dec is not None:
        ht = str(home_m.get("ticker") or "")
        at = str(away_m.get("ticker") or "")
        row: Dict[str, Any] = {
            "home": home_dec,
            "away": away_dec,
            "home_href": market_href(ht),
            "away_href": market_href(at),
            "href": market_href(ht),
            "ticker": ht,
            "away_ticker": at,
        }
        if draw_dec is not None:
            dt = str(draw_m.get("ticker") or "")
            row["draw"] = draw_dec
            row["draw_href"] = market_href(dt)
            row["draw_ticker"] = dt
        return row
    if draw_dec is not None:
        dt = str(draw_m.get("ticker") or "")
        href = market_href(dt)
        return {
            "draw": draw_dec,
            "draw_href": href,
            "draw_ticker": dt,
            "href": href,
            "ticker": dt,
        }
    return None


def _spread_row(market: Dict[str, Any], home: str, away: str) -> Optional[Dict[str, Any]]:
    strike = _floor_strike(market)
    if strike is None or strike <= 0:
        return None
    fav_side = assign_kalshi_title_side(_market_team_title(market), home, away)
    if fav_side is None:
        return None
    yes_dec = decimal_from_ask(_ask_prob(market, "yes"))
    no_dec = decimal_from_ask(_ask_prob(market, "no"))
    if yes_dec is None or no_dec is None:
        return None
    # Home-centric hdp. Favorite YES = that side covers. Away favorite → home gets points.
    hdp = -float(strike) if fav_side == "home" else float(strike)
    ticker = str(market.get("ticker") or "")
    href = market_href(ticker)
    if fav_side == "home":
        home_dec, away_dec = yes_dec, no_dec
    else:
        home_dec, away_dec = no_dec, yes_dec
    return {
        "hdp": hdp,
        "home": home_dec,
        "away": away_dec,
        "home_href": href,
        "away_href": href,
        "href": href,
        "ticker": ticker,
    }


def _total_row(market: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    line = _floor_strike(market)
    if line is None or line <= 0:
        return None
    over = decimal_from_ask(_ask_prob(market, "yes"))
    under = decimal_from_ask(_ask_prob(market, "no"))
    if over is None or under is None:
        return None
    ticker = str(market.get("ticker") or "")
    href = market_href(ticker)
    return {
        "hdp": line,
        "max": line,
        "line": line,
        "over": over,
        "under": under,
        "over_href": href,
        "under_href": href,
        "href": href,
        "ticker": ticker,
    }


def _numeric_line_close(a: Any, b: Any) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return False


def _market_matches_alert_line(market: Dict[str, Any], line: Any) -> bool:
    """Exact strike: ceil(|line|) suffix + floor_strike == |line|. Neighbor denied."""
    if not isinstance(market, dict) or line is None or line == "":
        return False
    try:
        mag = abs(float(line))
    except (TypeError, ValueError):
        return False
    parsed = parse_kalshi_ticker(market.get("ticker"))
    want = kalshi_line_int(mag)
    if parsed and parsed.line_int is not None and want is not None:
        if parsed.line_int != want:
            return False
        return market_floor_strike_matches_alert(market, mag)
    raw = market.get("floor_strike")
    if raw is None or raw == "":
        return False
    try:
        return abs(float(raw) - mag) < 1e-6
    except (TypeError, ValueError):
        return False


def _unique_strike_market(
    markets: Sequence[Dict[str, Any]],
    line: Any,
    home: str = "",
    away: str = "",
    *,
    kind: str = "total",
) -> Optional[Dict[str, Any]]:
    """Fail-closed: 0 or 2+ markets at this strike → None. Spread checks fav side."""
    if line is None or line == "":
        return None
    try:
        signed = float(line)
        mag = abs(signed)
    except (TypeError, ValueError):
        return None
    hits: List[Dict[str, Any]] = []
    for m in markets or []:
        if not isinstance(m, dict) or not _market_matches_alert_line(m, mag):
            continue
        if kind == "spread" and home and away:
            fav = assign_kalshi_title_side(_market_team_title(m), home, away)
            if fav is None:
                continue
            expected = "home" if signed < 0 else "away" if signed > 0 else None
            if expected and fav != expected:
                continue
        hits.append(m)
    if len(hits) != 1:
        return None
    return hits[0]


def _odds_rows_for_strikes(
    markets: Sequence[Dict[str, Any]],
    home: str,
    away: str,
    *,
    kind: str,
) -> List[Dict[str, Any]]:
    """Main line first, then remaining exact-strike alts (Iowa -27.5, not only -3.5)."""
    out: List[Dict[str, Any]] = []
    seen: List[float] = []
    ordered: List[Dict[str, Any]] = []
    main = _pick_main(markets)
    if main:
        ordered.append(main)
    for m in markets or []:
        if m is main or not isinstance(m, dict):
            continue
        ordered.append(m)
    builder = _spread_row if kind == "spread" else (lambda m, _h, _a: _total_row(m))
    for m in ordered:
        row = builder(m, home, away)
        if not row:
            continue
        key = abs(float(row.get("hdp") or row.get("line") or 0.0))
        if any(_numeric_line_close(key, s) for s in seen):
            continue
        seen.append(key)
        out.append(row)
        if len(out) >= 12:
            break
    return out


def book_from_event(
    buckets: Dict[str, List[Dict[str, Any]]],
    home: str,
    away: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    ml = _ml_row(buckets.get("moneyline") or [], home, away)
    if ml:
        out.append({"name": "ML", "odds": [ml]})
    spr_rows = _odds_rows_for_strikes(buckets.get("spread") or [], home, away, kind="spread")
    if spr_rows:
        out.append({"name": "Spread", "odds": spr_rows})
    tot_rows = _odds_rows_for_strikes(buckets.get("total") or [], home, away, kind="total")
    if tot_rows:
        out.append({"name": "Totals", "odds": tot_rows})
    return out


def _copy_ticker_fields(dst: Dict[str, Any], src: Dict[str, Any]) -> bool:
    changed = False
    for key in (
        "ticker",
        "href",
        "home_ticker",
        "away_ticker",
        "draw_ticker",
        "over_ticker",
        "under_ticker",
        "home_href",
        "away_href",
        "draw_href",
        "over_href",
        "under_href",
    ):
        val = src.get(key)
        if val and dst.get(key) != val:
            dst[key] = val
            changed = True
    return changed


def enrich_public_kalshi_hrefs(
    doc: Dict[str, Any],
    buckets: Dict[str, List[Dict[str, Any]]],
    now: Optional[float] = None,
) -> int:
    """Paint real KX hrefs onto existing Odds-API Kalshi rows. Keep decimals.

    Fresh tickerless last stays the take. Public catalog supplies identity
    only. Exact strike — neighbor / wrong fav denied.
    """
    raw = _kalshi_book_list(doc)
    if not raw:
        return 0
    home = str(doc.get("home") or "")
    away = str(doc.get("away") or "")
    painted = 0
    for mk in raw:
        if not isinstance(mk, dict):
            continue
        name = str(mk.get("name") or "").lower()
        if "spread" in name:
            family = "spread"
        elif "total" in name:
            family = "total"
        else:
            family = "moneyline"
        for row in mk.get("odds") or []:
            if not isinstance(row, dict) or kalshi_row_has_real_kx(row):
                continue
            if family == "moneyline":
                ml = _ml_row(buckets.get("moneyline") or [], home, away)
                if ml and _copy_ticker_fields(row, ml):
                    painted += 1
                continue
            if family == "spread":
                line = row.get("hdp")
                mkt = _unique_strike_market(
                    buckets.get("spread") or [], line, home, away, kind="spread"
                )
                built = _spread_row(mkt or {}, home, away) if mkt else None
                if built and _copy_ticker_fields(row, built):
                    painted += 1
                continue
            line = row.get("hdp") if row.get("hdp") is not None else row.get("max")
            if line is None:
                line = row.get("line")
            mkt = _unique_strike_market(
                buckets.get("total") or [], line, home, away, kind="total"
            )
            built = _total_row(mkt) if mkt else None
            if built and _copy_ticker_fields(row, built):
                painted += 1
    if painted:
        clock = float(now if now is not None else time.time())
        # Identity only — do not refresh book_updated_at (prices unchanged).
        _ = clock
    return painted


def _ticker_from_href(href: Any) -> str:
    raw = str(href or "").strip()
    if not raw:
        return ""
    parts = raw.rstrip("/").split("/")
    cand = parts[-1].upper() if parts else ""
    return cand if cand.startswith("KX") else ""


def kalshi_tickers_from_doc(doc: Dict[str, Any]) -> List[str]:
    found: List[str] = []
    seen: Set[str] = set()
    raw = _kalshi_book_list(doc) or []
    keys = (
        "ticker",
        "home_ticker",
        "away_ticker",
        "draw_ticker",
        "over_ticker",
        "under_ticker",
    )
    href_keys = ("href", "home_href", "away_href", "draw_href", "over_href", "under_href")
    for mk in raw:
        if not isinstance(mk, dict):
            continue
        for row in mk.get("odds") or []:
            if not isinstance(row, dict):
                continue
            for key in keys:
                tok = str(row.get(key) or "").strip().upper()
                if tok.startswith("KX") and tok not in seen:
                    seen.add(tok)
                    found.append(tok)
            for key in href_keys:
                tok = _ticker_from_href(row.get(key))
                if tok and tok not in seen:
                    seen.add(tok)
                    found.append(tok)
    return found


def apply_public_yes_asks(
    docs: Union[Dict[Any, Dict[str, Any]], Sequence[Dict[str, Any]]],
    markets: Sequence[Dict[str, Any]],
    now: Optional[float] = None,
) -> int:
    """Overwrite Kalshi decimals with the public YES ask when tickers match.

    Odds-API last is not the take. Executable ask is. Stamps ``book_updated_at``.
    Does not copy PLive. No ticker → no overlay (attach may still replace).
    """
    by_ticker: Dict[str, Dict[str, Any]] = {}
    for m in markets or []:
        if not isinstance(m, dict):
            continue
        tok = str(m.get("ticker") or "").strip().upper()
        if tok:
            by_ticker[tok] = m
    if not by_ticker:
        return 0
    clock = float(now if now is not None else time.time())
    updated = 0
    side_ticker = {
        "home": ("ticker", "home_ticker", "home_href", "href"),
        "away": ("away_ticker", "away_href"),
        "draw": ("draw_ticker", "draw_href"),
        "over": ("over_ticker", "ticker", "over_href", "href"),
        "under": ("under_ticker", "under_href"),
    }
    for doc in _as_docs(docs):
        raw = _kalshi_book_list(doc)
        if not raw:
            continue
        changed = False
        for mk in raw:
            if not isinstance(mk, dict):
                continue
            for row in mk.get("odds") or []:
                if not isinstance(row, dict):
                    continue
                for side, keys in side_ticker.items():
                    ticker = ""
                    for key in keys:
                        if key.endswith("href"):
                            ticker = _ticker_from_href(row.get(key))
                        else:
                            ticker = str(row.get(key) or "").strip().upper()
                        if ticker.startswith("KX"):
                            break
                        ticker = ""
                    if not ticker or ticker not in by_ticker:
                        continue
                    ask_side = "no" if side == "under" else "yes"
                    dec = decimal_from_ask(_ask_prob(by_ticker[ticker], ask_side))
                    if dec is None or dec <= 1.0:
                        continue
                    row[side] = dec
                    row["book_updated_at"] = clock
                    changed = True
        if changed:
            stamp_kalshi_book(doc, clock)
            updated += 1
    return updated


def attach_public_kalshi_markets(
    docs: Union[Dict[Any, Dict[str, Any]], Sequence[Dict[str, Any]]],
    markets: Sequence[Dict[str, Any]],
    now: Optional[float] = None,
) -> int:
    """Inject Odds-API-shaped ``bookmakers['Kalshi']`` from public markets. Mutates docs.

    Fresh priced Odds-API Kalshi **with** a real KX ticker is kept.
    Fresh priced **without** href/ticker is enriched (hrefs only; decimals stay).
    Stale or missing is replaced with the public YES ask and stamped now.
    WS-up does not skip this path.
    """
    grouped = _group_events(markets)
    clock = float(now if now is not None else time.time())
    attached = 0
    for doc in _as_docs(docs):
        ev = match_public_event(doc, grouped)
        if not ev:
            continue
        fresh = kalshi_already_priced(doc, now=clock)
        if fresh and kalshi_doc_has_real_kx(doc):
            # Still enrich any tickerless alt rows (Iowa -27.5 next to a KX main).
            n_en = enrich_public_kalshi_hrefs(doc, grouped[ev], now=clock)
            if n_en:
                attached += 1
            continue
        if fresh and kalshi_has_priced_decimal(doc):
            n_en = enrich_public_kalshi_hrefs(doc, grouped[ev], now=clock)
            if n_en:
                attached += 1
            continue
        book = book_from_event(
            grouped[ev],
            str(doc.get("home") or ""),
            str(doc.get("away") or ""),
        )
        if not book:
            continue
        bks = doc.get("bookmakers")
        if not isinstance(bks, dict):
            bks = {}
            doc["bookmakers"] = bks
        bks["Kalshi"] = book
        stamp_kalshi_book(doc, clock)
        attached += 1
    return attached


async def fetch_open_series_markets(
    series_tickers: Iterable[str],
    *,
    session: Any = None,
) -> List[Dict[str, Any]]:
    """Unsigned GET /markets?series_ticker=…&status=open. Cached."""
    series = [str(s).strip().upper() for s in series_tickers if str(s).strip()]
    if not series:
        return []
    key = ",".join(sorted(set(series)))
    now = time.time()
    async with _lock():
        if _cache.get("key") == key and (now - float(_cache.get("ts") or 0)) < CACHE_TTL_SEC:
            return list(_cache.get("markets") or [])

    import aiohttp

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    markets: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    try:
        for series_ticker in sorted(set(series)):
            cursor = None
            for _page in range(MAX_PAGES):
                params = {
                    "series_ticker": series_ticker,
                    "status": "open",
                    "limit": PAGE_LIMIT,
                }
                if cursor:
                    params["cursor"] = cursor
                try:
                    async with session.get(
                        f"{KALSHI_BASE}{KALSHI_MARKETS_PATH}",
                        params=params,
                    ) as resp:
                        if resp.status == 429:
                            await asyncio.sleep(2.0)
                            continue
                        if resp.status != 200:
                            print(
                                f"[KALSHI PUBLIC] list {series_ticker} status={resp.status}"
                            )
                            break
                        data = await resp.json()
                except Exception as ex:
                    print(f"[KALSHI PUBLIC] list {series_ticker} failed: {ex}")
                    break
                for m in data.get("markets") or []:
                    if not isinstance(m, dict):
                        continue
                    ticker = str(m.get("ticker") or "")
                    if ticker and ticker not in seen:
                        seen.add(ticker)
                        markets.append(m)
                cursor = data.get("cursor") or None
                if not cursor:
                    break
                await asyncio.sleep(0.15)
            await asyncio.sleep(0.15)
    finally:
        if own_session:
            await session.close()

    async with _lock():
        return commit_fetched_markets(key, markets)


async def fetch_soccer_series_catalog(
    *,
    session: Any = None,
) -> List[Dict[str, Any]]:
    """Unsigned GET /series. Keep real Soccer GAME/TOTAL tickers only."""
    now = time.time()
    async with _lock():
        if (now - float(_soccer_series_cache.get("ts") or 0)) < SOCCER_SERIES_TTL_SEC:
            return list(_soccer_series_cache.get("series") or [])

    import aiohttp

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    raw: List[Dict[str, Any]] = []
    try:
        cursor = None
        for _page in range(80):
            params: Dict[str, Any] = {"limit": 1000}
            if cursor:
                params["cursor"] = cursor
            try:
                async with session.get(
                    f"{KALSHI_BASE}{KALSHI_SERIES_PATH}",
                    params=params,
                ) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(2.0)
                        continue
                    if resp.status != 200:
                        print(f"[KALSHI PUBLIC] series list status={resp.status}")
                        break
                    data = await resp.json()
            except Exception as ex:
                print(f"[KALSHI PUBLIC] series list failed: {ex}")
                break
            page = data.get("series") or []
            for s in page:
                if isinstance(s, dict):
                    raw.append(s)
            cursor = data.get("cursor") or None
            if not cursor:
                break
            await asyncio.sleep(0.15)
    finally:
        if own_session:
            await session.close()

    filtered = filter_soccer_gameline_catalog(raw)
    async with _lock():
        _soccer_series_cache["ts"] = time.time()
        _soccer_series_cache["series"] = filtered
    return list(filtered)


async def attach_public_kalshi_to_docs(
    docs: Union[Dict[Any, Dict[str, Any]], Sequence[Dict[str, Any]]],
    markets: Optional[Sequence[Dict[str, Any]]] = None,
) -> int:
    """Attach public Kalshi take lines. Fetches when ``markets`` is omitted."""
    if markets is None:
        soccer_docs = [d for d in _as_docs(docs) if sport_key_for_doc(d) == "soccer"]
        if soccer_docs and not (_soccer_series_cache.get("series")):
            try:
                await fetch_soccer_series_catalog()
            except Exception as ex:
                print(f"[KALSHI PUBLIC] soccer series catalog failed: {ex}")
        series = series_for_docs(docs)
        if not series:
            return 0
        try:
            markets = await fetch_open_series_markets(series)
        except Exception as ex:
            print(f"[KALSHI PUBLIC] fetch failed: {ex}")
            return 0
    n = attach_public_kalshi_markets(docs, markets)
    n_ask = apply_public_yes_asks(docs, markets)
    n_docs = len(_as_docs(docs))
    n_kx = sum(1 for d in _as_docs(docs) if kalshi_doc_has_real_kx(d))
    print(
        f"[PIPELINE] Public Kalshi: attached take lines to {n} event(s) "
        f"yes_ask_refresh={n_ask} markets={len(markets or [])} "
        f"docs={n_docs} realKX={n_kx} "
        f"(no private key; tickerless Odds-API is enriched, not skipped)"
    )
    return n + n_ask
