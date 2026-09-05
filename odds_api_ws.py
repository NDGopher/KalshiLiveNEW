"""
Odds-API.io WebSocket feed (https://docs.odds-api.io/guides/websockets).

Primary live-odds path. REST stays for slate (``/events``, ``/events/live``),
REST→WS handoff (``includeSeq=true`` + ``X-OddsAPI-Seq``), ``resync_required``,
and fallback (prefer ``/odds/updated``).

Official contract (do not invent):
- ``wss://api.odds-api.io/v3/ws?apiKey=...`` plus query filters.
- One connection per API key; a new connection closes the old one.
- ``markets`` required for the odds channel (default ``ML,Spread,Totals``, max 20).
- ``channels`` is an allowlist: ``odds``, ``scores``, ``status``. Keep ``odds``
  if scores/status are requested.
- ``leagues`` and ``eventIds`` are mutually exclusive (eventIds max 50).
- Message types: welcome, created, updated, deleted, no_markets, score, status,
  resync_required.
- Merge-by-market-name on ``created``/``updated`` (and REST snapshots).
  Keep existing Totals/Spread unless THIS payload includes that market
  name. Subscribe ``ML,Spread,Totals`` is not proof they survive — an
  ML-only ``updated`` must not replace the whole list. ``no_markets``
  and ``deleted`` still clear. A payload that includes Totals replaces
  only Totals.
- Track ``seq``; reconnect with ``lastSeq`` (compacted latest-state replay).
- On ``resync_required``: REST snapshot with ``includeSeq=true``, then reconnect.
- Process updates asynchronously; bounded exponential backoff + jitter on reconnect.
- Close 1013 (try again later) must not reset backoff or fan out REST /odds/updated.
- REST fallback is single-flight, cooldown-bounded, and fail-closed (no stale lines).

The API key is read from ``ODDS_API_KEY`` only. Never log the raw key.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from odds_api_client import (
    _canonical_odds_api_bookmaker,
    api_wire_bookmakers,
    get_shared_odds_client,
    is_local_only_bookmaker,
    odds_api_master_bookmakers,
    odds_api_sports_list,
    odds_updated_sport_names,
    sport_name_for_odds_updated,
    sport_slug_query_for_api,
)

WS_DEFAULT_URL = "wss://api.odds-api.io/v3/ws"
DEFAULT_WS_MARKETS = ("ML", "Spread", "Totals")
# Official Odds-API slugs. football = soccer; american-football = NFL/CFB.
# Do not pin usa-mlb when this set is active — that would drop soccer/CFB.
DEFAULT_WS_SPORTS = ("baseball", "football", "american-football")
MAX_MARKETS = 20
MAX_SPORTS = 10
MAX_LEAGUES = 20
MAX_EVENT_IDS = 50
ALLOWED_CHANNELS = ("odds", "scores", "status")
ALLOWED_STATUS = ("live", "prematch")
# Server asked us to back off — never treat these as a healthy session.
WS_BACKOFF_CLOSE_CODES = frozenset({1013, 1008})

_MLB_CLOCK_LOG_GAP_SEC = 30.0


def _copy_ws_clock_fields(meta: Dict[str, Any], src: Dict[str, Any]) -> None:
    """Persist Odds-API clock / statusDetail / scores. Do not invent baseball innings."""
    nest = src.get("event") if isinstance(src.get("event"), dict) else {}
    for blob in (src, nest):
        if not isinstance(blob, dict):
            continue
        if blob.get("clock") is not None:
            meta["clock"] = blob.get("clock")
        for key in ("statusDetail", "status_detail"):
            if blob.get(key) is not None:
                meta["statusDetail"] = blob.get(key)
                meta[key] = blob.get(key)
        for key in ("scores", "score", "ss"):
            if blob.get(key) is not None:
                meta[key] = blob.get(key)
        if blob.get("sport") is not None:
            meta["sport"] = blob.get("sport")
        if blob.get("league") is not None:
            meta["league"] = blob.get("league")


def maybe_log_mlb_clock_sample(
    store: "OddsWsStore",
    event_id: int,
    meta: Dict[str, Any],
    *,
    source: str,
) -> None:
    """Sample raw MLB clock + statusDetail from the WS. Never call BookieBeats."""
    try:
        from stoppage_gate import is_baseball_event
    except Exception:
        return
    detail_hint = str(meta.get("statusDetail") or meta.get("status_detail") or "")
    if not is_baseball_event(meta) and "inning" not in detail_hint.lower():
        return
    now = time.time()
    last = store.mlb_clock_log_at.get(event_id) or 0.0
    if now - last < _MLB_CLOCK_LOG_GAP_SEC:
        return
    store.mlb_clock_log_at[event_id] = now
    clock = meta.get("clock")
    detail = meta.get("statusDetail")
    if detail is None:
        detail = meta.get("status_detail")
    print(
        f"[ODDS-WS] MLB clock sample source={source} event_id={event_id} "
        f"status={meta.get('status')!r} statusDetail={detail!r} clock={clock!r}"
    )

# Odds-channel types that carry per-book markets.
ODDS_MARKET_TYPES = frozenset({"created", "updated", "deleted", "no_markets"})


class WsFilterError(ValueError):
    """Invalid WebSocket query filters (mirrors Odds-API 400 messages)."""


def _parse_csv_values(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    s = str(raw).strip()
    if not s:
        return []
    return [x.strip() for x in s.replace(";", ",").split(",") if x.strip()]


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float, *, lo: float, hi: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def ws_close_code(obj: Any) -> Optional[int]:
    """Best-effort close code from a websockets exception, protocol frame, or socket."""
    if obj is None:
        return None
    for attr in ("code", "close_code"):
        raw = getattr(obj, attr, None)
        if raw is not None and not callable(raw):
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
    for wrap_attr in ("rcvd", "sent"):
        wrap = getattr(obj, wrap_attr, None)
        if wrap is None:
            continue
        raw = getattr(wrap, "code", None)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def is_odds_api_rate_limit_error(ex: BaseException) -> bool:
    status = getattr(ex, "status", None)
    if status is None:
        status = getattr(ex, "status_code", None)
    try:
        if int(status) == 429:
            return True
    except (TypeError, ValueError):
        pass
    text = str(ex).lower()
    if "429" not in text:
        return False
    return "too many" in text or "rate" in text or "try again later" in text


def ws_reconnect_delay_sec(
    attempt: int,
    *,
    close_code: Optional[int] = None,
    rng: Optional[Callable[[], float]] = None,
) -> float:
    """Bounded exponential backoff with symmetric jitter.

    Close 1013 uses a higher floor so 'try again later' cannot 1s-loop.
    """
    base = _env_float("ODDS_API_WS_RECONNECT_BASE_SEC", 2.0, lo=0.25, hi=120.0)
    cap = _env_float("ODDS_API_WS_RECONNECT_MAX_SEC", 60.0, lo=1.0, hi=600.0)
    jitter_frac = _env_float("ODDS_API_WS_RECONNECT_JITTER", 0.25, lo=0.0, hi=0.9)
    if close_code in WS_BACKOFF_CLOSE_CODES:
        base = max(base, _env_float("ODDS_API_WS_1013_BASE_SEC", 8.0, lo=1.0, hi=180.0))
        cap = max(cap, _env_float("ODDS_API_WS_1013_MAX_SEC", 120.0, lo=8.0, hi=600.0))
    n = max(1, int(attempt))
    exp = min(cap, base * (2 ** (n - 1)))
    pick = rng if rng is not None else random.random
    u = float(pick())
    u = min(1.0, max(0.0, u))
    jitter = exp * jitter_frac * (2.0 * u - 1.0)
    return max(0.25, min(cap, exp + jitter))


def odds_api_ws_wanted() -> bool:
    """
    ``ODDS_API_WS`` defaults true when ``ODDS_API_KEY`` is present.
    Explicit false/0/off disables the WebSocket even if a key exists.
    """
    key = (os.getenv("ODDS_API_KEY") or "").strip()
    if not key:
        return False
    raw = os.getenv("ODDS_API_WS")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def parse_ws_markets(raw: Any = None) -> List[str]:
    """Markets for the odds channel. Default ML,Spread,Totals. Max 20."""
    vals = _parse_csv_values(raw if raw is not None else os.getenv("ODDS_API_WS_MARKETS"))
    if not vals:
        vals = list(DEFAULT_WS_MARKETS)
    # preserve order, cap at 20
    seen: Set[str] = set()
    out: List[str] = []
    for v in vals:
        k = v.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(v)
        if len(out) >= MAX_MARKETS:
            break
    return out


def parse_ws_channels(raw: Any = None) -> List[str]:
    """
    Channel allowlist. Default ``odds``.
    If scores/status are requested, keep ``odds`` unless
    ``ODDS_API_WS_ODDS=false`` (scoreboard-only opt-out).
    """
    vals = [v.lower() for v in _parse_csv_values(raw if raw is not None else os.getenv("ODDS_API_WS_CHANNELS"))]
    if not vals:
        # scores + status carry clock / statusDetail (needed for Stoppages Only + MLB samples).
        vals = ["odds", "scores", "status"]
    unknown = [v for v in vals if v not in ALLOWED_CHANNELS]
    if unknown:
        raise WsFilterError(f"Invalid channel: {unknown[0]}. Allowed: odds, scores, status")
    keep_odds = _env_bool("ODDS_API_WS_ODDS", "true")
    if keep_odds and ("scores" in vals or "status" in vals) and "odds" not in vals:
        vals = ["odds"] + vals
    seen: Set[str] = set()
    out: List[str] = []
    for v in vals:
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    if "odds" not in out and keep_odds and not vals:
        out.insert(0, "odds")
    return out


def parse_ws_status(raw: Any = None) -> Optional[str]:
    """Single status filter: live | prematch.

    Official API accepts one value. Omit the param for prematch+live (MLB tonight).
    Set ``ODDS_API_WS_STATUS=live`` or ``prematch`` to narrow.
    """
    if raw is None:
        raw = os.getenv("ODDS_API_WS_STATUS")
        if raw is None or not str(raw).strip():
            # MLB-first: both prematch and live on one connection (cannot send both values).
            return None
    s = str(raw).strip().lower()
    if not s or s in ("all", "*", "both", "prematch+live", "live+prematch"):
        return None
    if s not in ALLOWED_STATUS:
        raise WsFilterError("Invalid status filter. Use 'prematch' or 'live'")
    return s


def _normalize_ws_sports(values: Sequence[str]) -> List[str]:
    """Map UI / env aliases to Odds-API sport slugs (soccer → football)."""
    seen: Set[str] = set()
    out: List[str] = []
    for raw in values:
        q = sport_slug_query_for_api(str(raw))
        k = q.lower()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(q)
    return out


def mlb_ws_slice_active() -> bool:
    """True only when this process is an explicit baseball-only WS slice.

    Unset / ``all`` is multi-sport (baseball + soccer + American football).
    Pinning ``usa-mlb`` happens only for this baseball-only slice so soccer
    and CFB/NFL event IDs are not dropped from the same connection.
    """
    sports = _parse_csv_values(
        os.getenv("ODDS_API_WS_SPORT") or os.getenv("ODDS_API_WS_SPORTS") or os.getenv("ODDS_API_SPORTS")
    )
    if not sports:
        return False
    if len(sports) == 1 and sports[0].lower() in ("all", "*", "everything"):
        return False
    mapped = _normalize_ws_sports(sports)
    return mapped == ["baseball"]


def _bounded_csv(values: Sequence[str], *, max_n: int, kind: str) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for v in values:
        t = str(v).strip()
        if not t:
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
        if len(out) > max_n:
            if kind == "sports":
                raise WsFilterError("Too many sports. Maximum 10 allowed.")
            if kind == "leagues":
                raise WsFilterError("Too many leagues. Maximum 20 allowed.")
            if kind == "eventIds":
                raise WsFilterError("Too many event IDs. Maximum 50 allowed.")
            if kind == "markets":
                raise WsFilterError("Too many markets. Maximum 20 allowed.")
            raise WsFilterError(f"Too many {kind}.")
    return out


def ws_filters_from_env(
    *,
    event_ids: Optional[Sequence[int]] = None,
    leagues: Optional[Sequence[str]] = None,
    sports: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Build validated filter dict from env + optional runtime slate IDs."""
    markets = parse_ws_markets()
    if len(markets) > MAX_MARKETS:
        raise WsFilterError("Too many markets. Maximum 20 allowed.")
    channels = parse_ws_channels()
    status = parse_ws_status()

    env_leagues = _parse_csv_values(os.getenv("ODDS_API_WS_LEAGUES"))
    env_eids = _parse_csv_values(os.getenv("ODDS_API_WS_EVENT_IDS"))
    scope = (os.getenv("ODDS_API_WS_SCOPE") or "leagues").strip().lower()

    use_leagues = list(leagues) if leagues else list(env_leagues)
    use_eids: List[str] = []
    if event_ids:
        use_eids = [str(int(x)) for x in event_ids]
    elif env_eids:
        use_eids = list(env_eids)
    elif scope in ("events", "eventids", "event_ids") and event_ids is None:
        # caller may pass slate later; leave empty here
        use_eids = []

    env_sports = _parse_csv_values(os.getenv("ODDS_API_WS_SPORT") or os.getenv("ODDS_API_WS_SPORTS"))
    env_rest_sports = _parse_csv_values(os.getenv("ODDS_API_SPORTS"))
    if sports:
        use_sports = list(sports)
    elif env_sports:
        use_sports = env_sports
    elif env_rest_sports and env_rest_sports[0].lower() not in ("all", "*", "everything"):
        use_sports = env_rest_sports
    else:
        # Multi-sport default: baseball + soccer (football) + NFL/CFB.
        # One WS connection; event IDs stay Odds-API ids. Do not pin usa-mlb here.
        use_sports = list(DEFAULT_WS_SPORTS)
    use_sports = _normalize_ws_sports(use_sports)

    if not use_leagues and not use_eids and scope not in ("events", "eventids", "event_ids"):
        sport_keys = {s.lower().replace("_", "-") for s in use_sports}
        # Pin usa-mlb only on an explicit baseball-only slice. A multi-sport
        # leagues=usa-mlb filter would drop soccer and CFB/NFL from the wire.
        if sport_keys == {"baseball"}:
            mlb_lg = (os.getenv("ODDS_API_LEAGUE_MLB") or "usa-mlb").strip()
            if mlb_lg:
                use_leagues = [mlb_lg]

    if use_leagues and use_eids:
        raise WsFilterError("Cannot use both 'leagues' and 'eventIds' filters together.")

    sport_out = _bounded_csv(use_sports, max_n=MAX_SPORTS, kind="sports")
    leagues_out = _bounded_csv(use_leagues, max_n=MAX_LEAGUES, kind="leagues") if use_leagues else []
    eids_out = _bounded_csv(use_eids, max_n=MAX_EVENT_IDS, kind="eventIds") if use_eids else []

    return {
        "markets": markets,
        "channels": channels,
        "sport": sport_out,
        "leagues": leagues_out,
        "eventIds": eids_out,
        "status": status,
    }


def redact_ws_url(url: str) -> str:
    """Replace apiKey query value so logs never print the secret."""
    parts = urlsplit(url)
    q = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        if k.lower() == "apikey":
            q.append((k, "***"))
        else:
            q.append((k, v))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def build_ws_url(
    api_key: str,
    *,
    markets: Optional[Sequence[str]] = None,
    channels: Optional[Sequence[str]] = None,
    sport: Optional[Sequence[str]] = None,
    leagues: Optional[Sequence[str]] = None,
    event_ids: Optional[Sequence[Any]] = None,
    status: Optional[str] = None,
    last_seq: Optional[int] = None,
    base_url: Optional[str] = None,
) -> str:
    """
    Build ``wss://api.odds-api.io/v3/ws?...`` with official query filters.

    ``markets`` is required when the odds channel is active (default).
    ``leagues`` and ``eventIds`` cannot be combined.
    """
    if not (api_key or "").strip():
        raise WsFilterError("ODDS_API_KEY is not set")
    ch = parse_ws_channels(channels)
    mk = parse_ws_markets(markets)
    if "odds" in ch and not mk:
        raise WsFilterError("markets is required for the odds channel")
    if mk and len(mk) > MAX_MARKETS:
        raise WsFilterError("Too many markets. Maximum 20 allowed.")

    lg = _bounded_csv(_parse_csv_values(leagues), max_n=MAX_LEAGUES, kind="leagues") if leagues else []
    eids = _bounded_csv([str(x) for x in event_ids], max_n=MAX_EVENT_IDS, kind="eventIds") if event_ids else []
    if lg and eids:
        raise WsFilterError("Cannot use both 'leagues' and 'eventIds' filters together.")
    sports = _bounded_csv(_parse_csv_values(sport), max_n=MAX_SPORTS, kind="sports") if sport else []
    st = parse_ws_status(status) if status is not None else None

    params: Dict[str, str] = {"apiKey": api_key.strip()}
    if "odds" in ch or ch == ["odds"]:
        params["markets"] = ",".join(mk)
    if ch != ["odds"]:
        params["channels"] = ",".join(ch)
    if sports:
        params["sport"] = ",".join(sports)
    if lg:
        params["leagues"] = ",".join(lg)
    if eids:
        params["eventIds"] = ",".join(eids)
    if st:
        params["status"] = st
    if last_seq is not None and int(last_seq) > 0:
        params["lastSeq"] = str(int(last_seq))
    base = (base_url or os.getenv("ODDS_API_WS_URL") or WS_DEFAULT_URL).rstrip("/")
    return f"{base}?{urlencode(params)}"


def bookmaker_list_mismatch(
    welcome_books: Sequence[str],
    configured: Sequence[str],
) -> Tuple[List[str], List[str]]:
    """Return (missing_from_welcome, extra_on_account) using canonical names."""
    wmap = {_canonical_odds_api_bookmaker(b).lower(): _canonical_odds_api_bookmaker(b) for b in welcome_books if str(b).strip()}
    cmap = {_canonical_odds_api_bookmaker(b).lower(): _canonical_odds_api_bookmaker(b) for b in configured if str(b).strip()}
    missing = [cmap[k] for k in cmap if k not in wmap]
    extra = [wmap[k] for k in wmap if k not in cmap]
    return missing, extra


def log_bookmaker_mismatch(welcome_books: Sequence[str], configured: Optional[Sequence[str]] = None) -> bool:
    """Loud log when welcome.bookmakers != ODDS_API_BOOKMAKERS. Returns True if mismatched."""
    cfg = list(configured) if configured is not None else odds_api_master_bookmakers()
    missing, extra = bookmaker_list_mismatch(welcome_books, cfg)
    if not missing and not extra:
        print(
            f"[ODDS-API WS] welcome.bookmakers match ODDS_API_BOOKMAKERS ({len(cfg)}): "
            f"{', '.join(cfg)}"
        )
        return False
    print("[ODDS-API WS] BOOKMAKER MISMATCH — welcome.bookmakers != ODDS_API_BOOKMAKERS")
    print(f"[ODDS-API WS]   configured ({len(cfg)}): {', '.join(cfg) or '(none)'}")
    print(f"[ODDS-API WS]   welcome    ({len(list(welcome_books))}): {', '.join(welcome_books) or '(none)'}")
    if missing:
        print(f"[ODDS-API WS]   missing from WS account: {', '.join(missing)}")
    if extra:
        print(f"[ODDS-API WS]   extra on account (not in ODDS_API_BOOKMAKERS): {', '.join(extra)}")
    print(
        "[ODDS-API WS]   Select the 10 catalog books in the Odds-API.io dashboard "
        "(Bookmakers → selected), or set ODDS_API_SELECT_BOOKS=true on startup. "
        "Do not select BookMaker.eu (catalog-inactive)."
    )
    return True


def _event_id(raw: Any) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


@dataclass
class AppliedMessage:
    type: str
    event_id: Optional[int] = None
    bookie: Optional[str] = None
    seq: Optional[int] = None
    reason: Optional[str] = None
    dirty: bool = False


def _ws_market_name_key(name: Any) -> str:
    """Stable market-name key. ``Totals`` ≠ ``Total Runs`` — do not merge by family."""
    return str(name or "").strip().casefold()


def _ws_market_family(name: Any) -> str:
    """ML / Spread / Totals family (diagnostics only). Team totals are not game Totals."""
    u = str(name or "").strip().upper()
    if not u:
        return "other"
    if "PLAYER" in u:
        return "other"
    if "TEAM" in u and "TOTAL" in u:
        return "other"
    if "TOTAL" in u or ("OVER" in u and "UNDER" in u) or u in ("OU", "O/U"):
        return "totals"
    if "SPREAD" in u or "HANDICAP" in u or "PUCK LINE" in u or "PUCKLINE" in u.replace(" ", ""):
        return "spread"
    if u == "ML" or "MONEY" in u or "WINNER" in u:
        return "ml"
    return "other"


@dataclass
class OddsWsStore:
    """In-memory latest-state store. created/updated merge by market name."""

    last_seq: Optional[int] = None
    welcome: Optional[Dict[str, Any]] = None
    event_meta: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    # (event_id, canonical_bookie) -> markets list (merge-by-name; no_markets/deleted clear)
    books: Dict[Tuple[int, str], List[Dict[str, Any]]] = field(default_factory=dict)
    # Last time this book was touched (WS created/updated or REST snapshot).
    book_updated_at: Dict[Tuple[int, str], float] = field(default_factory=dict)
    # Rate-limit raw MLB clock/statusDetail samples so we can see if Odds-API
    # ever sends top/bottom or Break (docs only list "1st inning"…"9th inning").
    mlb_clock_log_at: Dict[int, float] = field(default_factory=dict)
    generation: int = 0

    def note_seq(self, seq: Any) -> Optional[int]:
        if seq is None or seq == "":
            return None
        try:
            n = int(seq)
        except (TypeError, ValueError):
            return None
        if self.last_seq is None or n > self.last_seq:
            self.last_seq = n
        return n

    def apply_slate(self, events: Iterable[Dict[str, Any]]) -> None:
        """REST /events or /events/live metadata (home/away/league/status/clock)."""
        for ev in events:
            if not isinstance(ev, dict):
                continue
            eid = _event_id(ev.get("id"))
            if eid is None:
                continue
            meta = self.event_meta.setdefault(eid, {"id": eid})
            for key in (
                "home",
                "away",
                "league",
                "sport",
                "status",
                "state",
                "date",
                "live",
                "isLive",
                "scores",
                # Odds-API clock.running is timed sports only (NBA/NFL/soccer).
                # MLB has no inning / pitching-change field — do not invent one.
                "clock",
                "statusDetail",
                "status_detail",
            ):
                if ev.get(key) is not None:
                    meta[key] = ev.get(key)
            meta["id"] = eid
            maybe_log_mlb_clock_sample(self, eid, meta, source="slate")

    def apply_rest_docs(self, docs: Iterable[Dict[str, Any]]) -> None:
        """Ingest /odds or /odds/multi snapshots. Merge by market name so an
        ML-only REST row cannot wipe prior Spread/Totals (same rule as WS).
        Empty markets list still clears that book (REST equivalent of no_markets).
        """
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("id") is None:
                continue
            eid = _event_id(doc.get("id"))
            if eid is None:
                continue
            self.apply_slate([doc])
            bks = doc.get("bookmakers")
            if not isinstance(bks, dict):
                continue
            for raw_k, markets in bks.items():
                bookie = _canonical_odds_api_bookmaker(str(raw_k))
                if is_local_only_bookmaker(bookie):
                    continue
                if isinstance(markets, list) and markets:
                    self._upsert_markets_by_name(eid, bookie, markets)
                else:
                    self._replace_markets(eid, bookie, markets if isinstance(markets, list) else [])
            self.generation += 1

    def _replace_markets(self, eid: int, bookie: str, markets: Any) -> None:
        """Full replace. Only ``no_markets`` / ``deleted`` / empty REST snapshot."""
        ck = _canonical_odds_api_bookmaker(bookie)
        if isinstance(markets, list):
            self.books[(eid, ck)] = list(markets)
        else:
            self.books[(eid, ck)] = []
        self.book_updated_at[(eid, ck)] = time.time()

    def _upsert_markets_by_name(self, eid: int, bookie: str, markets: Any) -> None:
        """created/updated: replace only market names present in THIS payload.

        Keep existing Totals/Spread unless the payload includes that name.
        Same-family aliases (``Total Runs`` vs ``Totals``) must not wipe.
        Empty ``markets`` on created/updated is a no-op (``no_markets`` clears).
        """
        ck = _canonical_odds_api_bookmaker(bookie)
        incoming = [m for m in (markets if isinstance(markets, list) else []) if isinstance(m, dict)]
        if not incoming:
            self.book_updated_at[(eid, ck)] = time.time()
            return
        incoming_names = {
            _ws_market_name_key(m.get("name"))
            for m in incoming
            if _ws_market_name_key(m.get("name"))
        }
        prev = list(self.books.get((eid, ck)) or [])
        kept = [
            m
            for m in prev
            if isinstance(m, dict) and _ws_market_name_key(m.get("name")) not in incoming_names
        ]
        self.books[(eid, ck)] = incoming + kept
        self.book_updated_at[(eid, ck)] = time.time()

    def _upsert_markets_by_family(self, eid: int, bookie: str, markets: Any) -> None:
        """Back-compat alias — ingest is merge-by-name, not family."""
        self._upsert_markets_by_name(eid, bookie, markets)

    def market_family_counts(self) -> Dict[str, int]:
        """How many stored market blocks are ML / Spread / Totals."""
        counts = {"ml": 0, "spread": 0, "totals": 0, "other": 0}
        for markets in self.books.values():
            for m in markets or []:
                if not isinstance(m, dict):
                    continue
                fam = _ws_market_family(m.get("name"))
                counts[fam] = counts.get(fam, 0) + 1
        return counts

    def apply_message(self, msg: Dict[str, Any]) -> AppliedMessage:
        """Apply one official WS JSON object. created/updated merge by market name."""
        if not isinstance(msg, dict):
            return AppliedMessage(type="unknown")
        t = str(msg.get("type") or "").strip().lower()
        if t == "welcome":
            self.welcome = msg
            return AppliedMessage(type="welcome")
        if t == "resync_required":
            return AppliedMessage(type="resync_required", reason=str(msg.get("reason") or ""))

        seq = self.note_seq(msg.get("seq"))
        eid = _event_id(msg.get("id"))
        bookie_raw = msg.get("bookie")
        bookie = _canonical_odds_api_bookmaker(str(bookie_raw)) if bookie_raw else None

        if t == "score":
            if eid is not None:
                meta = self.event_meta.setdefault(eid, {"id": eid})
                _copy_ws_clock_fields(meta, msg)
                maybe_log_mlb_clock_sample(self, eid, meta, source="score")
            return AppliedMessage(type="score", event_id=eid, seq=seq, dirty=False)

        if t == "status":
            if eid is not None:
                meta = self.event_meta.setdefault(eid, {"id": eid})
                if msg.get("status") is not None:
                    meta["status"] = msg.get("status")
                if msg.get("scores") is not None:
                    meta["scores"] = msg.get("scores")
                _copy_ws_clock_fields(meta, msg)
                maybe_log_mlb_clock_sample(self, eid, meta, source="status")
                st = str(msg.get("status") or "").lower()
                if st in ("settled", "cancelled"):
                    to_drop = [k for k in self.books if k[0] == eid]
                    for k in to_drop:
                        del self.books[k]
                    self.generation += 1
                    return AppliedMessage(type="status", event_id=eid, seq=seq, dirty=True)
            return AppliedMessage(type="status", event_id=eid, seq=seq, dirty=False)

        if t in ("created", "updated"):
            if eid is None or not bookie:
                return AppliedMessage(type=t, event_id=eid, bookie=bookie, seq=seq)
            if is_local_only_bookmaker(bookie):
                return AppliedMessage(type=t, event_id=eid, bookie=bookie, seq=seq)
            # Do not replace the whole markets list. ML-only updated keeps Totals.
            self._upsert_markets_by_name(eid, bookie, msg.get("markets"))
            meta = self.event_meta.setdefault(eid, {"id": eid})
            if msg.get("url"):
                meta.setdefault("urls", {})
                if isinstance(meta["urls"], dict):
                    meta["urls"][bookie] = msg.get("url")
            if msg.get("timestamp") is not None:
                meta["timestamp"] = msg.get("timestamp")
            self.generation += 1
            return AppliedMessage(type=t, event_id=eid, bookie=bookie, seq=seq, dirty=True)

        if t == "no_markets":
            if eid is not None and bookie:
                # Replace with empty — book still exists, no priced markets.
                self._replace_markets(eid, bookie, [])
                self.generation += 1
            return AppliedMessage(type=t, event_id=eid, bookie=bookie, seq=seq, dirty=True)

        if t == "deleted":
            if eid is not None and bookie:
                self.books.pop((eid, bookie), None)
                self.book_updated_at.pop((eid, bookie), None)
                leftover = any(k[0] == eid for k in self.books)
                if not leftover:
                    self.event_meta.pop(eid, None)
                self.generation += 1
            elif eid is not None:
                to_drop = [k for k in self.books if k[0] == eid]
                for k in to_drop:
                    del self.books[k]
                    self.book_updated_at.pop(k, None)
                self.event_meta.pop(eid, None)
                self.generation += 1
            return AppliedMessage(type=t, event_id=eid, bookie=bookie, seq=seq, dirty=True)

        return AppliedMessage(type=t or "unknown", event_id=eid, bookie=bookie, seq=seq)

    def merged_doc(self, event_id: int) -> Dict[str, Any]:
        """Same shape as REST ``/odds`` / ``/odds/multi``: id + meta + bookmakers dict."""
        meta = dict(self.event_meta.get(event_id) or {"id": event_id})
        meta["id"] = event_id
        bks: Dict[str, List[Dict[str, Any]]] = {}
        for (eid, book), markets in self.books.items():
            if eid == event_id:
                if is_local_only_bookmaker(book):
                    continue
                bks[book] = list(markets)
        meta["bookmakers"] = bks
        meta["book_updated_at"] = {
            book: self.book_updated_at.get((event_id, book))
            for book in bks
            if self.book_updated_at.get((event_id, book)) is not None
        }
        return meta

    def slate_event_ids(self) -> List[int]:
        """Event IDs known from REST slate metadata or WS book rows."""
        ids = {eid for eid, _ in self.books.keys()} | set(self.event_meta.keys())
        return sorted(eid for eid in ids if eid is not None)

    def slate_events(self) -> List[Dict[str, Any]]:
        """Event-list rows from the WS store (id + last known metadata)."""
        out: List[Dict[str, Any]] = []
        for eid in self.slate_event_ids():
            meta = dict(self.event_meta.get(eid) or {"id": eid})
            meta["id"] = eid
            out.append(meta)
        return out

    def merged_docs(self, event_ids: Optional[Sequence[int]] = None) -> List[Dict[str, Any]]:
        if event_ids is None:
            ids = sorted({eid for eid, _ in self.books.keys()} | set(self.event_meta.keys()))
        else:
            ids = []
            seen: Set[int] = set()
            for raw in event_ids:
                eid = _event_id(raw)
                if eid is None or eid in seen:
                    continue
                seen.add(eid)
                ids.append(eid)
        return [self.merged_doc(eid) for eid in ids]

    def clear_odds(self) -> None:
        """Drop book markets (keep seq) after a failed replay that needs a REST rebuild."""
        self.books.clear()
        self.generation += 1


class OddsApiWsFeed:
    """
    One connection per API key. Background receive loop + async processor.

    ``healthy`` is True after welcome until the socket drops or resync is in progress.
    """

    def __init__(
        self,
        rest_client: Any,
        *,
        api_key: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        connect_fn: Optional[Any] = None,
    ):
        self.rest = rest_client
        self.api_key = (api_key if api_key is not None else os.getenv("ODDS_API_KEY") or "").strip()
        self.store = OddsWsStore()
        self.filters = filters or ws_filters_from_env()
        self._connect_fn = connect_fn  # injectable for tests
        self._task: Optional[asyncio.Task] = None
        self._proc_task: Optional[asyncio.Task] = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self.connected = False
        self.welcome_ok = False
        self.resyncing = False
        self.last_error: Optional[str] = None
        self._reconnect_attempts = 0
        self._reconnect_lock = asyncio.Lock()
        self._reconnecting = False
        self._fallback_lock = asyncio.Lock()
        self._fallback_cooldown_until = 0.0
        self._fallback_rate_limited = False
        self._session_started_at: Optional[float] = None
        self._unhealthy_since: Optional[float] = None
        self.last_close_code: Optional[int] = None
        self._ws = None
        self._dirty = asyncio.Event()
        self.generation = 0
        self.source_label = "websocket"

    @property
    def healthy(self) -> bool:
        return bool(self.connected and self.welcome_ok and not self.resyncing and self._running)

    def note_unhealthy(self) -> None:
        """Mark the start of a disconnect/resync gap (for short store-grace reads)."""
        if self._unhealthy_since is None:
            self._unhealthy_since = time.time()

    def note_healthy(self) -> None:
        self._unhealthy_since = None

    def _mark_dirty(self) -> None:
        self.generation = self.store.generation
        self._dirty.set()

    async def wait_dirty_or_timeout(self, timeout: float) -> bool:
        """Wait for a replace-style odds change, or ``timeout`` seconds (slate idle)."""
        try:
            await asyncio.wait_for(self._dirty.wait(), timeout=max(0.05, float(timeout)))
        except asyncio.TimeoutError:
            return False
        debounce = float(os.getenv("ODDS_API_WS_DEBOUNCE_SEC", "0.35"))
        if debounce > 0:
            await asyncio.sleep(debounce)
        self._dirty.clear()
        return True

    def current_url(self, *, last_seq: Optional[int] = None) -> str:
        f = self.filters
        seq = last_seq if last_seq is not None else self.store.last_seq
        return build_ws_url(
            self.api_key,
            markets=f.get("markets"),
            channels=f.get("channels"),
            sport=f.get("sport"),
            leagues=f.get("leagues"),
            event_ids=f.get("eventIds"),
            status=f.get("status"),
            last_seq=seq,
        )

    async def start(self) -> None:
        if self._running:
            return
        if not self.api_key:
            print("[ODDS-API WS] ODDS_API_KEY missing — WebSocket not started")
            return
        self._running = True
        if self._proc_task is None or self._proc_task.done():
            self._proc_task = asyncio.create_task(self._process_loop(), name="odds-api-ws-process")
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop(), name="odds-api-ws-recv")
        print("[ODDS-API WS] feed starting (REST slate/resync only; live lines via WebSocket)")

    async def stop(self) -> None:
        self._running = False
        self.connected = False
        self.welcome_ok = False
        self.note_unhealthy()
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        for t in (self._task, self._proc_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._task = None
        self._proc_task = None

    async def maybe_select_books(self) -> None:
        if not _env_bool("ODDS_API_SELECT_BOOKS", "false"):
            return
        names = api_wire_bookmakers(odds_api_master_bookmakers())
        print(f"[ODDS-API WS] ODDS_API_SELECT_BOOKS=true — PUT /bookmakers/selected/select ({len(names)}): {', '.join(names)}")
        try:
            await self.rest.select_bookmakers(names)
        except Exception as ex:
            print(f"[ODDS-API WS] [WARN] select bookmakers failed: {ex}")

    async def rest_snapshot(self, event_ids: Sequence[int], *, include_seq: bool = True) -> List[Dict[str, Any]]:
        """REST /odds/multi snapshot used for handoff and resync_required."""
        if not event_ids:
            return []
        books = odds_api_master_bookmakers()
        docs = await self.rest.get_odds_multi(
            [int(x) for x in event_ids],
            books,
            odds_cache_ttl=0.0,
            include_seq=include_seq,
        )
        self.store.apply_rest_docs(docs)
        seq = getattr(self.rest, "last_seq", None)
        if seq is not None:
            self.store.note_seq(seq)
        return docs

    def fallback_cooling_down(self) -> bool:
        return time.time() < float(self._fallback_cooldown_until or 0.0)

    def _arm_fallback_cooldown(self, *, rate_limited: bool) -> None:
        if rate_limited:
            extra = _env_float("ODDS_API_REST_FALLBACK_429_COOLDOWN_SEC", 60.0, lo=5.0, hi=600.0)
            self._fallback_rate_limited = True
        else:
            extra = _env_float("ODDS_API_REST_FALLBACK_COOLDOWN_SEC", 20.0, lo=1.0, hi=300.0)
        self._fallback_cooldown_until = time.time() + extra
        kind = "429" if rate_limited else "pass"
        print(f"[ODDS-API WS] REST fallback cooldown {extra:.0f}s ({kind})")

    def should_attempt_rest_fallback(self) -> bool:
        """True only after a disconnect — not during first connect, not on cooldown."""
        if not _env_bool("ODDS_API_REST_UPDATED_FALLBACK", "true"):
            return False
        if self.fallback_cooling_down():
            return False
        if not self.store.event_meta:
            return False
        if self.connected or self.healthy:
            return False
        return bool(self._reconnect_attempts > 0 or self.last_error)

    async def rest_updated_fallback(
        self,
        since: int,
        books: Optional[Sequence[str]] = None,
        sport: Optional[Any] = None,
        sports: Optional[Sequence[Any]] = None,
    ) -> int:
        """Patch store from ``/odds/updated`` (REST fallback; one book × sport per call).

        Odds-API.io requires the sport *display name* (e.g. ``Baseball``). We loop
        configured sports when callers do not pass one — calling without sport is a
        hard 400 and was wiping the board on every WS flap.

        Single-flight: concurrent callers wait, then see cooldown and skip.
        A 429 stops the remaining book/sport loop and arms a longer cooldown.
        """
        async with self._fallback_lock:
            if self.fallback_cooling_down():
                return 0
            n = 0
            rate_limited = False
            use = list(books) if books else odds_api_master_bookmakers()
            if sport is not None:
                sport_names = odds_updated_sport_names([sport])
            elif sports is not None:
                sport_names = odds_updated_sport_names(list(sports))
            else:
                sport_names = odds_updated_sport_names()
            if not sport_names:
                print("[ODDS-API WS] [WARN] /odds/updated skipped — no sport names resolved")
                self._arm_fallback_cooldown(rate_limited=False)
                return 0
            for bm in use:
                for sp in sport_names:
                    try:
                        docs = await self.rest.get_odds_updated(since, bm, sport=sp)
                    except Exception as ex:
                        if is_odds_api_rate_limit_error(ex):
                            print(
                                f"[ODDS-API WS] [WARN] /odds/updated 429 for {bm}/{sp} — stopping book loop"
                            )
                            rate_limited = True
                            break
                        print(f"[ODDS-API WS] [WARN] /odds/updated failed for {bm}/{sp}: {ex}")
                        continue
                    if docs:
                        self.store.apply_rest_docs(docs)
                        n += len(docs)
                if rate_limited:
                    break
            self._arm_fallback_cooldown(rate_limited=rate_limited)
            if n:
                self._mark_dirty()
            return n

    async def _process_loop(self) -> None:
        while self._running:
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            try:
                applied = self.store.apply_message(msg)
                if applied.type == "welcome":
                    self.welcome_ok = True
                    self.note_healthy()
                    books = []
                    if isinstance(msg.get("bookmakers"), list):
                        books = [str(x) for x in msg["bookmakers"]]
                    print(
                        f"[ODDS-API WS] welcome: channels={msg.get('channels')} "
                        f"status={msg.get('status_filter')} markets={msg.get('market_filter')}"
                    )
                    if msg.get("warning"):
                        print(f"[ODDS-API WS] welcome warning: {msg.get('warning')}")
                    log_bookmaker_mismatch(books)
                    self._mark_dirty()
                elif applied.type == "resync_required":
                    print(f"[ODDS-API WS] resync_required: {applied.reason}")
                    await self._handle_resync()
                elif applied.dirty:
                    self._mark_dirty()
            except Exception as ex:
                print(f"[ODDS-API WS] [WARN] process message failed: {ex}")

    async def _handle_resync(self) -> None:
        self.resyncing = True
        try:
            self.store.clear_odds()
            ids = list({eid for eid, _ in self.store.books.keys()} | set(self.store.event_meta.keys()))
            if not ids:
                # Slate from REST live events, then snapshot.
                try:
                    liv = await self.rest.list_live_events(None)
                    self.store.apply_slate(liv or [])
                    ids = []
                    for ev in liv or []:
                        eid = _event_id(ev.get("id") if isinstance(ev, dict) else None)
                        if eid is not None:
                            ids.append(eid)
                except Exception as ex:
                    print(f"[ODDS-API WS] [WARN] resync slate failed: {ex}")
            if ids:
                await self.rest_snapshot(ids[:MAX_EVENT_IDS], include_seq=True)
            # Discard stored last_seq if snapshot did not yield a header — next reconnect
            # uses REST seq when present; otherwise starts fresh (no lastSeq).
            if getattr(self.rest, "last_seq", None) is None:
                self.store.last_seq = None
            self._mark_dirty()
        except Exception as ex:
            print(f"[ODDS-API WS] [ERR] REST resync failed: {ex}")
            self.store.last_seq = None
        finally:
            self.resyncing = False
            # Close the socket so the recv loop reconnects with the new lastSeq.
            ws = self._ws
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass

    async def _handoff_snapshot(self) -> None:
        """REST includeSeq snapshot then connect with lastSeq (official handoff)."""
        try:
            liv = await self.rest.list_live_events(None)
            self.store.apply_slate(liv or [])
            # Majors first — raw /events/live order buries EPL under FA Cup floods,
            # so a first-50 cut never seeds soccer majors into the WS store.
            from odds_api_client import prioritize_live_events_for_scan

            seed = prioritize_live_events_for_scan(list(liv or []), MAX_EVENT_IDS)
            ids: List[int] = []
            for ev in seed:
                eid = _event_id(ev.get("id") if isinstance(ev, dict) else None)
                if eid is not None:
                    ids.append(eid)
            if ids:
                await self.rest_snapshot(ids, include_seq=True)
                sample_slug = ""
                if seed:
                    lg = seed[0].get("league") if isinstance(seed[0].get("league"), dict) else {}
                    sample_slug = str((lg or {}).get("slug") or "")
                print(
                    f"[ODDS-API WS] REST→WS handoff: snapshot {len(ids)} live ids "
                    f"seq={self.store.last_seq} majors-first sample={sample_slug or '?'}"
                )
        except Exception as ex:
            print(f"[ODDS-API WS] [WARN] handoff snapshot failed (connecting without lastSeq): {ex}")

    def _should_reset_reconnect_backoff(self, *, had_welcome: bool) -> bool:
        """Reset only after a real healthy session — never after 1013 / brief flaps."""
        code = self.last_close_code
        if code in WS_BACKOFF_CLOSE_CODES:
            return False
        if not had_welcome:
            return False
        started = self._session_started_at
        if started is None:
            return False
        need = _env_float("ODDS_API_WS_HEALTHY_RESET_SEC", 15.0, lo=1.0, hi=300.0)
        return (time.time() - started) >= need

    async def _run_loop(self) -> None:
        await self.maybe_select_books()
        first = True
        while self._running:
            if first:
                await self._handoff_snapshot()
                first = False
            # One connection per API key. A second process kicking us off looks like 1013.
            async with self._reconnect_lock:
                if self._reconnecting:
                    continue
                self._reconnecting = True
                url = self.current_url()
                print(f"[ODDS-API WS] connecting {redact_ws_url(url)}")
                had_welcome = False
                try:
                    await self._connect_once(url)
                except asyncio.CancelledError:
                    self._reconnecting = False
                    break
                except Exception as ex:
                    self.last_error = str(ex)
                    code = ws_close_code(ex)
                    if code is not None:
                        self.last_close_code = code
                    print(f"[ODDS-API WS] [WARN] connection ended: {ex}")
                else:
                    self.last_error = None
                finally:
                    had_welcome = bool(self.welcome_ok)
                    self.connected = False
                    self.welcome_ok = False
                    self.note_unhealthy()
                    self._reconnecting = False
            if not self._running:
                break
            if self._should_reset_reconnect_backoff(had_welcome=had_welcome):
                self._reconnect_attempts = 0
            self._reconnect_attempts += 1
            delay = ws_reconnect_delay_sec(
                self._reconnect_attempts,
                close_code=self.last_close_code,
            )
            print(
                f"[ODDS-API WS] reconnect in {delay:.1f}s "
                f"(attempt {self._reconnect_attempts}, lastSeq={self.store.last_seq}, "
                f"close={self.last_close_code})"
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    async def _connect_once(self, url: str) -> None:
        opener = self._connect_fn
        if opener is None:
            import websockets

            opener = websockets.connect
        close_code: Optional[int] = None
        try:
            async with opener(url) as ws:
                self._ws = ws
                self.connected = True
                self._session_started_at = time.time()
                self.last_error = None
                self.last_close_code = None
                async for raw in ws:
                    if not self._running:
                        break
                    try:
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")
                        msg = json.loads(raw)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if not isinstance(msg, dict):
                        continue
                    # Enqueue so receive is not blocked by EV / REST work.
                    try:
                        self._queue.put_nowait(msg)
                    except asyncio.QueueFull:
                        await self._queue.put(msg)
                close_code = ws_close_code(ws)
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            close_code = ws_close_code(ex) or close_code
            raise
        finally:
            self._ws = None
            if close_code is not None:
                self.last_close_code = close_code


_shared_feed: Optional[OddsApiWsFeed] = None
_shared_feed_lock = asyncio.Lock()
_recovery_lock: Optional[asyncio.Lock] = None


def _get_recovery_lock() -> asyncio.Lock:
    """Process-wide single-flight for REST recovery (monitors + dashboard)."""
    global _recovery_lock
    if _recovery_lock is None:
        _recovery_lock = asyncio.Lock()
    return _recovery_lock


async def get_shared_odds_ws_feed() -> Optional[OddsApiWsFeed]:
    """Process-wide singleton — Odds-API allows one WS connection per API key."""
    global _shared_feed
    if not odds_api_ws_wanted():
        return None
    async with _shared_feed_lock:
        if _shared_feed is None:
            rest = await get_shared_odds_client()
            _shared_feed = OddsApiWsFeed(rest_client=rest)
            await _shared_feed.start()
        return _shared_feed


def peek_shared_odds_ws_feed() -> Optional[OddsApiWsFeed]:
    return _shared_feed


def live_events_from_ws_store() -> List[Dict[str, Any]]:
    """Live-event list from the shared Odds-API WS store (no HTTP).

    Used when REST ``/events/live`` 429s or fails. This is the event
    *list* only — price recovery stays fail-closed in ``resolve_odds_docs``.
    """
    feed = peek_shared_odds_ws_feed()
    if feed is None:
        return []
    return feed.store.slate_events()


async def reset_shared_odds_ws_feed() -> None:
    global _shared_feed
    async with _shared_feed_lock:
        if _shared_feed is not None:
            await _shared_feed.stop()
            _shared_feed = None


def odds_docs_from_ws(event_ids: Sequence[int]) -> Optional[List[Dict[str, Any]]]:
    """
    Return merged /odds-shaped docs from the live WS store, or None if WS is not usable.

    While the socket is healthy, always serve the store. During a short reconnect
    gap, keep serving the last store snapshot (grace) so a flap cannot empty the
    board before REST ``/odds/updated`` recovers.
    """
    feed = peek_shared_odds_ws_feed()
    if feed is None:
        return None
    if feed.healthy:
        return feed.store.merged_docs(event_ids)
    try:
        grace = float(os.getenv("ODDS_API_WS_STORE_GRACE_SEC", "45") or "45")
    except ValueError:
        grace = 45.0
    if grace <= 0:
        return None
    since = feed._unhealthy_since
    if since is None:
        return None
    if (time.time() - float(since)) > grace:
        return None
    docs = feed.store.merged_docs(event_ids)
    return docs if docs else None


def _updated_since_window_sec() -> int:
    since_env = os.getenv("ODDS_API_UPDATED_SINCE_SEC", "60")
    try:
        return max(5, min(90, int(since_env)))
    except ValueError:
        return 60


async def resolve_odds_docs(
    rest_client: Any,
    event_ids: Sequence[int],
    bookmakers: Optional[Sequence[str]] = None,
    *,
    odds_cache_ttl: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    WS-first odds fetch. Returns ``(docs, source)`` where source is
    ``websocket`` | ``rest_updated`` | ``rest_multi`` | ``unavailable``.

    When the WebSocket is enabled but down, recovery is single-flight REST
    ``/odds/updated`` with cooldown. Rate limits and cooldown fail closed
    (empty docs) instead of serving stale store lines or hammering
    ``/odds/multi``.
    """
    ids = [int(x) for x in event_ids]
    ws_docs = odds_docs_from_ws(ids)
    if ws_docs is not None:
        return ws_docs, "websocket"

    books = list(bookmakers) if bookmakers else odds_api_master_bookmakers()
    feed = peek_shared_odds_ws_feed()

    if odds_api_ws_wanted():
        async with _get_recovery_lock():
            ws_docs = odds_docs_from_ws(ids)
            if ws_docs is not None:
                return ws_docs, "websocket"
            feed = peek_shared_odds_ws_feed()
            if feed is None or not feed.should_attempt_rest_fallback():
                return [], "unavailable"
            since = int(time.time()) - _updated_since_window_sec()
            # /odds/updated requires sport display names (Baseball, not baseball).
            sports_for_fallback = []
            for eid in ids:
                meta = (feed.store.event_meta or {}).get(int(eid)) or {}
                if meta.get("sport") is not None:
                    sports_for_fallback.append(meta.get("sport"))
            try:
                n = await feed.rest_updated_fallback(
                    since, books, sports=sports_for_fallback or None
                )
            except Exception as ex:
                print(f"[ODDS-API WS] [WARN] updated fallback failed: {ex}")
                if is_odds_api_rate_limit_error(ex):
                    feed._arm_fallback_cooldown(rate_limited=True)
                return [], "unavailable"
            if n > 0:
                return feed.store.merged_docs(ids), "rest_updated"
            return [], "unavailable"

    docs = await rest_client.get_odds_multi(ids, books, odds_cache_ttl=odds_cache_ttl)
    if feed is not None:
        feed.store.apply_rest_docs(docs)
    return docs, "rest_multi"
