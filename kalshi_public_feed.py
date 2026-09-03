"""Public Kalshi market listing → Odds-API take-book attach.

Odds-API supplies the rec pack. When that feed has no priced Kalshi
gameline, this module lists open GAME/SPREAD/TOTAL markets with an
unsigned GET and maps them onto Odds-API events.

Private-key credentials are not used here. Orders still require a key.
Fail-closed: zero or two-plus event matches, swapped/ambiguous teams,
or a missing ask → no attach. Existing priced Odds-API Kalshi is kept.
PLive-only docs may receive a Kalshi book; PLive itself is untouched.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from execution_guard import event_ticker_from_any
from plive_pandora import _norm_team, _team_identity_tokens, odds_event_start_unix

KALSHI_BASE = "https://api.elections.kalshi.com"
KALSHI_MARKETS_PATH = "/trade-api/v2/markets"
KALSHI_SERIES_PATH = "/trade-api/v2/series"
KALSHI_HREF = "https://kalshi.com/markets/{ticker}"

CACHE_TTL_SEC = float(os.getenv("KALSHI_PUBLIC_FEED_TTL_SEC", "45") or "45")
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

_cache_lock: Optional[asyncio.Lock] = None
_cache: Dict[str, Any] = {"ts": 0.0, "key": "", "markets": []}
_soccer_series_cache: Dict[str, Any] = {"ts": 0.0, "series": []}


def _lock() -> asyncio.Lock:
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


def market_href(ticker: str) -> str:
    return KALSHI_HREF.format(ticker=str(ticker or "").strip().upper())


def _as_docs(docs: Union[Dict[Any, Dict[str, Any]], Sequence[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if isinstance(docs, dict):
        return [d for d in docs.values() if isinstance(d, dict)]
    return [d for d in (docs or []) if isinstance(d, dict)]


def _league_blob(doc: Dict[str, Any]) -> str:
    lg = doc.get("league")
    if isinstance(lg, dict):
        league = str(lg.get("name") or lg.get("slug") or "")
    else:
        league = str(lg or "")
    sp = doc.get("sport") or doc.get("sport_slug")
    if isinstance(sp, dict):
        sport = str(sp.get("slug") or sp.get("name") or "")
    else:
        sport = str(sp or "")
    return f"{league} {sport}".lower()


def _sport_slug_from_doc(doc: Dict[str, Any]) -> str:
    for key in ("sport", "sport_slug"):
        sp = doc.get(key)
        if isinstance(sp, dict):
            return str(sp.get("slug") or sp.get("name") or "").lower()
        if sp:
            return str(sp).lower()
    return ""


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
        )
    ):
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
    blob = _league_blob(doc)
    if "ncaab" in blob or "ncaamb" in blob or "college basketball" in blob:
        return "ncaab"
    if "ncaaf" in blob or "college football" in blob:
        return "ncaaf"
    if "wnba" in blob:
        return "wnba"
    if "mlb" in blob or "baseball" in blob:
        return "mlb"
    if "nba" in blob or ("basketball" in blob and "college" not in blob):
        return "nba"
    if "nhl" in blob or "hockey" in blob:
        return "nhl"
    if "nfl" in blob or "american-football" in blob or "american football" in blob:
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
            # Kalshi totals are halves only (2 → 2.5). Never invent 1.75 / 1.25.
            return float(n) + 0.5
    return None


def _title(market: Dict[str, Any]) -> str:
    for key in ("yes_sub_title", "yes_subtitle", "subtitle", "title"):
        val = market.get(key)
        if val:
            return str(val)
    return ""


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
            from datetime import datetime, timezone

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


def tokens_match_team(odds_name: str, kalshi_name: str) -> bool:
    a = _team_identity_tokens(odds_name)
    b = _team_identity_tokens(kalshi_name)
    if not a or not b:
        return False
    if a == b or a <= b or b <= a:
        return True
    return bool(a & b)


def _is_draw_title(title: str) -> bool:
    t = re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()
    if not t:
        return False
    return t in {"draw", "tie", "x", "draw tie", "tie draw"} or t.endswith(" draw")


def assign_kalshi_title_side(title: str, odds_home: str, odds_away: str) -> Optional[str]:
    """Map a Kalshi yes-title onto odds home/away/draw. Ambiguous → None."""
    if _is_draw_title(title):
        return "draw"
    hit_h = tokens_match_team(odds_home, title)
    hit_a = tokens_match_team(odds_away, title)
    if hit_h and hit_a:
        return None
    if hit_h:
        return "home"
    if hit_a:
        return "away"
    return None


def kalshi_already_priced(doc: Dict[str, Any]) -> bool:
    bks = doc.get("bookmakers")
    if not isinstance(bks, dict):
        return False
    raw = None
    for key, val in bks.items():
        if str(key).strip().lower() == "kalshi":
            raw = val
            break
    if not isinstance(raw, list):
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


def _timing_ok(event_ticker: str, doc: Dict[str, Any]) -> bool:
    k_start, _a, _b = parse_event_suffix(event_ticker)
    o_start = odds_event_start_unix(doc)
    if k_start and o_start:
        return abs(int(k_start) - int(o_start)) <= max(0, START_TOLERANCE_SEC)
    return True


def _unique_title_sides(
    titles: Sequence[str], home: str, away: str
) -> Optional[Set[str]]:
    assigned: Set[str] = set()
    for title in titles:
        if not title or str(title).lower() in ("over", "under") or _is_draw_title(title):
            continue
        hit_h = tokens_match_team(home, title)
        hit_a = tokens_match_team(away, title)
        if hit_h and hit_a:
            return None
        if hit_h:
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
        spr_titles = [_title(m) for m in (buckets.get("spread") or [])]
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
    fav_side = assign_kalshi_title_side(_title(market), home, away)
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


def book_from_event(
    buckets: Dict[str, List[Dict[str, Any]]],
    home: str,
    away: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    ml = _ml_row(buckets.get("moneyline") or [], home, away)
    if ml:
        out.append({"name": "ML", "odds": [ml]})
    spr = _spread_row(_pick_main(buckets.get("spread") or []) or {}, home, away)
    if spr:
        out.append({"name": "Spread", "odds": [spr]})
    tot = _total_row(_pick_main(buckets.get("total") or []) or {})
    if tot:
        out.append({"name": "Totals", "odds": [tot]})
    return out


def attach_public_kalshi_markets(
    docs: Union[Dict[Any, Dict[str, Any]], Sequence[Dict[str, Any]]],
    markets: Sequence[Dict[str, Any]],
) -> int:
    """Inject Odds-API-shaped ``bookmakers['Kalshi']`` from public markets. Mutates docs."""
    grouped = _group_events(markets)
    attached = 0
    for doc in _as_docs(docs):
        if kalshi_already_priced(doc):
            continue
        ev = match_public_event(doc, grouped)
        if not ev:
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
        _cache["ts"] = time.time()
        _cache["key"] = key
        _cache["markets"] = markets
    return markets


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
    if n:
        print(f"[PIPELINE] Public Kalshi: attached take lines to {n} event(s) (no private key)")
    return n
