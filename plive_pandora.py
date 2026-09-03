"""
PLive (Pandora) odds subscriber — port of NDGopher/UnifiedBetting
``backend/pandora_odds_subscriber.py``.

Connects directly to ``wss://pandora.ganchrow.com`` Socket.IO with
``Origin: https://plive.becoms.co``. No login. No BetBCK scrape.

MLB on PLive is ``#!/sport/1``. This client keeps sport-1 events and converts
ML / Spread / Totals into the same ``bookmakers["PLive"]`` market list shape
used by Odds-API.io ``/odds`` so EvAlerts still come from ``ev_calculator.py``.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from odds_api_client import _canonical_odds_api_bookmaker

PLIVE_BOOK_NAME = "PLive"
PLIVE_ORIGIN = "https://plive.becoms.co"
PLIVE_URL = "wss://pandora.ganchrow.com"
PLIVE_SOCKETIO_PATH = "/socket.io/"
PLIVE_MLB_SPORT_ID = 1
PLIVE_MLB_HASH = "#!/sport/1"

# Ganchrow coefficient tree (same parse as UnifiedBetting):
#   /c/m/{market}/o/{outcome}/{index}
# index 0 ≈ money price, index 1 ≈ decimal odds (see Unified README).
# Defaults from that subscriber's MLB-looking examples (market 10 ML, 5 totals)
# plus common 2-way handicap = 2. Override with env if the tree differs.
_DEFAULT_ML_MARKETS = (10, 1)
_DEFAULT_SPREAD_MARKETS = (2,)
_DEFAULT_TOTAL_MARKETS = (5, 3)


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


def plive_sport_id() -> int:
    try:
        return int(os.getenv("PLIVE_SPORT_ID", str(PLIVE_MLB_SPORT_ID)))
    except ValueError:
        return PLIVE_MLB_SPORT_ID


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


_EVENT_ID_RE = re.compile(r"(?:eventCoefficients|event)[./](\d+)", re.I)


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

    def apply_meta(self, eid: str, data: Dict[str, Any]) -> None:
        ev = self._event(eid)
        sport = data.get("sportId") or data.get("sport_id") or data.get("sport")
        if isinstance(sport, dict):
            sport = sport.get("id") or sport.get("sportId")
        if sport is not None:
            try:
                ev["sport_id"] = int(sport)
            except (TypeError, ValueError):
                if str(sport).lower() in ("baseball", "mlb"):
                    ev["sport_id"] = self.sport_id
        home = data.get("home") or data.get("homeTeam") or data.get("home_name")
        away = data.get("away") or data.get("awayTeam") or data.get("away_name")
        if isinstance(home, dict):
            home = home.get("name") or home.get("shortName")
        if isinstance(away, dict):
            away = away.get("name") or away.get("shortName")
        if home:
            ev["home"] = str(home)
        if away:
            ev["away"] = str(away)
        participants = data.get("participants") or data.get("teams")
        if isinstance(participants, list) and len(participants) >= 2 and not (ev.get("home") and ev.get("away")):
            names = []
            for p in participants:
                if isinstance(p, dict):
                    names.append(str(p.get("name") or p.get("shortName") or ""))
                else:
                    names.append(str(p))
            if len(names) >= 2:
                ev["away"] = ev.get("away") or names[0]
                ev["home"] = ev.get("home") or names[1]
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

        # Spread: outcome is the home handicap (e.g. -1.5) or home/away pair + hdp slot.
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
                if ocl in ("1", "home", "h"):
                    home_dec = dec
                    h = _as_float(slots.get(2) or slots.get("hdp"))
                    if h is not None:
                        hdp = h
                elif ocl in ("2", "away", "a"):
                    away_dec = dec
                else:
                    line = _as_float(oc)
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
                    if "home" not in sides:
                        continue
                    opp = by_line.get(-line, {})
                    # If we only have one side, skip (need two-way for EV).
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
                line = _as_float(slots.get(2) or slots.get("hdp") or slots.get("max"))
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

    @property
    def healthy(self) -> bool:
        return bool(self._running and self.connected)

    def _mark_dirty(self) -> None:
        self.generation = self.store.generation
        self._dirty.set()

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

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop(), name="plive-pandora")
        print(
            f"[PLIVE] starting Pandora Socket.IO ({PLIVE_URL}) origin={PLIVE_ORIGIN} "
            f"MLB {PLIVE_MLB_HASH} sportId={plive_sport_id()} (no login)"
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

        @sio.on("connect")
        async def _on_connect() -> None:
            self.connected = True
            print(f"[PLIVE] connected sid={getattr(sio, 'sid', None)} — filter MLB {PLIVE_MLB_HASH}")
            # Best-effort subscribe; the UnifiedBetting client also received a broadcast without this.
            for payload in (PLIVE_MLB_HASH, {"sport": plive_sport_id()}, f"sport/{plive_sport_id()}"):
                try:
                    await sio.emit("subscribe", payload)
                except Exception:
                    pass

        @sio.on("disconnect")
        async def _on_disconnect() -> None:
            self.connected = False
            print("[PLIVE] disconnected from pandora.ganchrow.com")

        @sio.on("*")
        async def _on_any(event_name: str, *args: Any) -> None:
            for arg in args:
                self.ingest_raw(arg, event_name)

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
        try:
            while self._running and sio.connected:
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
