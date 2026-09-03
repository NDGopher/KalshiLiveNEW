"""
PLive (Pandora) odds subscriber — Origin-only Socket.IO, no login.

Handshake matches the public live UI at https://plive.becoms.co/live/ :

  wss://pandora.ganchrow.com/socket.io/?EIO=4&transport=websocket
  Header Origin: https://plive.becoms.co
  After CONNECT:
    1) setSocketMetadata {partnerId: 113, flavor: "live"}
    2) subscribeSystemEvents {partnerId: 113}
    3) subscribe + getCache once for live.sports (catalog names) and
       live.main.<LINE_SET>.eventData (directory). live.events is dead.
    4) For each wanted live eventId: subscribe AND getCache
       ``live.main.<LINE_SET>.eventCoefficients.{eventId}``
       (MLB sport 1 / league 8, plus soccer sport 5 and Top Soccer 220).
       getCache often delivers a JSON-patch *list* (or ``{isDiff, payload}``)
       on the room name — that list is a coeff snapshot, not a catalog.
       subscribe keeps the room open for live diffs. Re-getCache rooms
       that stay unpriced; re-subscribe+getCache if prices go stale.
       On reconnect, drop room bindings and subscribe/getCache again.
       Unsubscribe that room when the event is finished.

Do not scrape ``https://plive.becoms.co/live/?#!/event/{id}`` — the hash
is a client-side route; ``{id}`` is the pandora event id. This is not a
per-sport price socket. Bare connect is silent. No BetBCK. No cookies.

MLB is catalog sport 1 (hash ``#!/sport/1``). Soccer is native sport 5
(``#!/sport/5``). ``#!/sport/220`` is the public-UI Top Soccer bucket.
``live.sports`` names 5=Soccer and 220=Top Soccer on the same
connection. Both ride one ``eventData`` directory (``s[5]`` / ``s[220]``)
— there is no extra sport-room subscribe. ``s[220]`` may be omitted
when that UI bucket is empty; native 5 still carries the soccer slate.
Trust the live.sports catalog over any old Selenium sport map.

Mapping boundary: Odds-API event IDs already join Odds-API books to
each other. PLive uses Pandora ids and needs a separate fixture join.
Soccer matching is conservative (normalized league, same-orientation
home+away tokens, start/live timing, stale-price age). Zero or two-plus
hits emit no PLive markets (no EV). MLB still uses the looser
swap-tolerant team matcher.

Lines become ``bookmakers["PLive"]``. PLive is a betting / take venue
(same role as Kalshi), not a sharp. Fair is the other pack.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import re
import time
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from odds_api_client import _canonical_odds_api_bookmaker, sport_slug_query_for_api

PLIVE_BOOK_NAME = "PLive"
PLIVE_ORIGIN = "https://plive.becoms.co"
PLIVE_URL = "wss://pandora.ganchrow.com"
PLIVE_SOCKETIO_PATH = "/socket.io/"
PLIVE_PARTNER_ID = 113
PLIVE_FLAVOR = "live"
PLIVE_DISTRO = "main"
# Public UI LINE_SET constant (live.main.<this>.eventData / eventCoefficients).
PLIVE_LINE_SET = "U0VWU1NWUkJSMFU9"
PLIVE_MLB_SPORT_ID = 1
PLIVE_MLB_LEAGUE_ID = 8  # eventData path s[1][…][8] = MLB; not catalog sport 8 (Tennis)
PLIVE_MLB_HASH = "#!/sport/1"
PLIVE_SOCCER_SPORT_ID = 5
PLIVE_SOCCER_HASH = "#!/sport/5"
PLIVE_TOP_SOCCER_SPORT_ID = 220
PLIVE_TOP_SOCCER_HASH = "#!/sport/220"
# Native Soccer (5) plus the public-UI Top Soccer bucket (220). eventData is one
# directory; both ids appear as s[5] and s[220]. Odds-API event ids do not join
# PLive — PLive is a separate fixture join.
DEFAULT_PLIVE_SPORT_IDS = (PLIVE_MLB_SPORT_ID, PLIVE_SOCCER_SPORT_ID, PLIVE_TOP_SOCCER_SPORT_ID)
PLIVE_SOCCER_SPORT_IDS = (PLIVE_SOCCER_SPORT_ID, PLIVE_TOP_SOCCER_SPORT_ID)
# Start-time join window when both sides publish a kickoff. 15 minutes.
DEFAULT_PLIVE_START_TOLERANCE_SEC = 900
# Reject PLive take prices older than this. Coeffs stamp ``coeff_updated_at``.
DEFAULT_PLIVE_STALE_SEC = 90

# Fallback only. Prefer names from the live.sports snapshot when present.
# Do NOT use the old UnifiedBetting2 Selenium map (nfl=2 / nba=3) — that
# conflicts with this live catalog (2=Basketball, 3=Football).
PLIVE_SPORT_CATALOG_FALLBACK: Dict[int, str] = {
    1: "Baseball",
    2: "Basketball",
    3: "Football",
    4: "Hockey",
    5: "Soccer",
    8: "Tennis",
    102: "College Basketball",
    114: "E-Sports",
    214: "FIFA World Cup 2026",
    220: "Top Soccer",
}

_SPORT_HASH_RE = re.compile(r"#!?/sport/(\d+)", re.I)
_EVENT_HASH_RE = re.compile(r"#!?/event/(\d+)", re.I)

# Ganchrow coefficient tree (live MLB, event 199298371):
#   /c/m/{market}/o/{outcome}/{index}
# Market 6 run line: each outcome is a HOME handicap. [idx0, idx1] is a
# 2-way pair (~7% hold), NOT [money price, decimal]. Both slots are decimals.
# Market 3 = Game Winner ML. idx1 is the public-UI decimal. Do not treat
# [idx0, idx1] as a 2-way pair. idx0 is not the take price. Live market 3
# replaces stale Odds-API PLive ML. Markets 10/9/1 are not Game Winner
# (first-5 / other) and must not paint a ML card.
# Market 5 = game totals. 7/8 = team totals (click-in only) — never on Spread.
# Soccer totals are not MLB market-3 idx1 and not a market-6 [idx0, idx1]
# home/away pair. Strike / market identity is the outcome key only
# (2.5 / 3.5 / 4.5, quarter-point alts, over_2.5 / under_2.5). Never infer
# a line from a coefficient (idx0/idx1/idx2 or a price like +186=2.86).
# Side-named soccer slots: idx0 is the take. idx1 is not Over/Under.
# Line-only keys emit only when [idx0, idx1] is a real Over/Under pair.
# Missing or mismatched strikes are dropped — never reuse a nearby line.
# eventData list is [home, away] (stadium home first).
# Sox @ Astros 199298371 Game tab: Astros −1.5 is unpriced. The only +325
# on that event is Chicago White Sox Total Over 2.5 (market 7/8), not a run line.
PLIVE_ML_MARKET = 3
_DEFAULT_ML_MARKETS = (3,)
_DEFAULT_SPREAD_MARKETS = (6,)
_DEFAULT_TOTAL_MARKETS = (5,)
_TEAM_TOTAL_MARKETS = (7, 8)
PLIVE_RUN_LINE_MARKET = 6
PLIVE_GAME_TOTAL_MARKET = 5


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def plive_wanted() -> bool:
    """On by default. Set ``PLIVE_ENABLED=false`` to disable."""
    raw = os.getenv("PLIVE_ENABLED")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _int_csv(name: str, default: Sequence[int]) -> Tuple[int, ...]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return tuple(int(x) for x in default)
    out: List[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return tuple(out) if out else tuple(int(x) for x in default)


def parse_sport_hash(value: str) -> Optional[int]:
    """Parse ``#!/sport/220`` or a full PLive live URL into a catalog sport id."""
    if not value:
        return None
    m = _SPORT_HASH_RE.search(str(value))
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def parse_event_hash(value: str) -> Optional[str]:
    """Parse ``#!/event/199298371`` — the pandora event id. No HTML fetch."""
    if not value:
        return None
    m = _EVENT_HASH_RE.search(str(value))
    return m.group(1) if m else None


def plive_sport_ids() -> List[int]:
    """Sports to keep from eventData. Default MLB (1) + Soccer (5) + Top Soccer (220).

    ``PLIVE_SPORT_IDS`` is the explicit list. ``PLIVE_PAGE`` / ``#!/sport/5`` is
    added to the default set so a soccer URL does not drop MLB. A single
    ``PLIVE_SPORT_ID`` is not used as an exclusive filter.
    """
    raw_multi = (os.getenv("PLIVE_SPORT_IDS") or "").strip()
    if raw_multi:
        out: List[int] = []
        seen: Set[int] = set()
        for part in raw_multi.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                n = int(part)
            except ValueError:
                continue
            if n not in seen:
                seen.add(n)
                out.append(n)
        if out:
            return out
    ids = list(DEFAULT_PLIVE_SPORT_IDS)
    hashed = parse_sport_hash((os.getenv("PLIVE_PAGE") or os.getenv("PLIVE_HASH") or "").strip())
    if hashed is not None and hashed not in ids:
        ids.append(hashed)
    return ids


def plive_soccer_sport_ids() -> Tuple[int, ...]:
    """PLive catalog ids treated as soccer: native 5 and Top Soccer 220."""
    wanted = set(plive_sport_ids())
    return tuple(i for i in PLIVE_SOCCER_SPORT_IDS if i in wanted) or PLIVE_SOCCER_SPORT_IDS


def plive_sport_id() -> int:
    """Primary / status sport. First of ``plive_sport_ids()`` (MLB=1 by default)."""
    ids = plive_sport_ids()
    return int(ids[0]) if ids else PLIVE_MLB_SPORT_ID


def plive_start_tolerance_sec() -> int:
    try:
        return max(0, int(os.getenv("PLIVE_START_TOLERANCE_SEC", str(DEFAULT_PLIVE_START_TOLERANCE_SEC))))
    except ValueError:
        return DEFAULT_PLIVE_START_TOLERANCE_SEC


def plive_stale_sec() -> int:
    try:
        return max(1, int(os.getenv("PLIVE_STALE_SEC", str(DEFAULT_PLIVE_STALE_SEC))))
    except ValueError:
        return DEFAULT_PLIVE_STALE_SEC


def plive_line_prefix() -> str:
    line_set = (os.getenv("PLIVE_LINE_SET") or PLIVE_LINE_SET).strip() or PLIVE_LINE_SET
    distro = (os.getenv("PLIVE_DISTRO") or PLIVE_DISTRO).strip() or PLIVE_DISTRO
    flavor = (os.getenv("PLIVE_FLAVOR") or PLIVE_FLAVOR).strip() or PLIVE_FLAVOR
    return f"{flavor}.{distro}.{line_set}"


def plive_event_data_topic() -> str:
    return f"{plive_line_prefix()}.eventData"


def plive_event_coefficients_topic() -> str:
    return f"{plive_line_prefix()}.eventCoefficients"


def public_ui_subscribe_topics() -> List[str]:
    """Directory + catalog names only. Per-event prices are ``eventCoefficients.{id}``."""
    flavor = (os.getenv("PLIVE_FLAVOR") or PLIVE_FLAVOR).strip() or PLIVE_FLAVOR
    topics = [
        f"{flavor}.sports",
        plive_event_data_topic(),
    ]
    extra = os.getenv("PLIVE_SUBSCRIBE_ROOMS", "").strip()
    if extra:
        topics.extend(p.strip() for p in extra.split(",") if p.strip())
    return list(dict.fromkeys(topics))


def handshake_emits() -> List[Tuple[str, Any]]:
    """Exact post-CONNECT emits. No cookies, no BetBCK."""
    partner = int(os.getenv("PLIVE_PARTNER_ID", str(PLIVE_PARTNER_ID)) or PLIVE_PARTNER_ID)
    flavor = (os.getenv("PLIVE_FLAVOR") or PLIVE_FLAVOR).strip() or PLIVE_FLAVOR
    topics = public_ui_subscribe_topics()
    return [
        ("setSocketMetadata", {"partnerId": partner, "flavor": flavor}),
        ("subscribeSystemEvents", {"partnerId": partner}),
        ("subscribe", topics),
        ("getCache", topics),
    ]


# Acks the public UI / confirmed live feed return after the handshake.
EXPECTED_SYSTEM_EVENT_ROOMS = ("system-events", "notifications.partner.113")
EXPECTED_SUBSCRIBED_ROOMS = (
    "live.sports",
    f"live.main.{PLIVE_LINE_SET}.eventData",
)


def note_handshake_ack(event_name: Optional[str], data: Any = None) -> Optional[str]:
    """Normalize socketMetadataSet / subscribedSystemEvents / subscribed acks."""
    name = str(event_name or "")
    if isinstance(data, dict) and data.get("event") == "socketMetadataSet":
        return "socketMetadataSet"
    if name in ("socketMetadataSet", "setSocketMetadata"):
        return "socketMetadataSet"
    if name == "subscribedSystemEvents":
        return "subscribedSystemEvents"
    if name == "subscribed":
        return "subscribed"
    return None


def coeff_room_for_event(event_id: str) -> str:
    return f"{plive_event_coefficients_topic()}.{event_id}"


def _is_sports_topic(event_name: Optional[str]) -> bool:
    t = str(event_name or "").lower()
    return t.endswith(".sports") or t == "sports" or t.endswith(".leagues")


def _is_event_list_topic(event_name: Optional[str]) -> bool:
    """``eventData`` is the slate directory. ``live.events`` is a dead room."""
    t = str(event_name or "")
    if not t:
        return False
    if "eventCoefficients" in t:
        return False
    if t.endswith(".events") or t == "live.events":
        return False
    return t.endswith(".eventData")


def _is_coeff_topic(event_name: Optional[str]) -> bool:
    return "eventCoefficients" in str(event_name or "")


def _looks_like_json_patch_list(data: Any) -> bool:
    """getCache on eventCoefficients often emits a bare JSON-patch list."""
    if not isinstance(data, list) or not data:
        return False
    sample = next((x for x in data if x is not None), None)
    return isinstance(sample, dict) and "op" in sample and "path" in sample


def _iso_utc(ts: Optional[float]) -> Optional[str]:
    try:
        stamp = float(ts or 0.0)
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))


def _eid_from_coeff_payload(data: Any) -> Optional[str]:
    """Event id from a cache wrapper when the Socket.IO event is not the room."""
    if not isinstance(data, dict):
        return None
    for key in ("room", "channel", "topic", "event", "name"):
        eid = event_id_from_channel(str(data.get(key) or ""))
        if eid:
            return eid
    return None


def _unwrap_coeff_body(data: Any) -> Any:
    """Pull ``c.m`` / patch lists out of getCache wrappers."""
    if not isinstance(data, dict):
        return data
    for key in ("payload", "data", "value", "d", "cache"):
        inner = data.get(key)
        if isinstance(inner, (dict, list)):
            return inner
    return data


def _coerce_socket_payload(data: Any) -> Any:
    """Bytes (gzip JSON) or a JSON string. Bare non-JSON strings are dropped."""
    if isinstance(data, (bytes, bytearray)):
        try:
            decompressed = gzip.decompress(bytes(data))
            return json.loads(decompressed.decode("utf-8"))
        except Exception:
            try:
                return json.loads(bytes(data).decode("utf-8"))
            except Exception:
                return None
    if isinstance(data, str):
        s = data.strip()
        if s[:1] in "{[":
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return None
        return None
    return data


def parse_coeff_path(path: str) -> Optional[Dict[str, Any]]:
    """Parse JSON Patch path ``/c/m/10/o/2/0`` into market / outcome / index."""
    if not path:
        return None
    parts = [p for p in str(path).split("/") if p]
    if len(parts) >= 5 and parts[0] == "c" and parts[1] == "m" and parts[3] == "o":
        try:
            market = int(parts[2])
        except ValueError:
            return None
        idx: Optional[int] = None
        if len(parts) > 5:
            try:
                idx = int(parts[5])
            except ValueError:
                idx = None
        return {
            "market": market,
            "outcome": parts[4],
            "index": idx,
            "full_path": path,
        }
    return None


def _as_float(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _decimal_from_slot(slots: Dict[int, Any]) -> Optional[float]:
    """Totals / non-ML: prefer index 1, then 0. Not used for market 6 pairs."""
    for idx in (1, 0):
        f = _as_float(slots.get(idx) if slots.get(idx) is not None else slots.get(str(idx)))
        if f is not None and f > 1.0:
            return f
    return None


def _ml_decimal_from_slot(slots: Dict[int, Any]) -> Optional[float]:
    """Market 3 Game Winner: idx1 only. idx0 is not a 2-way pair and is not the UI price."""
    if not isinstance(slots, dict):
        return None
    f = _as_float(slots.get(1) if slots.get(1) is not None else slots.get("1"))
    if f is not None and f > 1.0:
        return f
    return None


def _as_decimal_pair(value: Any) -> Optional[Tuple[float, float]]:
    """Unwrap ``[home, away]`` or ``{0: home, 1: away}``. Both values are decimals."""
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        a, b = _as_float(value[0]), _as_float(value[1])
        if a is not None and b is not None and a > 1.0 and b > 1.0:
            return (a, b)
        return None
    if isinstance(value, dict):
        a = _as_float(value.get(0) if value.get(0) is not None else value.get("0"))
        b = _as_float(value.get(1) if value.get(1) is not None else value.get("1"))
        if a is None:
            a = _as_float(value.get("home"))
        if b is None:
            b = _as_float(value.get("away"))
        if a is not None and b is not None and a > 1.0 and b > 1.0:
            return (a, b)
    return None


def is_team_total_market_id(market: Any) -> bool:
    try:
        return int(market) in _TEAM_TOTAL_MARKETS
    except (TypeError, ValueError):
        return False


def is_run_line_spread_row(row: Any) -> bool:
    """True only for a market-6 home/away pair. Team-total overs never pass."""
    if not isinstance(row, dict):
        return False
    mk = row.get("plive_market")
    if is_team_total_market_id(mk):
        return False
    if mk is not None:
        try:
            if int(mk) == PLIVE_GAME_TOTAL_MARKET or int(mk) in _DEFAULT_TOTAL_MARKETS:
                return False
        except (TypeError, ValueError):
            pass
    if str(row.get("market_type") or "").lower() in ("team_total", "game_total"):
        return False
    if row.get("over") is not None and row.get("home") is None:
        return False
    try:
        home = float(row.get("home"))
        away = float(row.get("away"))
    except (TypeError, ValueError):
        return False
    return home > 1.0 and away > 1.0


def sanitize_plive_markets(markets: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Drop team-total rows from Spread / game Totals before they hit a tile."""
    out: List[Dict[str, Any]] = []
    for m in markets or []:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        name = str(m.get("name"))
        rows = [r for r in (m.get("odds") or []) if isinstance(r, dict)]
        if name == "Spread":
            rows = [r for r in rows if is_run_line_spread_row(r)]
            if not rows:
                continue
            out.append({**m, "odds": rows})
            continue
        if name == "Totals":
            rows = [r for r in rows if not is_team_total_market_id(r.get("plive_market"))]
            if not rows:
                continue
            out.append({**m, "odds": rows})
            continue
        out.append(m)
    return out


def _spread_pair_from_slots(slots: Dict[int, Any]) -> Optional[Tuple[float, float]]:
    """Market 6: [idx0, idx1] is the 2-way pair. Keep alts (9.78 / 1.03 is a real hold)."""
    if not isinstance(slots, dict):
        return None
    a = _as_float(slots.get(0))
    b = _as_float(slots.get(1))
    if a is not None and b is not None and a > 1.0 and b > 1.0:
        return (a, b)
    for idx in (1, 0, 2):
        pair = _as_decimal_pair(slots.get(idx))
        if pair:
            return pair
    return None


def _slot_decimal(slots: Any, idx: int) -> Optional[float]:
    if not isinstance(slots, dict):
        return None
    f = _as_float(slots.get(idx) if slots.get(idx) is not None else slots.get(str(idx)))
    if f is not None and f > 1.0:
        return f
    return None


def is_game_totals_market_name(name: Any) -> bool:
    n = str(name or "").strip()
    if not n:
        return False
    u = n.upper()
    if "TEAM" in u:
        return False
    return n == "Totals" or "TOTAL" in u


def _is_plausible_game_total_line(line: Optional[float], *, soccer: bool) -> bool:
    """Half-point (MLB) or quarter-point (soccer Asian) grids only.

    Prices are not lines: 2.86 (+186) and 3.45 (+245) fail the 0.25 grid.
    """
    if line is None:
        return False
    try:
        lf = float(line)
    except (TypeError, ValueError):
        return False
    if lf != lf or lf <= 0:
        return False
    step = 4.0 if soccer else 2.0
    stepped = lf * step
    if abs(stepped - round(stepped)) > 1e-6:
        return False
    if soccer:
        return 0.25 <= lf <= 15.0
    return 0.25 <= lf <= 50.0


def _valid_ou_hold(over: float, under: float) -> bool:
    """True when idx0/idx1 look like a real Over/Under two-way, not two take coeffs."""
    if over <= 1.0 or under <= 1.0:
        return False
    implied = (1.0 / over) + (1.0 / under)
    return 0.88 <= implied <= 1.15


_SOCCER_TOTAL_OUTCOME_RE = re.compile(
    r"^(?:(?P<side>over|under|o|u)[\s_\-/]*)?(?P<line>\d+(?:\.\d+)?)(?:[\s_\-/]*(?P<side2>over|under|o|u))?$",
    re.I,
)


def parse_soccer_total_outcome(outcome: Any) -> Tuple[Optional[float], Optional[str]]:
    """Strike + side from the outcome / market key only. Never from a price.

    Bare integers (``3`` / ``4``) are over/under codes, not 3.0 / 4.0 lines.
    """
    raw = str(outcome or "").strip().lower()
    if not raw:
        return None, None
    if raw in ("over", "o"):
        return None, "over"
    if raw in ("under", "u"):
        return None, "under"
    if re.fullmatch(r"\d{1,2}", raw):
        return None, None
    m = _SOCCER_TOTAL_OUTCOME_RE.match(raw)
    if not m:
        return None, None
    line_tok = m.group("line")
    if line_tok is not None and re.fullmatch(r"\d{1,2}", line_tok) and "." not in raw:
        # ``over3`` / ``u4`` without a decimal — not an authoritative strike.
        return None, None
    line = _as_float(line_tok)
    if not _is_plausible_game_total_line(line, soccer=True):
        line = None
    side_tok = (m.group("side") or m.group("side2") or "").lower()
    side = None
    if side_tok in ("over", "o"):
        side = "over"
    elif side_tok in ("under", "u"):
        side = "under"
    return line, side


def soccer_totals_identity_rows(markets: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Visible board rows: exact strike + Over/Under American. For payload diffs."""
    from ev_calculator import decimal_to_american

    out: List[Dict[str, Any]] = []
    for m in markets or []:
        if not isinstance(m, dict) or not is_game_totals_market_name(m.get("name")):
            continue
        for row in m.get("odds") or []:
            if not isinstance(row, dict):
                continue
            line = row.get("hdp")
            if line is None:
                continue
            try:
                lf = float(line)
            except (TypeError, ValueError):
                continue
            over = _as_float(row.get("over"))
            under = _as_float(row.get("under"))
            out.append(
                {
                    "line": lf,
                    "over": over,
                    "under": under,
                    "over_am": decimal_to_american(over) if over and over > 1.0 else None,
                    "under_am": decimal_to_american(under) if under and under > 1.0 else None,
                }
            )
    return out


def _soccer_total_side_take_decimal(slots: Any) -> Optional[float]:
    """Side-named soccer total: idx0 is the take. idx1 is never Over/Under.

    Independent of MLB Game Winner (idx1). If only idx1 is populated (patch
    with a single live slot), use that sole price.
    """
    a = _slot_decimal(slots, 0)
    if a is not None:
        return a
    return _slot_decimal(slots, 1)


_EVENT_ID_RE = re.compile(r"(?:eventCoefficients|eventData|event)[./](\d+)", re.I)


def _first_name(obj: Any) -> Optional[str]:
    if isinstance(obj, str) and obj.strip():
        return obj.strip()
    if isinstance(obj, dict):
        for k in ("name", "n", "shortName", "sn", "displayName", "teamName", "desc"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _teams_from_event(data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    home = data.get("home") or data.get("homeTeam") or data.get("home_name") or data.get("t2")
    away = data.get("away") or data.get("awayTeam") or data.get("away_name") or data.get("t1")
    home = _first_name(home) if not isinstance(home, str) else home
    away = _first_name(away) if not isinstance(away, str) else away
    participants = (
        data.get("participants")
        or data.get("teams")
        or data.get("p")
        or data.get("contestants")
    )
    names: List[str] = []
    if isinstance(participants, list):
        for p in participants:
            n = _first_name(p) if not isinstance(p, str) else p.strip()
            if n:
                names.append(n)
    elif isinstance(participants, dict):
        for key in ("1", 1, "away", "a"):
            n = _first_name(participants.get(key))
            if n:
                names.append(n)
                break
        for key in ("2", 2, "home", "h"):
            n = _first_name(participants.get(key))
            if n:
                names.append(n)
                break
    if len(names) >= 2:
        away = away or names[0]
        home = home or names[1]
    return (str(home) if home else None, str(away) if away else None)


def _looks_like_event(rec: Dict[str, Any]) -> bool:
    if not isinstance(rec, dict):
        return False
    if rec.get("id") or rec.get("eventId") or rec.get("ei"):
        if rec.get("sportId") or rec.get("si") or rec.get("s") or rec.get("home") or rec.get("p") or rec.get("participants"):
            return True
    home, away = _teams_from_event(rec)
    return bool(home and away)


def _team_name_from_row(row: Any) -> Optional[str]:
    if isinstance(row, str) and row.strip():
        return row.strip()
    if isinstance(row, (list, tuple)) and row:
        return _team_name_from_row(row[0])
    if isinstance(row, dict):
        return _first_name(row)
    return None


def walk_event_data_tree(
    node: Any,
    *,
    sport_id: Optional[int] = None,
    path: Optional[List[str]] = None,
) -> Iterable[Tuple[str, Dict[str, Any]]]:
    """Walk ``payload.s[sport][…][eventId] = [homeRow, awayRow, …]`` (stadium home first)."""
    path = path or []
    if isinstance(node, list) and len(node) >= 2 and isinstance(node[0], (list, tuple, dict, str)):
        home = _team_name_from_row(node[0])
        away = _team_name_from_row(node[1])
        if away and home and path:
            eid = path[-1]
            if eid.isdigit():
                rec: Dict[str, Any] = {"id": eid, "away": away, "home": home}
                if sport_id is not None:
                    rec["sportId"] = sport_id
                if len(path) >= 2 and str(path[-2]).isdigit():
                    rec["leagueId"] = int(path[-2])
                for extra in node[2:]:
                    if isinstance(extra, (int, float)) and extra > 1_000_000_000:
                        rec["start"] = int(extra)
                        continue
                    if not isinstance(extra, dict):
                        continue
                    if extra.get("ip") is True:
                        rec["finished"] = False
                        rec["ip"] = True
                    elif extra.get("ip") is False or extra.get("finished") is True:
                        rec["finished"] = True
                    for lk in ("league", "leagueName", "competition", "name"):
                        val = extra.get(lk)
                        if isinstance(val, str) and val.strip():
                            rec["leagueName"] = val.strip()
                            break
                    st = extra.get("start") or extra.get("startTime") or extra.get("date")
                    unix = _parse_start_unix(st)
                    if unix:
                        rec["start"] = unix
                yield eid, rec
        return
    if not isinstance(node, dict):
        return
    for key, val in node.items():
        sid = sport_id
        if not path:
            try:
                sid = int(key)
            except (TypeError, ValueError):
                sid = sport_id
        yield from walk_event_data_tree(val, sport_id=sid, path=path + [str(key)])


def iter_event_records(data: Any, *, _depth: int = 0) -> Iterable[Tuple[str, Dict[str, Any]]]:
    """Walk an eventData / live.events snapshot for id + team records."""
    if _depth > 8 or data is None:
        return
    if isinstance(data, list):
        for item in data:
            yield from iter_event_records(item, _depth=_depth + 1)
        return
    if not isinstance(data, dict):
        return
    inner = data.get("payload") if "payload" in data and data.get("isDiff") is not True else None
    if inner is not None and inner is not data:
        yield from iter_event_records(inner, _depth=_depth + 1)
        return
    if isinstance(data.get("s"), dict):
        yield from walk_event_data_tree(data["s"])
        return
    if data.get("isDiff") and isinstance(data.get("payload"), list):
        for op in data["payload"]:
            if not isinstance(op, dict) or op.get("op") not in ("add", "replace"):
                continue
            val = op.get("value")
            path = [p for p in str(op.get("path") or "").split("/") if p]
            if isinstance(val, list) and path and path[0] == "s":
                try:
                    sid = int(path[1])
                except (IndexError, ValueError):
                    sid = None
                yield from walk_event_data_tree(val, sport_id=sid, path=path[1:])
                continue
            if isinstance(val, dict) and _looks_like_event(val):
                eid = str(val.get("id") or val.get("eventId") or val.get("ei") or "")
                if eid:
                    yield eid, val
        return
    if _looks_like_event(data):
        eid = str(data.get("id") or data.get("eventId") or data.get("ei") or "")
        if eid:
            yield eid, data
            return
    for key, val in data.items():
        if key in ("payload", "parsedPayload", "topicInfo", "ti"):
            yield from iter_event_records(val, _depth=_depth + 1)
            continue
        if key == "s" and isinstance(val, dict):
            yield from walk_event_data_tree(val)
            continue
        if isinstance(val, dict) and _looks_like_event(val):
            eid = str(val.get("id") or val.get("eventId") or val.get("ei") or key)
            if eid and eid not in ("payload", "c", "m"):
                yield eid, val
        elif isinstance(val, (dict, list)):
            yield from iter_event_records(val, _depth=_depth + 1)


def _unwrap_catalog(data: Any) -> List[Tuple[str, Dict[str, Any]]]:
    out: List[Tuple[str, Dict[str, Any]]] = []
    if isinstance(data, dict) and data.get("payload") is not None and not data.get("isDiff"):
        data = data["payload"]
    if isinstance(data, dict):
        for key in ("sports", "leagues", "value"):
            if isinstance(data.get(key), (dict, list)):
                data = data[key]
                break
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                out.append((str(item.get("id") or ""), item))
    elif isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, dict):
                out.append((str(key), val))
            elif isinstance(val, str) and str(key).isdigit():
                out.append((str(key), {"id": key, "name": val}))
    return out


def _catalog_has_s_tree(data: Any) -> bool:
    """True when this payload is a full ``payload.s`` directory (not a lone event)."""
    if not isinstance(data, dict):
        return False
    if isinstance(data.get("s"), dict):
        return True
    payload = data.get("payload")
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("s"), dict)
        and data.get("isDiff") is not True
    ):
        return True
    return False


def event_id_from_channel(event_name: Optional[str]) -> Optional[str]:
    if not event_name:
        return None
    m = _EVENT_ID_RE.search(str(event_name))
    if m:
        return m.group(1)
    return None


_TEAM_STOPWORDS = frozenset(
    {
        "fc",
        "cf",
        "club",
        "sc",
        "ac",
        "afc",
        "cfc",
        "the",
        "de",
        "la",
        "el",
        "football",
        "soccer",
        "team",
    }
)
_COMP_STOPWORDS = frozenset(
    {
        "the",
        "league",
        "liga",
        "cup",
        "division",
        "conference",
        "men",
        "women",
        "football",
        "soccer",
        "club",
    }
)


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def _norm_team(s: str) -> str:
    t = _strip_accents(s or "").lower()
    for ch in ("'", ".", ",", "-", "_"):
        t = t.replace(ch, " ")
    t = t.replace("utd", "united")
    return " ".join(t.split())


def _team_identity_tokens(s: str) -> Set[str]:
    return {w for w in _norm_team(s).split() if w and w not in _TEAM_STOPWORDS}


def _comp_identity_tokens(s: str) -> Set[str]:
    t = _strip_accents(s or "").lower()
    for ch in ("'", ".", ",", "-", "_"):
        t = t.replace(ch, " ")
    return {w for w in t.split() if w and w not in _COMP_STOPWORDS}


def _parse_start_unix(raw: Any) -> Optional[int]:
    """Odds-API ``date`` / ``startsAt`` / ``startTime`` / ``commence_time``, or unix seconds."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        n = float(raw)
        if n > 1e12:
            n = n / 1000.0
        if n > 1_000_000_000:
            return int(n)
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        n = float(s)
        if n > 1e12:
            n = n / 1000.0
        if n > 1_000_000_000:
            return int(n)
    except ValueError:
        pass
    iso = s.replace("Z", "+00:00")
    try:
        from datetime import datetime

        dt = datetime.fromisoformat(iso)
        return int(dt.timestamp())
    except ValueError:
        return None


def odds_event_start_unix(doc: Optional[Dict[str, Any]]) -> Optional[int]:
    ev = doc if isinstance(doc, dict) else {}
    nest = ev.get("event") if isinstance(ev.get("event"), dict) else {}
    for blob in (ev, nest):
        if not isinstance(blob, dict):
            continue
        for key in ("startTime", "startsAt", "commence_time", "date", "start"):
            unix = _parse_start_unix(blob.get(key))
            if unix:
                return unix
    return None


def plive_event_start_unix(ev: Optional[Dict[str, Any]]) -> Optional[int]:
    if not isinstance(ev, dict):
        return None
    return _parse_start_unix(ev.get("start") or ev.get("startTime") or ev.get("date"))


def _odds_doc_is_soccer(doc: Optional[Dict[str, Any]]) -> bool:
    ev = doc if isinstance(doc, dict) else {}
    sp = ev.get("sport") or ev.get("sport_slug")
    slug = ""
    if isinstance(sp, dict):
        slug = str(sp.get("slug") or sp.get("name") or "")
    else:
        slug = str(sp or "")
    q = sport_slug_query_for_api(slug)
    return q == "football"


def _odds_event_is_live(doc: Optional[Dict[str, Any]]) -> bool:
    ev = doc if isinstance(doc, dict) else {}
    if ev.get("live") is True or ev.get("isLive") is True or ev.get("ip") is True:
        return True
    st = str(ev.get("status") or ev.get("state") or "").lower().replace(" ", "")
    return st in ("live", "inprogress", "inplay", "started", "running")


def _plive_event_is_live(ev: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(ev, dict) or ev.get("finished") is True:
        return False
    if ev.get("ip") is True or ev.get("live") is True:
        return True
    return ev.get("finished") is False


def _odds_league_name(doc: Optional[Dict[str, Any]]) -> str:
    ev = doc if isinstance(doc, dict) else {}
    lg = ev.get("league")
    if isinstance(lg, dict):
        return str(lg.get("name") or lg.get("slug") or "").strip()
    if isinstance(lg, str):
        return lg.strip()
    return str(ev.get("league_name") or ev.get("leagueName") or "").strip()


def leagues_compatible(plive_name: str, odds_name: str) -> Optional[bool]:
    """True/False when both names exist. None if either side is missing (unknown)."""
    pa, pb = _comp_identity_tokens(plive_name), _comp_identity_tokens(odds_name)
    if not pa or not pb:
        return None
    if pa == pb:
        return True
    if pa <= pb or pb <= pa:
        return True
    na, nb = " ".join(sorted(pa)), " ".join(sorted(pb))
    if na in nb or nb in na:
        return True
    return False


def teams_same_orientation(odds_home: str, odds_away: str, plive_home: str, plive_away: str) -> bool:
    """Both teams must match on the same home/away side. Never join a swap."""
    th_o, ta_o = _team_identity_tokens(odds_home), _team_identity_tokens(odds_away)
    th_p, ta_p = _team_identity_tokens(plive_home), _team_identity_tokens(plive_away)
    if not th_o or not ta_o or not th_p or not ta_p:
        return False
    return th_o == th_p and ta_o == ta_p


def timing_compatible(
    plive_ev: Optional[Dict[str, Any]],
    odds_doc: Optional[Dict[str, Any]],
    *,
    tolerance_sec: Optional[int] = None,
) -> bool:
    """Require a start-time window when both publish kickoff; else both must be live."""
    if isinstance(plive_ev, dict) and plive_ev.get("finished") is True:
        return False
    tol = plive_start_tolerance_sec() if tolerance_sec is None else max(0, int(tolerance_sec))
    p_start = plive_event_start_unix(plive_ev)
    o_start = odds_event_start_unix(odds_doc)
    if p_start and o_start:
        return abs(p_start - o_start) <= tol
    return bool(_plive_event_is_live(plive_ev) and _odds_event_is_live(odds_doc))


def plive_price_is_stale(ev: Optional[Dict[str, Any]], *, now: Optional[float] = None) -> bool:
    if not isinstance(ev, dict):
        return True
    stamp = ev.get("coeff_updated_at")
    try:
        ts = float(stamp)
    except (TypeError, ValueError):
        return True
    if ts <= 0:
        return True
    age = (time.time() if now is None else float(now)) - ts
    return age > float(plive_stale_sec())


def match_plive_soccer_to_odds_doc(
    plive_events: Dict[str, Dict[str, Any]],
    odds_doc: Dict[str, Any],
    *,
    now: Optional[float] = None,
) -> Optional[str]:
    """Conservative soccer join. PLive ids ≠ Odds-API ids.

    Requires soccer sport family (PLive 5 or 220), same-orientation home+away
    token identity, non-conflicting competition, and start/live timing.
    Team-name fuzzy-only is forbidden. Swapped home/away is forbidden.
    Zero or two-plus survivors → None (no EV).
    """
    if not _odds_doc_is_soccer(odds_doc):
        return None
    home = str(odds_doc.get("home") or "")
    away = str(odds_doc.get("away") or "")
    if not home or not away:
        return None
    odds_league = _odds_league_name(odds_doc)
    soccer_ids = set(plive_soccer_sport_ids())
    hits: List[str] = []
    for eid, ev in (plive_events or {}).items():
        if not isinstance(ev, dict):
            continue
        sid = ev.get("sport_id")
        try:
            if sid is not None and int(sid) not in soccer_ids:
                continue
        except (TypeError, ValueError):
            continue
        if ev.get("finished") is True:
            continue
        ph = str(ev.get("home") or "")
        pa = str(ev.get("away") or "")
        if not teams_same_orientation(home, away, ph, pa):
            continue
        lg = str(ev.get("league_name") or ev.get("leagueName") or "")
        compat = leagues_compatible(lg, odds_league)
        if compat is False:
            continue
        if not timing_compatible(ev, odds_doc):
            continue
        if plive_price_is_stale(ev, now=now):
            continue
        hits.append(str(eid))
    if len(hits) != 1:
        return None
    return hits[0]


def _team_score(a: str, b: str) -> int:
    na, nb = _norm_team(a), _norm_team(b)
    if not na or not nb:
        return 0
    if na == nb:
        return 100
    if na in nb or nb in na:
        return 90
    try:
        from thefuzz import fuzz

        return int(fuzz.token_set_ratio(na, nb))
    except Exception:
        aw, bw = set(na.split()), set(nb.split())
        if not aw or not bw:
            return 0
        return int(100 * len(aw & bw) / len(aw | bw))


def plive_orientation_swapped_vs_odds(
    plive_home: str,
    plive_away: str,
    odds_home: str,
    odds_away: str,
    *,
    min_score: int = 72,
) -> bool:
    """True when Pandora home/away is flipped vs the Odds-API fixture (Sox @ Astros)."""
    if not plive_home or not plive_away or not odds_home or not odds_away:
        return False
    normal = min(_team_score(odds_home, plive_home), _team_score(odds_away, plive_away))
    swapped = min(_team_score(odds_home, plive_away), _team_score(odds_away, plive_home))
    return swapped > normal and swapped >= min_score


def align_plive_markets_to_odds_fixture(
    markets: List[Dict[str, Any]],
    *,
    plive_home: Optional[str],
    plive_away: Optional[str],
    odds_home: str,
    odds_away: str,
) -> List[Dict[str, Any]]:
    """Odds-API home/away is the fixture. Do not flip from Pandora t1/t2 labels."""
    del plive_home, plive_away, odds_home, odds_away
    return list(markets or [])


def merge_plive_market_lists(
    existing: Optional[List[Dict[str, Any]]],
    incoming: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Live market-3 Game Winner replaces Odds-API / stale PLive ML. Overlay Spread/Totals.

    Soccer Odds-API PLive may be named ``Total Goals`` while live coeffs emit
    ``Totals``. Keep one totals family so the dashboard and pipeline log
    cannot read two different PLive total blocks for the same event.
    """
    incoming_ml = any(
        isinstance(m, dict) and str(m.get("name")) == "ML" for m in (incoming or [])
    )
    incoming_totals = any(
        isinstance(m, dict) and is_game_totals_market_name(m.get("name")) for m in (incoming or [])
    )
    incoming_spread = any(
        isinstance(m, dict) and str(m.get("name")) == "Spread" for m in (incoming or [])
    )
    by_name: Dict[str, Dict[str, Any]] = {}
    for m in existing or []:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        name = str(m.get("name"))
        if name == "ML" and incoming_ml:
            continue
        if incoming_totals and is_game_totals_market_name(name):
            continue
        if incoming_spread and name == "Spread":
            continue
        by_name[name] = m
    for m in incoming or []:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        name = str(m.get("name"))
        if name in ("Spread", "Totals", "ML") or is_game_totals_market_name(name):
            by_name[name] = m
    return sanitize_plive_markets(list(by_name.values()))


def match_plive_event_to_odds_doc(
    plive_events: Dict[str, Dict[str, Any]],
    home: str,
    away: str,
    *,
    min_score: int = 72,
) -> Optional[str]:
    """Return PLive event id whose teams best match Odds-API home/away."""
    if not home or not away:
        return None
    best_id: Optional[str] = None
    best = 0
    for eid, ev in plive_events.items():
        ph = str(ev.get("home") or "")
        pa = str(ev.get("away") or "")
        if not ph or not pa:
            continue
        s = min(_team_score(home, ph), _team_score(away, pa))
        # Allow swapped home/away (some feeds flip).
        s_swap = min(_team_score(home, pa), _team_score(away, ph))
        s = max(s, s_swap)
        if s > best:
            best = s
            best_id = str(eid)
    if best < min_score:
        return None
    return best_id


class PliveStore:
    """Latest-state PLive coefficients, converted to Odds-API market lists."""

    def __init__(self) -> None:
        self.events: Dict[str, Dict[str, Any]] = {}
        self.generation = 0
        self.ml_markets = _int_csv("PLIVE_MARKET_ML", _DEFAULT_ML_MARKETS)
        raw_spread = _int_csv("PLIVE_MARKET_SPREAD", _DEFAULT_SPREAD_MARKETS)
        raw_totals = _int_csv("PLIVE_MARKET_TOTALS", _DEFAULT_TOTAL_MARKETS)
        self.total_markets = tuple(
            m for m in raw_totals if not is_team_total_market_id(m)
        ) or _DEFAULT_TOTAL_MARKETS
        self.spread_markets = tuple(
            m
            for m in raw_spread
            if not is_team_total_market_id(m) and int(m) not in self.total_markets
        ) or _DEFAULT_SPREAD_MARKETS
        self.sport_id = plive_sport_id()
        self.sport_ids = list(plive_sport_ids())
        self.sport_catalog: Dict[int, str] = dict(PLIVE_SPORT_CATALOG_FALLBACK)

    def _event(self, eid: str) -> Dict[str, Any]:
        ev = self.events.get(eid)
        if ev is None:
            ev = {
                "id": eid,
                "sport_id": None,
                "league_id": None,
                "league_name": None,
                "start": None,
                "ip": None,
                "finished": False,
                "home": None,
                "away": None,
                "coeff_updated_at": 0.0,
                "coeffs": {},  # (market, outcome) -> {index: value}
            }
            self.events[eid] = ev
        return ev

    def apply_sports_catalog(self, data: Any) -> None:
        """Merge live.sports snapshot. Trust this over the old Selenium map."""
        records = _unwrap_catalog(data)
        changed = False
        for key, rec in records:
            try:
                sid = int(rec.get("id") or rec.get("sportId") or rec.get("si") or key)
            except (TypeError, ValueError):
                continue
            name = _first_name(rec) or rec.get("n") or rec.get("name") or rec.get("displayName")
            if name:
                self.sport_catalog[sid] = str(name)
                changed = True
        if changed:
            self.generation += 1
            sample = {k: self.sport_catalog.get(k) for k in (1, 2, 3, 4, 5, 8, 102, 114, 214, 220)}
            print(
                f"[PLIVE] live.sports catalog (trust WS, not Selenium map): {sample}"
            )

    def apply_event_catalog(self, data: Any) -> List[str]:
        """Ingest eventData directory. Keep MLB (1), Soccer (5), and Top Soccer (220)."""
        seen: List[str] = []
        want = {int(x) for x in (self.sport_ids or [self.sport_id])}
        for eid, rec in iter_event_records(data):
            sid = rec.get("sportId") or rec.get("sport_id") or rec.get("si") or rec.get("s")
            if sid is not None:
                try:
                    if int(sid) not in want:
                        continue
                except (TypeError, ValueError):
                    continue
            self.apply_meta(eid, rec)
            seen.append(eid)
        if _catalog_has_s_tree(data):
            seen_set = set(seen)
            for eid, ev in list(self.events.items()):
                if eid in seen_set:
                    continue
                sid = ev.get("sport_id")
                if sid is not None:
                    try:
                        if int(sid) not in want:
                            continue
                    except (TypeError, ValueError):
                        pass
                if not ev.get("finished"):
                    ev["finished"] = True
                    self.generation += 1
        return seen

    def apply_meta(self, eid: str, data: Dict[str, Any]) -> None:
        ev = self._event(eid)
        sport = (
            data.get("sportId")
            or data.get("sport_id")
            or data.get("si")
            or data.get("s")
            or data.get("sport")
        )
        if isinstance(sport, dict):
            sport = sport.get("id") or sport.get("sportId") or sport.get("si")
        if sport is not None:
            try:
                ev["sport_id"] = int(sport)
            except (TypeError, ValueError):
                name = str(sport).lower()
                if name in ("baseball", "mlb"):
                    ev["sport_id"] = 1
                elif name in ("basketball", "nba"):
                    ev["sport_id"] = 2
                elif name in ("nfl", "american football", "americanfootball"):
                    ev["sport_id"] = 3
                elif name == "top soccer":
                    ev["sport_id"] = 220
                elif name == "soccer":
                    ev["sport_id"] = 5
        home, away = _teams_from_event(data)
        if home:
            ev["home"] = home
        if away:
            ev["away"] = away
        league = data.get("leagueId") or data.get("league_id") or data.get("li") or data.get("lg")
        if isinstance(league, dict):
            lname = league.get("name") or league.get("slug")
            if lname:
                ev["league_name"] = str(lname)
            league = league.get("id") or league.get("leagueId")
        if league is not None:
            try:
                ev["league_id"] = int(league)
            except (TypeError, ValueError):
                if isinstance(league, str) and league.strip():
                    ev["league_name"] = league.strip()
        for lk in ("leagueName", "league_name", "competition"):
            raw = data.get(lk)
            if isinstance(raw, str) and raw.strip():
                ev["league_name"] = raw.strip()
                break
        unix = _parse_start_unix(data.get("start") or data.get("startTime") or data.get("date"))
        if unix:
            ev["start"] = unix
        if data.get("ip") is True:
            ev["ip"] = True
        if data.get("finished") is True:
            ev["finished"] = True
        elif data.get("finished") is False or data.get("ip") is True:
            ev["finished"] = False
        self.generation += 1

    def set_coeff(self, eid: str, market: int, outcome: str, index: Optional[int], value: Any) -> None:
        ev = self._event(eid)
        key = (int(market), str(outcome))
        slots = ev["coeffs"].setdefault(key, {})
        if isinstance(value, (list, tuple)) and len(value) >= 2 and index in (None, 0):
            slots[0] = value[0]
            slots[1] = value[1]
        elif index is None:
            slots[1] = value
        else:
            slots[int(index)] = value
        ev["coeff_updated_at"] = time.time()
        self.generation += 1

    def apply_coeff_tree(self, eid: str, tree: Any) -> None:
        """Ingest a full ``c.m[market].o[outcome] = [price, decimal]`` snapshot."""
        if not isinstance(tree, dict):
            return
        c = tree.get("c") if "c" in tree else tree
        if not isinstance(c, dict):
            return
        mblock = c.get("m") if "m" in c else c
        if not isinstance(mblock, dict):
            return
        for mk, mval in mblock.items():
            try:
                market = int(mk)
            except (TypeError, ValueError):
                continue
            if not isinstance(mval, dict):
                continue
            oblock = mval.get("o") if "o" in mval else mval
            if not isinstance(oblock, dict):
                continue
            for outcome, oval in oblock.items():
                self._ingest_coeff_value(eid, market, str(outcome), oval)

    def _ingest_coeff_value(self, eid: str, market: int, outcome: str, oval: Any) -> None:
        oc_l = str(outcome).lower()
        if isinstance(oval, (list, tuple)):
            for i, v in enumerate(oval):
                self.set_coeff(eid, market, str(outcome), i, v)
            return
        if isinstance(oval, dict):
            for i, v in oval.items():
                ik = str(i)
                ik_l = ik.lower()
                line_from_key = _as_float(ik)
                line_from_oc = _as_float(outcome)
                if oc_l in ("over", "under", "o", "u") and _is_plausible_game_total_line(
                    line_from_key, soccer=True
                ):
                    self._ingest_coeff_value(eid, market, f"{oc_l}_{ik}", v)
                    continue
                if ik_l in ("over", "under", "o", "u") and _is_plausible_game_total_line(
                    line_from_oc, soccer=True
                ):
                    self._ingest_coeff_value(eid, market, f"{ik_l}_{outcome}", v)
                    continue
                try:
                    ii = int(i)
                except (TypeError, ValueError):
                    ii = None
                self.set_coeff(eid, market, str(outcome), ii, v)
            return
        self.set_coeff(eid, market, str(outcome), 1, oval)

    def apply_json_patch(self, eid: str, ops: Iterable[Dict[str, Any]]) -> None:
        for op in ops:
            if not isinstance(op, dict):
                continue
            if op.get("op") not in ("replace", "add"):
                continue
            parsed = parse_coeff_path(str(op.get("path") or ""))
            if not parsed:
                continue
            self.set_coeff(eid, parsed["market"], parsed["outcome"], parsed["index"], op.get("value"))

    def apply_message(self, data: Any, event_name: Optional[str] = None) -> bool:
        """Apply one Pandora payload. Returns True if state changed."""
        before = self.generation
        if _is_sports_topic(event_name):
            self.apply_sports_catalog(data)
            return self.generation != before
        if _is_event_list_topic(event_name):
            self.apply_event_catalog(data)
            return self.generation != before
        eid = event_id_from_channel(event_name) or _eid_from_coeff_payload(data)
        if _is_coeff_topic(event_name) or (
            eid and isinstance(data, (dict, list)) and not _is_event_list_topic(event_name)
        ):
            if isinstance(data, dict) and data.get("id") is not None and eid is None:
                eid = str(data.get("id"))
            if eid and isinstance(data, dict):
                self.apply_meta(eid, data)
            body = _unwrap_coeff_body(data)
            if eid and isinstance(data, dict) and data.get("isDiff") and isinstance(data.get("payload"), list):
                self.apply_json_patch(eid, data["payload"])
            elif eid and isinstance(body, dict) and body.get("isDiff") and isinstance(body.get("payload"), list):
                self.apply_json_patch(eid, body["payload"])
            elif eid and isinstance(body, list):
                self.apply_json_patch(eid, body)
            elif eid and isinstance(body, dict) and ("c" in body or "m" in body):
                self.apply_coeff_tree(eid, body)
            elif eid and isinstance(data, dict):
                tree = data.get("payload") if isinstance(data.get("payload"), dict) else data
                if isinstance(tree, dict) and ("c" in tree or "m" in tree):
                    self.apply_coeff_tree(eid, tree)
            return self.generation != before
        if isinstance(data, dict):
            if data.get("id") is not None and eid is None:
                eid = str(data.get("id"))
            if eid:
                self.apply_meta(eid, data)
            if data.get("isDiff") and isinstance(data.get("payload"), list):
                if eid:
                    self.apply_json_patch(eid, data["payload"])
            elif eid:
                tree = data.get("payload") if isinstance(data.get("payload"), dict) else data
                if isinstance(tree, dict) and ("c" in tree or "m" in tree):
                    self.apply_coeff_tree(eid, tree)
            elif _looks_like_event(data) or (isinstance(data.get("payload"), (dict, list))):
                self.apply_event_catalog(data)
        elif isinstance(data, list):
            self.apply_event_catalog(data)
        return self.generation != before

    def is_mlb_event(self, ev: Dict[str, Any]) -> bool:
        sid = ev.get("sport_id")
        if sid is None:
            return True  # unknown: keep and let team-match against MLB slate decide
        try:
            return int(sid) == int(self.sport_id)
        except (TypeError, ValueError):
            return str(sid).lower() in ("1", "baseball", "mlb")

    def mlb_events(self) -> Dict[str, Dict[str, Any]]:
        return {k: v for k, v in self.events.items() if self.is_mlb_event(v)}

    def is_soccer_event(self, ev: Dict[str, Any]) -> bool:
        sid = ev.get("sport_id")
        try:
            return int(sid) in set(plive_soccer_sport_ids())
        except (TypeError, ValueError):
            return False

    def soccer_events(self) -> Dict[str, Dict[str, Any]]:
        return {k: v for k, v in self.events.items() if self.is_soccer_event(v)}

    def wants_mlb_coeff(self, ev: Dict[str, Any]) -> bool:
        """Live MLB (league 8) only. MiLB on sport 1 stays out when league is set."""
        if ev.get("finished"):
            return False
        if not self.is_mlb_event(ev):
            return False
        lg = ev.get("league_id")
        if lg is None:
            return True
        try:
            return int(lg) == int(PLIVE_MLB_LEAGUE_ID)
        except (TypeError, ValueError):
            return True

    def wants_coeff(self, ev: Dict[str, Any]) -> bool:
        """MLB league-8 plus native soccer 5 and Top Soccer 220."""
        if self.wants_mlb_coeff(ev):
            return True
        if ev.get("finished"):
            return False
        return self.is_soccer_event(ev)

    def markets_for_event(self, eid: str) -> List[Dict[str, Any]]:
        ev = self.events.get(str(eid))
        if not ev:
            return []
        return self._markets_from_coeffs(
            ev.get("coeffs") or {}, soccer=self.is_soccer_event(ev)
        )

    def _markets_from_coeffs(
        self,
        coeffs: Dict[Tuple[int, str], Dict[int, Any]],
        *,
        soccer: bool = False,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []

        # Game Winner (market 3): outcomes 1/home vs 2/away. idx1 only.
        for mk in self.ml_markets:
            if int(mk) in _TEAM_TOTAL_MARKETS or int(mk) in self.total_markets:
                continue
            if int(mk) == PLIVE_RUN_LINE_MARKET:
                continue
            home = away = None
            for (market, outcome), slots in coeffs.items():
                if market != mk:
                    continue
                dec = _ml_decimal_from_slot(slots) if int(mk) == PLIVE_ML_MARKET else _decimal_from_slot(slots)
                if dec is None:
                    continue
                oc = str(outcome).lower()
                if oc in ("1", "home", "h"):
                    home = dec
                elif oc in ("2", "away", "a"):
                    away = dec
            if home and away:
                out.append(
                    {
                        "name": "ML",
                        "odds": [
                            {
                                "home": home,
                                "away": away,
                                "plive_market": int(mk),
                                "market_type": "game_winner",
                            }
                        ],
                    }
                )
                break

        # Run line only (market 6). Team totals 7/8 and game totals 5 never land here.
        spread_rows: List[Dict[str, Any]] = []
        for mk in self.spread_markets:
            if int(mk) in _TEAM_TOTAL_MARKETS or int(mk) in self.total_markets:
                continue
            by_line: Dict[float, Dict[str, float]] = {}
            singles: Dict[float, float] = {}
            for (market, outcome), slots in coeffs.items():
                if market != mk:
                    continue
                line = _as_float(outcome)
                if line is None:
                    continue
                pair = _spread_pair_from_slots(slots)
                if pair:
                    by_line[float(line)] = {"home": pair[0], "away": pair[1]}
                    continue
                dec = _decimal_from_slot(slots)
                if dec is not None and dec > 1.0:
                    singles[float(line)] = dec
            seen_abs: Set[float] = set()
            for line, home_dec in singles.items():
                key = abs(float(line))
                if key in seen_abs or float(line) in by_line:
                    continue
                opp = singles.get(-float(line))
                if opp is None:
                    continue
                seen_abs.add(key)
                by_line[float(line)] = {"home": home_dec, "away": opp}
            for line, sides in by_line.items():
                spread_rows.append(
                    {
                        "hdp": line,
                        "home": sides["home"],
                        "away": sides["away"],
                        "line_style": "american",
                        "plive_market": int(mk),
                        "market_type": "run_line",
                    }
                )
            if spread_rows:
                break
        if spread_rows:
            out.append({"name": "Spread", "odds": spread_rows[:12]})

        # Totals (market 5). Soccer uses exact strike identity; MLB keeps
        # the verified [idx0=over, idx1=under] pair when the outcome is the line.
        total_rows: List[Dict[str, Any]] = []
        if soccer:
            total_rows = self._soccer_total_rows_from_coeffs(coeffs)
        else:
            for mk in self.total_markets:
                by_line: Dict[float, Dict[str, float]] = {}
                for (market, outcome), slots in coeffs.items():
                    if market != mk:
                        continue
                    ocl = str(outcome).lower()
                    line = _as_float(slots.get(2) or slots.get("hdp") or slots.get("max")) or _as_float(ocl)
                    pair = _spread_pair_from_slots(slots) if isinstance(slots, dict) else None
                    if pair is None and isinstance(slots, (list, tuple)):
                        pair = _as_decimal_pair(slots)
                    if line is not None and pair is not None:
                        by_line.setdefault(float(line), {})["over"] = pair[0]
                        by_line[float(line)]["under"] = pair[1]
                        continue
                    dec = _decimal_from_slot(slots) if isinstance(slots, dict) else _as_float(slots)
                    if dec is None:
                        continue
                    side = None
                    if "over" in ocl or ocl in ("o", "3"):
                        side = "over"
                        if line is None:
                            line = _as_float(ocl.replace("over", "").replace("o", "").replace("_", ""))
                    elif "under" in ocl or ocl in ("u", "4"):
                        side = "under"
                        if line is None:
                            line = _as_float(ocl.replace("under", "").replace("u", "").replace("_", ""))
                    else:
                        line = _as_float(ocl) if line is None else line
                        side = "over" if line is not None and line not in by_line else "under"
                    if line is None or side is None:
                        continue
                    by_line.setdefault(float(line), {})[side] = dec
                for line, sides in by_line.items():
                    if "over" in sides and "under" in sides:
                        total_rows.append(
                            {
                                "hdp": line,
                                "max": line,
                                "line": line,
                                "over": sides["over"],
                                "under": sides["under"],
                                "plive_market": int(mk),
                                "market_type": "game_total",
                            }
                        )
                if total_rows:
                    break
        if total_rows:
            out.append({"name": "Totals", "odds": total_rows[:12]})

        return sanitize_plive_markets(out)

    def _soccer_total_rows_from_coeffs(
        self, coeffs: Dict[Tuple[int, str], Dict[int, Any]]
    ) -> List[Dict[str, Any]]:
        by_line: Dict[float, Dict[str, Any]] = {}
        rejected: Set[float] = set()

        def _reject(line: float) -> None:
            rejected.add(float(line))
            by_line.pop(float(line), None)

        def _put(line: float, side: str, dec: float, mk: int) -> None:
            lf = float(line)
            if lf in rejected:
                return
            if side not in ("over", "under") or dec <= 1.0:
                _reject(lf)
                return
            rec = by_line.setdefault(lf, {"plive_market": int(mk)})
            prev = rec.get(side)
            if prev is not None and abs(float(prev) - float(dec)) > 1e-9:
                _reject(lf)
                return
            rec[side] = float(dec)

        wanted = {int(m) for m in self.total_markets}
        named: List[Tuple[int, str, Any]] = []
        line_only: List[Tuple[int, str, Any]] = []
        for (market, outcome), slots in coeffs.items():
            if int(market) not in wanted:
                continue
            # Outcome / market key is the only strike identity. Slot values
            # (idx0/1/2, leftover hdp/max) are prices — never lines.
            line, side = parse_soccer_total_outcome(outcome)
            if line is None or not _is_plausible_game_total_line(line, soccer=True):
                continue
            if side:
                named.append((int(market), str(outcome), slots))
            else:
                line_only.append((int(market), str(outcome), slots))

        for market, outcome, slots in named:
            line, side = parse_soccer_total_outcome(outcome)
            if line is None or side is None:
                continue
            dec = _soccer_total_side_take_decimal(slots)
            if dec is None or abs(float(dec) - float(line)) < 1e-6:
                _reject(float(line))
                continue
            _put(float(line), side, dec, market)

        for market, outcome, slots in line_only:
            line, _side = parse_soccer_total_outcome(outcome)
            if line is None:
                continue
            lf = float(line)
            if lf in rejected:
                continue
            pair = _spread_pair_from_slots(slots) if isinstance(slots, dict) else None
            if pair is None and isinstance(slots, (list, tuple)):
                pair = _as_decimal_pair(slots)
            if pair is None or not _valid_ou_hold(pair[0], pair[1]):
                if lf in by_line:
                    _reject(lf)
                continue
            if abs(pair[0] - lf) < 1e-6 or abs(pair[1] - lf) < 1e-6:
                _reject(lf)
                continue
            rec = by_line.get(lf)
            if rec and (
                ("over" in rec and abs(float(rec["over"]) - pair[0]) > 1e-9)
                or ("under" in rec and abs(float(rec["under"]) - pair[1]) > 1e-9)
            ):
                _reject(lf)
                continue
            _put(lf, "over", pair[0], market)
            _put(lf, "under", pair[1], market)

        rows: List[Dict[str, Any]] = []
        for line, sides in sorted(by_line.items(), key=lambda kv: kv[0]):
            if line in rejected:
                continue
            if "over" not in sides or "under" not in sides:
                continue
            rows.append(
                {
                    "hdp": line,
                    "max": line,
                    "line": line,
                    "over": sides["over"],
                    "under": sides["under"],
                    "plive_market": int(sides.get("plive_market") or PLIVE_GAME_TOTAL_MARKET),
                    "market_type": "game_total",
                }
            )
        return rows


class PlivePandoraFeed:
    """Async Socket.IO client. One connection; exponential backoff reconnect."""

    def __init__(self, *, connect_fn: Optional[Any] = None) -> None:
        self.store = PliveStore()
        self._connect_fn = connect_fn
        self._running = False
        self.connected = False
        self.last_error: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._sio = None
        self._reconnect_attempts = 0
        self._dirty = asyncio.Event()
        self.generation = 0
        self._coeff_subscribed: Set[str] = set()
        self._ack_names: Set[str] = set()
        self._bound_rooms: Set[str] = set()
        self.events_received = 0
        self._logged_prices = False
        self._last_event_data_at: float = 0.0
        self._last_coeff_at: float = 0.0
        self._last_subscribe_at: float = 0.0
        self._last_getcache_refresh_at: float = 0.0

    @property
    def healthy(self) -> bool:
        return bool(self._running and self.connected and self.price_feed_ok())

    def price_feed_ok(self, *, now: Optional[float] = None) -> bool:
        """Fail closed when live events are subscribed but coefficients are missing or stale."""
        now = time.time() if now is None else float(now)
        wanted = [ev for ev in self.store.events.values() if self.store.wants_coeff(ev)]
        if not wanted:
            return True
        newest = float(self._last_coeff_at or 0.0)
        for ev in wanted:
            try:
                newest = max(newest, float(ev.get("coeff_updated_at") or 0.0))
            except (TypeError, ValueError):
                continue
        if newest <= 0:
            return False
        return (now - newest) <= float(plive_stale_sec())

    def _mark_dirty(self) -> None:
        self.generation = self.store.generation
        self._dirty.set()

    async def wait_dirty_or_timeout(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._dirty.wait(), timeout=max(0.05, float(timeout)))
        except asyncio.TimeoutError:
            return False
        debounce = float(os.getenv("PLIVE_DEBOUNCE_SEC", "0.25"))
        if debounce > 0:
            await asyncio.sleep(debounce)
        self._dirty.clear()
        return True

    def handle_payload(self, data: Any, event_name: Optional[str] = None) -> None:
        self.events_received += 1
        if _is_event_list_topic(event_name):
            self._last_event_data_at = time.time()
        if self.store.apply_message(data, event_name):
            if _is_coeff_topic(event_name) or _eid_from_coeff_payload(data):
                self._last_coeff_at = time.time()
            self._mark_dirty()
            snap = self.status_snapshot()
            if snap.get("receiving_prices") and not self._logged_prices:
                self._logged_prices = True
                print(
                    f"[PLIVE] receiving events with prices: {snap['mlb_with_prices']} MLB | "
                    f"{snap.get('soccer_with_prices') or 0} soccer | "
                    f"{'; '.join(snap.get('samples') or []) or 'priced'}"
                )

    def _dec_to_am(self, d: Optional[float]) -> Optional[int]:
        if d is None or d <= 1.0:
            return None
        if d >= 2.0:
            return int(round((d - 1.0) * 100))
        return int(round(-100 / (d - 1.0)))

    def _priced_summaries(self, events: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for eid, ev in events.items():
            mk = self.store.markets_for_event(eid)
            if not mk:
                continue
            names = [m.get("name") for m in mk]
            ml = next((m for m in mk if m.get("name") == "ML"), None)
            row = ((ml or {}).get("odds") or [{}])[0] if ml else {}
            home_dec = _as_float((row or {}).get("home"))
            away_dec = _as_float((row or {}).get("away"))
            tot = next((m for m in mk if m.get("name") == "Totals"), None)
            tot_row = ((tot or {}).get("odds") or [{}])[0] if tot else {}
            spr = next((m for m in mk if m.get("name") == "Spread"), None)
            spr_row = ((spr or {}).get("odds") or [{}])[0] if spr else {}
            if home_dec is None and away_dec is None and not tot_row and not spr_row:
                continue
            out.append(
                {
                    "id": eid,
                    "home": ev.get("home"),
                    "away": ev.get("away"),
                    "markets": names,
                    "home_dec": home_dec,
                    "away_dec": away_dec,
                    "home_am": self._dec_to_am(home_dec),
                    "away_am": self._dec_to_am(away_dec),
                    "tot_line": tot_row.get("hdp") if tot_row else None,
                    "tot_over": tot_row.get("over") if tot_row else None,
                    "tot_under": tot_row.get("under") if tot_row else None,
                    "tot_over_am": self._dec_to_am(_as_float(tot_row.get("over")) if tot_row else None),
                    "tot_under_am": self._dec_to_am(_as_float(tot_row.get("under")) if tot_row else None),
                }
            )
        return out

    def priced_mlb_summaries(self) -> List[Dict[str, Any]]:
        return self._priced_summaries(self.store.mlb_events())

    def status_snapshot(self) -> Dict[str, Any]:
        mlb = self.store.mlb_events()
        soccer = self.store.soccer_events()
        priced_mlb = self._priced_summaries(mlb)
        priced_soccer = self._priced_summaries(soccer)
        priced = priced_mlb + priced_soccer
        samples = []
        for s in priced[:5]:
            bits = [f"{s.get('away')}@{s.get('home')}"]
            if s.get("away_am") is not None and s.get("home_am") is not None:
                bits.append(f"ML {s.get('away_am')}/{s.get('home_am')}")
            if s.get("tot_line") is not None:
                bits.append(
                    f"Tot {s.get('tot_line')} {s.get('tot_over_am')}/{s.get('tot_under_am')}"
                )
            samples.append(" ".join(bits))
        now = time.time()
        last_event_unix = self._last_event_data_at or None
        last_coeff_unix = self._last_coeff_at or None
        last_sub_unix = self._last_subscribe_at or None
        return {
            "connected": bool(self.connected),
            "healthy": self.healthy,
            "partner_id": PLIVE_PARTNER_ID,
            "flavor": PLIVE_FLAVOR,
            "sport_id": self.store.sport_id,
            "mlb_events": len(mlb),
            "mlb_with_prices": len(priced_mlb),
            "soccer_events": len(soccer),
            "soccer_with_prices": len(priced_soccer),
            "receiving_events": bool(mlb or soccer) or self.store.generation > 0 or self.events_received > 0,
            "receiving_prices": len(priced) > 0,
            "price_feed_ok": self.price_feed_ok(now=now),
            "last_event_data_at": _iso_utc(self._last_event_data_at),
            "last_coeff_at": _iso_utc(self._last_coeff_at),
            "last_subscribe_at": _iso_utc(self._last_subscribe_at),
            "last_event_data_unix": last_event_unix,
            "last_coeff_unix": last_coeff_unix,
            "last_subscribe_unix": last_sub_unix,
            "coeff_age_sec": (now - self._last_coeff_at) if self._last_coeff_at else None,
            "acks": sorted(self._ack_names),
            "samples": samples,
            "last_error": self.last_error,
            "generation": self.store.generation,
            "events_received": self.events_received,
        }

    def log_status(self, *, prefix: str = "[PLIVE] status") -> Dict[str, Any]:
        snap = self.status_snapshot()
        age = snap.get("coeff_age_sec")
        age_s = f"{age:.0f}s" if isinstance(age, (int, float)) else "none"
        print(
            f"{prefix} connected={snap['connected']} receiving_events={snap['receiving_events']} "
            f"receiving_prices={snap['receiving_prices']} price_ok={snap['price_feed_ok']} "
            f"mlb={snap['mlb_events']} soccer={snap['soccer_events']} "
            f"priced={snap['mlb_with_prices'] + snap['soccer_with_prices']} "
            f"coeff_age={age_s} sample={'; '.join(snap['samples'][:3]) or 'none'}"
        )
        return snap

    def decode_binary(self, binary_data: bytes) -> Optional[Any]:
        return _coerce_socket_payload(binary_data)

    def ingest_raw(self, data: Any, event_name: Optional[str] = None) -> None:
        data = _coerce_socket_payload(data)
        if data is None:
            return
        # A coeff getCache snapshot is often a JSON-patch *list*. Do not
        # walk it as separate catalog records — that drops every price.
        if isinstance(data, list) and (
            _is_coeff_topic(event_name)
            or _looks_like_json_patch_list(data)
            or _eid_from_coeff_payload(data)
        ):
            self.handle_payload(data, event_name)
            return
        if isinstance(data, list):
            for item in data:
                self.handle_payload(item, event_name)
            return
        if isinstance(data, dict):
            self.handle_payload(data, event_name)

    def markets_for_odds_event(self, doc: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Join PLive take prices onto an Odds-API event.

        Odds-API event IDs already join its own books. PLive uses Pandora
        ids. Soccer uses a separate conservative fixture join and never
        emits markets when the match is missing, swapped, stale, or
        ambiguous. MLB keeps the existing swap-tolerant matcher.
        """
        home = str(doc.get("home") or "")
        away = str(doc.get("away") or "")
        if _odds_doc_is_soccer(doc):
            eid = match_plive_soccer_to_odds_doc(self.store.soccer_events(), doc)
            if not eid:
                return []
            ev = self.store.events.get(str(eid)) or {}
            if plive_price_is_stale(ev):
                return []
            return align_plive_markets_to_odds_fixture(
                self.store.markets_for_event(eid),
                plive_home=str(ev.get("home") or ""),
                plive_away=str(ev.get("away") or ""),
                odds_home=home,
                odds_away=away,
            )
        eid = match_plive_event_to_odds_doc(self.store.mlb_events(), home, away)
        if not eid:
            return []
        ev = self.store.events.get(str(eid)) or {}
        if plive_price_is_stale(ev):
            return []
        return align_plive_markets_to_odds_fixture(
            self.store.markets_for_event(eid),
            plive_home=str(ev.get("home") or ""),
            plive_away=str(ev.get("away") or ""),
            odds_home=home,
            odds_away=away,
        )

    def _record_ack(self, event_name: Optional[str], data: Any = None) -> None:
        kind = note_handshake_ack(event_name, data)
        if not kind:
            return
        first = kind not in self._ack_names
        self._ack_names.add(kind)
        if not first:
            return
        if kind == "socketMetadataSet":
            print("[PLIVE] ack socketMetadataSet")
        elif kind == "subscribedSystemEvents":
            rooms = []
            if isinstance(data, dict):
                if data.get("room"):
                    rooms.append(str(data["room"]))
                rooms.extend(str(r) for r in (data.get("rooms") or []) if r)
            print(f"[PLIVE] ack subscribedSystemEvents rooms={rooms}")
        elif kind == "subscribed":
            preview = repr(data)
            print(f"[PLIVE] ack subscribed {preview[:220]}")

    def _bind_room(self, sio: Any, room: str) -> None:
        """Binary snapshots arrive on the room name, not on the catch-all ``*``.

        The handler must accept ``*args``. A two-arg emit (meta, payload)
        used to overwrite the captured room name and drop the snapshot.
        """
        if not room or room in self._bound_rooms:
            return
        self._bound_rooms.add(room)

        @sio.on(room)
        def _on_room(*args: Any, _room: str = room) -> None:
            if not args:
                return
            for arg in args:
                self.ingest_raw(arg, _room)
            if _is_event_list_topic(_room) and sio is not None:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._subscribe_mlb_coefficients(sio))
                except RuntimeError:
                    pass

    async def _emit_public_handshake(self, sio: Any) -> None:
        topics = public_ui_subscribe_topics()
        for room in topics:
            self._bind_room(sio, room)
        for event, payload in handshake_emits():
            try:
                await sio.emit(event, payload)
            except Exception as ex:
                print(f"[PLIVE] [WARN] emit {event} failed: {ex}")
        print(f"[PLIVE] handshake emitted setSocketMetadata + subscribe/getCache ({len(topics)} rooms)")

    def _coeff_rooms_to_drop(self) -> List[str]:
        drop: List[str] = []
        for room in list(self._coeff_subscribed):
            eid = event_id_from_channel(room)
            ev = self.store.events.get(str(eid)) if eid else None
            if ev is None or not self.store.wants_coeff(ev):
                drop.append(room)
        return drop

    async def _unsubscribe_finished_coefficients(self, sio: Any) -> None:
        """Drop eventCoefficients.{id} when the event is finished or gone."""
        rooms = self._coeff_rooms_to_drop()
        if not rooms:
            return
        for i in range(0, len(rooms), 80):
            batch = rooms[i : i + 80]
            try:
                await sio.emit("unsubscribe", batch)
            except Exception as ex:
                print(f"[PLIVE] [WARN] coeff unsubscribe failed: {ex}")
            for room in batch:
                self._coeff_subscribed.discard(room)
                self._bound_rooms.discard(room)
            print(f"[PLIVE] unsubscribed eventCoefficients for {len(batch)} finished events")

    def reset_socket_bindings(self) -> None:
        """Reconnect: keep eventData, drop room bindings so subscribe/getCache rerun."""
        self._coeff_subscribed = set()
        self._bound_rooms = set()
        self._ack_names = set()
        self._logged_prices = False

    def _rooms_needing_coeff_cache(self) -> List[str]:
        """Subscribed rooms that never produced a coefficient snapshot."""
        rooms: List[str] = []
        for eid, ev in self.store.events.items():
            if not self.store.wants_coeff(ev):
                continue
            room = coeff_room_for_event(str(eid))
            if room not in self._coeff_subscribed:
                continue
            stamp = ev.get("coeff_updated_at") or 0
            try:
                ts = float(stamp)
            except (TypeError, ValueError):
                ts = 0.0
            if ts <= 0 or not (ev.get("coeffs") or {}):
                rooms.append(room)
        return rooms

    def _rooms_needing_stale_resubscribe(self, *, now: Optional[float] = None) -> List[str]:
        """Subscribed rooms whose last coeff is older than PLIVE_STALE_SEC."""
        now = time.time() if now is None else float(now)
        stale_after = float(plive_stale_sec())
        rooms: List[str] = []
        for eid, ev in self.store.events.items():
            if not self.store.wants_coeff(ev):
                continue
            room = coeff_room_for_event(str(eid))
            if room not in self._coeff_subscribed:
                continue
            if not (ev.get("coeffs") or {}):
                continue
            stamp = ev.get("coeff_updated_at") or 0
            try:
                ts = float(stamp)
            except (TypeError, ValueError):
                ts = 0.0
            if ts <= 0 or (now - ts) > stale_after:
                rooms.append(room)
        return rooms

    async def _subscribe_mlb_coefficients(self, sio: Any) -> None:
        """Per-event click-in coeff rooms. subscribe + getCache; retry cache if empty."""
        await self._unsubscribe_finished_coefficients(sio)
        want = list(self.store.sport_ids or [self.store.sport_id])
        new_rooms: List[str] = []
        for eid, ev in self.store.events.items():
            if not self.store.wants_coeff(ev):
                continue
            room = coeff_room_for_event(str(eid))
            if room in self._coeff_subscribed:
                continue
            self._coeff_subscribed.add(room)
            self._bind_room(sio, room)
            new_rooms.append(room)
        refresh = self._rooms_needing_coeff_cache() if not new_rooms else []
        stale = self._rooms_needing_stale_resubscribe() if not new_rooms else []
        if not new_rooms and not refresh and not stale:
            return
        try:
            if new_rooms:
                for i in range(0, len(new_rooms), 80):
                    batch = new_rooms[i : i + 80]
                    await sio.emit("subscribe", batch)
                    await sio.emit("getCache", batch)
                    self._last_subscribe_at = time.time()
                    print(f"[PLIVE] subscribed eventCoefficients for {len(batch)} events (sports {want})")
            else:
                now = time.time()
                if now - self._last_getcache_refresh_at < 2.0:
                    return
                self._last_getcache_refresh_at = now
                if stale:
                    for i in range(0, len(stale), 80):
                        batch = stale[i : i + 80]
                        await sio.emit("subscribe", batch)
                        await sio.emit("getCache", batch)
                        self._last_subscribe_at = time.time()
                    print(
                        f"[PLIVE] resubscribe stale eventCoefficients for {len(stale)} events (sports {want})"
                    )
                elif refresh:
                    for i in range(0, len(refresh), 80):
                        batch = refresh[i : i + 80]
                        await sio.emit("getCache", batch)
                    print(f"[PLIVE] getCache refresh eventCoefficients for {len(refresh)} events still unpriced")
        except Exception as ex:
            print(f"[PLIVE] [WARN] coeff subscribe failed: {ex}")

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop(), name="plive-pandora")
        print(
            f"[PLIVE] starting Pandora Socket.IO ({PLIVE_URL}) origin={PLIVE_ORIGIN} "
            f"partner={PLIVE_PARTNER_ID} flavor={PLIVE_FLAVOR} MLB {PLIVE_MLB_HASH} "
            f"soccer {PLIVE_SOCCER_HASH}+{PLIVE_TOP_SOCCER_HASH} "
            f"sportIds={plive_sport_ids()} (no login, no cookies)"
        )

    async def stop(self) -> None:
        self._running = False
        self.connected = False
        sio = self._sio
        self._sio = None
        if sio is not None:
            try:
                await sio.disconnect()
            except Exception:
                pass
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._connect_once()
                self._reconnect_attempts = 0
            except asyncio.CancelledError:
                break
            except Exception as ex:
                self.last_error = str(ex)
                print(f"[PLIVE] [WARN] connection ended: {ex}")
            self.connected = False
            if not self._running:
                break
            self._reconnect_attempts += 1
            delay = min(30.0, 1.0 * (2 ** max(0, self._reconnect_attempts - 1)))
            print(f"[PLIVE] reconnect in {delay:.0f}s (attempt {self._reconnect_attempts})")
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    async def _connect_once(self) -> None:
        if self._connect_fn is not None:
            await self._connect_fn(self)
            return
        try:
            import socketio
        except ImportError as ex:
            raise RuntimeError("python-socketio is required for PLive (pip install python-socketio)") from ex

        sio = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
        self._sio = sio
        self.reset_socket_bindings()

        @sio.on("connect")
        async def _on_connect() -> None:
            self.connected = True
            print(
                f"[PLIVE] connected sid={getattr(sio, 'sid', None)} — "
                f"public-UI handshake partner={PLIVE_PARTNER_ID} MLB {PLIVE_MLB_HASH}"
            )
            await self._emit_public_handshake(sio)

        @sio.on("disconnect")
        async def _on_disconnect() -> None:
            self.connected = False
            print("[PLIVE] disconnected from pandora.ganchrow.com")

        @sio.on("socketMetadataSet")
        async def _on_meta(data: Any = None) -> None:
            self._record_ack("socketMetadataSet", data)

        @sio.on("setSocketMetadata")
        async def _on_meta_alias(data: Any = None) -> None:
            self._record_ack("setSocketMetadata", data)

        @sio.on("subscribedSystemEvents")
        async def _on_sys(data: Any = None) -> None:
            self._record_ack("subscribedSystemEvents", data)

        @sio.on("subscribed")
        async def _on_sub(data: Any = None) -> None:
            self._record_ack("subscribed", data)

        @sio.on("*")
        async def _on_any(event_name: str, *args: Any) -> None:
            for arg in args:
                ack = note_handshake_ack(event_name, arg)
                if ack:
                    self._record_ack(event_name, arg)
                    continue
                self.ingest_raw(arg, event_name)
            if _is_event_list_topic(event_name):
                await self._subscribe_mlb_coefficients(sio)

        origin = (os.getenv("PLIVE_ORIGIN") or PLIVE_ORIGIN).strip()
        url = (os.getenv("PLIVE_URL") or PLIVE_URL).strip()
        await sio.connect(
            url,
            transports=["websocket"],
            socketio_path=PLIVE_SOCKETIO_PATH,
            headers={
                "Origin": origin,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
            wait_timeout=10,
        )
        ping_every = 4 * 60
        last_ping = asyncio.get_event_loop().time()
        last_coeff = 0.0
        last_status = 0.0
        try:
            while self._running and sio.connected:
                now = asyncio.get_event_loop().time()
                if now - last_ping >= ping_every:
                    try:
                        await sio.emit("ping")
                    except Exception:
                        pass
                    last_ping = now
                if now - last_coeff >= 2.0:
                    await self._subscribe_mlb_coefficients(sio)
                    last_coeff = now
                if now - last_status >= 10.0:
                    self.log_status()
                    last_status = now
                await asyncio.sleep(0.5)
        finally:
            if sio.connected:
                await sio.disconnect()
            self._sio = None


_shared_plive: Optional[PlivePandoraFeed] = None
_shared_plive_lock = asyncio.Lock()


async def get_shared_plive_feed() -> Optional[PlivePandoraFeed]:
    global _shared_plive
    if not plive_wanted():
        return None
    async with _shared_plive_lock:
        if _shared_plive is None:
            _shared_plive = PlivePandoraFeed()
            await _shared_plive.start()
        return _shared_plive


def peek_shared_plive_feed() -> Optional[PlivePandoraFeed]:
    return _shared_plive


async def reset_shared_plive_feed() -> None:
    global _shared_plive
    async with _shared_plive_lock:
        if _shared_plive is not None:
            await _shared_plive.stop()
            _shared_plive = None


def merge_plive_into_docs(docs: List[Dict[str, Any]]) -> int:
    """
    Replace (not merge-into-markets) ``bookmakers["PLive"]`` on each Odds-API doc
    when a PLive MLB event matches. Returns how many docs gained a PLive book.
    """
    feed = peek_shared_plive_feed()
    if feed is None:
        return 0
    n = 0
    book = _canonical_odds_api_bookmaker(PLIVE_BOOK_NAME)
    feed_ok = feed.price_feed_ok()
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        bks = doc.setdefault("bookmakers", {})
        if not isinstance(bks, dict):
            doc["bookmakers"] = {}
            bks = doc["bookmakers"]
        if not feed_ok:
            # Subscribed but silent/stale — do not keep a stale PLive take.
            bks.pop(book, None)
            continue
        markets = feed.markets_for_odds_event(doc)
        existing = bks.get(book) if isinstance(bks.get(book), list) else []
        if not markets:
            # No live coeff match — do not invent. Existing Odds-API PLive stays.
            continue
        incoming_spread = any(str(m.get("name")) == "Spread" for m in markets if isinstance(m, dict))
        merged = merge_plive_market_lists(existing, markets)
        if not incoming_spread:
            merged = [m for m in merged if str(m.get("name")) != "Spread"]
        bks[book] = merged
        stamps = doc.setdefault("book_updated_at", {})
        if isinstance(stamps, dict):
            stamps[book] = time.time()
        n += 1
    return n


def extra_local_bookmakers() -> List[str]:
    return [PLIVE_BOOK_NAME] if plive_wanted() else []
