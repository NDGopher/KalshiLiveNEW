"""
PLive (Pandora) odds subscriber — Origin-only Socket.IO, no login.

Handshake matches the public live UI at https://plive.becoms.co/live/ :

  wss://pandora.ganchrow.com/socket.io/?EIO=4&transport=websocket
  Header Origin: https://plive.becoms.co
  After CONNECT:
    1) setSocketMetadata {partnerId: 113, flavor: "live"}
    2) subscribeSystemEvents {partnerId: 113}
    3) subscribe + getCache for live.sports / live.events /
       live.main.<LINE_SET> eventData + eventCoefficients

Bare connect is silent. Do not scrape BetBCK. Do not send cookies.

MLB is catalog sport 1 (hash ``#!/sport/1``). ``#!/sport/220`` is Top Soccer.
Trust the live.sports catalog over any old Selenium sport map.

Lines become ``bookmakers["PLive"]`` so EvAlerts still use the existing
filter / dollar-size pipeline in ``ev_calculator.py`` (Kalshi remains the
take venue).
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import re
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

# Ganchrow coefficient tree (same parse as UnifiedBetting):
#   /c/m/{market}/o/{outcome}/{index}
# index 0 ≈ money price, index 1 ≈ decimal odds (see Unified README).
# Defaults from that subscriber's MLB-looking examples (market 10 ML, 5 totals)
# plus common 2-way handicap = 2. Override with env if the tree differs.
# Live catalog game period ``c.m`` (not the old UnifiedBetting guess of 10/1):
#   3 = 2-way moneyline, 6 = spread (pair odds), 5 = totals (pair odds).
_DEFAULT_ML_MARKETS = (3, 9, 10, 1)
_DEFAULT_SPREAD_MARKETS = (6, 2)
_DEFAULT_TOTAL_MARKETS = (5,)


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
    """Rooms the public live UI subscribes after metadata (MLB-first set)."""
    prefix = plive_line_prefix()
    flavor = (os.getenv("PLIVE_FLAVOR") or PLIVE_FLAVOR).strip() or PLIVE_FLAVOR
    return [
        f"{flavor}.sports",
        "sports",
        f"{flavor}.events",
        "live.events",
        prefix,
        plive_event_data_topic(),
        plive_event_coefficients_topic(),
        f"{flavor}.leagues",
        f"{flavor}.wagerTypes",
        f"{flavor}.sportPeriod",
    ]


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


def coeff_room_for_event(event_id: str) -> str:
    return f"{plive_event_coefficients_topic()}.{event_id}"


def _is_sports_topic(event_name: Optional[str]) -> bool:
    t = str(event_name or "").lower()
    return t.endswith(".sports") or t == "sports" or t.endswith(".leagues")


def _is_event_list_topic(event_name: Optional[str]) -> bool:
    t = str(event_name or "")
    if not t:
        return False
    if "eventCoefficients" in t:
        return False
    return (
        t.endswith(".eventData")
        or t.endswith(".events")
        or t == "live.events"
        or t == plive_line_prefix()
    )


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
    """Prefer index 1 (decimal odds in the UnifiedBetting README), else index 0 if it looks like odds."""
    for idx in (1, 0):
        f = _as_float(slots.get(idx))
        if f is not None and f > 1.0:
            return f
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
    """Walk ``payload.s[sport][…][eventId] = [awayRow, homeRow, …]``."""
    path = path or []
    if isinstance(node, list) and len(node) >= 2 and isinstance(node[0], (list, tuple, dict, str)):
        away = _team_name_from_row(node[0])
        home = _team_name_from_row(node[1])
        if away and home and path:
            eid = path[-1]
            if eid.isdigit():
                rec: Dict[str, Any] = {"id": eid, "away": away, "home": home}
                if sport_id is not None:
                    rec["sportId"] = sport_id
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
        self.spread_markets = _int_csv("PLIVE_MARKET_SPREAD", _DEFAULT_SPREAD_MARKETS)
        self.total_markets = _int_csv("PLIVE_MARKET_TOTALS", _DEFAULT_TOTAL_MARKETS)
        self.sport_id = plive_sport_id()
        self.sport_catalog: Dict[int, str] = dict(PLIVE_SPORT_CATALOG_FALLBACK)

    def _event(self, eid: str) -> Dict[str, Any]:
        ev = self.events.get(eid)
        if ev is None:
            ev = {
                "id": eid,
                "sport_id": None,
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

    def apply_event_catalog(self, data: Any) -> List[str]:
        """Ingest eventData / live.events snapshot. Returns event ids seen."""
        seen: List[str] = []
        for eid, rec in iter_event_records(data):
            self.apply_meta(eid, rec)
            seen.append(eid)
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

        # Spread: outcome is the home handicap (e.g. -1.5) or a [home, away] pair.
        spread_rows: List[Dict[str, Any]] = []
        for mk in self.spread_markets:
            by_line: Dict[float, Dict[str, float]] = {}
            home_dec = away_dec = None
            hdp = None
            for (market, outcome), slots in coeffs.items():
                if market != mk:
                    continue
                dec = _decimal_from_slot(slots)
                if dec is None:
                    continue
                oc = str(outcome)
                ocl = oc.lower()
                pair_home = _as_float(slots.get(0))
                pair_away = _as_float(slots.get(1))
                line = _as_float(oc)
                if (
                    line is not None
                    and pair_home is not None
                    and pair_away is not None
                    and pair_home > 1.0
                    and pair_away > 1.0
                ):
                    by_line.setdefault(line, {})["home"] = pair_home
                    by_line.setdefault(line, {})["away"] = pair_away
                    continue
                if ocl in ("1", "home", "h"):
                    home_dec = dec
                    h = _as_float(slots.get(2) or slots.get("hdp"))
                    if h is not None:
                        hdp = h
                elif ocl in ("2", "away", "a"):
                    away_dec = dec
                else:
                    if line is None:
                        continue
                    by_line.setdefault(line, {})["home"] = dec
                    by_line.setdefault(-line, {})
            if home_dec and away_dec:
                spread_rows.append(
                    {"hdp": hdp if hdp is not None else 0.0, "home": home_dec, "away": away_dec}
                )
            else:
                for line, sides in by_line.items():
                    if "home" in sides and "away" in sides:
                        spread_rows.append(
                            {"hdp": line, "home": sides["home"], "away": sides["away"]}
                        )
                        continue
                    if "home" not in sides:
                        continue
                    opp = by_line.get(-line, {})
                    away_s = opp.get("home")
                    if away_s is None:
                        continue
                    spread_rows.append({"hdp": line, "home": sides["home"], "away": away_s})
            if spread_rows:
                break
        if spread_rows:
            out.append({"name": "Spread", "odds": spread_rows[:12]})

        # Totals: outcome "over"/"under" or numeric line + o/u suffix.
        total_rows: List[Dict[str, Any]] = []
        for mk in self.total_markets:
            by_line: Dict[float, Dict[str, float]] = {}
            for (market, outcome), slots in coeffs.items():
                if market != mk:
                    continue
                dec = _decimal_from_slot(slots)
                if dec is None:
                    continue
                ocl = str(outcome).lower()
                pair_over = _as_float(slots.get(0))
                pair_under = _as_float(slots.get(1))
                line = _as_float(slots.get(2) or slots.get("hdp") or slots.get("max")) or _as_float(ocl)
                if (
                    line is not None
                    and pair_over is not None
                    and pair_under is not None
                    and pair_over > 1.0
                    and pair_under > 1.0
                ):
                    by_line.setdefault(float(line), {})["over"] = pair_over
                    by_line[float(line)]["under"] = pair_under
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
                    # Bare line: treat even index / first as over if we later see the other.
                    side = "over" if line is not None and line not in by_line else "under"
                if line is None or side is None:
                    continue
                by_line.setdefault(float(line), {})[side] = dec
            for line, sides in by_line.items():
                if "over" in sides and "under" in sides:
                    total_rows.append(
                        {"hdp": line, "over": sides["over"], "under": sides["under"]}
                    )
            if total_rows:
                break
        if total_rows:
            out.append({"name": "Totals", "odds": total_rows[:12]})

        return out


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
        if self.store.apply_message(data, event_name):
            self._mark_dirty()

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
        return self.store.markets_for_event(eid)

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

    async def _subscribe_mlb_coefficients(self, sio: Any) -> None:
        """Per-event coeff rooms — required for team names + live game lines."""
        want = int(self.store.sport_id)
        new_rooms: List[str] = []
        for eid, ev in self.store.events.items():
            sid = ev.get("sport_id")
            if sid is not None:
                try:
                    if int(sid) != want:
                        continue
                except (TypeError, ValueError):
                    continue
            room = coeff_room_for_event(str(eid))
            if room in self._coeff_subscribed:
                continue
            self._coeff_subscribed.add(room)
            self._bind_room(sio, room)
            new_rooms.append(room)
        if not new_rooms:
            return
        # Cap so we stay on MLB first and do not flood soccer/NFL rooms.
        batch = new_rooms[:80]
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
            self._ack_names.add("socketMetadataSet")
            print("[PLIVE] ack socketMetadataSet")

        @sio.on("subscribedSystemEvents")
        async def _on_sys(data: Any = None) -> None:
            self._ack_names.add("subscribedSystemEvents")
            rooms = []
            if isinstance(data, dict):
                if data.get("room"):
                    rooms.append(str(data["room"]))
                rooms.extend(str(r) for r in (data.get("rooms") or []) if r)
            print(f"[PLIVE] ack subscribedSystemEvents rooms={rooms}")

        @sio.on("subscribed")
        async def _on_sub(data: Any = None) -> None:
            self._ack_names.add("subscribed")
            print(f"[PLIVE] ack subscribed {data!r}"[:240])

        @sio.on("*")
        async def _on_any(event_name: str, *args: Any) -> None:
            for arg in args:
                self.ingest_raw(arg, event_name)
            if _is_event_list_topic(event_name) or event_name in ("live.events", "sports"):
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
        if markets:
            bks[book] = list(markets)  # replace PLive markets only
            n += 1
        elif book in bks:
            del bks[book]
    return n


def extra_local_bookmakers() -> List[str]:
    return [PLIVE_BOOK_NAME] if plive_wanted() else []
