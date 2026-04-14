"""
Drop-in replacement for BookieBeatsAPIMonitor using Odds-API.io.

FREE TIER SAFE -- poll interval defaults to 45s, shared client + TTL caches; optional
  ODDS_API_MLB_NBA_ONLY=true restricts alerts to NBA/MLB leagues.
UPGRADE PATH: add WebSocket here (Odds-API.io WS) and push deltas instead of polling.

Public interface matches BookieBeatsAPIMonitor (same __init__ signature, callbacks, loop).
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv

# --- .env must load before odds_api_client / OddsAPIClient read ODDS_API_KEY (cwd != project root when using `python path/to/file.py`). ---
_SCRIPT_DIR = Path(__file__).resolve().parent
_DOTENV_SCRIPT = _SCRIPT_DIR / ".env"
_DOTENV_CWD = Path.cwd() / ".env"
_LOADED_ENV_SCRIPT = load_dotenv(_DOTENV_SCRIPT, override=True, encoding="utf-8-sig")
_LOADED_ENV_CWD = load_dotenv(_DOTENV_CWD, override=True, encoding="utf-8-sig")

import aiohttp

from ev_alert import EvAlert
from ev_calculator import (
    EVCalculator,
    _passes_hold,
    decimal_to_american,
    ev_percent_three_methods_multi_sharp,
    ev_percent_three_methods_three_way,
    format_ev_percent_display,
)
from odds_api_client import (
    get_shared_odds_client,
    odds_api_master_bookmakers,
    reset_shared_odds_client,
    _norm_book,
)

_DOTENV_BOOTSTRAP_DONE = False


def _reload_dotenv_safely() -> None:
    """Re-apply .env from project root and cwd (override=True)."""
    load_dotenv(_DOTENV_SCRIPT, override=True, encoding="utf-8-sig")
    load_dotenv(_DOTENV_CWD, override=True, encoding="utf-8-sig")


def print_env_debug(*, standalone: bool = False) -> None:
    """Print cwd, .env locations, and key Odds-API settings (call from standalone test or for support)."""
    key = os.getenv("ODDS_API_KEY", "").strip()
    print("=" * 60)
    print("ENV DEBUG (Odds-API.io)" + (" -- STANDALONE TEST" if standalone else ""))
    print("=" * 60)
    print(f"  Python: {sys.version.split()[0]}  |  script: {Path(__file__).resolve()}")
    print(f"  Current working directory: {Path.cwd()}")
    print(f"  Project dir (this file):   {_SCRIPT_DIR}")
    print(f"  .env at {_DOTENV_SCRIPT}: {'FOUND, loaded' if _DOTENV_SCRIPT.is_file() else 'NOT FOUND'} (dotenv return={_LOADED_ENV_SCRIPT})")
    print(f"  .env at {_DOTENV_CWD}: {'FOUND, loaded' if _DOTENV_CWD.is_file() else 'NOT FOUND'} (dotenv return={_LOADED_ENV_CWD})")
    if key:
        print(f"  ODDS_API_KEY: YES -- prefix={key[:8]}... (len={len(key)})")
    else:
        print("  ODDS_API_KEY: MISSING")
        print("     Checked paths above. Ensure the file is UTF-8, has a line ODDS_API_KEY=... (no quotes needed),")
        print("     and that you run the script from the intended directory or keep .env next to odds_ev_monitor.py.")
    print(f"  USE_ODDS_API: {os.getenv('USE_ODDS_API', '(unset)')}")
    print(f"  ODDS_API_BOOKMAKERS: {os.getenv('ODDS_API_BOOKMAKERS', '(unset)')}")
    print(f"  ODDS_API_SPORTS: {os.getenv('ODDS_API_SPORTS', '(unset)')}")
    print(f"  ODDS_POLL_INTERVAL_SECONDS: {os.getenv('ODDS_POLL_INTERVAL_SECONDS', '(unset)')}")
    print(f"  ODDS_API_MAX_REQUESTS_PER_HOUR: {os.getenv('ODDS_API_MAX_REQUESTS_PER_HOUR', '(unset)')}")
    print(f"  ODDS_API_MLB_NBA_ONLY: {os.getenv('ODDS_API_MLB_NBA_ONLY', '(unset)')}")
    print(f"  ODDS_API_LIVE_ONLY: {os.getenv('ODDS_API_LIVE_ONLY', '(unset)')}")
    print(f"  ODDS_DEBUG_MODE: {os.getenv('ODDS_DEBUG_MODE', '(unset)')}")
    print(f"  ODDS_DEBUG_MAX_EVENTS: {os.getenv('ODDS_DEBUG_MAX_EVENTS', '(unset)')}")
    print(f"  ODDS_TEST_MINUTES: {os.getenv('ODDS_TEST_MINUTES', '(unset)')}")
    print("  Note: load_dotenv(override=True) applies .env over existing shell variables.")
    print("=" * 60)


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes", "on")


def _diagnostic_mode() -> bool:
    """Rich [PIPELINE] logging + relaxed minRoi/minEv for dashboard display (not auto-bet). Default ON."""
    return os.getenv("ODDS_DIAGNOSTIC_MODE", "true").lower() in ("1", "true", "yes", "on")


async def _pipeline_live_league_counts(client: Any) -> Tuple[int, int, int]:
    """MLB / NHL / total from Odds-API /events/live (best-effort)."""
    liv = await client.list_live_events()
    mlb = nhl = tot = 0
    for e in liv or []:
        tot += 1
        lg = _league_str(e.get("league")).upper()
        if "MLB" in lg or "MAJOR LEAGUE" in lg:
            mlb += 1
        elif "NHL" in lg:
            nhl += 1
    return mlb, nhl, tot


def _league_matches_filter(league: str, leagues_filter: List[str]) -> bool:
    """Map BookieBeats-style league ids to Odds-API.io league name substrings."""
    league_u = (league or "").upper()
    if not leagues_filter:
        return True
    for token in leagues_filter:
        t = token.upper()
        if t in ("BASEBALL_ALL", "BASEBALL_MLB", "MLB"):
            if "MLB" in league_u or "MAJOR LEAGUE" in league_u:
                return True
        elif t in ("BASKETBALL_ALL", "BASKETBALL_NBA", "NBA"):
            if "NBA" in league_u or "NATIONAL BASKETBALL" in league_u:
                return True
        elif t == "NCAAB":
            if any(x in league_u for x in ("NCAAB", "NCAA", "COLLEGE", "NCAAM", "DIVISION")):
                return True
        elif t in ("FOOTBALL_ALL", "NFL"):
            if "NFL" in league_u:
                return True
        elif t in ("HOCKEY_ALL", "NHL"):
            if "NHL" in league_u:
                return True
        elif t in ("SOCCER_ALL",):
            if any(x in league_u for x in ("SOCCER", "PREMIER", "UEFA", "MLS", "LIGUE", "BUNDES", "SERIE A", "LA LIGA")):
                return True
        else:
            if t.replace("_", " ") in league_u or t in league_u:
                return True
    return False


def _league_str(league: Any) -> str:
    """Odds-API often returns league as {\"name\": \"...\", \"slug\": \"...\"}."""
    if league is None:
        return ""
    if isinstance(league, dict):
        return str(league.get("name") or league.get("slug") or "")
    return str(league)


def _sport_slug(ev: Dict[str, Any]) -> str:
    sp = ev.get("sport")
    if isinstance(sp, dict):
        return str(sp.get("slug") or "").lower()
    return str(sp or "").lower()


def _event_odds_actionable(ev: Dict[str, Any]) -> bool:
    """Settled/finished events return empty bookmakers from /odds — skip for inspection."""
    st = str(ev.get("status") or ev.get("state") or "").lower().strip()
    if st in ("settled", "finished", "closed", "cancelled", "void", "walkover", "abandoned"):
        return False
    return True


def _mlb_nba_only(league: Any) -> bool:
    u = _league_str(league).upper()
    return "NBA" in u or "MLB" in u or "MAJOR LEAGUE" in u or "NATIONAL BASKETBALL" in u


def _mlb_nba_gate_applies(leagues_filter: List[str], mlb_nba_env: bool) -> bool:
    """
    ODDS_API_MLB_NBA_ONLY is a free-tier shortcut for MLB+NBA only.
    If the dashboard filter already includes hockey, CBB, NFL, etc., skip that gate so
    those leagues are not incorrectly dropped.
    """
    if not mlb_nba_env:
        return False
    tokens = {str(x).upper() for x in (leagues_filter or [])}
    if not tokens:
        return True
    broad = {
        "HOCKEY_ALL",
        "NHL",
        "NCAAB",
        "FOOTBALL_ALL",
        "NFL",
        "SOCCER_ALL",
        "TENNIS_ALL",
        "UFC_ALL",
    }
    if tokens & broad:
        return False
    return True


def _event_debug_sort_key(ev: Dict[str, Any]) -> Tuple[int, str]:
    st = str(ev.get("status") or "").lower()
    live = st == "live" or ev.get("live") is True or ev.get("isLive") is True
    return (0 if live else 1, str(ev.get("date") or ""))


def extract_kalshi_ticker_from_href(href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    m = re.search(r"kalshi\.com/(?:[^/]+/)*([A-Z0-9]{3,}[A-Z0-9\-]*)", href, re.I)
    if m:
        return m.group(1).upper()
    parts = href.rstrip("/").split("/")
    if parts:
        cand = parts[-1].upper()
        if cand.startswith("KX"):
            return cand
    return None


def _float_dec(s: Any) -> Optional[float]:
    try:
        if s is None:
            return None
        return float(str(s).strip())
    except (TypeError, ValueError):
        return None


def _fmt_american_from_dec(d: Optional[float]) -> str:
    if d is None or d <= 1.0:
        return "  —  "
    a = decimal_to_american(float(d))
    if a > 0:
        return f"+{a}"
    return str(a)


def _debug_row_is_noise(ks_dec: float, pwr: float, wc: float, av: float) -> bool:
    if ks_dec < 1.01 or ks_dec > 10.0:
        return True
    if abs(pwr + 999.0) < 0.5:
        return True
    if max(abs(pwr), abs(wc), abs(av)) < 1.0:
        return True
    return False


def _debug_row_abs_ev_max(pwr: float, wc: float, av: float) -> float:
    return max(abs(pwr), abs(wc), abs(av))


def _liq_hint(row: Optional[Dict[str, Any]]) -> str:
    if not row:
        return "-"
    for k in ("maxStake", "maxBet", "limit", "volume", "liquidity", "stake"):
        v = row.get(k)
        if v is not None and str(v).strip() not in ("", "N/A", "n/a", "None"):
            return str(v)[:10]
    return "-"


def _market_names_match(a: str, b: str) -> bool:
    a_u = (a or "").upper().replace(" ", "")
    b_u = (b or "").upper().replace(" ", "")
    if a_u == b_u:
        return True
    aliases = (
        ("ML", "MONEYLINE", "MONEY"),
        ("SPREAD", "HANDICAP", "ASIANHANDICAP"),
        ("TOTAL", "TOTALS", "OVER/UNDER", "OU"),
    )
    for group in aliases:
        if a_u in group and b_u in group:
            return True
    return False


def _find_market_block(book_odds: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    for m in book_odds or []:
        if _market_names_match(str(m.get("name", "")), name):
            return m
    return None


def _first_odds_row(market: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    rows = market.get("odds") or []
    if rows and isinstance(rows[0], dict):
        return rows[0]
    return None


def _pick_qualifier_line_for_side(
    event_home: str,
    event_away: str,
    market_name: str,
    bet_side: str,
    row: Dict[str, Any],
) -> Tuple[str, Optional[str], Optional[float]]:
    side = (bet_side or "").lower()
    mname = (market_name or "").upper()
    if "TOTAL" in mname or "OVER" in mname or "UNDER" in mname or mname in ("OU", "O/U"):
        line = row.get("max")
        if line is None:
            line = row.get("line")
        lf = float(line) if line is not None else None
        if side == "over":
            return "Over", (f"{lf:.1f}" if lf is not None else None), lf
        if side == "under":
            return "Under", (f"{lf:.1f}" if lf is not None else None), lf
    if "SPREAD" in mname or "HANDICAP" in mname:
        hdp = row.get("hdp")
        try:
            hf = float(hdp) if hdp is not None else None
        except (TypeError, ValueError):
            hf = None
        if side == "home":
            return event_home, (f"{hf:+.1f}" if hf is not None else None), hf
        if side == "away":
            return event_away, (f"{hf:+.1f}" if hf is not None else None), hf
    # Moneyline
    if side == "home":
        return event_home, None, None
    if side == "away":
        return event_away, None, None
    if side == "draw":
        return "Draw", None, None
    return side.title(), None, None


def _decimal_for_side(row: Dict[str, Any], bet_side: str) -> Optional[float]:
    side = (bet_side or "").lower()
    key_map = {
        "home": "home",
        "away": "away",
        "draw": "draw",
        "over": "over",
        "under": "under",
    }
    k = key_map.get(side)
    if not k:
        return None
    return _float_dec(row.get(k))


def _kalshi_moneyline_display_books(
    pick: str,
    kalshi_am: int,
    fd_am: int,
    kalshi_limit: float,
    fd_limit: float,
) -> Dict[str, List[Dict[str, Any]]]:
    return {
        pick: [
            {"book": "Kalshi", "odds": kalshi_am, "limit": kalshi_limit},
            {"book": "FanDuel", "odds": fd_am, "limit": fd_limit},
        ]
    }


def _markets_list_for_book(bks: Dict[str, Any], book: str) -> List[Dict[str, Any]]:
    if not isinstance(bks, dict):
        return []
    nb = _norm_book(book).lower()
    for k, v in bks.items():
        if _norm_book(str(k)).lower() == nb:
            return v if isinstance(v, list) else []
    return []


def _row_limit_hint(row: Dict[str, Any]) -> Optional[float]:
    for key in ("maxStake", "maxBet", "limit", "volume", "liquidity", "stake", "max"):
        v = row.get(key)
        if v is None:
            continue
        try:
            f = float(str(v).replace("$", "").replace(",", ""))
            if f > 0:
                return f
        except (TypeError, ValueError):
            continue
    return None


def _min_sharp_floor(book: str, rules: List[Dict[str, Any]]) -> float:
    b_low = _norm_book(book).lower()
    for r in rules or []:
        if _norm_book(str(r.get("book", ""))).lower() == b_low:
            return float(r.get("min", 0))
    return 0.0


def _row_passes_sharp_limit(row: Dict[str, Any], book: str, rules: List[Dict[str, Any]]) -> bool:
    need = _min_sharp_floor(book, rules)
    if need <= 0:
        return True
    liq = _row_limit_hint(row)
    if liq is None:
        return True
    return liq + 1e-9 >= need


def _two_way_pick_opp_decimals(row: Dict[str, Any], bet_side: str) -> Optional[Tuple[float, float]]:
    side = (bet_side or "").lower()
    if side in ("over", "under"):
        d1 = _float_dec(row.get("over"))
        d2 = _float_dec(row.get("under"))
        if not d1 or not d2 or d1 <= 1.0 or d2 <= 1.0:
            return None
        return (d1, d2) if side == "over" else (d2, d1)
    if side == "draw":
        return None
    dh = _float_dec(row.get("home"))
    da = _float_dec(row.get("away"))
    if not dh or not da or dh <= 1.0 or da <= 1.0:
        return None
    if side == "home":
        return dh, da
    if side == "away":
        return da, dh
    return None


def _three_way_draw_decimals(row: Dict[str, Any]) -> Optional[Tuple[float, float, float]]:
    dh = _float_dec(row.get("home"))
    dd = _float_dec(row.get("draw"))
    da = _float_dec(row.get("away"))
    if dh and dd and da and min(dh, dd, da) > 1.0:
        return dh, dd, da
    return None


def _build_display_books_payload(
    pick: str,
    bks: Optional[Dict[str, Any]],
    mname: str,
    bet_side: str,
    display_names: List[str],
    kalshi_am: int,
    k_row: Dict[str, Any],
) -> Dict[str, List[Dict[str, Any]]]:
    rows_out: List[Dict[str, Any]] = [
        {"book": "Kalshi", "odds": kalshi_am, "limit": float(_row_limit_hint(k_row) or 0.0)}
    ]
    if not bks or not isinstance(bks, dict):
        return {pick: rows_out}
    for nm in display_names:
        if _norm_book(str(nm)).lower() == "kalshi":
            continue
        mk = _find_market_block(_markets_list_for_book(bks, nm), mname)
        row = _first_odds_row(mk or {}) or {}
        d = _decimal_for_side(row, bet_side)
        if d and d > 1.0:
            rows_out.append(
                {
                    "book": _norm_book(str(nm)),
                    "odds": decimal_to_american(float(d)),
                    "limit": float(_row_limit_hint(row) or 0.0),
                }
            )
    return {pick: rows_out}


class OddsEVMonitor:
    """Same public surface as BookieBeatsAPIMonitor; backed by Odds-API.io + local devig."""

    def __init__(
        self,
        api_url: str = "https://api.odds-api.io/v3/value-bets",
        auth_token: Optional[str] = None,
        cookies: Optional[str] = None,
    ):
        del api_url  # unused; kept for drop-in compatibility
        self.auth_token = auth_token
        self.cookies = cookies
        self.running = False
        self._seen_alerts: Set[str] = set()
        self.alert_callbacks: List[Callable] = []
        self.removed_alert_callbacks: List[Callable] = []
        self.updated_alert_callbacks: List[Callable] = []
        self.poll_interval = float(os.getenv("ODDS_POLL_INTERVAL_SECONDS", "45"))
        self.last_check_time = None
        self.session: Optional[aiohttp.ClientSession] = None
        self._empty_poll_count = 0
        self._token_error_count = 0
        self.last_poll_time: Optional[float] = None
        self.filter_payload: Dict[str, Any] = {}
        self._previous_alert_values: Dict[str, Dict[str, Any]] = {}
        self._empty_poll_log_counter = 0
        self._last_alert_count = 0
        self._calc = EVCalculator(self.filter_payload)
        self._target_book = "Kalshi"
        self._reference_book = "FanDuel"
        self._last_cycle_alert_count = 0
        self._debug_stats: Dict[str, int] = {}
        self._debug_aggregate = {
            "events_max": 0,
            "events_sum": 0,
            "markets": 0,
            "rows": 0,
            "noise": 0,
            "alerts": 0,
            "edges8": 0,
            "polls": 0,
        }

    def set_filter(self, filter_payload: Dict[str, Any]) -> None:
        self.filter_payload = filter_payload
        self._calc.set_filter(filter_payload)
        bb = filter_payload.get("bettingBooks") or ["Kalshi"]
        self._target_book = _norm_book(str(bb[0])) if bb else "Kalshi"
        sharps = (filter_payload.get("devigFilter") or {}).get("sharps") or ["FanDuel"]
        for s in sharps:
            if _norm_book(str(s)) in ("Fanduel", "FanDuel"):
                self._reference_book = "FanDuel"
                break
        else:
            self._reference_book = _norm_book(str(sharps[0])) if sharps else "FanDuel"

    def add_alert_callback(self, callback: Callable) -> None:
        self.alert_callbacks.append(callback)

    def extract_ticker_from_link(self, link: str) -> Optional[str]:
        return extract_kalshi_ticker_from_href(link)

    def parse_bet_to_alert(self, bet: Dict[str, Any], event: Dict[str, Any]) -> Optional[EvAlert]:
        """Same contract as BookieBeatsAPIMonitor.parse_bet_to_alert (normalized `bet` dict)."""
        try:
            market_type = bet.get("market", "")
            teams = bet.get("teams", "")
            selection = bet.get("selection", "")
            line = bet.get("line")
            qualifier = bet.get("qualifier")
            odds_american = bet.get("odds")
            odds_str = None
            if odds_american is not None:
                oi = int(odds_american)
                odds_str = f"+{oi}" if oi > 0 else str(oi)
            price_cents = bet.get("price")
            ev_percent = float(bet.get("ev", 0.0))
            limit = float(bet.get("limit", 0.0))
            fair_odds = bet.get("fairOdds")
            fair_odds_str = None
            if fair_odds is not None:
                fo = int(fair_odds)
                fair_odds_str = f"+{fo}" if fo > 0 else str(fo)
            link = bet.get("link", "")
            expected_profit = (ev_percent / 100.0) * limit if limit > 0 else 0.0
            book_price = f"{int(price_cents)}¢" if price_cents is not None else ""
            display_books = bet.get("displayBooks", {})
            devig_books = bet.get("devigBooks", [])
            alert_data = {
                "market_type": market_type,
                "teams": teams,
                "ev_percent": ev_percent,
                "expected_profit": expected_profit,
                "pick": selection,
                "qualifier": qualifier,
                "odds": odds_str,
                "liquidity": limit,
                "book_price": book_price,
                "fair_odds": fair_odds_str,
                "market_url": link,
                "display_books": display_books,
                "devig_books": devig_books,
                "raw_html": json.dumps(bet),
                "strict_pass": bool(bet.get("strict_pass", True)),
            }
            alert = EvAlert(alert_data)
            alert.ticker = self.extract_ticker_from_link(link) or bet.get("ticker")
            alert.price_cents = price_cents
            alert.line = line
            return alert
        except Exception as e:
            print(f"[WARN] Error parsing bet: {e}")
            return None

    def _match_kalshi_row(self, f_row: Dict[str, Any], ks_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not ks_rows:
            return {}
        fhdp = f_row.get("hdp")
        fmax = f_row.get("max")
        if fmax is not None:
            for kr in ks_rows:
                try:
                    if kr.get("max") is not None and float(kr.get("max")) == float(fmax):
                        return kr
                except (TypeError, ValueError):
                    continue
        if fhdp is not None:
            for kr in ks_rows:
                try:
                    if kr.get("hdp") is not None and float(kr.get("hdp")) == float(fhdp):
                        return kr
                except (TypeError, ValueError):
                    continue
        return ks_rows[0] if ks_rows else {}

    def _fmt_event_banner(self, doc: Dict[str, Any]) -> str:
        home = str(doc.get("home") or "")
        away = str(doc.get("away") or "")
        league = _league_str(doc.get("league"))
        dt = str(doc.get("date") or doc.get("startTime") or doc.get("startsAt") or "")
        st = str(doc.get("status") or doc.get("state") or "")
        lv = doc.get("live", "")
        return f"{away} @ {home}  |  {league}  |  {dt}  |  status={st}  live={lv}"

    def _market_type_label(self, mname: str, league: str) -> str:
        u = (mname or "").upper()
        if "TOTAL" in u or "OVER" in u or "UNDER" in u:
            return "Total Points" if ("NBA" in league.upper() or "BASKETBALL" in league.upper()) else "Total Runs"
        if "SPREAD" in u or "HANDICAP" in u:
            return "Point Spread"
        return "Moneyline"

    def _debug_row_would_alert_production(self, row: Dict[str, Any], league: str) -> Tuple[bool, float]:
        """
        Apply the same devig + ROI + devigFilter + limits + oddsRanges checks as production
        (_value_bet_to_normalized_bet), without value-bet envelope or league/live gating.
        """
        mname = str(row.get("mname") or "")
        bet_types = [str(x).upper() for x in (self.filter_payload.get("betTypes") or ["GAMELINES"])]
        if "GAMELINES" in bet_types and "PLAYER" in mname.upper():
            return False, -999.0
        for ex in self.filter_payload.get("excludedCategories") or []:
            if ex and str(ex).lower() in mname.lower():
                return False, -999.0

        method = str((self.filter_payload.get("devigFilter") or {}).get("method", "POWER")).upper()
        price_cents = int(row["kc"])
        k_dec = float(row["ks_d"])
        kalshi_am = decimal_to_american(k_dec)

        tw = row.get("three_way")
        if tw is not None and isinstance(tw, (list, tuple)) and len(tw) == 3:
            h_home, h_d, h_a = float(tw[0]), float(tw[1]), float(tw[2])
            if min(h_home, h_d, h_a) <= 1.0:
                return False, -999.0
            fh, fd_, fa = self._calc.fair_probs_three_way(h_home, h_d, h_a, method)
            fair = fd_
            sharp_decimals = [h_home, h_d, h_a]
        else:
            d_pick = row.get("fd_pick")
            d_opp = row.get("fd_opp")
            if d_pick is None or d_opp is None:
                return False, -999.0
            d_pick = float(d_pick)
            d_opp = float(d_opp)
            if d_pick <= 1.0 or d_opp <= 1.0:
                return False, -999.0
            fp, _fo = self._calc.fair_probs_two_way(d_pick, d_opp, method)
            fair = fp
            sharp_decimals = [d_pick, d_opp]

        ev_percent = self._calc.ev_percent_vs_kalshi(fair, price_cents)
        if not self._calc.passes_roi(ev_percent):
            return False, ev_percent
        sharp_books_used = 1 if len(sharp_decimals) >= 2 else 0
        if not self._calc.passes_devig_filter(
            sharp_decimals if len(sharp_decimals) >= 2 else [k_dec, max(1.02, k_dec * 1.01)],
            ev_percent,
            0.0,
            sharp_books_count=sharp_books_used,
        ):
            return False, ev_percent
        if not self._calc.passes_min_limits_kalshi(0.0):
            return False, ev_percent
        if not self._calc.passes_odds_ranges(kalshi_am):
            return False, ev_percent
        return True, ev_percent

    async def _fetch_alerts_debug_inspection(self, client: Any) -> Dict[str, int]:
        """
        ODDS_DEBUG_MODE: pull value-bets (cache), then MLB/NBA events + odds/multi; print all
        comparable Kalshi+FanDuel lines with POWER / WORST_CASE / AVERAGE EV. No strict filters.
        """
        stats: Dict[str, int] = {
            "events": 0,
            "markets_analyzed": 0,
            "table_rows": 0,
            "noise_filtered": 0,
            "rows_that_would_alert": 0,
            "edges_gt8": 0,
        }
        ref = self._reference_book
        prod_method = str((self.filter_payload.get("devigFilter") or {}).get("method", "POWER")).upper()
        print("\n" + "-" * 100)
        print(f"[DEBUG] Poll {datetime.now().isoformat(timespec='seconds')} -- Kalshi + {ref} inspection (MLB/NBA, live+pregame)")
        print("-" * 100)
        try:
            raw_vb = await client.get_value_bets(self._target_book, True)
            print(f"[DEBUG] value-bets (Kalshi) rows: {len(raw_vb)} (cached; warms same feed as production)")
        except Exception as e:
            print(f"[DEBUG] value-bets fetch skipped: {e}")
            raw_vb = []
        vb_ids: Set[int] = set()
        for vb in raw_vb:
            eid = vb.get("eventId")
            if eid is not None:
                try:
                    vb_ids.add(int(eid))
                except (TypeError, ValueError):
                    pass

        try:
            ev_bb = await client.list_events_for_sport("baseball")
            ev_bk = await client.list_events_for_sport("basketball")
        except Exception as e:
            print(f"[DEBUG] list_events_for_sport error: {e}")
            ev_bb, ev_bk = [], []

        try:
            ev_live = await client.list_live_events()
        except Exception as e:
            print(f"[DEBUG] list_live_events error: {e}")
            ev_live = []

        max_ev = int(os.getenv("ODDS_DEBUG_MAX_EVENTS", "28"))
        raw_all = (ev_bb or []) + (ev_bk or [])
        print(f"[DEBUG] list_events: baseball={len(ev_bb or [])}, basketball={len(ev_bk or [])}, combined={len(raw_all)}")

        mlb_nba_events: List[Dict[str, Any]] = []
        for e in raw_all:
            if _mlb_nba_only(e.get("league")):
                mlb_nba_events.append(e)
        if not mlb_nba_events and raw_all:
            sample = raw_all[0]
            print(
                f"[DEBUG] MLB/NBA substring filter matched 0; sample league={sample.get('league')!r} "
                f"sport={sample.get('sport')!r} -- using first {max_ev} combined events for inspection."
            )
            mlb_nba_events = raw_all[: max_ev]

        live_mlb_nba: List[Dict[str, Any]] = []
        for e in ev_live or []:
            if _sport_slug(e) not in ("baseball", "basketball"):
                continue
            if not _mlb_nba_only(e.get("league")):
                continue
            if not _event_odds_actionable(e):
                continue
            live_mlb_nba.append(e)
        print(f"[DEBUG] live_events: total={len(ev_live or [])}, MLB/NBA actionable={len(live_mlb_nba)}")

        actionable = [e for e in mlb_nba_events if _event_odds_actionable(e)]
        dropped = len(mlb_nba_events) - len(actionable)
        if dropped > 0:
            print(f"[DEBUG] Dropped {dropped} settled/closed MLB/NBA rows from /events (no /odds bookmakers).")
        if not actionable and mlb_nba_events:
            deeper: List[Dict[str, Any]] = []
            for e in raw_all:
                if not _mlb_nba_only(e.get("league")):
                    continue
                if _event_odds_actionable(e):
                    deeper.append(e)
                if len(deeper) >= max_ev * 6:
                    break
            if deeper:
                print(f"[DEBUG] Deep scan: {len(deeper)} actionable MLB/NBA events (initial slice was all settled).")
            actionable = deeper
        pool = actionable
        if not pool:
            print("[DEBUG] [WARN] No actionable MLB/NBA events (try later or check league filters).")
        pool.sort(key=_event_debug_sort_key)
        live_mlb_nba.sort(key=_event_debug_sort_key)

        seen: Set[int] = set()
        ordered_ids: List[int] = []
        for e in live_mlb_nba + pool:
            eid = e.get("id")
            if eid is None:
                continue
            i = int(eid)
            if i not in seen:
                seen.add(i)
                ordered_ids.append(i)
            if len(ordered_ids) >= max_ev:
                break
        for i in vb_ids:
            if i not in seen and len(ordered_ids) < max_ev:
                ordered_ids.append(i)
                seen.add(i)

        if not ordered_ids:
            print("[DEBUG] No MLB/NBA event ids to query (check API / league strings).")
            return stats

        odds_docs: List[Dict[str, Any]] = []
        try:
            odds_docs = await client.get_odds_multi(ordered_ids, odds_api_master_bookmakers())
        except Exception as e:
            print(f"[DEBUG] odds/multi error: {e}")
            return stats

        stats["events"] = len(odds_docs)
        sep = "-" * 118
        for doc in odds_docs:
            league = _league_str(doc.get("league"))
            rows_for_event: List[Dict[str, Any]] = []
            bks = doc.get("bookmakers") or {}
            fd_all = bks.get(ref) or []
            ks_all = bks.get("Kalshi") or []

            for fd_mk in fd_all:
                mname = str(fd_mk.get("name") or "")
                ks_mk = _find_market_block(ks_all, mname)
                if not ks_mk:
                    continue
                fd_rows = fd_mk.get("odds") or []
                ks_rows = ks_mk.get("odds") or []
                if not fd_rows:
                    continue
                stats["markets_analyzed"] += 1
                mlabel = self._market_type_label(mname, league)

                mn_u = (mname or "").upper()
                is_spread_m = "SPREAD" in mn_u or "HANDICAP" in mn_u
                is_total_m = any(x in mn_u for x in ("TOTAL", "OVER/UNDER", "OU")) and "PLAYER" not in mn_u
                is_ml_m = not is_spread_m and not is_total_m

                for f_row in fd_rows:
                    k_row = self._match_kalshi_row(f_row, ks_rows if isinstance(ks_rows, list) else [])
                    if not isinstance(f_row, dict):
                        continue
                    lk = _liq_hint(k_row if isinstance(k_row, dict) else None)
                    liq_s = lk if lk != "-" else _liq_hint(f_row if isinstance(f_row, dict) else None)

                    def queue_two_way(pick_label: str, fd_d: Any, ks_d: Any, fd_o: Any) -> None:
                        d1 = _float_dec(fd_d)
                        d2 = _float_dec(fd_o)
                        kd = _float_dec(ks_d)
                        if not d1 or not d2 or not kd or d1 <= 1.0 or d2 <= 1.0 or kd <= 1.0:
                            return
                        kc = int(max(1, min(99, round(100.0 / kd))))
                        evs = ev_percent_three_methods_multi_sharp([(float(d1), float(d2))], float(kd))
                        pwr = float(evs.get("POWER", -999.0))
                        wc = float(evs.get("WORST_CASE", -999.0))
                        av = float(evs.get("AVERAGE", -999.0))
                        rows_for_event.append(
                            {
                                "mlabel": mlabel,
                                "pick": pick_label[:28],
                                "mname": str(mname),
                                "fd_pick": float(d1),
                                "fd_opp": float(d2),
                                "fd_d": float(d1),
                                "ks_d": float(kd),
                                "kc": kc,
                                "evp": pwr,
                                "evw": wc,
                                "eva": av,
                                "liq": liq_s,
                                "three_way": None,
                            }
                        )

                    # Moneyline / 1x2 (skip for spread/total rows — same keys mean different things)
                    dh, dd, da = f_row.get("home"), f_row.get("draw"), f_row.get("away")
                    kh, k_draw, ka = (k_row or {}).get("home"), (k_row or {}).get("draw"), (k_row or {}).get("away")
                    if is_ml_m and dh and da and kh and ka:
                        h_home = _float_dec(dh)
                        h_away = _float_dec(da)
                        k_home = _float_dec(kh)
                        k_away = _float_dec(ka)
                        if h_home and h_away and k_home and k_away:
                            home_name = str(doc.get("home") or "Home")[:12]
                            away_name = str(doc.get("away") or "Away")[:12]
                            queue_two_way(f"ML {home_name}", dh, kh, da)
                            queue_two_way(f"ML {away_name}", da, ka, dh)
                        if dd and k_draw:
                            h_d = _float_dec(dd)
                            k_d = _float_dec(k_draw)
                            if h_d and h_home and h_away and k_d and k_home and k_away:
                                price_c = int(max(1, min(99, round(100.0 / float(k_d)))))
                                ev3 = ev_percent_three_methods_three_way(
                                    float(h_home), float(h_d), float(h_away), 1, float(k_d)
                                )
                                pwr = float(ev3["POWER"])
                                wc = float(ev3["WORST_CASE"])
                                av = float(ev3["AVERAGE"])
                                rows_for_event.append(
                                    {
                                        "mlabel": mlabel,
                                        "pick": "ML Draw",
                                        "mname": str(mname),
                                        "fd_d": float(h_d),
                                        "ks_d": float(k_d),
                                        "kc": price_c,
                                        "evp": pwr,
                                        "evw": wc,
                                        "eva": av,
                                        "liq": "-",
                                        "three_way": (float(h_home), float(h_d), float(h_away)),
                                    }
                                )
                    # Totals
                    o, u = f_row.get("over"), f_row.get("under")
                    ko, ku = (k_row or {}).get("over"), (k_row or {}).get("under")
                    if is_total_m and o and u and ko and ku:
                        line = f_row.get("max") or f_row.get("line") or ""
                        queue_two_way(f"Over {line}", o, ko, u)
                        queue_two_way(f"Under {line}", u, ku, o)
                    # Spread
                    sh, sa = f_row.get("home"), f_row.get("away")
                    skh, ska = (k_row or {}).get("home"), (k_row or {}).get("away")
                    hdp = f_row.get("hdp")
                    if is_spread_m and sh and sa and skh and ska and hdp is not None:
                        hn = str(doc.get("home") or "Home")[:10]
                        an = str(doc.get("away") or "Away")[:10]
                        queue_two_way(f"Spr {hdp} {hn}", sh, skh, sa)
                        queue_two_way(f"Spr {hdp} {an}", sa, ska, sh)

            raw_count = len(rows_for_event)
            kept: List[Dict[str, Any]] = []
            for r in rows_for_event:
                if _debug_row_is_noise(r["ks_d"], r["evp"], r["evw"], r["eva"]):
                    stats["noise_filtered"] += 1
                else:
                    kept.append(r)
            kept.sort(key=lambda row: -_debug_row_abs_ev_max(row["evp"], row["evw"], row["eva"]))
            stats["table_rows"] += len(kept)
            for r in kept:
                if _debug_row_abs_ev_max(r["evp"], r["evw"], r["eva"]) > 8.0:
                    stats["edges_gt8"] += 1

            print(sep)
            print(self._fmt_event_banner(doc))
            print(sep)
            hdr = (
                f"{'Market':<16} {'Pick/Line':<26} {'FD Am':>8} {'KS Am':>8} {'KSc':>5} "
                f"{'EV% PWR':>9} {'EV% WC':>9} {'EV% AVG':>9} {'Liq':>5}"
            )
            print(hdr)
            print("-" * len(hdr))
            if not kept:
                print("(no rows after noise filter)")
            else:
                for r in kept:
                    fd_am = _fmt_american_from_dec(r["fd_d"])
                    ks_am = _fmt_american_from_dec(r["ks_d"])
                    sp = format_ev_percent_display(r["evp"])
                    sw = format_ev_percent_display(r["evw"])
                    sa_ = format_ev_percent_display(r["eva"])
                    print(
                        f"{str(r['mlabel'])[:16]:<16} {str(r['pick'])[:26]:<26} "
                        f"{fd_am:>8} {ks_am:>8} {r['kc']:>5} "
                        f"{sp:>9} {sw:>9} {sa_:>9} {str(r['liq'])[:5]:>5}"
                    )
            print(f"[DEBUG] This event: raw_rows={raw_count}, shown={len(kept)}, noise_filtered_here={raw_count - len(kept)}")

            sim_rows: List[Tuple[Dict[str, Any], float]] = []
            for r in kept:
                ok, ev_prod = self._debug_row_would_alert_production(r, league)
                if ok:
                    sim_rows.append((r, ev_prod))
            stats["rows_that_would_alert"] += len(sim_rows)
            sim_rows.sort(key=lambda t: -abs(t[1]))
            print("-" * len(hdr))
            print(
                "PRODUCTION SIMULATION (what would actually become alerts with current filter_payload)"
            )
            print("-" * len(hdr))
            if not sim_rows:
                print("(none - no rows passed minEv / minRoi / minSharpBooks / oddsRanges / minLimits / hold)")
            else:
                prod_col = f"EV%{prod_method}"
                sh = (
                    f"{'Market':<16} {'Pick/Line':<26} {'FD Am':>8} {'KS Am':>8} {'KSc':>5} "
                    f"{prod_col:>10}"
                )
                print(sh)
                print("-" * len(sh))
                for r, ev_prod in sim_rows:
                    fd_am = _fmt_american_from_dec(r["fd_d"])
                    ks_am = _fmt_american_from_dec(r["ks_d"])
                    evs = format_ev_percent_display(ev_prod)
                    print(
                        f"{str(r['mlabel'])[:16]:<16} {str(r['pick'])[:26]:<26} "
                        f"{fd_am:>8} {ks_am:>8} {r['kc']:>5} {evs:>10}"
                    )

        print(sep)
        print(
            f"[DEBUG] Summary this poll: events={stats['events']}, "
            f"markets_analyzed={stats['markets_analyzed']}, table_rows_shown={stats['table_rows']}, "
            f"noise_filtered={stats['noise_filtered']}, rows_that_would_alert={stats['rows_that_would_alert']}, "
            f"edges_absEV_gt8={stats['edges_gt8']}, "
            f"http_requests={getattr(client, 'http_request_count', 0)}"
        )
        print(sep + "\n")
        return stats

    def _diagnostic_sharp_quotes(
        self, vb: Dict[str, Any], odds_doc: Optional[Dict[str, Any]]
    ) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float, float]], str, str, int]:
        """Sharp two-way panels or three-way tuples for the value-bet row (for EV triplet logging)."""
        market = vb.get("market") or {}
        mname = str(market.get("name") or "")
        bet_side = str(vb.get("betSide") or "").lower()
        df = self.filter_payload.get("devigFilter") or {}
        sharp_names = [str(x) for x in (df.get("sharps") or [])]
        min_sharp = max(1, int(df.get("minSharpBooks", 1)))
        min_sharp_rules = self.filter_payload.get("minSharpLimits") or []
        hold_rules = df.get("hold") or []
        panels: List[Tuple[float, float]] = []
        triples: List[Tuple[float, float, float]] = []
        if not odds_doc or not odds_doc.get("bookmakers") or not sharp_names:
            return panels, triples, bet_side, mname, min_sharp
        bks = odds_doc["bookmakers"]
        if bet_side == "draw":
            for sn in sharp_names:
                mk = _find_market_block(_markets_list_for_book(bks, sn), mname)
                row = _first_odds_row(mk or {}) or {}
                t3 = _three_way_draw_decimals(row)
                if not t3:
                    continue
                dh, dd, da = t3
                if not _passes_hold([dh, dd, da], hold_rules):
                    continue
                if not _row_passes_sharp_limit(row, sn, min_sharp_rules):
                    continue
                triples.append((dh, dd, da))
        else:
            for sn in sharp_names:
                mk = _find_market_block(_markets_list_for_book(bks, sn), mname)
                row = _first_odds_row(mk or {}) or {}
                tw = _two_way_pick_opp_decimals(row, bet_side)
                if not tw:
                    continue
                d_pick, d_opp = tw
                if not _passes_hold([d_pick, d_opp], hold_rules):
                    continue
                if not _row_passes_sharp_limit(row, sn, min_sharp_rules):
                    continue
                panels.append((d_pick, d_opp))
        return panels, triples, bet_side, mname, min_sharp

    def _pipeline_log_game_if_new(self, vb: Dict[str, Any], odds_doc: Optional[Dict[str, Any]]) -> None:
        """Once per event id per poll: game banner, market count, books posting odds."""
        eid = vb.get("eventId")
        if eid is None:
            return
        k = int(eid)
        seen: Set[int] = getattr(self, "_diag_seen_eids", set())
        if k in seen:
            return
        seen.add(k)
        self._diag_seen_eids = seen
        ev_obj = vb.get("event") or {}
        home = str(ev_obj.get("home") or "")
        away = str(ev_obj.get("away") or "")
        teams = f"{away} @ {home}" if away and home else str(ev_obj.get("name") or "")
        st = str(ev_obj.get("status") or ev_obj.get("state") or "")
        lv = ev_obj.get("live", ev_obj.get("isLive", ""))
        n_expected = int(getattr(self, "_pipeline_disp_book_count", 0) or 0)
        if not odds_doc or not odds_doc.get("bookmakers"):
            print(
                f"[PIPELINE] Game: {teams} | status={st} live={lv} | markets_analyzed=0 | "
                f"books_with_odds=0/{max(n_expected, 1)}"
            )
            return
        bks = odds_doc["bookmakers"]
        names: Set[str] = set()
        n_books_posting = 0
        for _bk, mkts in bks.items():
            if not isinstance(mkts, list) or not mkts:
                continue
            has = False
            for m in mkts:
                if not isinstance(m, dict):
                    continue
                n = str(m.get("name") or "").strip()
                if n:
                    names.add(n)
                row = _first_odds_row(m)
                if row and any(_float_dec(row.get(x)) for x in ("home", "away", "draw", "over", "under")):
                    has = True
            if has:
                n_books_posting += 1
        denom = max(n_expected, len(bks), 1)
        print(
            f"[PIPELINE] Game: {teams} | status={st} live={lv} | markets_analyzed={len(names)} | "
            f"books_with_odds={n_books_posting}/{denom}"
        )

    def _pipeline_log_row_ev(self, vb: Dict[str, Any], odds_doc: Optional[Dict[str, Any]], built: Dict[str, Any]) -> None:
        """POWER / WORST_CASE / AVERAGE EV vs Kalshi for this row (even when model EV is ~0 or negative)."""
        k_dec = float(built["price"]) / 100.0
        teams = str(built.get("teams") or "")
        panels, triples, bs, mname, _ms = self._diagnostic_sharp_quotes(vb, odds_doc)
        api_ev = float(vb.get("expectedValue") or 0) * 100.0
        model_ev = float(built.get("ev", 0.0))
        triplet_str = ""
        if bs == "draw" and triples:
            dh, dd, da = triples[0]
            evm = ev_percent_three_methods_three_way(dh, dd, da, 1, k_dec)
            triplet_str = (
                f"POWER={evm['POWER']:.2f}% WC={evm['WORST_CASE']:.2f}% AVG={evm['AVERAGE']:.2f}%"
            )
        elif panels:
            evm = ev_percent_three_methods_multi_sharp(panels, k_dec)
            triplet_str = (
                f"POWER={evm['POWER']:.2f}% WC={evm['WORST_CASE']:.2f}% AVG={evm['AVERAGE']:.2f}%"
            )
        else:
            triplet_str = "POWER/WC/AVG=n/a (no sharp panels for this market/side)"
        sp = built.get("strict_pass", True)
        print(
            f"[PIPELINE] Row: {teams} | market={mname} side={bs} | api_ev={api_ev:.2f}% "
            f"model={model_ev:.2f}% | {triplet_str} | strict_pass={sp}"
        )

    async def fetch_alerts(self) -> List[EvAlert]:
        global _DOTENV_BOOTSTRAP_DONE
        if not _DOTENV_BOOTSTRAP_DONE:
            _reload_dotenv_safely()
            _DOTENV_BOOTSTRAP_DONE = True
            await reset_shared_odds_client()
        client = await get_shared_odds_client()
        if not client.api_key:
            if not hasattr(self, "_no_key_warned"):
                print("[MONITOR] [WARN] ODDS_API_KEY missing -- cannot fetch Odds-API.io")
                print(f"[MONITOR]    Expected .env at {_DOTENV_SCRIPT} or {_DOTENV_CWD} (reload attempted once per process).")
                self._no_key_warned = True
            return []
        if _env_bool("ODDS_DEBUG_MODE", "false"):
            self._debug_stats = await self._fetch_alerts_debug_inspection(client)
            return []

        try:
            raw_vb = await client.get_value_bets(self._target_book, True)
        except Exception as e:
            print(f"[MONITOR] [ERR] Odds-API value-bets error: {e}")
            return []

        if _diagnostic_mode() and not _env_bool("ODDS_DEBUG_MODE", "false"):
            try:
                mlb_c, nhl_c, tot_live = await _pipeline_live_league_counts(client)
                print(
                    f"[PIPELINE] Live MLB games: {mlb_c} | NHL: {nhl_c} | Total live events: {tot_live}"
                )
            except Exception as e:
                print(f"[PIPELINE] Live event counts unavailable: {e}")
        print(f"[PIPELINE] Raw value-bets from Kalshi: {len(raw_vb)}")

        leagues_filter = list(self.filter_payload.get("leagues") or [])
        mlb_nba_env = _env_bool("ODDS_API_MLB_NBA_ONLY", "true")
        mlb_nba_gate = _mlb_nba_gate_applies(leagues_filter, mlb_nba_env)
        # Default live-only: focus on in-play (set ODDS_API_LIVE_ONLY=false for pregame).
        live_only = _env_bool("ODDS_API_LIVE_ONLY", "true")

        filtered_vb: List[Dict[str, Any]] = []
        for vb in raw_vb:
            ev = vb.get("expectedValue")
            if ev is None:
                continue
            # Allow API rows at ~0% edge (and tiny negative float noise); we re-check EV after multi-sharp devig.
            if float(ev) < -1e-6:
                continue
            evp = float(ev) * 100.0
            if evp + 1e-9 < float(self.filter_payload.get("minRoi", 0)):
                continue
            ev_obj = vb.get("event") or {}
            league = _league_str(ev_obj.get("league"))
            if not _league_matches_filter(league, leagues_filter):
                continue
            if mlb_nba_gate and not _mlb_nba_only(league):
                continue
            if live_only:
                if ev_obj.get("live") is True or ev_obj.get("isLive") is True:
                    pass
                else:
                    st = str(ev_obj.get("status", "") or ev_obj.get("state", "") or "").lower().replace(" ", "")
                    if st not in ("live", "inprogress", "inplay", "started", "running"):
                        continue
            filtered_vb.append(vb)

        print(f"[PIPELINE] After league/live gates: {len(filtered_vb)}")

        event_ids = list({int(v["eventId"]) for v in filtered_vb if v.get("eventId") is not None})
        odds_by_id: Dict[int, Dict[str, Any]] = {}
        self._pipeline_disp_book_count = 0
        if event_ids:
            try:
                # Always request the ENV subscription list (e.g. all 10). Filter displayBooks only
                # affects which columns appear on alert cards (_build_display_books_payload), not /odds/multi.
                multi_books = odds_api_master_bookmakers()
                if not multi_books:
                    multi_books = ["Kalshi", self._reference_book]
                self._pipeline_disp_book_count = len(multi_books)
                multi = await client.get_odds_multi(event_ids, multi_books)
                for doc in multi:
                    eid = doc.get("id")
                    if eid is not None:
                        odds_by_id[int(eid)] = doc
            except Exception as e:
                print(f"[MONITOR] [WARN] odds/multi failed (using value-bet payload only): {e}")

        alerts: List[EvAlert] = []
        self._diag_seen_eids = set()
        for vb in filtered_vb:
            eid = vb.get("eventId")
            odds_doc = odds_by_id.get(int(eid)) if eid is not None else None
            if _diagnostic_mode():
                self._pipeline_log_game_if_new(vb, odds_doc)
            built = self._value_bet_to_normalized_bet(vb, odds_doc)
            if not built:
                continue
            if _diagnostic_mode():
                self._pipeline_log_row_ev(vb, odds_doc, built)
            ev_obj = vb.get("event") or {}
            alert = self.parse_bet_to_alert(built, ev_obj)
            if not alert:
                if _diagnostic_mode():
                    print(
                        f"[PIPELINE] Dropped: parse_bet_to_alert failed | {built.get('teams')} | "
                        f"{built.get('selection')}"
                    )
                continue
            alerts.append(alert)

        ms = int((self.filter_payload.get("devigFilter") or {}).get("minSharpBooks", 1))
        self._pipe_log_counter = getattr(self, "_pipe_log_counter", 0) + 1
        plc = self._pipe_log_counter
        if len(alerts) > 0 or len(filtered_vb) > 0 or plc == 1 or plc % 20 == 0:
            miss = len(filtered_vb) - len(alerts)
            print(
                f"[PIPELINE] Summary: value_bets_raw={len(raw_vb)} after_gates={len(filtered_vb)} "
                f"alerts_built={len(alerts)} dropped_or_skipped={miss} minSharpBooks={ms}"
            )
        return alerts

    def _value_bet_to_normalized_bet(
        self,
        vb: Dict[str, Any],
        odds_doc: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        ev_obj = vb.get("event") or {}
        home = str(ev_obj.get("home") or "")
        away = str(ev_obj.get("away") or "")
        teams = f"{away} @ {home}" if away and home else ""
        league = _league_str(ev_obj.get("league"))
        market = vb.get("market") or {}
        mname = str(market.get("name") or "")
        bet_side = str(vb.get("betSide") or "").lower()

        bo = vb.get("bookmakerOdds") or {}
        href = bo.get("href") if isinstance(bo, dict) else None
        ticker = extract_kalshi_ticker_from_href(href)

        k_dec = _float_dec(bo.get(bet_side)) if isinstance(bo, dict) else None
        if k_dec is None or k_dec <= 1.0:
            if _diagnostic_mode():
                print(
                    f"[PIPELINE] Dropped: invalid Kalshi decimal for side | {teams} | {mname} | "
                    f"side={bet_side} | href={href}"
                )
            return None
        price_cents = int(max(1, min(99, round(100.0 / k_dec))))

        # --- betTypes / excludedCategories (BookieBeats-style) ---
        bet_types = [str(x).upper() for x in (self.filter_payload.get("betTypes") or ["GAMELINES"])]
        if "GAMELINES" in bet_types and "PLAYER" in mname.upper():
            if _diagnostic_mode():
                print(f"[PIPELINE] Dropped: player prop (GAMELINES) | {teams} | {mname}")
            return None
        for ex in self.filter_payload.get("excludedCategories") or []:
            if ex and str(ex).lower() in mname.lower():
                if _diagnostic_mode():
                    print(f"[PIPELINE] Dropped: excluded category ({ex}) | {teams} | {mname}")
                return None

        market_type_bb = "Moneyline"
        if "TOTAL" in mname.upper() or "OVER" in mname.upper() or "UNDER" in mname.upper():
            market_type_bb = "Total Points" if ("NBA" in league.upper() or "BASKETBALL" in league.upper()) else "Total Runs"
        elif "SPREAD" in mname.upper() or "HANDICAP" in mname.upper():
            market_type_bb = "Point Spread"

        k_row: Dict[str, Any] = {}
        f_row: Dict[str, Any] = {}
        bks: Optional[Dict[str, Any]] = None
        if odds_doc and odds_doc.get("bookmakers"):
            bks = odds_doc["bookmakers"]
            kalshi_mk = _find_market_block(_markets_list_for_book(bks, "Kalshi"), mname)
            fd_mk = _find_market_block(_markets_list_for_book(bks, self._reference_book), mname)
            k_row = _first_odds_row(kalshi_mk or {}) or {}
            f_row = _first_odds_row(fd_mk or {}) or {}

        row_for_pick = k_row if k_row else f_row if f_row else market
        pick, qualifier, line_val = _pick_qualifier_line_for_side(home, away, mname, bet_side, row_for_pick)

        df = self.filter_payload.get("devigFilter") or {}
        method = str(df.get("method", "POWER")).upper()
        comb_type = str(df.get("type", "AVERAGE")).upper()
        sharp_names = [str(x) for x in (df.get("sharps") or [])]
        min_sharp = max(1, int(df.get("minSharpBooks", 1)))
        min_sharp_rules = self.filter_payload.get("minSharpLimits") or []
        hold_rules = df.get("hold") or []

        fair_prob: Optional[float] = None
        fd_dec_for_side: Optional[float] = None
        sharp_decimals: List[float] = []
        sharp_books_used = 0
        devig_book_labels: List[str] = []
        panels: List[Tuple[float, float, str]] = []
        triples: List[Tuple[float, float, float, str]] = []

        if bks and sharp_names:
            if bet_side == "draw":
                triples.clear()
                for sn in sharp_names:
                    mk = _find_market_block(_markets_list_for_book(bks, sn), mname)
                    row = _first_odds_row(mk or {}) or {}
                    t3 = _three_way_draw_decimals(row)
                    if not t3:
                        continue
                    dh, dd, da = t3
                    if not _passes_hold([dh, dd, da], hold_rules):
                        continue
                    if not _row_passes_sharp_limit(row, sn, min_sharp_rules):
                        continue
                    triples.append((dh, dd, da, sn))
                if len(triples) >= min_sharp:
                    draw_probs: List[float] = []
                    for dh, dd, da, _sn in triples:
                        _, f_draw, _ = self._calc.fair_probs_three_way(dh, dd, da, method)
                        draw_probs.append(f_draw)
                    fair_prob = min(draw_probs) if comb_type == "WORST_CASE" else sum(draw_probs) / len(draw_probs)
                    sharp_books_used = len(triples)
                    devig_book_labels = [t[3] for t in triples[: min_sharp + 2]]
                    dh0, dd0, da0, _ = triples[0]
                    sharp_decimals = [dh0, dd0, da0]
                    fd_dec_for_side = dd0
            else:
                panels.clear()
                for sn in sharp_names:
                    mk = _find_market_block(_markets_list_for_book(bks, sn), mname)
                    row = _first_odds_row(mk or {}) or {}
                    tw = _two_way_pick_opp_decimals(row, bet_side)
                    if not tw:
                        continue
                    d_pick, d_opp = tw
                    if not _passes_hold([d_pick, d_opp], hold_rules):
                        continue
                    if not _row_passes_sharp_limit(row, sn, min_sharp_rules):
                        continue
                    panels.append((d_pick, d_opp, sn))
                if len(panels) >= min_sharp:
                    pick_probs: List[float] = []
                    for d_pick, d_opp, _sn in panels:
                        p_side_a, p_side_b = self._calc.fair_probs_two_way(d_pick, d_opp, method)
                        pick_probs.append(p_side_a)
                    fair_prob = min(pick_probs) if comb_type == "WORST_CASE" else sum(pick_probs) / len(pick_probs)
                    sharp_books_used = len(panels)
                    devig_book_labels = [p[2] for p in panels[: min_sharp + 2]]
                    d0, opp0 = panels[0][0], panels[0][1]
                    sharp_decimals = [d0, opp0]
                    fd_dec_for_side = d0

        multi_panel_mode = bool(bks and sharp_names and min_sharp > 1)
        if multi_panel_mode and fair_prob is None:
            pc = len(triples) if bet_side == "draw" else len(panels)
            if _env_bool("ODDS_ALERT_DIAG", "false") or _diagnostic_mode():
                print(
                    f"[PIPELINE] Dropped: insufficient sharp quotes ({pc}/{min_sharp}) for "
                    f"\"{mname}\" side={bet_side} | {teams}"
                )
            return None

        if fair_prob is None and f_row and not multi_panel_mode:
            if bet_side in ("over", "under"):
                d1 = _float_dec(f_row.get("over"))
                d2 = _float_dec(f_row.get("under"))
                if d1 and d2 and d1 > 1.0 and d2 > 1.0:
                    sharp_decimals = [d1, d2]
                    p_over, p_under = self._calc.fair_probs_two_way(d1, d2, method)
                    fair_prob = p_over if bet_side == "over" else p_under
                    fd_dec_for_side = d1 if bet_side == "over" else d2
                    sharp_books_used = 1
            elif bet_side == "draw":
                dh = _float_dec(f_row.get("home"))
                dd = _float_dec(f_row.get("draw"))
                da = _float_dec(f_row.get("away"))
                if dh and dd and da and min(dh, dd, da) > 1.0:
                    sharp_decimals = [dh, dd, da]
                    _, f_draw, _ = self._calc.fair_probs_three_way(dh, dd, da, method)
                    fair_prob = f_draw
                    fd_dec_for_side = dd
                    sharp_books_used = 1
            else:
                dh = _float_dec(f_row.get("home"))
                da = _float_dec(f_row.get("away"))
                if dh and da and dh > 1.0 and da > 1.0:
                    sharp_decimals = [dh, da]
                    p_home, p_away = self._calc.fair_probs_two_way(dh, da, method)
                    fair_prob = p_home if bet_side == "home" else p_away
                    fd_dec_for_side = dh if bet_side == "home" else da
                    sharp_books_used = 1

        if fair_prob is None and not multi_panel_mode:
            fair_prob = 1.0 / k_dec
            mh = _float_dec(market.get("home"))
            ma = _float_dec(market.get("away"))
            if mh and ma and mh > 1.0 and ma > 1.0:
                sharp_decimals = [mh, ma]
            else:
                sharp_decimals = [k_dec, max(1.02, k_dec * 1.01)]
            sharp_books_used = max(sharp_books_used, 1)

        ev_percent = self._calc.ev_percent_vs_kalshi(fair_prob, price_cents)
        kalshi_am = decimal_to_american(k_dec)
        fd_am = decimal_to_american(fd_dec_for_side) if fd_dec_for_side else kalshi_am

        relaxed_fp = copy.deepcopy(self.filter_payload)
        relaxed_fp["minRoi"] = -1e9
        relaxed_fp.setdefault("devigFilter", {})["minEv"] = -1e9
        relaxed_fp.setdefault("devigFilter", {})["minLimit"] = -1e9
        calc_relaxed = EVCalculator(relaxed_fp)
        decs_for_devig = sharp_decimals if len(sharp_decimals) >= 2 else [k_dec, max(1.02, k_dec)]

        strict_roi = self._calc.passes_roi(ev_percent)
        relaxed_roi = calc_relaxed.passes_roi(ev_percent)
        strict_devig = self._calc.passes_devig_filter(
            decs_for_devig, ev_percent, 0.0, sharp_books_count=sharp_books_used
        )
        relaxed_devig = calc_relaxed.passes_devig_filter(
            decs_for_devig, ev_percent, 0.0, sharp_books_count=sharp_books_used
        )
        kal_li = _row_limit_hint(k_row)
        kal_ok = kal_li is None or self._calc.passes_min_limits_kalshi(float(kal_li))
        odds_ok = self._calc.passes_odds_ranges(kalshi_am)

        strict_ok = bool(strict_roi and strict_devig and kal_ok and odds_ok)
        relaxed_ok = bool(relaxed_roi and relaxed_devig and kal_ok and odds_ok)

        if not strict_ok and not (relaxed_ok and _diagnostic_mode()):
            if _diagnostic_mode():
                parts = []
                if not strict_roi:
                    parts.append(f"minRoi(ev={ev_percent:.3f}%)")
                if not strict_devig:
                    parts.append("devig(minEv/minSharp/hold)")
                if not kal_ok:
                    parts.append("kalshi_min_contract")
                if not odds_ok:
                    parts.append("odds_range")
                print(
                    f"[PIPELINE] Dropped: {'; '.join(parts) or 'unknown'} | {teams} | {mname} | "
                    f"side={bet_side} | sharp_books={sharp_books_used}/{min_sharp}"
                )
            return None

        fair_odds_am = decimal_to_american(1.0 / fair_prob) if fair_prob and fair_prob > 0 else None

        devig_books = devig_book_labels[:8] if devig_book_labels else [self._reference_book]

        disp_names = [str(x) for x in (self.filter_payload.get("displayBooks") or [])]
        display = _build_display_books_payload(
            pick, bks, mname, bet_side, disp_names, kalshi_am, k_row
        )

        return {
            "market": market_type_bb,
            "teams": teams,
            "selection": pick,
            "line": line_val,
            "qualifier": qualifier,
            "odds": kalshi_am,
            "price": price_cents,
            "ev": ev_percent,
            "limit": 0.0,
            "fairOdds": fair_odds_am,
            "link": href or "",
            "displayBooks": display,
            "devigBooks": devig_books,
            "ticker": ticker,
            "strict_pass": strict_ok,
        }

    async def check_for_new_alerts(self) -> None:
        self.last_poll_time = time.time()
        debug = _env_bool("ODDS_DEBUG_MODE", "false")
        alerts = await self.fetch_alerts()
        if debug:
            self._last_cycle_alert_count = 0
            ds = getattr(self, "_debug_stats", {}) or {}
            ag = self._debug_aggregate
            ag["polls"] = ag.get("polls", 0) + 1
            ev_n = int(ds.get("events", 0))
            ag["events_max"] = max(ag.get("events_max", 0), ev_n)
            ag["events_sum"] = ag.get("events_sum", 0) + ev_n
            ag["markets"] = ag.get("markets", 0) + int(ds.get("markets_analyzed", 0))
            ag["rows"] = ag.get("rows", 0) + int(ds.get("table_rows", 0))
            ag["noise"] = ag.get("noise", 0) + int(ds.get("noise_filtered", 0))
            ag["alerts"] = ag.get("alerts", 0) + int(ds.get("rows_that_would_alert", 0))
            ag["edges8"] = ag.get("edges8", 0) + int(ds.get("edges_gt8", 0))
            self.last_check_time = datetime.now()
            return

        if alerts:
            print(f"[MONITOR] Fetched {len(alerts)} alert(s) from Odds-API.io")
            for i, alert in enumerate(alerts[:3]):
                print(f"[MONITOR]   Alert {i+1}: {alert.teams} - {alert.pick} (EV: {alert.ev_percent:.2f}%)")
        else:
            self._empty_poll_log_counter += 1
            if self._empty_poll_log_counter == 1 or self._empty_poll_log_counter % 20 == 0:
                print(f"[MONITOR] No alerts (poll #{self._empty_poll_log_counter}) -- Odds-API idle or filtered")

        current_hashes: Set[str] = set()
        current_alerts_by_hash: Dict[str, EvAlert] = {}
        for alert in alerts:
            alert_hash = f"{alert.ticker}|{alert.pick}|{alert.qualifier}|{alert.odds}"
            current_hashes.add(alert_hash)
            current_alerts_by_hash[alert_hash] = alert

        if not alerts:
            self._empty_poll_count += 1
            if self._empty_poll_count >= 2 and self._seen_alerts:
                all_removed = self._seen_alerts.copy()
                self._seen_alerts.clear()
                for callback in self.removed_alert_callbacks:
                    try:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(all_removed)
                        else:
                            callback(all_removed)
                    except Exception as e:
                        print(f"[WARN] Error in removed alert callback: {e}")
                print(f"[DEL] Odds-API returned empty for {self._empty_poll_count} polls - cleared all {len(all_removed)} alerts")
                self._empty_poll_count = 0
            return
        if self._empty_poll_count > 0:
            self._empty_poll_count = 0

        removed_hashes = self._seen_alerts - current_hashes
        if removed_hashes:
            self._seen_alerts -= removed_hashes
            for callback in self.removed_alert_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(removed_hashes)
                    else:
                        callback(removed_hashes)
                except Exception as e:
                    print(f"[WARN] Error in removed alert callback: {e}")

        new_alerts: List[EvAlert] = []
        updated_alerts: List[EvAlert] = []
        for alert_hash, alert in current_alerts_by_hash.items():
            if alert_hash not in self._seen_alerts:
                self._seen_alerts.add(alert_hash)
                new_alerts.append(alert)
                self._previous_alert_values[alert_hash] = {
                    "ev_percent": alert.ev_percent,
                    "liquidity": getattr(alert, "liquidity", 0),
                    "odds": alert.odds,
                }
            else:
                prev = self._previous_alert_values.get(alert_hash, {})
                ev_changed = abs(float(prev.get("ev_percent", 0)) - float(alert.ev_percent)) > 0.01
                liq_changed = abs(float(prev.get("liquidity", 0)) - float(getattr(alert, "liquidity", 0))) > 0.01
                odds_changed = prev.get("odds") != alert.odds
                if ev_changed or liq_changed or odds_changed:
                    updated_alerts.append(alert)
                    self._previous_alert_values[alert_hash] = {
                        "ev_percent": alert.ev_percent,
                        "liquidity": getattr(alert, "liquidity", 0),
                        "odds": alert.odds,
                    }

        if new_alerts:
            print(f"[ODDS API] Emitting {len(new_alerts)} new alert(s) to callbacks")
        for alert in new_alerts:
            print(f"[ODDS API]   Alert: {alert.teams} - {alert.pick} ({alert.ev_percent:.2f}% EV)")
            for callback in self.alert_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(alert)
                    else:
                        callback(alert)
                except Exception as e:
                    print(f"[WARN] Error in alert callback: {e}")
                    import traceback

                    traceback.print_exc()

        for alert in updated_alerts:
            for callback in self.updated_alert_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(alert)
                    else:
                        callback(alert)
                except Exception as e:
                    print(f"[WARN] Error in updated alert callback: {e}")

        for removed_hash in removed_hashes:
            self._previous_alert_values.pop(removed_hash, None)

        if new_alerts:
            print(f"[ALERT] Found {len(new_alerts)} new/reappeared alert(s): {[f'{a.teams} - {a.pick}' for a in new_alerts[:3]]}")
        if updated_alerts:
            print(f"[UPD] Updated {len(updated_alerts)} alert(s) with new EV/liquidity")
        if removed_hashes:
            print(f"[DEL] {len(removed_hashes)} alert(s) disappeared from Odds-API feed")

        if not hasattr(self, "_last_alert_count"):
            self._last_alert_count = 0
        current_count = len(alerts)
        if current_count != self._last_alert_count:
            if current_count == 0:
                print(f"[MONITOR] Odds-API feed is now empty (was {self._last_alert_count} alerts)")
            else:
                print(f"[MONITOR] Odds-API has {current_count} active alert(s) (was {self._last_alert_count})")
            self._last_alert_count = current_count

        self.last_check_time = datetime.now()
        self._last_cycle_alert_count = len(alerts)

    async def monitor_loop(self) -> None:
        print("[MONITOR] Starting Odds-API.io monitoring loop...")
        print(f"   Polling every {self.poll_interval}s (FREE TIER SAFE -- only MLB+NBA when ODDS_API_MLB_NBA_ONLY=true)")
        while self.running:
            try:
                await self.check_for_new_alerts()
                await asyncio.sleep(self.poll_interval)
            except Exception as e:
                print(f"[ERR] Error in Odds-API monitor loop: {e}")
                await asyncio.sleep(5)

    async def start(self) -> bool:
        self.running = True
        self.session = aiohttp.ClientSession()
        print("[OK] Odds-API.io EV monitor started")
        return True

    def update_token(self, new_token: str) -> None:
        """Compatibility: BookieBeats bearer token. For Odds-API prefer ODDS_API_KEY in .env."""
        self.auth_token = new_token
        self._token_error_count = 0
        if not self.running:
            print("[TOKEN] Odds-API monitor was stopped -- mark running (restart loop externally if needed)")
            self.running = True
        print(f"[TOKEN] update_token ignored for Odds-API (set ODDS_API_KEY); BB token len={len(new_token or '')}")

    async def stop(self) -> None:
        self.running = False
        if self.session:
            await self.session.close()
            self.session = None
        print("[STOP] Odds-API.io EV monitor stopped")


async def _test_run() -> None:
    """
    Standalone test: load .env, print ENV DEBUG, poll for ODDS_TEST_MINUTES (default 10), then summary.
    FREE TIER SAFE -- respects ODDS_API_MLB_NBA_ONLY; live-focused (ODDS_API_LIVE_ONLY default true in monitor).
    UPGRADE PATH: add WebSocket here instead of polling.
    """
    global _DOTENV_BOOTSTRAP_DONE
    _reload_dotenv_safely()
    if "--debug" in sys.argv:
        os.environ["ODDS_DEBUG_MODE"] = "true"
        os.environ.setdefault("ODDS_DEBUG_MAX_EVENTS", "12")
        os.environ.setdefault("ODDS_POLL_INTERVAL_SECONDS", "45")
    if _env_bool("ODDS_DEBUG_MODE", "false"):
        # Standalone debug: always use a 10-minute window for validation (.env cannot shorten it).
        os.environ["ODDS_TEST_MINUTES"] = "10"
    _DOTENV_BOOTSTRAP_DONE = False
    await reset_shared_odds_client()

    print_env_debug(standalone=True)
    if _env_bool("ODDS_DEBUG_MODE", "false"):
        print("\n" + "#" * 76)
        print("# DEBUG MODE - American odds + 3 devig methods + PRODUCTION SIMULATION - all MLB/NBA markets")
        print("# Main table: strict filters bypassed | After each game: rows that would alert under filter_payload")
        print("# Production path unchanged when ODDS_DEBUG_MODE=false")
        print("#" * 76 + "\n")

    minutes = float(os.getenv("ODDS_TEST_MINUTES", "10"))
    poll_sec = float(os.getenv("ODDS_POLL_INTERVAL_SECONDS", "45"))
    print(f"\n[MONITOR] Standalone test -- duration={minutes} min, poll={poll_sec}s (live games only unless ODDS_API_LIVE_ONLY=false)\n")

    m = OddsEVMonitor(auth_token=None)
    m._debug_aggregate = {
        "events_max": 0,
        "events_sum": 0,
        "markets": 0,
        "rows": 0,
        "noise": 0,
        "alerts": 0,
        "edges8": 0,
        "polls": 0,
    }
    m.set_filter(
        {
            "state": "ND",
            "bettingBooks": ["Kalshi"],
            "displayBooks": ["Kalshi", "FanDuel"],
            "leagues": ["BASEBALL_ALL", "BASKETBALL_ALL"],
            "betTypes": ["GAMELINES"],
            "minRoi": 0,
            "middleStatus": "INCLUDE",
            "middleFilters": [{"sport": "Any", "minHold": 0, "minMiddle": 0}],
            "devigFilter": {
                "sharps": ["FanDuel"],
                "method": "POWER",
                "type": "AVERAGE",
                "minEv": 0,
                "minLimit": 0,
                "minSharpBooks": 1,
                "hold": [{"book": "Any", "max": 8}],
            },
            "oddsRanges": [{"book": "Any", "min": -500, "max": 500}],
            "minLimits": [{"book": "Kalshi", "min": 0}],
        }
    )
    m.poll_interval = poll_sec
    print(f"[MONITOR] Effective poll_interval={m.poll_interval}s (from env after .env reload)\n")

    stats = {"new_callbacks": 0, "updated_callbacks": 0, "polls": 0, "peak_active": 0}

    async def _on_new(a: EvAlert) -> None:
        stats["new_callbacks"] += 1
        tk = a.ticker or "?"
        print(
            f"[TEST ALERT] {a.teams} | {a.pick} | EV={a.ev_percent:.2f}% | "
            f"ticker={tk} | {a.price_cents}¢ | {a.market_type}"
        )

    async def _on_upd(a: EvAlert) -> None:
        stats["updated_callbacks"] += 1
        print(f"[TEST UPDATE] {a.teams} | {a.pick} | EV={a.ev_percent:.2f}%")

    m.add_alert_callback(_on_new)
    m.updated_alert_callbacks.append(_on_upd)

    await m.start()
    t_end = time.time() + 60.0 * minutes
    try:
        while True:
            now = time.time()
            if now >= t_end:
                break
            stats["polls"] += 1
            await m.check_for_new_alerts()
            n = getattr(m, "_last_cycle_alert_count", 0)
            stats["peak_active"] = max(stats["peak_active"], n)
            oc = await get_shared_odds_client()
            print(
                f"[MONITOR] poll #{stats['polls']} | active_alerts={n} | "
                f"new_cb={stats['new_callbacks']} | http_requests={oc.http_request_count}"
            )
            remaining = t_end - time.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(m.poll_interval, max(0.25, remaining)))
    finally:
        client = await get_shared_odds_client()
        http_n = getattr(client, "http_request_count", 0)
        await m.stop()
        print("\n" + "=" * 60)
        if _env_bool("ODDS_DEBUG_MODE", "false"):
            ag = getattr(m, "_debug_aggregate", {})
            print(
                f"Test completed (DEBUG) -- events_fetched_sum={ag.get('events_sum', 0)}, "
                f"events_fetched_max_per_poll={ag.get('events_max', 0)}, "
                f"markets_analyzed_sum={ag.get('markets', 0)}, "
                f"table_rows_shown_sum={ag.get('rows', 0)}, "
                f"noise_filtered_sum={ag.get('noise', 0)}, "
                f"rows_that_would_alert_sum={ag.get('alerts', 0)}, "
                f"edges_absEV_gt8_sum={ag.get('edges8', 0)}, "
                f"polls={ag.get('polls', 0)}, http_requests={http_n}, "
                f"peak_active_alerts={stats['peak_active']}"
            )
            http_safe = "safe" if http_n < 90 else ("near hourly cap" if http_n < 100 else "at/above typical free-tier burst")
            print(
                "\n--- Analysis (debug) ---\n"
                f"Data quality: {ag.get('edges8', 0)} row-polls with max|EV%|>8 across shown lines, "
                f"{ag.get('alerts', 0)} production-simulation alert rows (passed current filter_payload), "
                f"{ag.get('noise', 0)} noise rows filtered, "
                f"HTTP usage {http_safe} ({http_n} requests)."
            )
        else:
            print(
                f"Test completed -- new_alert_callbacks={stats['new_callbacks']}, "
                f"updated={stats['updated_callbacks']}, polls={stats['polls']}, "
                f"peak_active={stats['peak_active']}, http_requests={http_n}"
            )
        print("=" * 60)
        try:
            await client.close()
        except Exception:
            pass
        await reset_shared_odds_client()


if __name__ == "__main__":
    # FREE TIER SAFE -- MLB/NBA filter; UPGRADE PATH: add WebSocket here
    # Tip: `python odds_ev_monitor.py --debug` forces ODDS_DEBUG_MODE on for one run (after .env load).
    asyncio.run(_test_run())
