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
    4) For each live MLB eventId (sport 1, league 8) subscribe
       eventCoefficients.{eventId} (click-in full book: run lines, team
       totals, margins). Unsubscribe that room when the event is finished.

Do not scrape ``https://plive.becoms.co/live/?#!/event/{id}`` — the hash
is a client-side route; ``{id}`` is the pandora event id. This is not a
per-sport price socket. Bare connect is silent. No BetBCK. No cookies.

MLB is catalog sport 1 (hash ``#!/sport/1``). ``#!/sport/220`` is Top Soccer.
Trust the live.sports catalog over any old Selenium sport map.

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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from odds_api_client import _canonical_odds_api_bookmaker

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
PLIVE_TOP_SOCCER_SPORT_ID = 220
PLIVE_TOP_SOCCER_HASH = "#!/sport/220"

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
# Market 3 ML: idx1 is the true decimal. Do not overwrite Odds-API PLive ML.
# Market 5 = game totals. 7/8 = team totals (click-in only) — never on Spread.
# eventData list is [home, away] (stadium home first).
# Sox @ Astros 199298371 Game tab: Astros −1.5 is unpriced. The only +325
# on that event is Chicago White Sox Total Over 2.5 (market 7/8), not a run line.
_DEFAULT_ML_MARKETS = (10, 9, 1)
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


def plive_sport_id() -> int:
    page = (os.getenv("PLIVE_PAGE") or os.getenv("PLIVE_HASH") or "").strip()
    hashed = parse_sport_hash(page)
    if hashed is not None:
        return hashed
    try:
        return int(os.getenv("PLIVE_SPORT_ID", str(PLIVE_MLB_SPORT_ID)))
    except ValueError:
        return PLIVE_MLB_SPORT_ID


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
    """ML / totals: prefer index 1 (true decimal on market 3). Not used for market 6 pairs."""
    for idx in (1, 0):
        f = _as_float(slots.get(idx))
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
                    if not isinstance(extra, dict):
                        continue
                    if extra.get("ip") is True:
                        rec["finished"] = False
                    elif extra.get("ip") is False or extra.get("finished") is True:
                        rec["finished"] = True
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


def _norm_team(s: str) -> str:
    t = (s or "").lower()
    for ch in ("'", ".", ",", "-", "_"):
        t = t.replace(ch, " ")
    return " ".join(t.split())


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
    """Odds-API PLive ML stays. Overlay run line (Spread) and game totals only."""
    by_name: Dict[str, Dict[str, Any]] = {}
    for m in existing or []:
        if isinstance(m, dict) and m.get("name"):
            by_name[str(m.get("name"))] = m
    for m in incoming or []:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        name = str(m.get("name"))
        if name == "ML" and "ML" in by_name:
            continue
        if name in ("Spread", "Totals", "ML"):
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
        self.sport_catalog: Dict[int, str] = dict(PLIVE_SPORT_CATALOG_FALLBACK)

    def _event(self, eid: str) -> Dict[str, Any]:
        ev = self.events.get(eid)
        if ev is None:
            ev = {
                "id": eid,
                "sport_id": None,
                "league_id": None,
                "finished": False,
                "home": None,
                "away": None,
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
        """Ingest eventData directory. Sport 1 baseball; league 8 is MLB."""
        seen: List[str] = []
        want = int(self.sport_id)
        for eid, rec in iter_event_records(data):
            sid = rec.get("sportId") or rec.get("sport_id") or rec.get("si") or rec.get("s")
            if sid is not None:
                try:
                    if int(sid) != want:
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
                        if int(sid) != want:
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
                elif name in ("football", "nfl"):
                    ev["sport_id"] = 3
        home, away = _teams_from_event(data)
        if home:
            ev["home"] = home
        if away:
            ev["away"] = away
        league = data.get("leagueId") or data.get("league_id") or data.get("li") or data.get("lg")
        if isinstance(league, dict):
            league = league.get("id") or league.get("leagueId")
        if league is not None:
            try:
                ev["league_id"] = int(league)
            except (TypeError, ValueError):
                pass
        if data.get("finished") is True:
            ev["finished"] = True
        elif data.get("finished") is False or data.get("ip") is True:
            ev["finished"] = False
        self.generation += 1

    def set_coeff(self, eid: str, market: int, outcome: str, index: Optional[int], value: Any) -> None:
        ev = self._event(eid)
        key = (int(market), str(outcome))
        slots = ev["coeffs"].setdefault(key, {})
        if index is None:
            slots[1] = value
        else:
            slots[int(index)] = value
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
                if isinstance(oval, (list, tuple)):
                    for i, v in enumerate(oval):
                        self.set_coeff(eid, market, str(outcome), i, v)
                elif isinstance(oval, dict):
                    for i, v in oval.items():
                        try:
                            ii = int(i)
                        except (TypeError, ValueError):
                            ii = None
                        self.set_coeff(eid, market, str(outcome), ii, v)
                else:
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
        eid = event_id_from_channel(event_name)
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

    def markets_for_event(self, eid: str) -> List[Dict[str, Any]]:
        ev = self.events.get(str(eid))
        if not ev:
            return []
        return self._markets_from_coeffs(ev.get("coeffs") or {})

    def _markets_from_coeffs(self, coeffs: Dict[Tuple[int, str], Dict[int, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []

        # Moneyline: outcomes 1/home vs 2/away (2-way MLB).
        for mk in self.ml_markets:
            home = away = None
            for (market, outcome), slots in coeffs.items():
                if market != mk:
                    continue
                dec = _decimal_from_slot(slots)
                if dec is None:
                    continue
                oc = str(outcome).lower()
                if oc in ("1", "home", "h"):
                    home = dec
                elif oc in ("2", "away", "a"):
                    away = dec
            if home and away:
                out.append({"name": "ML", "odds": [{"home": home, "away": away}]})
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

        # Totals (market 5): same 2-way pair as market 6 — slot 0 over, slot 1 under.
        total_rows: List[Dict[str, Any]] = []
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

    @property
    def healthy(self) -> bool:
        return bool(self._running and self.connected)

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
        if self.store.apply_message(data, event_name):
            self._mark_dirty()
            snap = self.status_snapshot()
            if snap.get("receiving_prices") and not self._logged_prices:
                self._logged_prices = True
                print(
                    f"[PLIVE] receiving events with prices: {snap['mlb_with_prices']} MLB | "
                    f"{'; '.join(snap.get('samples') or []) or 'priced'}"
                )

    def _dec_to_am(self, d: Optional[float]) -> Optional[int]:
        if d is None or d <= 1.0:
            return None
        if d >= 2.0:
            return int(round((d - 1.0) * 100))
        return int(round(-100 / (d - 1.0)))

    def priced_mlb_summaries(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for eid, ev in self.store.mlb_events().items():
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

    def status_snapshot(self) -> Dict[str, Any]:
        priced = self.priced_mlb_summaries()
        mlb = self.store.mlb_events()
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
        return {
            "connected": bool(self.connected),
            "healthy": self.healthy,
            "partner_id": PLIVE_PARTNER_ID,
            "flavor": PLIVE_FLAVOR,
            "sport_id": self.store.sport_id,
            "mlb_events": len(mlb),
            "mlb_with_prices": len(priced),
            "receiving_events": bool(mlb) or self.store.generation > 0 or self.events_received > 0,
            "receiving_prices": len(priced) > 0,
            "acks": sorted(self._ack_names),
            "samples": samples,
            "last_error": self.last_error,
            "generation": self.store.generation,
            "events_received": self.events_received,
        }

    def log_status(self, *, prefix: str = "[PLIVE] status") -> Dict[str, Any]:
        snap = self.status_snapshot()
        print(
            f"{prefix} connected={snap['connected']} receiving_events={snap['receiving_events']} "
            f"receiving_prices={snap['receiving_prices']} mlb={snap['mlb_events']} "
            f"priced={snap['mlb_with_prices']} sample={'; '.join(snap['samples'][:3]) or 'none'}"
        )
        return snap

    def decode_binary(self, binary_data: bytes) -> Optional[Any]:
        try:
            decompressed = gzip.decompress(binary_data)
            return json.loads(decompressed.decode("utf-8"))
        except Exception:
            return None

    def ingest_raw(self, data: Any, event_name: Optional[str] = None) -> None:
        if isinstance(data, bytes):
            decoded = self.decode_binary(data)
            if decoded is not None:
                self.handle_payload(decoded, event_name)
            return
        if isinstance(data, str):
            return
        if isinstance(data, (dict, list)):
            if isinstance(data, list):
                for item in data:
                    self.handle_payload(item, event_name)
            else:
                self.handle_payload(data, event_name)

    def markets_for_odds_event(self, doc: Dict[str, Any]) -> List[Dict[str, Any]]:
        home = str(doc.get("home") or "")
        away = str(doc.get("away") or "")
        eid = match_plive_event_to_odds_doc(self.store.mlb_events(), home, away)
        if not eid:
            return []
        ev = self.store.events.get(str(eid)) or {}
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
        """Binary snapshots arrive on the room name, not on the catch-all ``*``."""
        if not room or room in self._bound_rooms:
            return
        self._bound_rooms.add(room)

        @sio.on(room)
        def _on_room(data: Any, _room: str = room) -> None:
            self.ingest_raw(data, _room)
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
            if ev is None or not self.store.wants_mlb_coeff(ev):
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

    async def _subscribe_mlb_coefficients(self, sio: Any) -> None:
        """Per-event click-in coeff rooms (full book: run lines, team totals, margins)."""
        await self._unsubscribe_finished_coefficients(sio)
        want = int(self.store.sport_id)
        new_rooms: List[str] = []
        for eid, ev in self.store.events.items():
            if not self.store.wants_mlb_coeff(ev):
                continue
            room = coeff_room_for_event(str(eid))
            if room in self._coeff_subscribed:
                continue
            self._coeff_subscribed.add(room)
            self._bind_room(sio, room)
            new_rooms.append(room)
        if not new_rooms:
            return
        for i in range(0, len(new_rooms), 80):
            batch = new_rooms[i : i + 80]
            try:
                await sio.emit("subscribe", batch)
                await sio.emit("getCache", batch)
                print(f"[PLIVE] subscribed eventCoefficients for {len(batch)} events (sport {want})")
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
            f"sportId={plive_sport_id()} (no login, no cookies)"
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
        self._coeff_subscribed = set()
        self._ack_names = set()
        self._bound_rooms = set()

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
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        markets = feed.markets_for_odds_event(doc)
        bks = doc.setdefault("bookmakers", {})
        if not isinstance(bks, dict):
            doc["bookmakers"] = {}
            bks = doc["bookmakers"]
        existing = bks.get(book) if isinstance(bks.get(book), list) else []
        if not markets:
            # Leave Odds-API PLive ML in place. Do not invent or wipe.
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
