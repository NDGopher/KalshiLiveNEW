"""
Real-Time Betting Dashboard
Web-based dashboard for instant betting on Kalshi alerts
"""
import asyncio
import json
import os
import sys
import csv
import io
import time
from pathlib import Path
from dotenv import load_dotenv

# Load project .env FIRST (before odds/kalshi imports read ODDS_*). utf-8-sig strips UTF-8 BOM so
# ODDS_API_BOOKMAKERS is not read as "\ufeffODDS_API_BOOKMAKERS" (which would fall back to 2 books).
_DOTENV_PROJECT = Path(__file__).resolve().parent / ".env"
_DOTENV_CWD = Path.cwd() / ".env"
load_dotenv(_DOTENV_PROJECT, override=True, encoding="utf-8-sig")
load_dotenv(_DOTENV_CWD, override=True, encoding="utf-8-sig")
# Some setups use `.env.env` only; load after `.env` so explicit `.env` wins on conflicts.
_DOTENV_ALT_PROJECT = Path(__file__).resolve().parent / ".env.env"
_DOTENV_ALT_CWD = Path.cwd() / ".env.env"
load_dotenv(_DOTENV_ALT_PROJECT, override=False, encoding="utf-8-sig")
load_dotenv(_DOTENV_ALT_CWD, override=False, encoding="utf-8-sig")

# Fix Unicode encoding for Windows console
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        # Python < 3.7 or encoding not available, use replacement
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict
from flask import Flask, render_template, jsonify, request, send_from_directory, Response, session, redirect
from flask_socketio import SocketIO, emit
from functools import wraps
import threading
import base64
import uuid
import warnings

# Suppress SSL and Future exception warnings
warnings.filterwarnings('ignore', category=RuntimeWarning, message='.*Future exception was never retrieved.*')
warnings.filterwarnings('ignore', message='.*APPLICATION_DATA_AFTER_CLOSE_NOTIFY.*')
warnings.filterwarnings('ignore', message='.*SSL.*')
from ev_alert import EvAlert
from market_matcher import MarketMatcher
from kalshi_client import KalshiClient
from odds_ev_monitor import OddsEVMonitor as EvMonitorImpl, _market_names_match
from odds_api_client import (
    get_shared_odds_client,
    _norm_book,
    major_league_slug_for_events,
    normalize_sport_slug_key,
    odds_api_master_bookmakers,
    sport_slug_query_for_api,
)
from ev_calculator import decimal_to_american


def monitor_poll_seconds() -> float:
    """Odds-API.io poll interval from env (default 45s; production often 8s)."""
    return float(os.getenv("ODDS_POLL_INTERVAL_SECONDS", "45"))


# Get initial deposit from .env (fallback if API doesn't return cumulative_deposits)
INITIAL_DEPOSIT_DOLLARS = float(os.getenv('INITIAL_DEPOSIT', '980.0'))  # Default to $980 if not set

# When started as `python dashboard.py`, import name is __main__; Flask can infer the wrong
# package root and load a stale templates/dashboard.html from cwd or elsewhere. Pin paths to
# this file's directory so the served page always matches templates/ on disk.
_APP_ROOT = os.path.dirname(os.path.abspath(__file__))
app = Flask(
    __name__,
    root_path=_APP_ROOT,
    static_folder=os.path.join(_APP_ROOT, 'static'),
    static_url_path='',
    template_folder=os.path.join(_APP_ROOT, 'templates'),
)
if not globals().get('_flask_template_paths_logged'):
    globals()['_flask_template_paths_logged'] = True
    print(f"[DASHBOARD] Flask paths — root_path={_APP_ROOT}")
    print(f"[DASHBOARD] Flask paths — template_folder={app.template_folder}")

# Bumped when dashboard HTML/JS changes; shown in-page and in X-Kalshi-Dashboard-UI response header.
DASHBOARD_UI_BUILD = os.getenv('DASHBOARD_UI_BUILD', '2026-04-14-tabs')

app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'kalshi-live-betting-secret-key-change-in-production')
app.config['SESSION_COOKIE_SECURE'] = False  # Set to True if using HTTPS
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Basic Authentication (simple username/password from .env)
def check_auth(username, password):
    """Check if username/password is correct"""
    expected_username = os.getenv('DASHBOARD_USERNAME', 'admin')
    expected_password = os.getenv('DASHBOARD_PASSWORD', '')
    return username == expected_username and password == expected_password

def authenticate():
    """Sends a 401 response that enables basic auth or redirects to login page"""
    # Check if request wants JSON (API call)
    if request.path.startswith('/api/') or request.headers.get('Accept', '').startswith('application/json'):
        return Response(
            'Could not verify your access level for that URL.\n'
            'You have to login with proper credentials', 401,
            {'WWW-Authenticate': 'Basic realm="Login Required"'})
    else:
        # For HTML pages, redirect to login page
        from flask import redirect, url_for
        return redirect('/login')

def requires_auth(f):
    """Decorator to require authentication"""
    @wraps(f)
    def decorated(*args, **kwargs):
        # Skip auth if no password is set (local development)
        if not os.getenv('DASHBOARD_PASSWORD'):
            return f(*args, **kwargs)
        
        # Check session first (from login form)
        if session.get('authenticated'):
            return f(*args, **kwargs)
        
        # Check basic auth (from browser or API)
        auth = request.authorization
        if auth and check_auth(auth.username, auth.password):
            # Set session for future requests
            session['authenticated'] = True
            session['username'] = auth.username
            return f(*args, **kwargs)
        
        # Not authenticated - show login
        return authenticate()
    return decorated

# Global state
kalshi_client: Optional[KalshiClient] = None
market_matcher: Optional[MarketMatcher] = None
odds_ev_monitor: Optional[EvMonitorImpl] = None  # primary Odds-API.io monitor (first selected filter)
active_alerts: Dict[str, Dict] = {}  # alert_id -> alert_data
monitor_thread: Optional[threading.Thread] = None
monitor_loop: Optional[asyncio.AbstractEventLoop] = None  # Store the monitor's event loop
monitor_running = False
# Dedicated API-only Kalshi client and loop - dashboard/portfolio NEVER use monitor_loop
# so monitoring and auto-betting are never interrupted by page loads or API calls
api_kalshi_client: Optional[KalshiClient] = None
api_loop: Optional[asyncio.AbstractEventLoop] = None
api_loop_thread: Optional[threading.Thread] = None
ALERT_TTL = 30  # Remove alerts after 30 seconds if EV drops
user_max_bet_amount = 100.0  # Default max bet amount in dollars (user-configurable)
dashboard_min_ev = 0.0  # Minimum EV to show on dashboard (for manual betting, default 0% to show all)
per_event_max_bet = 404.0  # Default max bet per event in dollars (user-configurable, default $404)

# Auto-bet settings (global toggle)
auto_bet_enabled = False  # Default OFF at startup; enable from dashboard when ready

# Per-filter auto-bet settings
# Format: {filter_name: {'ev_min': float, 'ev_max': float, 'odds_min': int, 'odds_max': int, 'amount': float, 'enabled': bool}}
auto_bet_settings_by_filter = {}  # Dict of filter_name -> settings dict
nhl_over_bet_amount = 202.0  # NHL over bet amount in dollars (configurable from frontend, shared across all filters)
px_novig_multiplier = 2.0  # Multiplier for bet amount when both ProphetX and Novig are in devig books (default 2.0x)

# Market-type specific bet amounts (applied before PX+Novig multiplier)
moneyline_bet_amount = 151.0  # Moneyline bet amount in dollars (35.75% ROI justifies larger bets)
total_bet_amount = 101.0  # Total Points/Goals bet amount in dollars (keep at default)
spread_bet_amount = 75.0  # Point Spread bet amount in dollars (1.10% ROI - reduce bet size)

# Legacy global settings (for backward compatibility, will be migrated to per-filter)
auto_bet_ev_min = 5.0  # Minimum EV percentage (auto-bettor uses conservative 5% threshold, dashboard shows 0% for manual betting)
auto_bet_ev_max = 25.0  # Maximum EV percentage
auto_bet_odds_min = -200  # Minimum American odds (matches new conservative filter range)
auto_bet_odds_max = 200  # Maximum American odds (matches new conservative filter range)
auto_bet_amount = 101.0  # Auto-bet amount in dollars

# Track auto-bet submarkets to prevent duplicates (ticker + side combination)
auto_bet_submarkets = set()  # Set of (ticker, side) tuples
auto_bet_submarket_data = {}  # Dict of (ticker, side) -> {'line': float, 'pick': str, 'qualifier': str, 'market_type': str, 'teams': str, 'pick_direction': str} (for reverse middle detection)
auto_bet_processing_submarkets = set()  # Set of (ticker, side) tuples currently being processed
auto_bet_processing_alert_ids = set()  # Set of alert_ids currently being processed (prevents duplicate tasks for same alert)
auto_bet_submarket_to_alert_id = {}  # Dict mapping (ticker, side) -> alert_id to track which alert is processing which submarket
auto_bet_submarket_tasks = {}  # Dict mapping (ticker, side) -> asyncio.Task to track active tasks
auto_bet_lock_holder = None  # Track which task is currently holding the lock (for debugging stuck locks)
auto_bet_lock_acquired_at = None  # Track when the lock was acquired (for debugging stuck locks)
# Track by: game_name -> market_type -> pick_direction -> list of (ticker, side) tuples
# For totals: pick_direction is "Over" or "Under"
# For spreads/moneylines: pick_direction is the team name (for reverse middle detection)

# Failed auto-bet tracking (for debugging/logging)
# Store last 100 failed auto-bet attempts with full details
failed_auto_bets = []  # List of dicts with failure details
MAX_FAILED_BETS = 100  # Keep last 100 failures

# Successful auto-bet tracking (for comparison with failures)
# Store last 100 successful auto-bet attempts with full details
successful_auto_bets = []  # List of dicts with success details
MAX_SUCCESSFUL_BETS = 100  # Keep last 100 successes

def store_successful_auto_bet(alert_id, alert, alert_data, ticker, side, expected_price, executed_price, price_delta, ev_percent, odds, filter_name, trade_timings, additional_logs=None):
    """Store a successful auto-bet with full details and timing for comparison with failures"""
    global successful_auto_bets
    from datetime import datetime
    import traceback
    
    try:
        success_entry = {
            'timestamp': datetime.now().isoformat(),
            'alert_id': alert_id,
            'teams': alert.teams if alert else alert_data.get('teams', 'N/A'),
            'pick': alert.pick if alert else alert_data.get('pick', 'N/A'),
            'market_type': alert.market_type if alert else alert_data.get('market_type', 'N/A'),
            'qualifier': alert.qualifier if alert else alert_data.get('qualifier', 'N/A'),
            'ev_percent': ev_percent or (alert.ev_percent if alert else alert_data.get('ev_percent', 0)),
            'odds': odds or (alert_data.get('american_odds', 'N/A')),
            'ticker': ticker or alert_data.get('ticker', 'N/A'),
            'side': side or alert_data.get('side', 'N/A'),
            'expected_price': expected_price or alert_data.get('price_cents'),
            'executed_price': executed_price,
            'price_delta': price_delta,
            'filter_name': filter_name or (getattr(alert, 'filter_name', None) if alert else alert_data.get('filter_name', 'N/A')),
            'trade_timings': trade_timings or {},
            'additional_logs': additional_logs or []
        }
        
        successful_auto_bets.append(success_entry)
        
        # Keep only last MAX_SUCCESSFUL_BETS
        if len(successful_auto_bets) > MAX_SUCCESSFUL_BETS:
            successful_auto_bets = successful_auto_bets[-MAX_SUCCESSFUL_BETS:]
        
        print(f"[SUCCESSFUL-BETS] ✅ Stored success: {alert.teams if alert else 'N/A'} - {alert.pick if alert else 'N/A'} | Total time: {trade_timings.get('total_trade_duration_ms', 'N/A')}ms | Total stored: {len(successful_auto_bets)}")
    except Exception as e:
        print(f"[SUCCESSFUL-BETS] ❌ ERROR storing successful bet: {e}")
        print(f"[SUCCESSFUL-BETS] Traceback: {traceback.format_exc()}")

def store_failed_auto_bet(alert_id, alert, alert_data, error, reason=None, ticker=None, side=None, expected_price=None, current_price=None, price_delta=None, ev_percent=None, odds=None, filter_name=None, additional_logs=None):
    """Store a failed auto-bet attempt with full details for debugging"""
    global failed_auto_bets
    from datetime import datetime
    import traceback
    
    try:
        failure_entry = {
            'timestamp': datetime.now().isoformat(),
            'alert_id': alert_id,
            'teams': alert.teams if alert else alert_data.get('teams', 'N/A'),
            'pick': alert.pick if alert else alert_data.get('pick', 'N/A'),
            'market_type': alert.market_type if alert else alert_data.get('market_type', 'N/A'),
            'qualifier': alert.qualifier if alert else alert_data.get('qualifier', 'N/A'),
            'ev_percent': ev_percent or (alert.ev_percent if alert else alert_data.get('ev_percent', 0)),
            'odds': odds or (alert_data.get('american_odds', 'N/A')),
            'ticker': ticker or alert_data.get('ticker', 'N/A'),
            'side': side or alert_data.get('side', 'N/A'),
            'expected_price': expected_price or alert_data.get('price_cents'),
            'current_price': current_price,
            'price_delta': price_delta,
            'error': error,
            'reason': reason,
            'filter_name': filter_name or (getattr(alert, 'filter_name', None) if alert else alert_data.get('filter_name', 'N/A')),
            'additional_logs': additional_logs or []
        }
        
        failed_auto_bets.append(failure_entry)
        
        # Keep only last MAX_FAILED_BETS
        if len(failed_auto_bets) > MAX_FAILED_BETS:
            failed_auto_bets = failed_auto_bets[-MAX_FAILED_BETS:]
        
        print(f"[FAILED-BETS] ✅ Stored failure: {alert.teams if alert else 'N/A'} - {alert.pick if alert else 'N/A'} | Error: {error} | Total stored: {len(failed_auto_bets)}")
    except Exception as e:
        print(f"[FAILED-BETS] ❌ ERROR storing failed bet: {e}")
        print(f"[FAILED-BETS] Traceback: {traceback.format_exc()}")

def should_log_high_ev_block(alert, alert_data, ev_threshold=5.0):
    """Check if a high-EV alert that should trigger is being blocked - returns True if should log"""
    ev_percent = alert.ev_percent if alert else alert_data.get('ev_percent', 0)
    return ev_percent >= ev_threshold
auto_bet_games = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))  # game_name -> market_type -> pick_direction -> list
auto_bet_lock = None  # Will be initialized as asyncio.Lock() when event loop is available
positions_loaded = False  # CRITICAL: Must be True before auto-betting can start

# Track total bet amount per event (for per-event max bet limit)
auto_bet_event_totals = {}  # Dict mapping event_base -> total_bet_amount (in dollars)

# CRITICAL: Track retry counts and cooldowns to prevent infinite retry loops
auto_bet_submarket_retry_count = {}  # Dict mapping (ticker, side) -> retry count
auto_bet_submarket_last_retry = {}  # Dict mapping (ticker, side) -> last retry timestamp
MAX_RETRIES_PER_SUBMARKET = 3  # Maximum retries per submarket (prevents infinite loops)
RETRY_COOLDOWN_SECONDS = 30  # Cooldown period before allowing retry (prevents rapid retries)

# CRITICAL: Thread-safe lock for cross-thread duplicate prevention
# asyncio.Lock only works within a single event loop - we need threading.Lock for cross-thread safety
import threading
auto_bet_thread_lock = threading.Lock()  # Thread-safe lock for duplicate prevention

# Filter Management System
# Store multiple named filters that can be selected for dashboard and auto-bettor
saved_filters = {}  # Dict of filter_name -> filter_payload
selected_dashboard_filters = []  # List of filter names selected for dashboard
selected_auto_bettor_filters = []  # List of filter names selected for auto-bettor
odds_ev_monitors: Dict[str, EvMonitorImpl] = {}  # filter_name -> OddsEVMonitor
odds_ev_monitor = None  # primary monitor (first selected dashboard filter)

# Default filter: "Kalshi All Sports (3 Sharps Live)" — BookieBeats-style; displayBooks/sharps from .env
# (ODDS_API_BOOKMAKERS / ODDS_API_DEVIG_SHARPS). No Pinnacle; Polymarket + Betfair included.
DEFAULT_FILTER_NAME = "Kalshi All Sports (3 Sharps Live)"
_DEFAULT_SHARPS_ORDER = [
    "Circa", "BookMaker", "Novig", "ProphetX", "SportTrade",
    "FanDuel", "DraftKings", "Polymarket", "Betfair",
]
DEFAULT_FILTER_PAYLOAD = {
    "state": "ND",
    "bettingBooks": ["Kalshi"],
    "displayBooks": [
        "Kalshi", "FanDuel", "Circa", "BookMaker", "DraftKings",
        "Novig", "ProphetX", "SportTrade", "Polymarket", "Betfair",
    ],
    "leagues": [
        "SOCCER_ALL", "TENNIS_ALL", "BASKETBALL_ALL", "FOOTBALL_ALL",
        "HOCKEY_ALL", "BASEBALL_ALL", "UFC_ALL",
    ],
    "betTypes": ["GAMELINES"],
    "minRoi": 0,
    "middleStatus": "INCLUDE",
    "middleFilters": [{"sport": "Any", "minHold": 0, "minMiddle": 0}],
    "sortOrder": "ROI",
    "devigFilter": {
        "sharps": list(_DEFAULT_SHARPS_ORDER),
        "method": "POWER",
        "type": "AVERAGE",
        "minEv": 0,
        "minLimit": 0,
        "minSharpBooks": 3,
        "hold": [{"book": "Any", "max": 8}],
    },
    "oddsRanges": [{"book": "Any", "min": -200, "max": 200}],
    "minLimits": [{"book": "Kalshi", "min": 75}],
    "minSharpLimits": [
        {"book": "BookMaker", "min": 250},
        {"book": "Circa", "min": 250},
        {"book": "Novig", "min": 200},
        {"book": "ProphetX", "min": 200},
        {"book": "SportTrade", "min": 200},
        {"book": "DraftKings", "min": 200},
        {"book": "FanDuel", "min": 200},
        {"book": "Polymarket", "min": 0},
        {"book": "Betfair", "min": 0},
    ],
    "linkType": "DESKTOP_BETSLIP",
}

# Second filter: "CBB EV Filter (Live - Kalshi)" - College basketball, broad for dashboard (0%+).
# Auto-bettor still only bets 10%+ via auto_bet_settings_by_filter[CBB_FILTER_NAME]['ev_min'].
CBB_FILTER_NAME = "CBB EV Filter (Live - Kalshi)"
CBB_FILTER_PAYLOAD = {
    "state": "ND",
    "bettingBooks": ["Kalshi"],
    "displayBooks": [
        "Kalshi", "FanDuel", "Circa", "BookMaker", "DraftKings",
        "Novig", "ProphetX", "SportTrade", "Polymarket", "Betfair",
    ],
    "leagues": ["NCAAB"],
    "excludedCategories": ["1st Quarter", "2nd Quarter", "3rd Quarter", "4th Quarter", "1st Half", "2nd Half"],
    "betTypes": ["GAMELINES"],
    "minRoi": 0,
    "middleStatus": "INCLUDE",
    "middleFilters": [{"sport": "Any", "minHold": 0, "minMiddle": 0}],
    "sortOrder": "ROI",
    "devigFilter": {
        "sharps": ["FanDuel", "DraftKings", "BookMaker", "ProphetX", "Novig", "SportTrade", "Polymarket", "Betfair"],
        "method": "WORST_CASE",
        "type": "AVERAGE",
        "minEv": 0,
        "minLimit": 0,
        "minSharpBooks": 2,
        "hold": [{"book": "Any", "max": 8}],
    },
    "oddsRanges": [{"book": "Any", "min": -200, "max": 200}],
    "minLimits": [{"book": "Any", "min": 25}, {"book": "Kalshi", "min": 75}],
    "minSharpLimits": [
        {"book": "Novig", "min": 1000},
        {"book": "ProphetX", "min": 1000},
        {"book": "SportTrade", "min": 1000},
        {"book": "FanDuel", "min": 200},
        {"book": "DraftKings", "min": 200},
        {"book": "BookMaker", "min": 250},
        {"book": "Polymarket", "min": 0},
        {"book": "Betfair", "min": 0},
    ],
    "linkType": "DESKTOP_BETSLIP",
}

# Initialize with default filter
saved_filters[DEFAULT_FILTER_NAME] = DEFAULT_FILTER_PAYLOAD
saved_filters[CBB_FILTER_NAME] = CBB_FILTER_PAYLOAD
# Odds-API.io: default filter displayBooks = ENV master list (ODDS_API_BOOKMAKERS). Monitors always
# request /odds/multi for that full list; a filter may list fewer displayBooks only for alert-card columns.
DEFAULT_FILTER_PAYLOAD["bettingBooks"] = ["Kalshi"]
display_books_list = odds_api_master_bookmakers()
DEFAULT_FILTER_PAYLOAD["displayBooks"] = display_books_list
CBB_FILTER_PAYLOAD["displayBooks"] = list(display_books_list)

_dnorm = lambda s: (s or "").strip().lower()


_disp_set = {_dnorm(x) for x in display_books_list}

_sharps_csv = (os.getenv("ODDS_API_DEVIG_SHARPS") or "").strip()
if _sharps_csv:
    sharps_list = [
        x.strip()
        for x in _sharps_csv.split(",")
        if x.strip() and _dnorm(x) != "pinnacle"
    ]
else:
    sharps_list = [
        b for b in _DEFAULT_SHARPS_ORDER
        if _dnorm(b) in _disp_set and _dnorm(b) != "kalshi"
    ]
if not sharps_list:
    sharps_list = ["FanDuel", "DraftKings", "Circa"]

DEFAULT_FILTER_PAYLOAD["devigFilter"]["sharps"] = sharps_list
# CBB: same sharp panel as main filter but keep WORST_CASE + minSharpBooks 2 in payload above.
cbb_sharps = [b for b in sharps_list if _dnorm(b) in _disp_set]
if len(cbb_sharps) < 2:
    cbb_sharps = list(sharps_list)
CBB_FILTER_PAYLOAD["devigFilter"]["sharps"] = cbb_sharps

_extra_leagues = [x.strip() for x in os.getenv("ODDS_API_LEAGUES", "").split(",") if x.strip()]
DEFAULT_FILTER_PAYLOAD["leagues"] = list(
    dict.fromkeys(list(DEFAULT_FILTER_PAYLOAD["leagues"]) + _extra_leagues)
)


def _merge_min_sharp_limits(payload: dict, sharp_names: List[str]) -> None:
    """Keep configured floors; add min=0 for any sharp in use that is not listed."""
    have = {_dnorm(r.get("book", "")) for r in (payload.get("minSharpLimits") or [])}
    out = list(payload.get("minSharpLimits") or [])
    for b in sharp_names:
        if _dnorm(b) not in have:
            out.append({"book": b, "min": 0})
            have.add(_dnorm(b))
    payload["minSharpLimits"] = out


_merge_min_sharp_limits(DEFAULT_FILTER_PAYLOAD, sharps_list)
_merge_min_sharp_limits(CBB_FILTER_PAYLOAD, cbb_sharps)
# By default, both filters should be selected for both dashboard and auto-bettor
selected_dashboard_filters = [DEFAULT_FILTER_NAME, CBB_FILTER_NAME]
selected_auto_bettor_filters = []


def _live_odds_display_books() -> List[str]:
    """Odds tab + /api/live_odds: always full ODDS_API_BOOKMAKERS master list (e.g. all 10)."""
    return list(odds_api_master_bookmakers())


def _sport_slug_event(ev: Dict[str, Any]) -> str:
    sp = ev.get("sport")
    if isinstance(sp, dict):
        return str(sp.get("slug") or "").lower()
    return str(sp or "").lower()


def _league_slug_name(ev: Dict[str, Any]) -> Tuple[str, str]:
    """Lower slug and upper name for Odds-API.io ``league`` object."""
    lg = ev.get("league")
    if isinstance(lg, dict):
        return (str(lg.get("slug") or "").lower(), str(lg.get("name") or "").upper())
    return ("", str(lg or "").upper())


def _sport_ui_matches_event(ui_sport: str, ev: Dict[str, Any]) -> bool:
    """Match UI / URL sport to Odds-API ``event.sport.slug`` (hyphen vs legacy forms)."""
    if not ui_sport or ui_sport == "all":
        return True
    return normalize_sport_slug_key(_sport_slug_event(ev)) == normalize_sport_slug_key(ui_sport)


def _event_matches_league_focus(ev: Dict[str, Any], focus: str) -> bool:
    """
    Narrow Odds-API.io events to major leagues (client-side) when /events?league=…
    is not used or returns mixed leagues. Compares league slug/name to MLB/NBA/NHL/NFL.
    """
    f = (focus or "all").strip().lower()
    if f in ("", "all"):
        return True
    slug, name = _league_slug_name(ev)
    sk = normalize_sport_slug_key(_sport_slug_event(ev))
    if f == "mlb":
        return sk == "baseball" and (
            "mlb" in slug or "mlb" in name or "MAJOR LEAGUE" in name
        )
    if f == "nba":
        if sk != "basketball":
            return False
        if "wnba" in slug or "WNBA" in name:
            return False
        if "g-league" in slug or "GLEAGUE" in name.replace(" ", "") or "G LEAGUE" in name:
            return False
        return (
            "nba" in slug
            or name == "NBA"
            or ("NBA" in name and "EURO" not in name)
        )
    if f == "nhl":
        return sk == "icehockey" and (
            "nhl" in slug or "NHL" in name or "NATIONAL HOCKEY" in name
        )
    if f == "nfl":
        if sk != "americanfootball":
            return False
        if "ncaa" in slug or "NCAA" in name or "COLLEGE" in name:
            return False
        return "nfl" in slug or "NFL" in name or "NATIONAL FOOTBALL" in name
    return True


def _event_is_live(ev: Dict[str, Any]) -> bool:
    if ev.get("live") is True or ev.get("isLive") is True:
        return True
    st = str(ev.get("status") or ev.get("state") or "").lower().replace(" ", "")
    return st in ("live", "inprogress", "inplay", "started", "running")


def _live_mkts_for_book(bks: Dict[str, Any], book: str) -> List[Dict[str, Any]]:
    nb = _norm_book(book).lower()
    if not isinstance(bks, dict):
        return []
    for k, v in bks.items():
        if _norm_book(str(k)).lower() == nb:
            return v if isinstance(v, list) else []
    return []


def _live_first_row(market: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not market:
        return {}
    rows = market.get("odds") or []
    if rows and isinstance(rows[0], dict):
        return rows[0]
    return {}


def _live_float_dec(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        d = float(str(x).strip())
        return d if d > 1.0 else None
    except (TypeError, ValueError):
        return None


def _live_pick_ml_name(bks: Dict[str, Any]) -> str:
    """Odds-API.io docs use market name ``ML`` for match winner; fall back to Moneyline synonyms."""
    for pref in ("Kalshi", "FanDuel", "DraftKings"):
        for m in _live_mkts_for_book(bks, pref):
            n = str(m.get("name") or "").strip()
            u = n.upper()
            if "PLAYER" in u:
                continue
            if u == "ML":
                return n
    for pref in ("Kalshi", "FanDuel", "DraftKings"):
        for m in _live_mkts_for_book(bks, pref):
            n = str(m.get("name") or "").strip()
            u = n.upper()
            if "PLAYER" in u:
                continue
            if "MONEY" in u or u in ("MONEYLINE",) or "WINNER" in u:
                return n
    for _bk, mkts in (bks or {}).items():
        if not isinstance(mkts, list):
            continue
        for m in mkts:
            n = str(m.get("name") or "").strip()
            u = n.upper()
            if "PLAYER" in u:
                continue
            if u == "ML" or "MONEY" in u or "WINNER" in u:
                return n
    return "ML"


def _live_find_market(book_odds: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    for m in book_odds or []:
        if _market_names_match(str(m.get("name", "")), name):
            return m
    return None


def _live_best_side(prices: Dict[str, Dict[str, Any]], side_key: str) -> Tuple[Optional[str], Optional[int]]:
    """Pick best book by highest decimal (best price for the bettor)."""
    best_b: Optional[str] = None
    best_d: Optional[float] = None
    for bk, row in prices.items():
        d = row.get(side_key)
        if d is None or d <= 1.0:
            continue
        if best_d is None or d > best_d:
            best_d = d
            best_b = bk
    if best_b is None or best_d is None:
        return None, None
    am = decimal_to_american(best_d)
    return best_b, int(am)


def _default_odds_screen_sports() -> List[str]:
    """Sport slugs passed to Odds-API /events (hyphenated where required by API docs)."""
    raw = os.getenv(
        "ODDS_API_SPORTS",
        "baseball,basketball,ice-hockey,american-football,football,tennis",
    )
    out = [sport_slug_query_for_api(x) for x in raw.split(",") if x.strip()]
    return out[:15] if out else ["baseball", "basketball", "ice-hockey"]


def _parse_event_start(ev: Dict[str, Any]) -> Optional[datetime]:
    for k in ("startsAt", "startTime", "commence_time", "date", "starts_at"):
        v = ev.get(k)
        if v is None or v == "":
            continue
        if isinstance(v, (int, float)):
            try:
                ts = float(v)
                if ts > 1e12:
                    ts /= 1000.0
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except (OSError, ValueError, OverflowError):
                continue
        s = str(v).strip()
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            try:
                dd = datetime.strptime(s[:10], "%Y-%m-%d").date()
                return datetime.combine(dd, datetime.min.time(), tzinfo=timezone.utc)
            except ValueError:
                pass
        if "T" in s or s.endswith("Z") or (len(s) > 6 and s[-6] in "+-"):
            try:
                ss = s.replace("Z", "+00:00")
                dt = datetime.fromisoformat(ss)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
    return None


def _event_sort_ts(ev: Dict[str, Any]) -> float:
    dt = _parse_event_start(ev)
    if dt is not None:
        return dt.timestamp()
    return float("inf")


def _event_sort_tuple(ev: Dict[str, Any]) -> Tuple[int, float]:
    """Live first, then soonest start time (pregame / scheduled)."""
    return (0 if _event_is_live(ev) else 1, _event_sort_ts(ev))


def _event_matches_date_filter(ev: Dict[str, Any], filt: str) -> bool:
    fit = (filt or "all").strip().lower()
    if fit == "all":
        return True
    dt = _parse_event_start(ev)
    if dt is None:
        return True
    local_d = dt.astimezone().date()
    today = datetime.now().astimezone().date()
    if fit == "today":
        return local_d == today
    if fit == "tomorrow":
        return local_d == today + timedelta(days=1)
    return True


def _log_live_odds_book_flow_and_pipeline(
    books: List[str],
    rows_out: List[Dict[str, Any]],
    timing_l: str,
    sport_l: str,
) -> None:
    """Console visibility: how many of the configured books have ML prices on the odds grid."""
    m = len(books)
    if not rows_out:
        print(f"[PIPELINE] live_odds timing={timing_l} sport={sport_l} rows=0 | master_books={m}")
        print(f"[BOOK FLOW] live_odds: no rows | configured={books}")
        return

    def npriced(r: Dict[str, Any]) -> int:
        pr = r.get("books") or {}
        return sum(
            1
            for b in books
            if (pr.get(b) or {}).get("home_am") is not None or (pr.get(b) or {}).get("away_am") is not None
        )

    live_rows = [r for r in rows_out if r.get("live")]
    counts = [npriced(r) for r in rows_out]
    live_counts = [npriced(r) for r in live_rows] if live_rows else []

    def fmt(cs: List[int]) -> str:
        if not cs:
            return "n/a"
        return f"min={min(cs)}/{m} max={max(cs)}/{m} avg={sum(cs) / len(cs):.2f}"

    print(
        f"[PIPELINE] live_odds timing={timing_l} sport={sport_l} rows={len(rows_out)} "
        f"live_rows={len(live_rows)} | master={m} ML_price_hit {fmt(counts)} | live_only {fmt(live_counts)}"
    )
    sample = ", ".join(f"{(r.get('teams') or '')[:22]}={npriced(r)}" for r in rows_out[:5])
    print(f"[BOOK FLOW] live_odds master=[{', '.join(books)}] | sample_priced_books (first 5): {sample}")


async def _live_odds_build_snapshot_with_client(
    client: Any,
    sport: str,
    timing: str,
    books: List[str],
    date_filter: str,
    league_focus: str = "all",
) -> Dict[str, Any]:
    if not getattr(client, "api_key", ""):
        return {
            "ok": False,
            "error": "ODDS_API_KEY missing",
            "updated": time.time(),
            "books": books,
            "events": [],
        }
    if not books:
        return {
            "ok": False,
            "error": "No bookmakers in ODDS_API_BOOKMAKERS (set your subscription list in .env)",
            "updated": time.time(),
            "books": [],
            "events": [],
        }
    sport_l = (sport or "all").strip().lower()
    timing_l = (timing or "live").strip().lower()
    date_f = (date_filter or "all").strip().lower()
    lf = (league_focus or "all").strip().lower()
    events: List[Dict[str, Any]] = []

    # --- gather events (live / pregame / both) ---
    # Docs: GET /events/live?sport={slug} filters server-side (optional).
    if timing_l in ("live", "both"):
        try:
            live_sport_arg = sport_l if sport_l and sport_l != "all" else None
            liv = await client.list_live_events(live_sport_arg)
        except Exception as e:
            liv = []
            err_l = str(e)
        else:
            err_l = ""
        for e in liv or []:
            if not _sport_ui_matches_event(sport_l, e):
                continue
            events.append(e)
        if err_l and not events and timing_l == "live":
            return {
                "ok": False,
                "error": f"list_live_events failed: {err_l}",
                "updated": time.time(),
                "books": books,
                "timing": timing_l,
                "sport": sport_l,
                "league_focus": lf,
                "date_filter": date_f,
                "events": [],
            }

    if timing_l in ("pregame", "both"):
        slugs: List[str] = (
            [sport_l]
            if sport_l and sport_l != "all"
            else _default_odds_screen_sports()
        )
        for slug in slugs:
            try:
                s_api = sport_slug_query_for_api(slug)
                lg_slug = major_league_slug_for_events(s_api, lf) if lf != "all" else None
                try:
                    evs = await client.list_events_for_sport(
                        slug,
                        league=lg_slug,
                        status="pending",
                    )
                except Exception:
                    # Some accounts use different league slugs; retry without league filter.
                    if lg_slug:
                        evs = await client.list_events_for_sport(slug, status="pending")
                    else:
                        raise
            except Exception:
                continue
            for e in evs or []:
                if _event_is_live(e):
                    continue
                if not _event_matches_date_filter(e, date_f):
                    continue
                if not _sport_ui_matches_event(sport_l if sport_l != "all" else slug, e):
                    continue
                events.append(e)

    if lf != "all":
        events = [e for e in events if _event_matches_league_focus(e, lf)]

    by_id: Dict[int, Dict[str, Any]] = {}
    for e in events:
        try:
            eid = int(e.get("id"))
        except (TypeError, ValueError):
            continue
        if eid not in by_id:
            by_id[eid] = e
    ev_sorted = sorted(by_id.values(), key=_event_sort_tuple)
    # Live-only: pull a wider pool before league/sport filters so major-league games are not pushed
    # out by unrelated live fixtures (Odds-API returns one global live list).
    max_ev = 50 if timing_l in ("pregame", "both") else 80
    ev_list = ev_sorted[:max_ev]
    ids = [int(e["id"]) for e in ev_list if e.get("id") is not None]
    odds_by_id: Dict[int, Dict[str, Any]] = {}
    if ids:
        try:
            for doc in await client.get_odds_multi(ids, books):
                if isinstance(doc, dict) and doc.get("id") is not None:
                    odds_by_id[int(doc["id"])] = doc
        except Exception as e:
            return {
                "ok": False,
                "error": f"get_odds_multi failed: {e}",
                "updated": time.time(),
                "books": books,
                "timing": timing_l,
                "sport": sport_l,
                "league_focus": lf,
                "date_filter": date_f,
                "events": [],
            }
    # Odds-API.io omits bookmaker keys when that book has no markets for the event — not a UI bug.
    books_with_lines: List[str] = []
    _seen_bl: set = set()
    for _doc in odds_by_id.values():
        for raw_key in (_doc.get("bookmakers") or {}):
            canon = _norm_book(str(raw_key))
            lk = canon.lower()
            if lk not in _seen_bl:
                _seen_bl.add(lk)
                books_with_lines.append(canon)
    books_with_lines.sort(key=lambda s: s.lower())
    rows_out: List[Dict[str, Any]] = []
    max_rows = 45 if timing_l in ("pregame", "both") else 40
    for e in ev_list[:max_rows]:
        eid = int(e["id"])
        doc = odds_by_id.get(eid) or {}
        bks = doc.get("bookmakers") or {}
        home = str(e.get("home") or "")
        away = str(e.get("away") or "")
        ml_name = _live_pick_ml_name(bks)
        prices: Dict[str, Dict[str, Any]] = {}
        for bk in books:
            mk = _live_find_market(_live_mkts_for_book(bks, bk), ml_name)
            row = _live_first_row(mk)
            dh = _live_float_dec(row.get("home"))
            da = _live_float_dec(row.get("away"))
            prices[bk] = {
                "home_dec": dh,
                "away_dec": da,
                "home_am": int(decimal_to_american(dh)) if dh else None,
                "away_am": int(decimal_to_american(da)) if da else None,
            }
        bh, ham = _live_best_side(prices, "home_dec")
        ba, aam = _live_best_side(prices, "away_dec")
        league = e.get("league")
        if isinstance(league, dict):
            league_s = str(league.get("name") or league.get("slug") or "")
        else:
            league_s = str(league or "")
        start_s = ""
        dt = _parse_event_start(e)
        if dt is not None:
            start_s = dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")
        rows_out.append(
            {
                "event_id": eid,
                "teams": f"{away} @ {home}" if away and home else str(e.get("name") or ""),
                "league": league_s,
                "sport_slug": _sport_slug_event(e),
                "live": _event_is_live(e),
                "status": str(e.get("status") or e.get("state") or ""),
                "start_display": start_s,
                "market": ml_name,
                "books": prices,
                "best": {
                    "home_book": bh,
                    "home_am": ham,
                    "away_book": ba,
                    "away_am": aam,
                },
            }
        )
    _log_live_odds_book_flow_and_pipeline(books, rows_out, timing_l, sport_l)
    return {
        "ok": True,
        "updated": time.time(),
        "books": books,
        "timing": timing_l,
        "sport": sport_l,
        "sport_api": sport_slug_query_for_api(sport_l) if sport_l != "all" else None,
        "league_focus": lf,
        "league_api": major_league_slug_for_events(
            sport_slug_query_for_api(sport_l), lf
        )
        if sport_l != "all" and lf != "all"
        else None,
        "date_filter": date_f,
        "events": rows_out,
        "books_with_lines": books_with_lines,
    }


async def _live_odds_build_snapshot(
    sport: str, timing: str, books: List[str], date_filter: str, league_focus: str = "all"
) -> Dict[str, Any]:
    """Uses shared Odds-API client (must run on the same asyncio loop that owns the client)."""
    client = await get_shared_odds_client()
    return await _live_odds_build_snapshot_with_client(
        client, sport, timing, books, date_filter, league_focus
    )


async def _live_odds_build_snapshot_isolated(
    sport: str, timing: str, books: List[str], date_filter: str, league_focus: str = "all"
) -> Dict[str, Any]:
    from odds_api_client import OddsAPIClient

    c = OddsAPIClient()
    try:
        return await _live_odds_build_snapshot_with_client(
            c, sport, timing, books, date_filter, league_focus
        )
    finally:
        await c.close()


# Initialize per-filter auto-bet settings with defaults
auto_bet_settings_by_filter[DEFAULT_FILTER_NAME] = {
    'ev_min': 5.0,
    'ev_max': 25.0,
    'odds_min': -200,
    'odds_max': 200,
    'amount': 101.0,
    'enabled': False,
}
auto_bet_settings_by_filter[CBB_FILTER_NAME] = {
    'ev_min': 10.0,
    'ev_max': 25.0,
    'odds_min': -200,
    'odds_max': 200,
    'amount': 101.0,
    'enabled': False,
}

# Auto-bet tracking for Google Sheets export and win/loss analysis
AUTO_BET_CSV_FILE = os.path.join(os.path.dirname(__file__), "auto_bets.csv")  # Backup CSV
AUTO_BET_ALERTS_LOG_FILE = os.path.join(os.path.dirname(__file__), "auto_bet_alerts_log.jsonl")  # Log all alerts that pass thresholds
GOOGLE_SHEETS_CREDENTIALS_FILE = os.getenv('GOOGLE_SHEETS_CREDENTIALS_FILE', 'credentials.json')
GOOGLE_SHEETS_SPREADSHEET_ID = os.getenv('GOOGLE_SHEETS_SPREADSHEET_ID', '')
GOOGLE_SHEETS_WORKSHEET_NAME = os.getenv('GOOGLE_SHEETS_WORKSHEET_NAME', 'Auto-Bets')
auto_bet_records = []  # In-memory tracking of all auto-bets
google_sheets_client = None  # Google Sheets client

# Telegram bot for alerts and remote control
telegram_bot = None
telegram_chat_id = None
telegram_bot_token = None


def init_google_sheets():
    """Initialize Google Sheets client"""
    global google_sheets_client
    
    if not GOOGLE_SHEETS_SPREADSHEET_ID:
        print("[GOOGLE SHEETS] WARNING: GOOGLE_SHEETS_SPREADSHEET_ID not set, using CSV fallback")
        return None
    
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        
        # Check if credentials file exists
        if not os.path.exists(GOOGLE_SHEETS_CREDENTIALS_FILE):
            print(f"[GOOGLE SHEETS] WARNING: Credentials file not found: {GOOGLE_SHEETS_CREDENTIALS_FILE}")
            print("[GOOGLE SHEETS] Using CSV fallback")
            return None
        
        # Authenticate
        scope = ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_file(GOOGLE_SHEETS_CREDENTIALS_FILE, scopes=scope)
        google_sheets_client = gspread.authorize(creds)
        
        # Open spreadsheet
        spreadsheet = google_sheets_client.open_by_key(GOOGLE_SHEETS_SPREADSHEET_ID)
        
        # Get or create worksheet
        try:
            worksheet = spreadsheet.worksheet(GOOGLE_SHEETS_WORKSHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=GOOGLE_SHEETS_WORKSHEET_NAME, rows=1000, cols=20)
            # Write header
            headers = [
                'Timestamp', 'Order ID', 'Ticker', 'Side', 'Teams', 'Market Type', 'Pick', 'Qualifier',
                'EV %', 'Expected Price (¢)', 'Executed Price (¢)', 'American Odds',
                'Contracts', 'Cost ($)', 'Payout ($)', 'Win Amount ($)', 'Sport', 'Status', 'Result', 'PNL ($)', 'Settled', 'Filter Name', 'Devig Books'
            ]
            worksheet.append_row(headers)
        
        print(f"[GOOGLE SHEETS] OK: Connected to spreadsheet: {spreadsheet.title}")
        return worksheet
    
    except Exception as e:
        print(f"[GOOGLE SHEETS] ERROR: Error initializing: {e}")
        import traceback
        print(f"[GOOGLE SHEETS] Full error details:")
        traceback.print_exc()
        print("[GOOGLE SHEETS] Using CSV fallback")
        return None


def write_auto_bet_to_sheets(bet_data: Dict):
    """Write auto-bet record to Google Sheets (with CSV fallback)"""
    global google_sheets_client
    
    # Try Google Sheets first
    if google_sheets_client and GOOGLE_SHEETS_SPREADSHEET_ID:
        try:
            import gspread
            spreadsheet = google_sheets_client.open_by_key(GOOGLE_SHEETS_SPREADSHEET_ID)
            worksheet = spreadsheet.worksheet(GOOGLE_SHEETS_WORKSHEET_NAME)
            
            # Convert bet_data dict to row (matching header order)
            # Removed 'Kalshi Odds' (duplicate of 'American Odds'), moved 'Devig Books' to end
            row = [
                bet_data.get('timestamp', ''),
                bet_data.get('order_id', ''),
                bet_data.get('ticker', ''),
                bet_data.get('side', ''),
                bet_data.get('teams', ''),
                bet_data.get('market_type', ''),
                bet_data.get('pick', ''),
                bet_data.get('qualifier', ''),
                bet_data.get('ev_percent', ''),
                bet_data.get('expected_price_cents', ''),
                bet_data.get('executed_price_cents', ''),
                bet_data.get('american_odds', ''),
                bet_data.get('contracts', ''),
                bet_data.get('cost', ''),
                bet_data.get('payout', ''),
                bet_data.get('win_amount', ''),
                bet_data.get('sport', ''),
                bet_data.get('status', ''),
                bet_data.get('result', ''),
                bet_data.get('pnl', ''),
                bet_data.get('settled', ''),
                bet_data.get('filter_name', ''),  # Filter name
                bet_data.get('devig_books', '')  # Devig books with odds (moved to end)
            ]
            
            worksheet.append_row(row)
            print(f"[GOOGLE SHEETS] OK: Wrote bet to sheet: {bet_data.get('ticker')} - {bet_data.get('pick')}")
            return
        
        except Exception as e:
            print(f"[GOOGLE SHEETS] ❌ Error writing to sheet: {e}, falling back to CSV")
    
    # Fallback to CSV
    write_auto_bet_to_csv(bet_data)


def write_auto_bet_to_csv(bet_data: Dict):
    """Write auto-bet record to CSV file (appends if file exists)"""
    file_exists = os.path.exists(AUTO_BET_CSV_FILE)
    
    # CSV columns (matching Google Sheets structure: removed 'kalshi_odds', moved 'devig_books' to end)
    fieldnames = [
        'timestamp', 'order_id', 'ticker', 'side', 'teams', 'market_type', 'pick', 'qualifier',
        'ev_percent', 'expected_price_cents', 'executed_price_cents', 'american_odds',
        'contracts', 'cost', 'payout', 'win_amount', 'sport', 'status', 'result', 'pnl', 'settled', 'filter_name', 'devig_books'
    ]
    
    try:
        with open(AUTO_BET_CSV_FILE, 'a', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            
            # Write header if file is new
            if not file_exists:
                writer.writeheader()
            
            # Write bet record
            writer.writerow(bet_data)
            print(f"[AUTO-BET CSV] OK: Wrote bet to {AUTO_BET_CSV_FILE}: {bet_data.get('ticker')} - {bet_data.get('pick')}")
    except Exception as e:
        print(f"[AUTO-BET CSV] ❌ Error writing to CSV: {e}")


def log_alert_passed_threshold(alert_id, alert, alert_data, filter_settings, decision_path):
    """
    Log all alerts that pass auto-bet thresholds to a JSONL file for analysis.
    This includes alerts that pass EV/odds checks, even if they don't end up placing a bet.
    
    Args:
        alert_id: Unique alert identifier
        alert: EvAlert object
        alert_data: Alert data dict with matching info
        filter_settings: Dict with filter-specific settings used
        decision_path: Dict with decision-making info (checks passed, skipped reasons, etc.)
    """
    try:
        log_entry = {
            'timestamp': datetime.now().isoformat(),
            'alert_id': str(alert_id),
            'alert': {
                'teams': alert.teams if alert else alert_data.get('teams', 'N/A'),
                'pick': alert.pick if alert else alert_data.get('pick', 'N/A'),
                'qualifier': alert.qualifier if alert else alert_data.get('qualifier', ''),
                'market_type': alert.market_type if alert else alert_data.get('market_type', 'N/A'),
                'ev_percent': alert.ev_percent if alert else alert_data.get('ev_percent', 0),
                'odds': alert.odds if alert else alert_data.get('odds', 'N/A'),
                'liquidity': getattr(alert, 'liquidity', None) or alert_data.get('liquidity', 0),
                'expected_profit': getattr(alert, 'expected_profit', None) or alert_data.get('expected_profit', 0),
                'market_url': alert.market_url if alert else alert_data.get('market_url', ''),
            },
            'matching': {
                'ticker': alert_data.get('ticker'),
                'event_ticker': alert_data.get('event_ticker'),
                'side': alert_data.get('side'),
                'match_confidence': alert_data.get('match_confidence', 0),
                'match_failed': alert_data.get('match_failed', False),
            },
            'filter_settings': filter_settings,
            'decision_path': decision_path,
            'price_info': {
                'price_cents': alert_data.get('price_cents'),
                'american_odds': alert_data.get('american_odds'),
                'book_price': alert_data.get('book_price'),
            },
            'market_data': {
                'yes_subtitle': alert_data.get('market_data', {}).get('yes_sub_title', 'N/A') if alert_data.get('market_data') else 'N/A',
                'no_subtitle': alert_data.get('market_data', {}).get('no_sub_title', 'N/A') if alert_data.get('market_data') else 'N/A',
                'market_title': alert_data.get('market_data', {}).get('title', 'N/A') if alert_data.get('market_data') else 'N/A',
            },
            'filter_name': getattr(alert, 'filter_name', None) or alert_data.get('filter_name', ''),
            'sharp_books': alert_data.get('sharp_books', []),
            'devig_books': alert_data.get('devig_books', []),
        }
        
        # Write as JSONL (one JSON object per line, append mode)
        with open(AUTO_BET_ALERTS_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
        
        print(f"[ALERT LOG] ✅ Logged alert {alert_id} to {AUTO_BET_ALERTS_LOG_FILE}")
    except Exception as e:
        print(f"[ALERT LOG] ❌ Error logging alert {alert_id}: {e}")
        import traceback
        traceback.print_exc()


def get_auto_bet_stats():
    """Calculate auto-bet statistics from Google Sheets or CSV"""
    global google_sheets_client, auto_bet_records
    
    stats = {
        'total_bets': 0,
        'total_cost': 0.0,
        'total_pnl': 0.0,
        'wins': 0,
        'losses': 0,
        'pending': 0,
        'win_rate': 0.0,
        'avg_ev': 0.0,
        'roi': 0.0
    }
    
    try:
        # Try to read from Google Sheets first
        if google_sheets_client and GOOGLE_SHEETS_SPREADSHEET_ID:
            import gspread
            spreadsheet = google_sheets_client.open_by_key(GOOGLE_SHEETS_SPREADSHEET_ID)
            worksheet = spreadsheet.worksheet(GOOGLE_SHEETS_WORKSHEET_NAME)
            
            # Get all rows (skip header)
            rows = worksheet.get_all_values()[1:]  # Skip header row
            
            for row in rows:
                if len(row) < 22:  # Skip incomplete rows (was 21, now 22 after removing Kalshi Odds and moving Devig Books)
                    continue
                
                try:
                    cost = float(row[13] or 0)  # Cost column (was 14, now 13 after removing Kalshi Odds)
                    pnl_str = row[19] or '0.00'  # PNL column (was 21, now 19 after removing Kalshi Odds and moving Devig Books)
                    pnl = float(pnl_str.replace('$', '').replace(',', '') or 0)
                    result = row[18] or 'OPEN'  # Result column (was 20, now 18 after removing Kalshi Odds and moving Devig Books)
                    ev = float(row[8] or 0)  # EV column
                    
                    stats['total_bets'] += 1
                    stats['total_cost'] += cost
                    stats['total_pnl'] += pnl
                    stats['avg_ev'] += ev
                    
                    if result == 'WIN':
                        stats['wins'] += 1
                    elif result == 'LOSS':
                        stats['losses'] += 1
                    else:
                        stats['pending'] += 1
                
                except (ValueError, IndexError) as e:
                    continue  # Skip invalid rows
        
        # Fallback to CSV
        elif os.path.exists(AUTO_BET_CSV_FILE):
            with open(AUTO_BET_CSV_FILE, 'r', encoding='utf-8') as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    try:
                        cost = float(row.get('cost', 0) or 0)
                        pnl_str = row.get('pnl', '0.00') or '0.00'
                        pnl = float(pnl_str.replace('$', '').replace(',', '') or 0)
                        result = row.get('result', 'OPEN') or 'OPEN'
                        ev = float(row.get('ev_percent', 0) or 0)
                        
                        stats['total_bets'] += 1
                        stats['total_cost'] += cost
                        stats['total_pnl'] += pnl
                        stats['avg_ev'] += ev
                        
                        if result == 'WIN':
                            stats['wins'] += 1
                        elif result == 'LOSS':
                            stats['losses'] += 1
                        else:
                            stats['pending'] += 1
                    
                    except (ValueError, KeyError):
                        continue
        
        # Calculate derived stats
        if stats['total_bets'] > 0:
            stats['avg_ev'] = stats['avg_ev'] / stats['total_bets']
            settled = stats['wins'] + stats['losses']
            if settled > 0:
                stats['win_rate'] = (stats['wins'] / settled) * 100
            if stats['total_cost'] > 0:
                stats['roi'] = (stats['total_pnl'] / stats['total_cost']) * 100
    
    except Exception as e:
        print(f"[STATS] ❌ Error calculating stats: {e}")
        import traceback
        traceback.print_exc()
    
    return stats


def determine_sport_from_ticker(ticker: str) -> str:
    """Extract sport/league from ticker (e.g., KXNHLGAME -> NHL)"""
    if not ticker:
        return "Unknown"
    ticker_upper = ticker.upper()
    if ticker_upper.startswith('KXNHL'):
        return "NHL"
    elif ticker_upper.startswith('KXNBA'):
        return "NBA"
    elif ticker_upper.startswith('KXNFL'):
        return "NFL"
    elif ticker_upper.startswith('KXNCAAM'):
        return "NCAA Men's Basketball"
    elif ticker_upper.startswith('KXNCAAB'):
        return "NCAA Men's Basketball"
    elif ticker_upper.startswith('KXUCL'):
        return "UCL"
    elif ticker_upper.startswith('KXEPL'):
        return "EPL"
    elif ticker_upper.startswith('KXMLB'):
        return "MLB"
    else:
        return "Other"


def send_telegram_message(message: str, reply_markup=None):
    """Send a message via Telegram bot with optional inline keyboard"""
    global telegram_bot, telegram_chat_id, telegram_bot_token
    
    if not telegram_bot_token or not telegram_chat_id:
        return  # Telegram not configured
    
    try:
        import requests
        url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
        payload = {
            'chat_id': telegram_chat_id,
            'text': message,
            'parse_mode': 'HTML'
        }
        if reply_markup:
            payload['reply_markup'] = reply_markup
        
        response = requests.post(url, json=payload, timeout=5)
        if response.status_code == 200:
            print(f"[TELEGRAM] OK: Sent message")
        else:
            print(f"[TELEGRAM] ERROR: Failed to send: {response.status_code}")
    except Exception as e:
        print(f"[TELEGRAM] ERROR: Error sending message: {e}")


def send_auto_bet_alert(bet_data: Dict):
    """Send Telegram alert for successful auto-bet"""
    teams = bet_data.get('teams', 'N/A')
    pick = bet_data.get('pick', 'N/A')
    qualifier = bet_data.get('qualifier', '')
    ev_percent = bet_data.get('ev_percent', '0.00')
    american_odds = bet_data.get('american_odds', 'N/A')
    # Cost and contracts can be strings from bet_record, convert to float/int
    cost_str = bet_data.get('cost', '0.00')
    try:
        cost = float(cost_str) if isinstance(cost_str, str) else float(cost_str)
    except (ValueError, TypeError):
        cost = 0.00
    
    contracts_str = bet_data.get('contracts', '0')
    try:
        contracts = int(float(contracts_str)) if isinstance(contracts_str, str) else int(contracts_str)
    except (ValueError, TypeError):
        contracts = 0
    sport = bet_data.get('sport', 'Unknown')
    
    # Get fee information
    fee_type = bet_data.get('fee_type', 'taker')  # 'maker' or 'taker'
    taker_fees_cents = bet_data.get('taker_fees_cents', 0)
    try:
        taker_fees_cents = int(float(taker_fees_cents)) if isinstance(taker_fees_cents, str) else int(taker_fees_cents)
    except (ValueError, TypeError):
        taker_fees_cents = 0
    taker_fees_dollars = taker_fees_cents / 100.0
    
    # Format pick with qualifier
    pick_display = f"{pick} {qualifier}".strip() if qualifier else pick
    
    # Get current balance (async, but don't block - use cached or default)
    # Use api_loop so we NEVER touch monitor_loop (monitoring/auto-bet stay uninterrupted)
    balance = "Loading..."
    try:
        global api_loop, api_kalshi_client, monitor_loop, kalshi_client
        client = api_kalshi_client or kalshi_client
        loop = api_loop if (api_loop and not api_loop.is_closed()) else (monitor_loop if (monitor_loop and not monitor_loop.is_closed()) else None)
        if client and loop:
            try:
                portfolio_future = asyncio.run_coroutine_threadsafe(client.get_portfolio(), loop)
                portfolio = portfolio_future.result(timeout=1)  # 1s timeout
                if portfolio:
                    balance_cents = portfolio.get('balance', 0) or portfolio.get('balance_cents', 0) or 0
                    balance_dollars = balance_cents / 100.0 if balance_cents else 0
                    balance = f"${balance_dollars:.2f}"
            except Exception:
                balance = "N/A"
        else:
            balance = "N/A"
    except Exception as e:
        balance = "N/A"
    
    # Format fee information for Telegram
    if fee_type == 'maker':
        fee_info = "✅ FEE-FREE (Maker)"
    else:
        fee_info = f"💰 Fees: ${taker_fees_dollars:.2f} (Taker)"
    
    # Get filter name for display
    filter_name = bet_data.get('filter_name', '')
    filter_display = f"\n🔍 Filter: {filter_name}" if filter_name else ""
    
    message = f"""🎲 <b>AUTO-BET PLACED</b>

📊 <b>{teams}</b>
🎯 Pick: {pick_display}
📈 EV: {ev_percent}%
💰 Amount: ${cost}
📦 Contracts: {contracts}
⚡ Odds: {american_odds}
{fee_info}
🏆 Sport: {sport}{filter_display}
💵 Balance: {balance}"""
    
    # Add quick action buttons
    keyboard = {
        'inline_keyboard': [[
            {'text': '⛔ Stop Auto-Bet', 'callback_data': 'stop_auto_bet'},
            {'text': '📊 Status', 'callback_data': 'status'}
        ], [
            {'text': '💰 Change Bet Amount', 'callback_data': 'change_bet_amount'}
        ]]
    }
    
    send_telegram_message(message, reply_markup=keyboard)


# Track last state change message to prevent duplicates
_last_state_change_message = None

def send_auto_bet_state_change(enabled: bool):
    """Send Telegram alert when auto-bet state changes (only if state actually changed)"""
    global _last_state_change_message
    
    # Prevent duplicate messages
    if _last_state_change_message == enabled:
        return  # State hasn't changed, don't send duplicate
    
    _last_state_change_message = enabled
    
    if enabled:
        message = "✅ <b>AUTO-BET ENABLED</b>\n\nBot is now active and will place bets matching criteria."
        keyboard = {
            'inline_keyboard': [[
                {'text': '⛔ Stop Auto-Bet', 'callback_data': 'stop_auto_bet'},
                {'text': '📊 Status', 'callback_data': 'status'}
            ]]
        }
    else:
        message = "⛔ <b>AUTO-BET DISABLED</b>\n\nBot is now inactive. No new bets will be placed."
        keyboard = {
            'inline_keyboard': [[
                {'text': '✅ Start Auto-Bet', 'callback_data': 'start_auto_bet'},
                {'text': '📊 Status', 'callback_data': 'status'}
            ]]
        }
    
    send_telegram_message(message, reply_markup=keyboard)


async def check_and_update_bet_results():
    """Check positions and update CSV with win/loss results"""
    global kalshi_client
    
    if not kalshi_client:
        return
    
    try:
        # Get current positions from Kalshi
        positions = await kalshi_client.get_positions()
        if not positions:
            return
        
        # Read existing CSV to update records
        if not os.path.exists(AUTO_BET_CSV_FILE):
            return
        
        # Read all records
        updated_records = []
        with open(AUTO_BET_CSV_FILE, 'r', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                ticker = row.get('ticker', '').upper()
                side = row.get('side', '').lower()
                contracts = int(float(row.get('contracts', 0) or 0))
                settled = row.get('settled', '').lower() == 'true'
                
                # Skip if already settled
                if settled:
                    updated_records.append(row)
                    continue
                
                # Find matching position
                matching_position = None
                for pos in positions:
                    pos_ticker = pos.get('ticker', '').upper()
                    pos_position = pos.get('position', 0)  # Positive = YES, Negative = NO
                    
                    if pos_ticker == ticker:
                        # Check if side matches
                        if (side == 'yes' and pos_position > 0) or (side == 'no' and pos_position < 0):
                            matching_position = pos
                            break
                
                # Determine result
                if matching_position:
                    position_count = abs(matching_position.get('position', 0))
                    market_exposure = matching_position.get('market_exposure', 0)  # Current value in dollars
                    realized_pnl = matching_position.get('realized_pnl', 0)  # Realized P&L in dollars
                    
                    # If position is closed (position = 0), check realized_pnl
                    if position_count == 0:
                        if realized_pnl > 0:
                            row['result'] = 'WIN'
                            row['pnl'] = f"{realized_pnl:.2f}"
                            row['settled'] = 'True'
                        elif realized_pnl < 0:
                            row['result'] = 'LOSS'
                            row['pnl'] = f"{realized_pnl:.2f}"
                            row['settled'] = 'True'
                        else:
                            # Position closed but no P&L yet (might be settling)
                            row['result'] = 'PENDING'
                            row['pnl'] = '0.00'
                            row['settled'] = 'False'
                    else:
                        # Position still open - check current value
                        # If market_exposure ≈ contracts * $1, it won (market settled)
                        # If market_exposure ≈ $0, it lost (market settled)
                        expected_win_value = position_count * 1.0
                        if abs(market_exposure - expected_win_value) < 0.10:  # Within 10 cents
                            row['result'] = 'WIN'
                            row['pnl'] = f"{expected_win_value - float(row.get('cost', 0)):.2f}"
                            row['settled'] = 'True'
                        elif market_exposure < 0.10:  # Essentially $0
                            row['result'] = 'LOSS'
                            row['pnl'] = f"-{float(row.get('cost', 0)):.2f}"
                            row['settled'] = 'True'
                        else:
                            # Market not settled yet
                            row['result'] = 'OPEN'
                            row['pnl'] = f"{market_exposure - float(row.get('cost', 0)):.2f}"
                            row['settled'] = 'False'
                else:
                    # No matching position found - might be settled and closed
                    # Check if we had contracts but position is gone (likely settled)
                    if contracts > 0:
                        # Position closed - assume it's being settled, mark as pending
                        row['result'] = 'PENDING'
                        row['settled'] = 'False'
                
                updated_records.append(row)
        
        # Write updated records back to CSV
        if updated_records:
            fieldnames = updated_records[0].keys()
            with open(AUTO_BET_CSV_FILE, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(updated_records)
            
            # Silently update CSV (no log spam) - positions are still tracked for reverse middle detection
    
    except Exception as e:
        print(f"[AUTO-BET CSV] ❌ Error checking bet results: {e}")
        import traceback
        traceback.print_exc()


def get_or_create_event_loop():
    """Get the monitor's event loop, or create a new one if not available"""
    global monitor_loop
    if monitor_loop and not monitor_loop.is_closed():
        return monitor_loop
    # Fallback: try to get current thread's loop
    try:
        loop = asyncio.get_running_loop()
        return loop
    except RuntimeError:
        # No running loop, create a new one
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


def create_alert_id(alert: EvAlert) -> str:
    """Create unique ID for an alert (stable, doesn't change with EV or odds)"""
    # Use stable hash without EV or odds - same alert with different EV/odds should have same ID
    # CRITICAL: Use hashlib for stable hash (Python's hash() is not stable across sessions)
    # CRITICAL: Don't include odds in hash - odds can change slightly but it's still the same alert
    # We want to update the existing alert, not create a new one when odds change
    import hashlib
    # Use ticker, pick, qualifier, and market_type to create stable ID
    # Also include filter_name if available to distinguish same alert from different filters
    filter_name = getattr(alert, 'filter_name', '') or ''
    ev_source = getattr(alert, "ev_source", "") or "odds_api_value_bets"
    # Same edge from API feed vs local scan must not collide; keep legacy IDs for default feed.
    src_part = f"|{ev_source}" if ev_source != "odds_api_value_bets" else ""
    key = f"{alert.ticker}|{alert.pick}|{alert.qualifier}|{alert.market_type}|{filter_name}{src_part}"
    # Use MD5 hash and take first 10 digits for consistent ID
    hash_obj = hashlib.md5(key.encode('utf-8'))
    hash_hex = hash_obj.hexdigest()
    # Convert hex to int and take modulo to get consistent numeric ID
    hash_int = int(hash_hex[:8], 16)  # Use first 8 hex chars (32 bits)
    return str(hash_int % (10 ** 10))  # Return as string for consistency


async def handle_new_alert(alert: EvAlert):
    """Handle a new alert from the Odds-API.io monitor — optimized for speed (async)."""
    global active_alerts, dashboard_min_ev, selected_dashboard_filters, selected_auto_bettor_filters, auto_bet_settings_by_filter, auto_bet_ev_min
    filter_name = getattr(alert, 'filter_name', '') or ''
    print(f"[HANDLE ALERT] 📥 Received alert | filter={filter_name} | {alert.teams} - {alert.pick} ({alert.ev_percent:.2f}% EV)")
    
    # CRITICAL: Check if this is a high-EV alert that should trigger auto-bet
    # Do this FIRST before any filtering, so we can track ALL high-EV alerts
    alert_filter_name = getattr(alert, 'filter_name', None)
    if alert_filter_name:
        # Get filter-specific EV threshold
        filter_ev_min = auto_bet_settings_by_filter.get(alert_filter_name, {}).get('ev_min', auto_bet_ev_min) if alert_filter_name else auto_bet_ev_min
    else:
        filter_ev_min = auto_bet_ev_min
    
    # Track high-EV alerts that should trigger (will be logged later if they don't)
    high_ev_should_trigger = False
    if alert.ev_percent >= filter_ev_min and auto_bet_enabled:
        if alert_filter_name and alert_filter_name in selected_auto_bettor_filters:
            high_ev_should_trigger = True
            print(f"[HIGH-EV TRACKING] 🎯 High-EV alert detected: {alert.ev_percent:.2f}% EV (>= {filter_ev_min}% threshold) from filter '{alert_filter_name}' - should trigger auto-bet")
    
    try:
        # CRITICAL: Extract EVENT ticker from link (not submarket ticker!)
        event_ticker = alert.ticker or alert.extract_ticker_from_url()
        
        # DIAGNOSTIC: Log sport type for debugging
        if event_ticker:
            ticker_upper = event_ticker.upper()
            if ticker_upper.startswith('KXNCAAMB'):
                print(f"[HANDLE ALERT] 🏀 NCAAB ALERT DETECTED: {event_ticker} - {alert.teams} - {alert.pick}")
            elif ticker_upper.startswith('KXNBAGAME') or ticker_upper.startswith('KXNBASPREAD') or ticker_upper.startswith('KXNBATOTAL'):
                print(f"[HANDLE ALERT] 🏀 NBA ALERT DETECTED: {event_ticker} - {alert.teams} - {alert.pick}")
            elif ticker_upper.startswith('KXNHL'):
                print(f"[HANDLE ALERT] 🏒 NHL ALERT DETECTED: {event_ticker} - {alert.teams} - {alert.pick}")
            elif ticker_upper.startswith('KXUCL'):
                print(f"[HANDLE ALERT] ⚽ UCL ALERT DETECTED: {event_ticker} - {alert.teams} - {alert.pick}")
            elif ticker_upper.startswith('KXEPL'):
                print(f"[HANDLE ALERT] ⚽ EPL ALERT DETECTED: {event_ticker} - {alert.teams} - {alert.pick}")
        
        if not event_ticker:
            filter_name = getattr(alert, 'filter_name', '') or ''
            if 'CBB' in filter_name or 'NCAAB' in filter_name:
                url = getattr(alert, 'market_url', '') or ''
                print(f"[CBB] ⚠️ Alert skipped: no Kalshi ticker in link (filter={filter_name}) | teams={alert.teams} | pick={alert.pick} | link={url[:100] if len(url) > 100 else url}")
            else:
                print(f"Warning: No ticker found in alert: {alert.market_url}")
            return
        
        # CRITICAL: Find the EXACT submarket within the event
        # The link gives us the EVENT ticker, but we need the SUBMARKET ticker
        # Use line from alert (stored from API), or parse from qualifier as fallback
        line = getattr(alert, 'line', None)
        if line is None and alert.qualifier:
            try:
                # Parse line from qualifier (e.g., "40.5", "+11.5", "-11.5")
                # CRITICAL: For spreads, preserve the sign! For totals, remove + but keep -
                qualifier_clean = alert.qualifier.replace('*', '').strip()
                if 'spread' in alert.market_type.lower() or 'puck line' in alert.market_type.lower():
                    # For spreads, preserve the sign (needed for underdog detection)
                    line = float(qualifier_clean)
                else:
                    # For totals, remove + sign (just need the number)
                    line_str = qualifier_clean.replace('+', '').strip()
                    line = float(line_str)
                print(f"[HANDLE ALERT] Parsed line from qualifier '{alert.qualifier}': {line}")
            except:
                pass
        
        # CRITICAL: Log the line value being used for matching
        if line is not None:
            print(f"[HANDLE ALERT] Using line value: {line} for market_type='{alert.market_type}', pick='{alert.pick}', qualifier='{alert.qualifier}'")
        else:
            print(f"[HANDLE ALERT] WARNING: No line value available! qualifier='{alert.qualifier}', alert.line={getattr(alert, 'line', None)}")
        
        # Find the exact submarket
        submarket = await kalshi_client.find_submarket(
            event_ticker=event_ticker,
            market_type=alert.market_type,
            line=line,
            selection=alert.pick,
            teams_str=alert.teams  # Pass teams string for opponent verification
        )
        
        match_result = None
        
        if submarket:
            # Found exact submarket!
            submarket_ticker = submarket.get('ticker', '').upper()
            # CRITICAL: If ticker is missing, try alternative field names
            if not submarket_ticker:
                submarket_ticker = submarket.get('market_ticker', '').upper() or submarket.get('id', '').upper()
            # If still missing, this is a problem - log it
            if not submarket_ticker:
                print(f"   ⚠️  WARNING: Market found but ticker field is missing! Market keys: {list(submarket.keys())[:10]}")
                # Try to get from built ticker if we have it
                built_ticker = await kalshi_client.build_market_ticker(event_ticker, alert.market_type, line, alert.pick)
                if built_ticker:
                    submarket_ticker = built_ticker.upper()
                    submarket['ticker'] = built_ticker
                    print(f"   ✅ Using built ticker: {submarket_ticker}")
            
            # CRITICAL: Validate that this is a standard market (not MULTIGAMEEXTENDED, etc.)
            excluded_types = ['MULTIGAMEEXTENDED', 'EXTENDED', 'MULTIGAME', 'PARLAY', 'COMBO']
            if any(excluded_type in submarket_ticker for excluded_type in excluded_types):
                print(f"❌ REJECTED: Non-standard market type matched: {submarket_ticker} - this is not a standard game market!")
                print(f"   This market type is not supported for auto-betting. Falling back to search...")
                submarket = None  # Force fallback to search
            else:
                match_result = {
                    'market': submarket,
                    'ticker': submarket_ticker,  # This is the SUBMARKET ticker, not event ticker!
                    'confidence': 1.0,
                    'match_method': 'exact_submarket_match'
                }
                print(f"OK: Found exact submarket: {submarket_ticker} for {alert.market_type} {alert.pick} {line}")
        
        # If submarket was rejected or not found, try fallback
        if not submarket or not match_result:
            # DIAGNOSTIC: Enhanced logging for NCAAB failures
            is_ncaab = event_ticker and event_ticker.upper().startswith('KXNCAAMB')
            if is_ncaab:
                print(f"⚠️  [NCAAB] WARNING: Could not find submarket for college basketball:")
                print(f"   Event={event_ticker}, Type={alert.market_type}, Line={line}, Selection={alert.pick}")
                print(f"   Teams={alert.teams}")
            else:
                print(f"WARNING: Could not find submarket: Event={event_ticker}, Type={alert.market_type}, Line={line}, Selection={alert.pick}")
            
            # REMOVED: Fallback search - we ONLY use direct ticker building now
            # This prevents matching wrong markets (e.g., KXMVESPORTSMULTIGAMEEXTENDED)
            # If direct ticker building fails, we skip the alert (lightning fast, no slow searches!)
        
        if not match_result:
            alert_id = create_alert_id(alert)  # Create alert_id early for logging
            print(f"⚠️  [MATCHING] WARNING: Could not match alert: {alert.teams} - {alert.pick} (Alert ID: {alert_id})")
            print(f"   [MATCHING] Event ticker: {event_ticker}")
            print(f"   [MATCHING] Market type: {alert.market_type}")
            print(f"   [MATCHING] Line: {line}")
            print(f"   [MATCHING] Selection: {alert.pick}")
            print(f"   [MATCHING] Qualifier: {alert.qualifier}")
            print(f"   [MATCHING] High-EV should trigger: {high_ev_should_trigger} (EV: {alert.ev_percent:.2f}%, threshold: {filter_ev_min}%)")
            
            # Enhanced diagnostic logging for moneyline failures
            if 'moneyline' in alert.market_type.lower() or 'game' in alert.market_type.lower():
                print(f"   [MATCHING] 💰 MONEYLINE MATCHING FAILURE:")
                print(f"      Selection: '{alert.pick}'")
                if event_ticker:
                    # Try to extract team codes from event ticker
                    try:
                        parts = event_ticker.upper().split('-')
                        if len(parts) >= 2:
                            event_suffix = parts[1]
                            # Extract date and team codes
                            import re
                            date_match = re.match(r'(\d{1,2}[A-Z]{3}\d{1,2})', event_suffix)
                            if date_match:
                                team_codes_part = event_suffix[len(date_match.group(1)):]
                                print(f"      Event suffix: {event_suffix}")
                                print(f"      Team codes part: {team_codes_part}")
                                # Skip possible splits logging - if team is mapped, we don't need this
                                # Only log if we truly can't find the team (diagnostic only, no splits shown)
                    except Exception as e:
                        print(f"      Error extracting team codes: {e}")
            
            print(f"   Alert will still be shown on dashboard for manual betting, but auto-betting is disabled")
            
            # Build basic alert_data even when matching fails (for manual betting)
            alert_id = create_alert_id(alert)
            filter_name = getattr(alert, 'filter_name', '')
            
            # Extract sharp books from filter (for frontend display)
            sharp_books = []
            if filter_name and filter_name in saved_filters:
                filter_payload = saved_filters[filter_name]
                if 'devigFilter' in filter_payload and 'sharps' in filter_payload['devigFilter']:
                    sharp_books = filter_payload['devigFilter']['sharps']
            
            # Build basic alert data (without market matching)
            price_cents = getattr(alert, 'price_cents', None)
            if price_cents is None:
                price_cents = market_matcher.parse_odds_to_price_cents(alert.odds)
            american_odds = price_to_american_odds(price_cents) if price_cents else "N/A"
            
            alert_data = {
                'id': alert_id,
                'timestamp': alert.timestamp.isoformat(),
                'market_type': alert.market_type,
                'teams': alert.teams,
                'pick': alert.pick,
                'qualifier': alert.qualifier,
                'ev_percent': alert.ev_percent,
                'expected_profit': alert.expected_profit,
                'odds': alert.odds,
                'liquidity': alert.liquidity,
                'book_price': american_odds,
                'fair_odds': alert.fair_odds,
                'ticker': None,  # No ticker - matching failed
                'event_ticker': event_ticker if event_ticker else None,
                'side': None,  # No side - matching failed
                'price_cents': price_cents,
                'american_odds': american_odds,
                'match_confidence': 0.0,
                'market_url': alert.market_url,
                'display_books': getattr(alert, 'display_books', {}),
                'devig_books': getattr(alert, 'devig_books', []),
                'sharp_books': sharp_books,
                'market_data': None,  # No market data - matching failed
                'filter_name': filter_name,
                'expiry': (datetime.now() + timedelta(seconds=30)).timestamp(),
                'last_seen': time.time(),
                'match_failed': True,  # Flag to indicate matching failed
                'match_failure_reason': 'Could not find matching submarket',
                'strict_pass': getattr(alert, 'strict_pass', True),
                'ev_source': getattr(alert, 'ev_source', 'odds_api_value_bets'),
            }
            
            # Store in active_alerts (for dashboard display)
            active_alerts[alert_id] = alert_data
            
            # Emit to dashboard (for manual betting) - skip auto-betting
            alert_filter_name = getattr(alert, 'filter_name', None) or alert_data.get('filter_name')
            
            if alert_filter_name and alert_filter_name not in selected_dashboard_filters:
                print(f"[ALERT] SKIP: Alert from filter '{alert_filter_name}' not in selected dashboard filters")
                return
            
            show_unmatched = alert.ev_percent >= dashboard_min_ev or not getattr(alert, 'strict_pass', True)
            if show_unmatched:
                print(
                    f"[ALERT] ✅ Emitting unmatched alert to dashboard: EV {alert.ev_percent:.2f}% "
                    f"(min {dashboard_min_ev:.2f}% or diagnostic strict_pass=False)"
                )
                socketio.emit('new_alert', alert_data)
                print(f"New alert (unmatched): {alert.teams} - {alert.pick} ({alert.ev_percent:.2f}% EV)")
            else:
                print(f"❌ Filtered unmatched alert (EV {alert.ev_percent:.2f}% < min {dashboard_min_ev:.2f}%)")
            
            # Skip auto-betting since matching failed
            # CRITICAL: Log ALL high-EV alerts (>= 10%) that fail matching, regardless of filter selection
            # This ensures we catch all high-value alerts that should have been bet
            if alert.ev_percent >= 10.0 or high_ev_should_trigger:
                store_failed_auto_bet(
                    alert_id=alert_id,
                    alert=alert,
                    alert_data=alert_data,
                    error="Market matching failed - could not find matching submarket",
                    reason=f"Alert has {alert.ev_percent:.2f}% EV (>= {filter_ev_min}% threshold) but matching failed - could not find matching submarket for {alert.market_type} {alert.pick} {alert.qualifier}",
                    ticker=None,
                    side=None,
                    ev_percent=alert.ev_percent,
                    odds=alert_data.get('american_odds'),
                    filter_name=alert_filter_name
                )
            return
        
        # Determine side (CRITICAL: Must match selection to market subtitles)
        yes_subtitle = match_result['market'].get('yes_sub_title', 'N/A')
        no_subtitle = match_result['market'].get('no_sub_title', 'N/A')
        market_title = match_result['market'].get('title', 'N/A')
        ticker = match_result.get('ticker', 'N/A')
        
        print(f"🔍 Determining side for: pick='{alert.pick}', market_type='{alert.market_type}', qualifier='{alert.qualifier}'")
        print(f"   Teams: {alert.teams}")
        print(f"   Ticker: {ticker}")
        print(f"   Market title: {market_title}")
        print(f"   Market subtitles: YES='{yes_subtitle}', NO='{no_subtitle}'")
        
        # CRITICAL: Ensure ticker is in market dict for ticker-based side determination
        market_dict = match_result['market'].copy()
        market_dict['ticker'] = match_result.get('ticker', market_dict.get('ticker', ''))
        side = market_matcher.determine_side(alert, market_dict)
        
        if not side:
            print(f"❌ Could not determine side for: {alert.pick} in market {match_result['ticker']}")
            print(f"   Market YES: {yes_subtitle}")
            print(f"   Market NO: {no_subtitle}")
            print(f"   Alert pick: '{alert.pick}', Alert qualifier: '{alert.qualifier}'")
            # Emit side determination failure to UI
            socketio.emit('alert_match_failed', {
                'teams': alert.teams,
                'pick': alert.pick,
                'market_type': alert.market_type,
                'reason': f'Could not determine side. YES: {yes_subtitle}, NO: {no_subtitle}'
            })
            return
        
        # CRITICAL VALIDATION: Verify side determination is correct for ALL market types
        # This prevents betting on the wrong side - absolutely critical!
        side_corrected = False
        market_type_lower = alert.market_type.lower() if alert.market_type else ""
        pick_upper = alert.pick.upper() if alert.pick else ""
        
        # Extract team names from alert.teams for validation
        teams_str = alert.teams.upper() if alert.teams else ""
        team1 = None
        team2 = None
        if teams_str:
            import re
            parts = re.split(r'\s*[@]\s*|\s*VS\s*', teams_str, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2:
                team1 = parts[0].strip()
                team2 = parts[1].strip()
        
        # VALIDATION FOR MONEYLINE - CRITICAL: This is where wrong-side bets happen!
        if market_type_lower == 'moneyline':
            pick_words = [w for w in pick_upper.split() if len(w) > 3] if len(pick_upper.split()) > 1 else [pick_upper]
            
            # Check which subtitle contains the pick team (using multiple methods for robustness)
            yes_contains_pick = False
            no_contains_pick = False
            
            # Method 1: Direct word matching
            yes_contains_pick = any(word in yes_subtitle.upper() for word in pick_words) or pick_upper in yes_subtitle.upper()
            no_contains_pick = any(word in no_subtitle.upper() for word in pick_words) or pick_upper in no_subtitle.upper()
            
            # Method 2: Full team name matching (more reliable)
            if team1 and team2:
                team1_words = [w for w in team1.split() if len(w) > 3]
                team2_words = [w for w in team2.split() if len(w) > 3]
                pick_is_team1 = any(word in pick_upper for word in team1_words) or pick_upper in team1 or team1 in pick_upper
                pick_is_team2 = any(word in pick_upper for word in team2_words) or pick_upper in team2 or team2 in pick_upper
                
                if pick_is_team1:
                    team1_in_yes = any(word in yes_subtitle.upper() for word in team1_words) or team1 in yes_subtitle.upper()
                    team1_in_no = any(word in no_subtitle.upper() for word in team1_words) or team1 in no_subtitle.upper()
                    # Override with team-based matching (more reliable)
                    if team1_in_yes and not team1_in_no:
                        yes_contains_pick = True
                        no_contains_pick = False
                    elif team1_in_no and not team1_in_yes:
                        yes_contains_pick = False
                        no_contains_pick = True
                elif pick_is_team2:
                    team2_in_yes = any(word in yes_subtitle.upper() for word in team2_words) or team2 in yes_subtitle.upper()
                    team2_in_no = any(word in no_subtitle.upper() for word in team2_words) or team2 in no_subtitle.upper()
                    # Override with team-based matching (more reliable)
                    if team2_in_yes and not team2_in_no:
                        yes_contains_pick = True
                        no_contains_pick = False
                    elif team2_in_no and not team2_in_yes:
                        yes_contains_pick = False
                        no_contains_pick = True
            
            # Determine if this is a non-tie sport (NBA, NHL, MLB)
            # For non-tie sports, betting NO on opponent = YES on pick (equivalent bets)
            sport = determine_sport_from_ticker(ticker)
            allows_ties = sport == "NFL"  # Only NFL allows ties in regular season
            is_non_tie_sport = sport in ["NBA", "NHL", "MLB", "NCAA Men's Basketball"]
            
            # For non-tie sports, check if we're betting NO on the opponent (equivalent to YES on pick)
            opponent_in_subtitle = False
            if is_non_tie_sport and team1 and team2:
                # Determine which team is the opponent
                pick_is_team1 = any(word in pick_upper for word in team1.split() if len(word) > 3) or pick_upper in team1 or team1 in pick_upper
                pick_is_team2 = any(word in pick_upper for word in team2.split() if len(word) > 3) or pick_upper in team2 or team2 in pick_upper
                
                if pick_is_team1:
                    opponent = team2
                elif pick_is_team2:
                    opponent = team1
                else:
                    opponent = None
                
                if opponent:
                    opponent_words = [w for w in opponent.split() if len(w) > 3]
                    if side == 'no':
                        # Check if NO subtitle contains opponent (meaning we're betting opponent loses = pick wins)
                        opponent_in_no = any(word in no_subtitle.upper() for word in opponent_words) or opponent in no_subtitle.upper()
                        if opponent_in_no:
                            opponent_in_subtitle = True
                            print(f"   ✅ For non-tie sport ({sport}): NO on opponent '{opponent}' = YES on pick '{alert.pick}' (equivalent bet)")
            
            # CRITICAL VALIDATION: The determined side MUST contain the pick team (or opponent for non-tie sports)
            # If it doesn't, we're betting the wrong side!
            # EXCEPTION: If subtitles are N/A (Kalshi bug), trust the ticker-based side determination
            subtitles_na = (yes_subtitle.upper() == 'N/A' and no_subtitle.upper() == 'N/A') or (not yes_subtitle and not no_subtitle)
            if side == 'yes' and not yes_contains_pick:
                if subtitles_na:
                    # Subtitles are N/A - trust ticker-based side determination (already validated in determine_side)
                    print(f"   ⚠️  Subtitles are N/A - trusting ticker-based side determination (side=yes)")
                else:
                    print(f"🚨 CRITICAL ERROR: Side determination is WRONG!")
                    print(f"   Determined side: YES, but YES subtitle '{yes_subtitle}' does NOT contain pick '{alert.pick}'")
                    print(f"   NO subtitle: '{no_subtitle}'")
                    if no_contains_pick:
                        print(f"   ✅ CORRECTION: NO subtitle contains pick - changing side to NO")
                        side = 'no'
                        side_corrected = True
                    else:
                        print(f"   ❌ FATAL: Neither subtitle clearly contains pick - REJECTING BET")
                        print(f"   This prevents betting on the wrong side!")
                        socketio.emit('alert_match_failed', {
                            'teams': alert.teams,
                            'pick': alert.pick,
                            'market_type': alert.market_type,
                            'reason': f'CRITICAL: Side validation failed. YES: {yes_subtitle}, NO: {no_subtitle}, Pick: {alert.pick}'
                        })
                        return
            elif side == 'no' and not no_contains_pick:
                # For non-tie sports, NO on opponent is valid (equivalent to YES on pick)
                if is_non_tie_sport and opponent_in_subtitle:
                    print(f"   ✅ VALID: For non-tie sport ({sport}), NO on opponent is equivalent to YES on pick")
                    # This is valid - don't reject
                elif subtitles_na:
                    # Subtitles are N/A - trust ticker-based side determination (already validated in determine_side)
                    pass  # Silently trust ticker-based determination
                else:
                    print(f"🚨 CRITICAL ERROR: Side determination is WRONG!")
                    print(f"   Determined side: NO, but NO subtitle '{no_subtitle}' does NOT contain pick '{alert.pick}'")
                    print(f"   YES subtitle: '{yes_subtitle}'")
                    if yes_contains_pick:
                        print(f"   ✅ CORRECTION: YES subtitle contains pick - changing side to YES")
                        side = 'yes'
                        side_corrected = True
                    else:
                        print(f"   ❌ FATAL: Neither subtitle clearly contains pick - REJECTING BET")
                        print(f"   This prevents betting on the wrong side!")
                        socketio.emit('alert_match_failed', {
                            'teams': alert.teams,
                            'pick': alert.pick,
                            'market_type': alert.market_type,
                            'reason': f'CRITICAL: Side validation failed. YES: {yes_subtitle}, NO: {no_subtitle}, Pick: {alert.pick}'
                        })
                        return
        
        # VALIDATION FOR SPREADS (Point Spread, Puck Line)
        elif market_type_lower in ['point spread', 'spread', 'puck line']:
            # CRITICAL: Check if subtitles are buggy (both say same thing) - trust ticker in that case
            subtitles_buggy = (yes_subtitle.upper() == no_subtitle.upper() and yes_subtitle) or (yes_subtitle.upper() == 'N/A' and no_subtitle.upper() == 'N/A')
            
            if subtitles_buggy:
                # Subtitles are buggy - trust ticker-based side determination (already validated in determine_side)
                pass  # Silently trust ticker-based determination
            else:
                # For spreads, verify the pick team matches the determined side
                # The subtitle should mention the pick team if we're betting on the correct side
                pick_words = [w for w in pick_upper.split() if len(w) > 3] if len(pick_upper.split()) > 1 else [pick_upper]
                
                # Check if pick team appears in the determined side's subtitle
                if side == 'yes':
                    # YES subtitle should mention pick team (or other team if pick is underdog)
                    # This is complex - rely on the determine_side logic but add warning if unclear
                    yes_mentions_pick = any(word in yes_subtitle.upper() for word in pick_words) or pick_upper in yes_subtitle.upper()
                    no_mentions_pick = any(word in no_subtitle.upper() for word in pick_words) or pick_upper in no_subtitle.upper()
                    
                    # If YES doesn't mention pick and NO does, that's suspicious
                    if not yes_mentions_pick and no_mentions_pick and team1 and team2:
                        # Check if pick is underdog - if so, YES might be correct (other team wins by X)
                        qualifier = alert.qualifier or ""
                        is_underdog = qualifier.startswith('+') or (qualifier and not qualifier.startswith('-') and float(qualifier.replace('+', '').replace('-', '').replace('*', '').strip()) > 0)
                        if not is_underdog:
                            # Pick is favorite but YES doesn't mention it - suspicious
                            print(f"⚠️  WARNING: Spread side validation - YES doesn't mention pick '{alert.pick}', but NO does")
                            print(f"   YES: '{yes_subtitle}', NO: '{no_subtitle}'")
                            print(f"   This may be correct if pick is underdog, but double-checking...")
                elif side == 'no':
                    # NO subtitle should mention pick team (or other team if pick is favorite)
                    no_mentions_pick = any(word in no_subtitle.upper() for word in pick_words) or pick_upper in no_subtitle.upper()
                    yes_mentions_pick = any(word in yes_subtitle.upper() for word in pick_words) or pick_upper in yes_subtitle.upper()
                    
                    # If NO doesn't mention pick and YES does, that's suspicious
                    if not no_mentions_pick and yes_mentions_pick and team1 and team2:
                        # Check if pick is favorite - if so, NO might be correct (other team wins by X)
                        qualifier = alert.qualifier or ""
                        is_favorite = qualifier.startswith('-')
                        if not is_favorite:
                            # Pick is underdog but NO doesn't mention it - suspicious
                            print(f"⚠️  WARNING: Spread side validation - NO doesn't mention pick '{alert.pick}', but YES does")
                            print(f"   YES: '{yes_subtitle}', NO: '{no_subtitle}'")
                            print(f"   This may be correct if pick is favorite, but double-checking...")
        
        # VALIDATION FOR TOTALS (Over/Under)
        # CRITICAL: For totals, trust the side determination - YES = Over, NO = Under (always)
        # Don't check subtitles because Kalshi subtitles are often buggy (both say "Over")
        elif 'total' in market_type_lower:
            pick_upper_check = pick_upper
            is_over = 'OVER' in pick_upper_check or pick_upper_check == 'OVER'
            is_under = 'UNDER' in pick_upper_check or pick_upper_check == 'UNDER'
            
            # Trust the side determination from market_matcher (Over = YES, Under = NO)
            # Only validate that the logic is correct, don't try to "correct" based on buggy subtitles
            if is_over and side != 'yes':
                print(f"⚠️  WARNING: Total side mismatch - Pick is OVER, but side is {side} (should be YES)")
                print(f"   ✅ CORRECTION: Changing side to YES (Over = YES, always)")
                side = 'yes'
                side_corrected = True
            elif is_under and side != 'no':
                print(f"⚠️  WARNING: Total side mismatch - Pick is UNDER, but side is {side} (should be NO)")
                print(f"   ✅ CORRECTION: Changing side to NO (Under = NO, always)")
                side = 'no'
                side_corrected = True
            else:
                # Side is correct - trust it (don't check subtitles)
                print(f"   ✅ Total side validation: Pick is {'Over' if is_over else 'Under'} → side={side} (correct)")
        
        if side_corrected:
            print(f"✅ CORRECTED: Final side: {side} for {alert.pick} (YES='{yes_subtitle}', NO='{no_subtitle}')")
        else:
            print(f"✅ FINAL: Determined side: {side} for {alert.pick} (YES='{yes_subtitle}', NO='{no_subtitle}')")
        
        # Get price from alert (already in cents from API) or parse from odds
        # CRITICAL: We use the sportsbook/Kalshi price field (e.g. 63¢) for the LIMIT ORDER
        # The 'odds' field (-183) is the effective price AFTER fees (what BB uses for EV calculation)
        # Example: price=63¢ (-170 American odds) = order price BEFORE fees
        #          odds=-183 = effective price AFTER Kalshi fees
        #          BB calculates EV based on -183 (after fees), but we place order at 63¢ (before fees)
        price_cents = getattr(alert, 'price_cents', None)
        if price_cents is None:
            # Fallback: parse from odds if price not available (shouldn't happen with API)
            price_cents = market_matcher.parse_odds_to_price_cents(alert.odds)
            print(f"   ⚠️  WARNING: No price_cents from API, parsed from odds: {price_cents} cents")
        
        # Debug: Log where price came from and confirm we're using the correct field
        if price_cents:
            price_source = 'API (price_cents)' if getattr(alert, 'price_cents', None) else 'Parsed from odds'
            price_american_odds = price_to_american_odds(price_cents)
            alert_odds = alert.odds or 'N/A'
            print(f"   Price source: {price_source}, value={price_cents} cents ({price_cents/100:.2f}¢) = {price_american_odds} American odds")
            print(f"   Alert odds field: {alert_odds} (effective price AFTER fees - BB calculates EV based on this)")
            print(f"   ✅ LIMIT ORDER WILL USE: {price_cents}¢ ({price_american_odds}) - Place order here to get {alert_odds} after fees")
        
        # SPEED OPTIMIZATION: Pre-fetch orderbook in background (for instant validation later)
        # Don't wait for it, just start the fetch (runs in background)
        asyncio.create_task(kalshi_client.fetch_orderbook(match_result['ticker']))
        
        # Create alert data
        alert_id = create_alert_id(alert)
        
        # Convert price to American odds for display
        american_odds = price_to_american_odds(price_cents) if price_cents else "N/A"
        
        # Extract sharp books from filter (for frontend display)
        filter_name = getattr(alert, 'filter_name', '')
        sharp_books = []
        if filter_name and filter_name in saved_filters:
            filter_payload = saved_filters[filter_name]
            if 'devigFilter' in filter_payload and 'sharps' in filter_payload['devigFilter']:
                sharp_books = filter_payload['devigFilter']['sharps']
        
        alert_data = {
            'id': alert_id,
            'timestamp': alert.timestamp.isoformat(),
            'market_type': alert.market_type,
            'teams': alert.teams,
            'pick': alert.pick,
            'qualifier': alert.qualifier,
            'ev_percent': alert.ev_percent,
            'expected_profit': alert.expected_profit,
            'odds': alert.odds,
            'liquidity': alert.liquidity,
            'book_price': american_odds,  # Show American odds instead of cents
            'fair_odds': alert.fair_odds,
            'ticker': match_result['ticker'],  # SUBMARKET ticker (for betting)
            'event_ticker': event_ticker,  # EVENT ticker (for hash matching with alert feed)
            'side': side,
            'price_cents': price_cents,
            'american_odds': american_odds,  # Store American odds separately
            'match_confidence': match_result['confidence'],
            'market_url': alert.market_url,
            'display_books': getattr(alert, 'display_books', {}),  # Store all book prices for UI
            'devig_books': getattr(alert, 'devig_books', []),  # Store books used for devigging
            'sharp_books': sharp_books,  # Store sharp books from filter (for frontend display)
            'market_data': match_result['market'],  # Store full market data for validation
            'filter_name': filter_name,  # Filter name that generated this alert
            'expiry': (datetime.now() + timedelta(seconds=30)).timestamp(),  # TTL: 30 seconds
            'last_seen': time.time(),  # Track when alert was last seen for stale detection
            'strict_pass': getattr(alert, 'strict_pass', True),
            'ev_source': getattr(alert, 'ev_source', 'odds_api_value_bets'),
        }
        
        # CRITICAL: Check if this alert_id is already being processed (prevent duplicate processing)
        if alert_id in active_alerts:
            existing_alert = active_alerts[alert_id]
            print(f"[HANDLE ALERT] ⚠️  DUPLICATE: Alert {alert_id} already exists in active_alerts - skipping duplicate processing")
            print(f"[HANDLE ALERT]    Existing: {existing_alert.get('teams', 'N/A')} - {existing_alert.get('pick', 'N/A')}")
            print(f"[HANDLE ALERT]    New: {alert.teams} - {alert.pick}")
            # Update all fields that may have changed (EV, odds, price, liquidity, etc.)
            # This ensures the frontend shows the latest data without creating duplicate alerts
            updated = False
            if alert.ev_percent != existing_alert.get('ev_percent', 0):
                existing_alert['ev_percent'] = alert.ev_percent
                updated = True
            if getattr(alert, 'liquidity', None) is not None and alert.liquidity != existing_alert.get('liquidity', 0):
                existing_alert['liquidity'] = alert.liquidity
                updated = True
            if price_cents is not None and price_cents != existing_alert.get('price_cents'):
                existing_alert['price_cents'] = price_cents
                existing_alert['book_price'] = price_to_american_odds(price_cents) if price_cents else None
                existing_alert['american_odds'] = price_to_american_odds(price_cents) if price_cents else None
                updated = True
            if alert.odds != existing_alert.get('odds'):
                existing_alert['odds'] = alert.odds
                updated = True
            if getattr(alert, 'expected_profit', None) is not None and alert.expected_profit != existing_alert.get('expected_profit', 0):
                existing_alert['expected_profit'] = alert.expected_profit
                updated = True
            if getattr(alert, 'display_books', None) is not None:
                existing_alert['display_books'] = alert.display_books
                updated = True
            if getattr(alert, 'devig_books', None) is not None:
                existing_alert['devig_books'] = alert.devig_books
                updated = True
            if getattr(alert, 'sharp_books', None) is not None:
                existing_alert['sharp_books'] = alert.sharp_books
                updated = True
            if hasattr(alert, 'strict_pass'):
                existing_alert['strict_pass'] = alert.strict_pass
                updated = True
            if getattr(alert, "ev_source", None) and alert.ev_source != existing_alert.get("ev_source"):
                existing_alert["ev_source"] = alert.ev_source
                updated = True
            
            # CRITICAL: Preserve filter_name - use existing if new alert doesn't have it, otherwise update
            if hasattr(alert, 'filter_name') and alert.filter_name:
                existing_alert['filter_name'] = alert.filter_name
                updated = True
            # If new alert doesn't have filter_name, preserve the existing one (don't overwrite with None)
            
            # Emit update to frontend if anything changed
            if updated:
                print(f"[HANDLE ALERT] 🔄 Updated existing alert {alert_id} with new data (EV: {existing_alert.get('ev_percent', 0):.2f}%, Odds: {existing_alert.get('odds', 'N/A')})")
                socketio.emit('alert_update', existing_alert)
            
            # CRITICAL: Check auto-bet even for duplicate alerts if EV increased above threshold
            # This handles the case where an alert's EV increases from below threshold to above threshold
            if auto_bet_enabled and match_result and match_result.get('ticker') and side:
                alert_filter_name = getattr(alert, 'filter_name', None) or existing_alert.get('filter_name')
                # Check if alert is from a filter selected for auto-bettor
                if not alert_filter_name or alert_filter_name in selected_auto_bettor_filters:
                    # Get filter-specific EV threshold
                    if alert_filter_name and alert_filter_name in auto_bet_settings_by_filter:
                        filter_ev_min = auto_bet_settings_by_filter[alert_filter_name].get('ev_min', auto_bet_ev_min)
                    else:
                        filter_ev_min = auto_bet_ev_min
                    
                    # Check if EV is now above threshold (might have increased from below threshold)
                    new_ev = alert.ev_percent
                    old_ev = existing_alert.get('ev_percent', 0)
                    
                    # Only trigger auto-bet if:
                    # 1. New EV is above threshold
                    # 2. Either old EV was below threshold OR this is first time we're checking this alert
                    if new_ev >= filter_ev_min:
                        submarket_key_for_check = (match_result['ticker'].upper(), side.lower())
                        # Check if already bet or processing
                        if (submarket_key_for_check not in auto_bet_submarkets and 
                            submarket_key_for_check not in auto_bet_processing_submarkets):
                            print(f"[HANDLE ALERT] 🔄 DUPLICATE ALERT: EV increased to {new_ev:.2f}% (was {old_ev:.2f}%), triggering auto-bet check")
                            # Create alert_data from existing_alert for auto-bet
                            duplicate_alert_data = existing_alert.copy()
                            duplicate_alert_data['ticker'] = match_result['ticker']
                            duplicate_alert_data['side'] = side
                            duplicate_alert_data['ev_percent'] = new_ev
                            # Trigger auto-bet check for this duplicate alert
                            if auto_bet_lock:
                                async def create_task_for_duplicate():
                                    try:
                                        print(f"[AUTO-BET] [LOCK] Duplicate alert {alert_id} attempting to acquire lock for {submarket_key_for_check}")
                                        async with auto_bet_lock:
                                            print(f"[AUTO-BET] [LOCK] Duplicate alert {alert_id} acquired lock for {submarket_key_for_check}")
                                            # Check if already bet or processing (double-check after acquiring lock)
                                            if submarket_key_for_check in auto_bet_submarkets:
                                                print(f"[AUTO-BET] SKIP: Duplicate alert {alert_id} - Submarket {submarket_key_for_check} already bet")
                                                return
                                            if submarket_key_for_check in auto_bet_processing_submarkets:
                                                print(f"[AUTO-BET] SKIP: Duplicate alert {alert_id} - Submarket {submarket_key_for_check} already processing")
                                                return
                                            # Mark as processing
                                            auto_bet_processing_submarkets.add(submarket_key_for_check)
                                            auto_bet_submarket_to_alert_id[submarket_key_for_check] = alert_id
                                            auto_bet_processing_alert_ids.add(alert_id)
                                            task = asyncio.create_task(check_and_auto_bet(alert_id, duplicate_alert_data, alert))
                                            auto_bet_submarket_tasks[submarket_key_for_check] = task
                                            print(f"[AUTO-BET] Created task for duplicate alert {alert_id}, submarket {submarket_key_for_check} marked as processing")
                                    except Exception as e:
                                        print(f"[AUTO-BET] Error creating task for duplicate alert: {e}")
                                        import traceback
                                        traceback.print_exc()
                                asyncio.create_task(create_task_for_duplicate())
                            else:
                                # Fallback if lock not initialized
                                if (submarket_key_for_check not in auto_bet_submarkets and 
                                    submarket_key_for_check not in auto_bet_processing_submarkets):
                                    auto_bet_processing_submarkets.add(submarket_key_for_check)
                                    auto_bet_submarket_to_alert_id[submarket_key_for_check] = alert_id
                                    auto_bet_processing_alert_ids.add(alert_id)
                                    task = asyncio.create_task(check_and_auto_bet(alert_id, duplicate_alert_data, alert))
                                    auto_bet_submarket_tasks[submarket_key_for_check] = task
                                    print(f"[AUTO-BET] Created task for duplicate alert {alert_id} (fallback, no lock)")
            
            return
        
        # Store with string ID (already converted in create_alert_id)
        active_alerts[alert_id] = alert_data
        
        # Subscribe to WebSocket for real-time orderbook updates (warm cache on-demand)
        submarket_ticker = match_result['ticker']
        await kalshi_client.subscribe_orderbook(submarket_ticker)
        # Add to warm cache set so it's tracked
        if submarket_ticker not in kalshi_client.warm_cache_tickers:
            kalshi_client.warm_cache_tickers.add(submarket_ticker)
            print(f"[WARM CACHE] Added {submarket_ticker} to warm cache (alert-driven)")
        
        # Filter by dashboard min EV AND selected dashboard filters before emitting
        alert_filter_name = getattr(alert, 'filter_name', None) or alert_data.get('filter_name')
        
        # Check if this alert's filter is selected for dashboard
        if alert_filter_name and alert_filter_name not in selected_dashboard_filters:
            print(f"[ALERT] SKIP: Alert from filter '{alert_filter_name}' not in selected dashboard filters: {selected_dashboard_filters}")
            # Still store in active_alerts for potential future use, but don't emit to frontend
            return
        
        print(f"[ALERT] Processing alert: {alert.teams} - {alert.pick} ({alert.ev_percent:.2f}% EV, dashboard_min_ev={dashboard_min_ev:.2f}%)")
        show_matched = alert.ev_percent >= dashboard_min_ev or not getattr(alert, 'strict_pass', True)
        if show_matched:
            # Emit to all connected clients IMMEDIATELY (don't wait for orderbook)
            print(
                f"[ALERT] ✅ Emitting to frontend: EV {alert.ev_percent:.2f}% "
                f"(min {dashboard_min_ev:.2f}% or diagnostic strict_pass=False)"
            )
            socketio.emit('new_alert', alert_data)
            print(f"New alert: {alert.teams} - {alert.pick} ({alert.ev_percent:.2f}% EV)")
        else:
            print(f"❌ Filtered alert (EV {alert.ev_percent:.2f}% < min {dashboard_min_ev:.2f}%): {alert.teams} - {alert.pick}")
        
        # LOG: Log ALL alerts that matched successfully (regardless of EV threshold)
        # This helps analyze what alerts are coming in and why they're not being bet
        if match_result and match_result.get('ticker') and side:
            alert_filter_name = getattr(alert, 'filter_name', None) or alert_data.get('filter_name', '')
            # Get filter settings for logging
            if alert_filter_name and alert_filter_name in auto_bet_settings_by_filter:
                filter_settings_dict = auto_bet_settings_by_filter[alert_filter_name]
            else:
                filter_settings_dict = {
                    'filter_name': alert_filter_name,
                    'ev_min': auto_bet_ev_min,
                    'ev_max': auto_bet_ev_max,
                    'odds_min': auto_bet_odds_min,
                    'odds_max': auto_bet_odds_max,
                    'amount': auto_bet_amount,
                    'enabled': auto_bet_enabled,
                }
            
            # Log with initial decision path (will be updated in check_and_auto_bet if it runs)
            decision_path_initial = {
                'matching_check': 'PASSED',
                'ticker': match_result.get('ticker'),
                'side': side,
                'ev_percent': alert.ev_percent,
                'ev_threshold': filter_settings_dict.get('ev_min', auto_bet_ev_min),
                'status': 'MATCHED_BUT_NOT_YET_CHECKED',  # Will be updated when check_and_auto_bet runs
            }
            log_alert_passed_threshold(alert_id, alert, alert_data, filter_settings_dict, decision_path_initial)
        
        # AUTO-BET: Check if alert matches auto-bet criteria and place bet automatically
        # CRITICAL: Only process alerts from filters selected for auto-bettor
        alert_filter_name = getattr(alert, 'filter_name', None)
        if auto_bet_enabled:
            # Check if alert is from a filter selected for auto-bettor
            if alert_filter_name and alert_filter_name not in selected_auto_bettor_filters:
                print(f"[AUTO-BET] SKIP: Alert {alert_id} from filter '{alert_filter_name}' not in selected auto-bettor filters: {selected_auto_bettor_filters} [FILTER NOT SELECTED FOR AUTO-BET]")
                # Log high-EV alerts that are blocked due to filter not being selected
                if should_log_high_ev_block(alert, alert_data):
                    # Get filter EV threshold to check if alert should have triggered
                    filter_ev_min = auto_bet_settings_by_filter.get(alert_filter_name, {}).get('ev_min', auto_bet_ev_min) if alert_filter_name else auto_bet_ev_min
                    if alert.ev_percent >= filter_ev_min:
                        store_failed_auto_bet(
                            alert_id=alert_id,
                            alert=alert,
                            alert_data=alert_data,
                            error=f"Filter '{alert_filter_name}' not selected for auto-bettor",
                            reason=f"Alert has {alert.ev_percent:.2f}% EV (>= {filter_ev_min}% threshold) but filter not in selected_auto_bettor_filters: {selected_auto_bettor_filters}",
                            ticker=match_result.get('ticker') if match_result else None,
                            side=side,
                            ev_percent=alert.ev_percent,
                            odds=alert_data.get('american_odds'),
                            filter_name=alert_filter_name
                        )
                # Don't process for auto-betting (dashboard filtering is handled earlier in handle_new_alert)
                return
            else:
                # CRITICAL: Only proceed if matching succeeded (we need ticker and side for auto-bet)
                if not match_result or not match_result.get('ticker') or not side:
                    print(f"[AUTO-BET] 🚨 SKIP: Alert {alert_id} - matching failed or incomplete (ticker={match_result.get('ticker') if match_result else None}, side={side})")
                    print(f"[AUTO-BET]   High-EV should trigger: {high_ev_should_trigger} (EV: {alert.ev_percent:.2f}%, threshold: {filter_ev_min}%)")
                    # CRITICAL: Log ALL high-EV alerts (>= 10%) that fail matching, regardless of filter threshold
                    # This ensures we catch all high-value alerts that should have been bet
                    filter_ev_min = auto_bet_settings_by_filter.get(alert_filter_name, {}).get('ev_min', auto_bet_ev_min) if alert_filter_name else auto_bet_ev_min
                    if alert.ev_percent >= 10.0 or (high_ev_should_trigger or should_log_high_ev_block(alert, alert_data)):
                        print(f"[FAILED AUTO-BET] 🚨 HIGH-EV ALERT MATCHING INCOMPLETE: Alert {alert_id} - {alert.teams} - {alert.pick} ({alert.ev_percent:.2f}% EV)")
                        store_failed_auto_bet(
                            alert_id=alert_id,
                            alert=alert,
                            alert_data=alert_data,
                            error="Market matching failed - could not find matching submarket",
                            reason=f"Alert has {alert.ev_percent:.2f}% EV (>= {filter_ev_min}% threshold) but matching failed - could not find matching submarket for {alert.market_type} {alert.pick} {alert.qualifier}",
                            ticker=match_result.get('ticker') if match_result else None,
                            side=side,
                            ev_percent=alert.ev_percent,
                            odds=alert_data.get('american_odds'),
                            filter_name=alert_filter_name
                        )
                    return
                
                # CRITICAL: Run in background (don't await) so it doesn't block alert processing or manual betting
                # BUT: Only create task if not already processing this alert_id (prevents race conditions)
                # ALSO: Check if submarket is already being processed (prevents duplicate tasks for same submarket with different alert IDs)
                # CRITICAL: Use lock to make check-and-mark atomic - mark submarket as processing HERE to prevent duplicate tasks
                submarket_key_for_check = (match_result['ticker'].upper(), side.lower())
                print(f"[AUTO-BET] 🔍 Checking if alert {alert_id} should trigger auto-bet:")
                print(f"[AUTO-BET]    Alert: {alert.teams} - {alert.pick} ({alert.ev_percent:.2f}% EV)")
                print(f"[AUTO-BET]    ticker={match_result['ticker']}, side={side}, submarket_key={submarket_key_for_check}")
                print(f"[AUTO-BET]    filter={alert_filter_name}, selected_auto_bettor_filters={selected_auto_bettor_filters}")
                print(f"[AUTO-BET]    auto_bet_enabled={auto_bet_enabled}")
                # Check filter-specific EV threshold
                if alert_filter_name and alert_filter_name in auto_bet_settings_by_filter:
                    filter_ev_min = auto_bet_settings_by_filter[alert_filter_name].get('ev_min', 5.0)
                    print(f"[AUTO-BET]    Filter '{alert_filter_name}' EV threshold: {filter_ev_min}% (alert EV: {alert.ev_percent:.2f}%)")
                print(f"[AUTO-BET]    alert_id in processing: {alert_id in auto_bet_processing_alert_ids}")
                print(f"[AUTO-BET]    submarket already bet: {submarket_key_for_check in auto_bet_submarkets}")
                print(f"[AUTO-BET]    submarket processing: {submarket_key_for_check in auto_bet_processing_submarkets}")
                
                # CRITICAL: Check if task is actually still running - if done, clean up and allow new task
                if alert_id in auto_bet_processing_alert_ids:
                    # Check if there's a task for this submarket and if it's done
                    existing_task = auto_bet_submarket_tasks.get(submarket_key_for_check)
                    try:
                        if existing_task and hasattr(existing_task, 'done') and existing_task.done():
                            # Task is done but still in processing set - clean up
                            print(f"[AUTO-BET] ⚠️  WARNING: Alert {alert_id} in processing set but task is done - cleaning up and allowing new task")
                            auto_bet_processing_alert_ids.discard(alert_id)
                            auto_bet_processing_submarkets.discard(submarket_key_for_check)
                            auto_bet_submarket_to_alert_id.pop(submarket_key_for_check, None)
                            auto_bet_submarket_tasks.pop(submarket_key_for_check, None)
                        elif existing_task and hasattr(existing_task, 'done') and not existing_task.done():
                            # Task is still running - skip
                            print(f"[AUTO-BET] SKIP: Alert {alert_id} already being processed, skipping duplicate task")
                            return
                        else:
                            # No task found but alert_id in processing - clean up (orphaned entry)
                            print(f"[AUTO-BET] ⚠️  WARNING: Alert {alert_id} in processing set but no task found - cleaning up orphaned entry")
                            auto_bet_processing_alert_ids.discard(alert_id)
                            auto_bet_processing_submarkets.discard(submarket_key_for_check)
                            auto_bet_submarket_to_alert_id.pop(submarket_key_for_check, None)
                            if submarket_key_for_check in auto_bet_submarket_tasks:
                                auto_bet_submarket_tasks.pop(submarket_key_for_check, None)
                    except Exception as e:
                        # If checking task status fails, clean up and allow new task (safer than blocking)
                        print(f"[AUTO-BET] ⚠️  ERROR checking task status for alert {alert_id}: {e} - cleaning up and allowing new task")
                        auto_bet_processing_alert_ids.discard(alert_id)
                        auto_bet_processing_submarkets.discard(submarket_key_for_check)
                        auto_bet_submarket_to_alert_id.pop(submarket_key_for_check, None)
                        if submarket_key_for_check in auto_bet_submarket_tasks:
                            auto_bet_submarket_tasks.pop(submarket_key_for_check, None)
                
                if alert_id not in auto_bet_processing_alert_ids:
                    print(f"[AUTO-BET] ✅ Alert {alert_id} passed initial checks, proceeding to create task...")
                    # CRITICAL: Use lock to atomically check AND mark submarket as processing
                    # This prevents race conditions where two tasks both see "not processing" and both create tasks
                    # We mark it here so that if another task checks before this task runs, it will see it's already processing
                    if auto_bet_lock:
                        async def create_task_safely():
                            global auto_bet_lock_holder, auto_bet_lock_acquired_at  # Access global lock tracking
                            print(f"[AUTO-BET] [CREATE_TASK_SAFELY] START: Alert {alert_id}, submarket {submarket_key_for_check}")
                            task_created = False
                            skip_reason = None
                            task_start_time = time.time()  # Track task creation timing
                            try:
                                print(f"[AUTO-BET] [LOCK] Alert {alert_id} attempting to acquire lock for {submarket_key_for_check}")
                                print(f"[AUTO-BET] [LOCK] DEBUG: auto_bet_lock = {auto_bet_lock}, type = {type(auto_bet_lock)}")
                                if auto_bet_lock is None:
                                    print(f"[AUTO-BET] [LOCK] ERROR: auto_bet_lock is None! Cannot acquire lock.")
                                    skip_reason = "auto_bet_lock is None"
                                    if high_ev_should_trigger:
                                        store_failed_auto_bet(
                                            alert_id=alert_id,
                                            alert=alert,
                                            alert_data=alert_data,
                                            error="auto_bet_lock is None",
                                            reason=f"Alert has {alert.ev_percent:.2f}% EV (>= {filter_ev_min}% threshold) but auto_bet_lock is None - cannot acquire lock",
                                            ticker=match_result.get('ticker') if match_result else None,
                                            side=side,
                                            ev_percent=alert.ev_percent,
                                            odds=alert_data.get('american_odds'),
                                            filter_name=alert_filter_name
                                        )
                                    return
                                
                                print(f"[AUTO-BET] [LOCK] About to acquire lock with timeout...")
                                # CRITICAL: Add timeout to prevent infinite waiting if lock is stuck
                                # Check lock state before attempting acquisition
                                lock_state_before = auto_bet_lock.locked()
                                waiters_before = len(auto_bet_lock._waiters) if (hasattr(auto_bet_lock, '_waiters') and auto_bet_lock._waiters is not None) else 'unknown'
                                print(f"[AUTO-BET] [LOCK] Pre-acquisition state: locked={lock_state_before}, waiters={waiters_before}")
                                
                                try:
                                    await asyncio.wait_for(auto_bet_lock.acquire(), timeout=2.0)
                                except asyncio.TimeoutError:
                                    skip_reason = f"Lock acquisition timeout (2s) - lock may be stuck"
                                    lock_state_after = auto_bet_lock.locked()
                                    waiters_after = len(auto_bet_lock._waiters) if (hasattr(auto_bet_lock, '_waiters') and auto_bet_lock._waiters is not None) else 'unknown'
                                    print(f"[AUTO-BET] ⚠️  TIMEOUT: Alert {alert_id} waited >2s for lock in create_task_safely - lock may be stuck")
                                    print(f"[AUTO-BET]   Lock state: locked={lock_state_after}, waiters={waiters_after}")
                                    print(f"[AUTO-BET]   Pre-acquisition: locked={lock_state_before}, waiters={waiters_before}")
                                    
                                    # DIAGNOSTIC: Check if there are stuck tasks and which one is holding the lock
                                    stuck_tasks = []
                                    for submarket_key_stuck, task in auto_bet_submarket_tasks.items():
                                        if task and not task.done():
                                            stuck_tasks.append(f"{submarket_key_stuck}: {task}")
                                    if stuck_tasks:
                                        print(f"[AUTO-BET]   ⚠️  Found {len(stuck_tasks)} potentially stuck task(s): {stuck_tasks[:3]}")  # Show first 3
                                    
                                    # CRITICAL: Show which task is currently holding the lock
                                    try:
                                        if auto_bet_lock_holder and auto_bet_lock_acquired_at:
                                            lock_held_duration = (time.time() - auto_bet_lock_acquired_at) * 1000
                                            print(f"[AUTO-BET]   🚨 LOCK HELD BY: {auto_bet_lock_holder} for {lock_held_duration:.1f}ms (LOCK IS STUCK!)")
                                        else:
                                            print(f"[AUTO-BET]   ⚠️  Lock holder tracking: holder={auto_bet_lock_holder}, acquired_at={auto_bet_lock_acquired_at}")
                                    except NameError:
                                        # Lock holder tracking variables not yet initialized
                                        print(f"[AUTO-BET]   ⚠️  Lock holder tracking not available (variables not initialized)")
                                    if high_ev_should_trigger:
                                        # Gather comprehensive diagnostics
                                        diagnostic_logs = [
                                            f"========== LOCK ACQUISITION TIMEOUT DIAGNOSTICS ==========",
                                            f"Alert ID: {alert_id}",
                                            f"Submarket: {submarket_key_for_check}",
                                            f"Lock state: locked={lock_state_after}, waiters={waiters_after}",
                                            f"Pre-acquisition: locked={lock_state_before}, waiters={waiters_before}",
                                            f"Timeout: 2.0s",
                                            f""
                                        ]
                                        
                                        # Add stuck tasks info
                                        if stuck_tasks:
                                            diagnostic_logs.append(f"⚠️  Found {len(stuck_tasks)} potentially stuck task(s):")
                                            for stuck_task in stuck_tasks[:5]:  # Show first 5
                                                diagnostic_logs.append(f"   - {stuck_task}")
                                        else:
                                            diagnostic_logs.append(f"✅ No stuck tasks found")
                                        
                                        diagnostic_logs.append(f"")
                                        
                                        # Add lock holder info
                                        try:
                                            if auto_bet_lock_holder and auto_bet_lock_acquired_at:
                                                lock_held_duration = (time.time() - auto_bet_lock_acquired_at) * 1000
                                                diagnostic_logs.append(f"🚨 LOCK HELD BY: {auto_bet_lock_holder}")
                                                diagnostic_logs.append(f"   Held for: {lock_held_duration:.1f}ms")
                                                diagnostic_logs.append(f"   Acquired at: {time.strftime('%H:%M:%S.%f', time.localtime(auto_bet_lock_acquired_at))}")
                                            else:
                                                diagnostic_logs.append(f"⚠️  Lock holder tracking: holder={auto_bet_lock_holder}, acquired_at={auto_bet_lock_acquired_at}")
                                        except NameError:
                                            diagnostic_logs.append(f"⚠️  Lock holder tracking not available")
                                        
                                        diagnostic_logs.extend([
                                            f"",
                                            f"Submarket status:",
                                            f"   Already bet: {submarket_key_for_check in auto_bet_submarkets}",
                                            f"   In processing: {submarket_key_for_check in auto_bet_processing_submarkets}",
                                            f"   Processing alert_id: {auto_bet_submarket_to_alert_id.get(submarket_key_for_check, 'N/A')}",
                                            f"   Task exists: {submarket_key_for_check in auto_bet_submarket_tasks}",
                                            f"   Task done: {auto_bet_submarket_tasks.get(submarket_key_for_check).done() if submarket_key_for_check in auto_bet_submarket_tasks and auto_bet_submarket_tasks.get(submarket_key_for_check) else 'N/A'}",
                                            f"",
                                            f"Alert ID status:",
                                            f"   In processing set: {alert_id in auto_bet_processing_alert_ids}",
                                            f"",
                                            f"Task creation timing:",
                                            f"   Task start time: {time.strftime('%H:%M:%S.%f', time.localtime(task_start_time))}",
                                            f"   Timeout occurred at: {time.strftime('%H:%M:%S.%f', time.localtime(time.time()))}",
                                            f"   Total wait time: {(time.time() - task_start_time) * 1000:.1f}ms",
                                            f"",
                                            f"====================================="
                                        ])
                                        
                                        store_failed_auto_bet(
                                            alert_id=alert_id,
                                            alert=alert,
                                            alert_data=alert_data,
                                            error=skip_reason,
                                            reason=f"Alert has {alert.ev_percent:.2f}% EV (>= {filter_ev_min}% threshold) but lock acquisition timed out in create_task_safely - lock may be stuck",
                                            ticker=match_result.get('ticker') if match_result else None,
                                            side=side,
                                            ev_percent=alert.ev_percent,
                                            odds=alert_data.get('american_odds'),
                                            filter_name=alert_filter_name,
                                            additional_logs=diagnostic_logs
                                        )
                                    return
                                
                                # Lock acquired - now perform operations
                                try:
                                    lock_acquired_time = time.time()
                                    print(f"[AUTO-BET] [LOCK] Alert {alert_id} acquired lock for {submarket_key_for_check} at {time.strftime('%H:%M:%S.%f', time.localtime(lock_acquired_time))}")
                                    print(f"[AUTO-BET] [LOCK] Lock acquisition took {(lock_acquired_time - task_start_time) * 1000:.1f}ms")
                                    # CRITICAL: Check retry count and cooldown FIRST (prevents infinite loops)
                                    current_time = time.time()
                                    retry_count = auto_bet_submarket_retry_count.get(submarket_key_for_check, 0)
                                    last_retry = auto_bet_submarket_last_retry.get(submarket_key_for_check, 0)
                                    
                                    if retry_count >= MAX_RETRIES_PER_SUBMARKET:
                                        time_since_last_retry = current_time - last_retry
                                        if time_since_last_retry < RETRY_COOLDOWN_SECONDS:
                                            skip_reason = f"Exceeded max retries ({retry_count}/{MAX_RETRIES_PER_SUBMARKET}) and still in cooldown ({int(time_since_last_retry)}s/{RETRY_COOLDOWN_SECONDS}s)"
                                            print(f"[AUTO-BET] 🚨 BLOCKED: Alert {alert_id} - Submarket {submarket_key_for_check} {skip_reason}, BLOCKING to prevent infinite loop")
                                            if high_ev_should_trigger:
                                                store_failed_auto_bet(
                                                    alert_id=alert_id,
                                                    alert=alert,
                                                    alert_data=alert_data,
                                                    error=f"Retry cooldown: {skip_reason}",
                                                    reason=f"Alert has {alert.ev_percent:.2f}% EV (>= {filter_ev_min}% threshold) but {skip_reason}",
                                                    ticker=match_result.get('ticker') if match_result else None,
                                                    side=side,
                                                    ev_percent=alert.ev_percent,
                                                    odds=alert_data.get('american_odds'),
                                                    filter_name=alert_filter_name
                                                )
                                            return
                                        else:
                                            # Cooldown expired - reset retry count
                                            print(f"[AUTO-BET] Cooldown expired for {submarket_key_for_check}, resetting retry count")
                                            auto_bet_submarket_retry_count[submarket_key_for_check] = 0
                                            auto_bet_submarket_last_retry.pop(submarket_key_for_check, None)  # Clear timestamp
                                    
                                    # CRITICAL: Check if already bet FIRST (most important check)
                                    # This prevents reappearing alerts from creating duplicate tasks
                                    # DOUBLE-CHECK: Verify it's truly not in the set (defensive check)
                                    if submarket_key_for_check in auto_bet_submarkets:
                                        skip_reason = "Submarket already bet (reappear protection)"
                                        print(f"[AUTO-BET] SKIP: Alert {alert_id} - Submarket {submarket_key_for_check} already bet, skipping duplicate task ({skip_reason})")
                                        # Don't log this - already bet is expected
                                        return
                                    
                                    # CRITICAL: Check if submarket is already being processed
                                    # This catches the case where a bet is in progress but not yet in auto_bet_submarkets
                                    if submarket_key_for_check in auto_bet_processing_submarkets:
                                        # Check if the task is still running - if not, clean up and allow this one
                                        existing_task = auto_bet_submarket_tasks.get(submarket_key_for_check)
                                        if existing_task and not existing_task.done():
                                            skip_reason = "Submarket already being processed (task still running)"
                                            print(f"[AUTO-BET] SKIP: Alert {alert_id} - Submarket {submarket_key_for_check} already being processed (task still running), skipping duplicate task")
                                            if high_ev_should_trigger:
                                                store_failed_auto_bet(
                                                    alert_id=alert_id,
                                                    alert=alert,
                                                    alert_data=alert_data,
                                                    error=skip_reason,
                                                    reason=f"Alert has {alert.ev_percent:.2f}% EV (>= {filter_ev_min}% threshold) but {skip_reason}",
                                                    ticker=match_result.get('ticker') if match_result else None,
                                                    side=side,
                                                    ev_percent=alert.ev_percent,
                                                    odds=alert_data.get('american_odds'),
                                                    filter_name=alert_filter_name
                                                )
                                            return
                                        else:
                                            # Task is done or doesn't exist - clean up and allow this one
                                            print(f"[AUTO-BET] WARNING: Submarket {submarket_key_for_check} marked as processing but task is done/missing - cleaning up and allowing new task")
                                            auto_bet_processing_submarkets.discard(submarket_key_for_check)
                                            auto_bet_submarket_to_alert_id.pop(submarket_key_for_check, None)
                                            auto_bet_submarket_tasks.pop(submarket_key_for_check, None)
                                            # Continue to create new task below
                                    
                                    # CRITICAL: Re-check if already bet AFTER checking processing (defensive double-check)
                                    # This catches the race condition where bet was added between initial check and now
                                    if submarket_key_for_check in auto_bet_submarkets:
                                        skip_reason = "Submarket already bet (caught in double-check)"
                                        print(f"[AUTO-BET] SKIP: Alert {alert_id} - Submarket {submarket_key_for_check} already bet (caught in double-check), skipping duplicate task")
                                        # Don't log this - already bet is expected
                                        return
                                    
                                    # CRITICAL: Mark submarket as processing HERE (within lock) to prevent duplicate tasks
                                    # Also track which alert_id is processing it so we can verify in check_and_auto_bet
                                    auto_bet_processing_submarkets.add(submarket_key_for_check)
                                    auto_bet_submarket_to_alert_id[submarket_key_for_check] = alert_id
                                    # Mark alert_id as processing (prevents duplicate tasks for same alert)
                                    auto_bet_processing_alert_ids.add(alert_id)
                                    # Create task and track it so we can verify if it's still running
                                    task = asyncio.create_task(check_and_auto_bet(alert_id, alert_data, alert))
                                    auto_bet_submarket_tasks[submarket_key_for_check] = task
                                    
                                    # CRITICAL: Add watchdog to kill stuck tasks AND force lock release after 30 seconds
                                    async def watchdog_task():
                                        """Kill task and force lock release if it runs longer than 30 seconds"""
                                        await asyncio.sleep(30.0)  # Wait 30 seconds
                                        if not task.done():
                                            print(f"[AUTO-BET] 🚨 WATCHDOG: Task for {submarket_key_for_check} (alert {alert_id}) has been running >30s - CANCELLING AND FORCING LOCK RELEASE")
                                            
                                            # CRITICAL: Force lock release if this task is holding it
                                            global auto_bet_lock_holder, auto_bet_lock_acquired_at
                                            if auto_bet_lock_holder and f"Alert {alert_id}" in auto_bet_lock_holder:
                                                lock_held_duration = (time.time() - auto_bet_lock_acquired_at) if auto_bet_lock_acquired_at else 0
                                                print(f"[AUTO-BET] 🚨 WATCHDOG: Task is holding lock for {lock_held_duration:.1f}s - FORCING RELEASE")
                                                if auto_bet_lock and auto_bet_lock.locked():
                                                    try:
                                                        # Force release the lock
                                                        auto_bet_lock.release()
                                                        print(f"[AUTO-BET] 🚨 WATCHDOG: Lock force-released")
                                                        # Clear lock holder tracking
                                                        auto_bet_lock_holder = None
                                                        auto_bet_lock_acquired_at = None
                                                    except Exception as e:
                                                        print(f"[AUTO-BET] 🚨 WATCHDOG: Failed to force-release lock: {e}")
                                            
                                            # Cancel the task
                                            task.cancel()
                                            
                                            # Clean up tracking
                                            auto_bet_processing_submarkets.discard(submarket_key_for_check)
                                            auto_bet_submarket_to_alert_id.pop(submarket_key_for_check, None)
                                            auto_bet_submarket_tasks.pop(submarket_key_for_check, None)
                                            auto_bet_processing_alert_ids.discard(alert_id)
                                            
                                            # Remove from bet set if it was added
                                            if submarket_key_for_check in auto_bet_submarkets:
                                                auto_bet_submarkets.discard(submarket_key_for_check)
                                            
                                            # Log to failed-bets
                                            if high_ev_should_trigger:
                                                store_failed_auto_bet(
                                                    alert_id=alert_id,
                                                    alert=alert,
                                                    alert_data=alert_data,
                                                    error="Task watchdog timeout (30s) - task was killed and lock force-released",
                                                    reason=f"Alert has {alert.ev_percent:.2f}% EV (>= {filter_ev_min}% threshold) but task ran >30s and was killed by watchdog. Lock was force-released.",
                                                    ticker=match_result.get('ticker') if match_result else None,
                                                    side=side,
                                                    ev_percent=alert.ev_percent,
                                                    odds=alert_data.get('american_odds'),
                                                    filter_name=alert_filter_name
                                                )
                                    asyncio.create_task(watchdog_task())
                                    task_created = True
                                    task_creation_time = (time.time() - task_start_time) * 1000  # ms
                                    print(f"[AUTO-BET] ✅ Created task for alert {alert_id}, submarket {submarket_key_for_check} marked as processing (took {task_creation_time:.1f}ms)")
                                finally:
                                    # CRITICAL: Always release lock when done (even on exceptions)
                                    if auto_bet_lock.locked():
                                        auto_bet_lock.release()
                                        print(f"[AUTO-BET] [LOCK] Alert {alert_id} released lock in create_task_safely")
                            except Exception as e:
                                skip_reason = f"Exception in create_task_safely: {str(e)}"
                                print(f"[AUTO-BET] ERROR in create_task_safely for alert {alert_id}: {e}")
                                import traceback
                                traceback.print_exc()
                                if high_ev_should_trigger:
                                    store_failed_auto_bet(
                                        alert_id=alert_id,
                                        alert=alert,
                                        alert_data=alert_data,
                                        error=skip_reason,
                                        reason=f"Alert has {alert.ev_percent:.2f}% EV (>= {filter_ev_min}% threshold) but {skip_reason}",
                                        ticker=match_result.get('ticker') if match_result else None,
                                        side=side,
                                        ev_percent=alert.ev_percent,
                                        odds=alert_data.get('american_odds'),
                                        filter_name=alert_filter_name,
                                        additional_logs=[traceback.format_exc()]
                                    )
                            
                            # If task wasn't created and no skip_reason was set, something unexpected happened
                            if not task_created and not skip_reason and high_ev_should_trigger:
                                task_creation_time = (time.time() - task_start_time) * 1000  # ms
                                print(f"[AUTO-BET] [CREATE_TASK_SAFELY] ⚠️  WARNING: Task not created, no skip_reason, task_created={task_created}, skip_reason={skip_reason}, took {task_creation_time:.1f}ms")
                                print(f"[AUTO-BET] [CREATE_TASK_SAFELY] END: Alert {alert_id}, task_created={task_created}, skip_reason={skip_reason}")
                                
                                # Gather comprehensive diagnostics about why task wasn't created
                                diagnostic_logs = [
                                    f"========== TASK CREATION FAILED SILENTLY ==========",
                                    f"Alert ID: {alert_id}",
                                    f"Market: {alert.teams} - {alert.pick}",
                                    f"EV: {alert.ev_percent:.2f}%",
                                    f"Filter: {alert_filter_name}",
                                    f"Filter EV threshold: {filter_ev_min}%",
                                    f"Auto-bet enabled: {auto_bet_enabled}",
                                    f"Filter in selected_auto_bettor_filters: {alert_filter_name in selected_auto_bettor_filters}",
                                    f"Match result: {match_result}",
                                    f"Ticker: {match_result.get('ticker') if match_result else None}",
                                    f"Side: {side}",
                                    f"",
                                    f"========== TASK STATUS ==========",
                                    f"Alert ID in processing set: {alert_id in auto_bet_processing_alert_ids}",
                                    f"Submarket key: {submarket_key_for_check}",
                                    f"Submarket in auto_bet_submarkets (bet placed): {submarket_key_for_check in auto_bet_submarkets}",
                                    f"Task completed successfully: False",
                                    f"Task creation task exists: {task_creation_task is not None if 'task_creation_task' in locals() else False}",
                                    f"Task creation task running: {not task_creation_task.done() if ('task_creation_task' in locals() and task_creation_task) else 'N/A (no task creation task)'}",
                                    f"Task creation task done: {task_creation_task.done() if ('task_creation_task' in locals() and task_creation_task) else 'N/A'}",
                                    f"Verification delay: {verification_time:.1f}ms",
                                    f"",
                                    f"========== SUBMARKET STATUS ==========",
                                    f"Submarket: {submarket_key_for_check}",
                                    f"   Already bet: {submarket_key_for_check in auto_bet_submarkets}",
                                    f"   In processing: {submarket_key_for_check in auto_bet_processing_submarkets}",
                                    f"   Processing alert_id: {auto_bet_submarket_to_alert_id.get(submarket_key_for_check, 'N/A')}",
                                    f"   Task exists: {submarket_key_for_check in auto_bet_submarket_tasks}",
                                    f"   Task: {auto_bet_submarket_tasks.get(submarket_key_for_check) if submarket_key_for_check in auto_bet_submarket_tasks else 'N/A (no task)'}",
                                    f"   Task done: {auto_bet_submarket_tasks.get(submarket_key_for_check).done() if (submarket_key_for_check in auto_bet_submarket_tasks and auto_bet_submarket_tasks.get(submarket_key_for_check)) else 'N/A'}",
                                    f"",
                                    f"========== LOCK STATUS ==========",
                                    f"Lock state: locked={auto_bet_lock.locked() if auto_bet_lock else 'N/A'}, waiters={len(auto_bet_lock._waiters) if (auto_bet_lock and hasattr(auto_bet_lock, '_waiters') and auto_bet_lock._waiters is not None) else 'unknown'}",
                                    f"Lock holder: {auto_bet_lock_holder}",
                                    f"Lock held for: {(time.time() - auto_bet_lock_acquired_at) * 1000:.1f}ms" if (auto_bet_lock_acquired_at and auto_bet_lock_holder) else "Lock held for: N/A",
                                    f"Lock acquired at: {time.strftime('%H:%M:%S.%f', time.localtime(auto_bet_lock_acquired_at))}" if auto_bet_lock_acquired_at else "Lock acquired at: N/A",
                                    f"",
                                    f"========== STUCK TASK ANALYSIS ==========",
                                ]
                                
                                # Find stuck tasks
                                stuck_tasks_info = []
                                for submarket_key_stuck, task in auto_bet_submarket_tasks.items():
                                    if task and not task.done():
                                        task_age = (time.time() - (task.get_name() if hasattr(task, 'get_name') else 0)) * 1000 if hasattr(task, 'get_name') else 'unknown'
                                        stuck_tasks_info.append({
                                            'submarket': submarket_key_stuck,
                                            'task': str(task),
                                            'age_ms': task_age,
                                            'exception': task.exception() if task.done() and task.exception() else None
                                        })
                                        diagnostic_logs.append(f"   Stuck task: {submarket_key_stuck}")
                                        diagnostic_logs.append(f"      Task: {task}")
                                        diagnostic_logs.append(f"      Done: {task.done()}")
                                        if task.done() and task.exception():
                                            diagnostic_logs.append(f"      Exception: {task.exception()}")
                                
                                if not stuck_tasks_info:
                                    diagnostic_logs.append(f"✅ No stuck tasks found")
                                
                                # Check if lock holder matches a stuck task
                                if auto_bet_lock_holder and auto_bet_lock_acquired_at:
                                    lock_held_duration = (time.time() - auto_bet_lock_acquired_at) * 1000
                                    diagnostic_logs.append(f"")
                                    diagnostic_logs.append(f"🚨 LOCK HELD BY STUCK TASK:")
                                    diagnostic_logs.append(f"   Holder: {auto_bet_lock_holder}")
                                    diagnostic_logs.append(f"   Held for: {lock_held_duration:.1f}ms ({lock_held_duration/1000:.1f}s)")
                                    diagnostic_logs.append(f"   Acquired at: {time.strftime('%H:%M:%S.%f', time.localtime(auto_bet_lock_acquired_at))}")
                                    if lock_held_duration > 10000:
                                        diagnostic_logs.append(f"   ⚠️  WATCHDOG SHOULD HAVE KILLED THIS (>10s)!")
                                        diagnostic_logs.append(f"   Possible reasons:")
                                        diagnostic_logs.append(f"      - Watchdog not running")
                                        diagnostic_logs.append(f"      - Watchdog failed to cancel task")
                                        diagnostic_logs.append(f"      - Task is in uninterruptible operation")
                                
                                diagnostic_logs.append(f"")
                                diagnostic_logs.append(f"This high-EV alert should have triggered auto-bet but no task was created or task failed.")
                                diagnostic_logs.append(f"=====================================")
                                
                                store_failed_auto_bet(
                                    alert_id=alert_id,
                                    alert=alert,
                                    alert_data=alert_data,
                                    error="Task creation failed silently",
                                    reason=f"Alert has {alert.ev_percent:.2f}% EV (>= {filter_ev_min}% threshold) and passed all checks, but no task was created. alert_id not in auto_bet_processing_alert_ids after {verification_time:.1f}ms delay.",
                                    ticker=match_result.get('ticker') if match_result else None,
                                    side=side,
                                    ev_percent=alert.ev_percent,
                                    odds=alert_data.get('american_odds'),
                                    filter_name=alert_filter_name,
                                    additional_logs=diagnostic_logs
                                )
                        
                        # CRITICAL: Assign to task_creation_task so we can track it
                        task_creation_task = asyncio.create_task(create_task_safely())
                        print(f"[AUTO-BET] [TASK CREATION] ✅ Created and assigned task_creation_task for alert {alert_id} (task_id={id(task_creation_task)})")
                    else:
                        # Fallback if lock not initialized yet (shouldn't happen, but be safe)
                        print(f"[AUTO-BET] WARNING: auto_bet_lock is None - using fallback task creation")
                        task_creation_task = None  # No task creation task for fallback path
                        if high_ev_should_trigger:
                            store_failed_auto_bet(
                                alert_id=alert_id,
                                alert=alert,
                                alert_data=alert_data,
                                error="auto_bet_lock is None - using fallback path",
                                reason=f"Alert has {alert.ev_percent:.2f}% EV (>= {filter_ev_min}% threshold) but auto_bet_lock is not initialized - using fallback task creation",
                                ticker=match_result.get('ticker') if match_result else None,
                                side=side,
                                ev_percent=alert.ev_percent,
                                odds=alert_data.get('american_odds'),
                                filter_name=alert_filter_name
                            )
                        # CRITICAL: Check already bet FIRST
                        if submarket_key_for_check in auto_bet_submarkets:
                            print(f"[AUTO-BET] SKIP: Alert {alert_id} - Submarket {submarket_key_for_check} already bet, skipping duplicate task (fallback check)")
                        elif submarket_key_for_check not in auto_bet_processing_submarkets:
                            auto_bet_processing_submarkets.add(submarket_key_for_check)
                            auto_bet_submarket_to_alert_id[submarket_key_for_check] = alert_id
                            auto_bet_processing_alert_ids.add(alert_id)
                            task = asyncio.create_task(check_and_auto_bet(alert_id, alert_data, alert))
                            auto_bet_submarket_tasks[submarket_key_for_check] = task
                            print(f"[AUTO-BET] ✅ Created task for alert {alert_id} (fallback path), submarket {submarket_key_for_check} marked as processing")
                        else:
                            print(f"[AUTO-BET] SKIP: Alert {alert_id} - Submarket {submarket_key_for_check} already processing, skipping duplicate task")
                            if high_ev_should_trigger:
                                store_failed_auto_bet(
                                    alert_id=alert_id,
                                    alert=alert,
                                    alert_data=alert_data,
                                    error="Submarket already processing (fallback path)",
                                    reason=f"Alert has {alert.ev_percent:.2f}% EV (>= {filter_ev_min}% threshold) but submarket already processing in fallback path",
                                    ticker=match_result.get('ticker') if match_result else None,
                                    side=side,
                                    ev_percent=alert.ev_percent,
                                    odds=alert_data.get('american_odds'),
                                    filter_name=alert_filter_name
                                )
                else:
                    print(f"[AUTO-BET] SKIP: Alert {alert_id} already being processed, skipping duplicate task")
                    # NOTE: We don't log "already being processed" as a failure because:
                    # 1. Another task is already handling this alert (expected behavior)
                    # 2. If that task fails, it will log its own failure with full details
                    # 3. If that task succeeds, there's no failure to log
                    # This is duplicate prevention, not a failure - don't log to failed-bets
                    return
        
        # CRITICAL: Track if task was created for high-EV alerts
        # We need to verify that high-EV alerts actually get processed
        task_was_created = False
        task_creation_task = None  # Will be set if we create a task creation task
        print(f"[AUTO-BET] [VERIFICATION] Starting verification for high-EV alert {alert_id}, task_creation_task={task_creation_task}")
        if high_ev_should_trigger:
            # Check if a task was actually created by checking if alert_id is in processing set
            # This happens synchronously in create_task_safely, so we can check it
            # Give it more time for the async task to start and acquire the lock (lock might be busy)
            verification_start = time.time()
            await asyncio.sleep(0.5)  # Increased from 0.1s to 0.5s to account for lock acquisition time
            verification_time = (time.time() - verification_start) * 1000  # ms
            
            # DIAGNOSTIC: Check if create_task_safely task is still running
            task_creation_task_running = False
            task_creation_task_done = False
            if task_creation_task is not None:
                task_creation_task_running = not task_creation_task.done()
                task_creation_task_done = task_creation_task.done()
                if task_creation_task.done():
                    # Task completed - check if it had an exception
                    try:
                        task_creation_task.result()  # This will raise if there was an exception
                        print(f"[HIGH-EV TRACKING] ✅ create_task_safely task completed after {verification_time:.1f}ms")
                    except Exception as e:
                        print(f"[HIGH-EV TRACKING] ⚠️  task_creation_task completed with exception: {e}")
                else:
                    print(f"[HIGH-EV TRACKING] ⚠️  task_creation_task is still running after {verification_time:.1f}ms")
            else:
                print(f"[HIGH-EV TRACKING] ⚠️  WARNING: No task_creation_task found (fallback path or error)")
            
            # CRITICAL: Check if task was created AND if it completed successfully
            # The alert_id might not be in processing set if task completed (cleaned up in finally block)
            # Check if submarket is in auto_bet_submarkets (bet was placed) to verify success
            submarket_key_for_verification = (match_result.get('ticker', '').upper(), side.lower()) if match_result and side else None
            task_completed_successfully = False
            if submarket_key_for_verification and submarket_key_for_verification in auto_bet_submarkets:
                task_completed_successfully = True
                print(f"[HIGH-EV TRACKING] ✅ Task completed SUCCESSFULLY - submarket {submarket_key_for_verification} is in auto_bet_submarkets (bet was placed)")
            
            # CRITICAL: Check if create_task_safely is still running (waiting for lock)
            # If it's still running, it might just be waiting for the lock - that's OK, don't log as failure yet
            task_creation_still_running = False
            if task_creation_task is not None:
                task_creation_still_running = not task_creation_task.done()
                if task_creation_still_running:
                    print(f"[HIGH-EV TRACKING] ⏳ create_task_safely still running after {verification_time:.1f}ms - likely waiting for lock, will check again later")
            
            if alert_id in auto_bet_processing_alert_ids:
                task_was_created = True
                print(f"[HIGH-EV TRACKING] ✅ Task confirmed created for high-EV alert {alert_id} (verified after {verification_time:.1f}ms) - still in processing set")
            elif task_completed_successfully:
                # Task completed successfully - this is OK, don't log as failure
                task_was_created = True
                print(f"[HIGH-EV TRACKING] ✅ Task completed successfully for high-EV alert {alert_id} (verified after {verification_time:.1f}ms) - bet was placed, no failure to log")
            elif task_creation_still_running:
                # Task creation is still running (waiting for lock) - this is OK, don't log as failure
                # The task will either succeed or fail on its own, and will log appropriately
                task_was_created = True  # Mark as created since it's in progress
                print(f"[HIGH-EV TRACKING] ⏳ Task creation in progress for high-EV alert {alert_id} (still waiting for lock after {verification_time:.1f}ms) - will complete or fail on its own")
            else:
                # Check lock state for diagnostics
                lock_state = "unknown"
                lock_waiters = "unknown"
                if auto_bet_lock:
                    lock_state = f"locked={auto_bet_lock.locked()}"
                    if hasattr(auto_bet_lock, '_waiters') and auto_bet_lock._waiters is not None:
                        lock_waiters = f"waiters={len(auto_bet_lock._waiters)}"
                    else:
                        lock_waiters = "waiters=unknown"
                print(f"[HIGH-EV TRACKING] ⚠️  WARNING: High-EV alert {alert_id} should have created task but alert_id not in processing set! Lock: {lock_state}, {lock_waiters}")
                print(f"[HIGH-EV TRACKING]   Submarket key: {submarket_key_for_verification}, In auto_bet_submarkets: {submarket_key_for_verification in auto_bet_submarkets if submarket_key_for_verification else 'N/A'}")
                # Only log as failure if task didn't complete successfully
                if not task_completed_successfully:
                    # Gather comprehensive diagnostics
                    diagnostic_logs = [
                        f"========== TASK CREATION FAILED SILENTLY ==========",
                        f"Alert ID: {alert_id}",
                        f"Market: {alert.teams} - {alert.pick}",
                        f"EV: {alert.ev_percent:.2f}%",
                        f"Filter: {alert_filter_name}",
                        f"Filter EV threshold: {filter_ev_min}%",
                        f"Auto-bet enabled: {auto_bet_enabled}",
                        f"Filter in selected_auto_bettor_filters: {alert_filter_name in selected_auto_bettor_filters if alert_filter_name else False}",
                        f"",
                        f"Match result: {match_result if 'match_result' in locals() else 'N/A'}",
                        f"Ticker: {match_result.get('ticker') if match_result and 'match_result' in locals() else 'N/A'}",
                        f"Side: {side if 'side' in locals() else 'N/A'}",
                        f"",
                        f"========== TASK STATUS ==========",
                        f"Alert ID in processing set: {alert_id in auto_bet_processing_alert_ids}",
                        f"Submarket key: {submarket_key_for_verification}",
                        f"Submarket in auto_bet_submarkets (bet placed): {submarket_key_for_verification in auto_bet_submarkets if submarket_key_for_verification else 'N/A'}",
                        f"Task completed successfully: {task_completed_successfully}",
                        f"Task creation task exists: {task_creation_task is not None}",
                        f"Task creation task running: {task_creation_task_running if task_creation_task is not None else 'N/A (no task creation task)'}",
                        f"Task creation task done: {task_creation_task.done() if task_creation_task is not None else 'N/A'}",
                        f"Verification delay: {verification_time:.1f}ms",
                        f""
                    ]
                    
                    # Add submarket status
                    if submarket_key_for_verification:
                        diagnostic_logs.extend([
                            f"========== SUBMARKET STATUS ==========",
                            f"Submarket: {submarket_key_for_verification}",
                            f"   Already bet: {submarket_key_for_verification in auto_bet_submarkets}",
                            f"   In processing: {submarket_key_for_verification in auto_bet_processing_submarkets}",
                            f"   Processing alert_id: {auto_bet_submarket_to_alert_id.get(submarket_key_for_verification, 'N/A')}",
                            f"   Task exists: {submarket_key_for_verification in auto_bet_submarket_tasks}",
                        ])
                        if submarket_key_for_verification in auto_bet_submarket_tasks:
                            task = auto_bet_submarket_tasks.get(submarket_key_for_verification)
                            diagnostic_logs.append(f"   Task done: {task.done() if task else 'N/A'}")
                            diagnostic_logs.append(f"   Task: {task}")
                        else:
                            diagnostic_logs.append(f"   Task: N/A (no task)")
                        diagnostic_logs.append(f"")
                    
                    # Add lock diagnostics
                    if auto_bet_lock:
                        lock_state = auto_bet_lock.locked()
                        lock_waiters = len(auto_bet_lock._waiters) if (hasattr(auto_bet_lock, '_waiters') and auto_bet_lock._waiters is not None) else 'unknown'
                        diagnostic_logs.extend([
                            f"========== LOCK STATUS ==========",
                            f"Lock state: locked={lock_state}, waiters={lock_waiters}",
                        ])
                        
                        # Add lock holder info
                        try:
                            if auto_bet_lock_holder and auto_bet_lock_acquired_at:
                                lock_held_duration = (time.time() - auto_bet_lock_acquired_at) * 1000
                                diagnostic_logs.append(f"Lock holder: {auto_bet_lock_holder}")
                                diagnostic_logs.append(f"Lock held for: {lock_held_duration:.1f}ms")
                                diagnostic_logs.append(f"Lock acquired at: {time.strftime('%H:%M:%S.%f', time.localtime(auto_bet_lock_acquired_at))}")
                            else:
                                diagnostic_logs.append(f"Lock holder: {auto_bet_lock_holder}")
                                diagnostic_logs.append(f"Lock acquired at: {auto_bet_lock_acquired_at}")
                        except NameError:
                            diagnostic_logs.append(f"Lock holder tracking: Not available")
                        
                        # Add stuck tasks
                        stuck_tasks = []
                        for submarket_key_stuck, task in auto_bet_submarket_tasks.items():
                            if task and not task.done():
                                stuck_tasks.append(f"{submarket_key_stuck}: {task}")
                        if stuck_tasks:
                            diagnostic_logs.append(f"")
                            diagnostic_logs.append(f"⚠️  Found {len(stuck_tasks)} potentially stuck task(s):")
                            for stuck_task in stuck_tasks[:5]:  # Show first 5
                                diagnostic_logs.append(f"   - {stuck_task}")
                        diagnostic_logs.append(f"")
                    
                    diagnostic_logs.extend([
                        f"",
                        f"This high-EV alert should have triggered auto-bet but no task was created or task failed.",
                        f"====================================="
                    ])
                    
                    # Log this as a failure - task should have been created but wasn't
                    store_failed_auto_bet(
                        alert_id=alert_id,
                        alert=alert,
                        alert_data=alert_data if 'alert_data' in locals() else {},
                        error="Task creation failed silently",
                        reason=f"Alert has {alert.ev_percent:.2f}% EV (>= {filter_ev_min}% threshold) and passed all checks, but no task was created. alert_id not in auto_bet_processing_alert_ids after 500ms delay.",
                        ticker=match_result.get('ticker') if match_result and 'match_result' in locals() else None,
                        side=side if 'side' in locals() else None,
                        ev_percent=alert.ev_percent,
                        odds=alert_data.get('american_odds') if 'alert_data' in locals() else getattr(alert, 'odds', 'N/A'),
                        filter_name=alert_filter_name,
                        additional_logs=diagnostic_logs
                    )
    
    except Exception as e:
        print(f"Error handling alert: {e}")
        import traceback
        traceback.print_exc()
        # Log high-EV alerts that error out
        if 'high_ev_should_trigger' in locals() and high_ev_should_trigger:
            store_failed_auto_bet(
                alert_id=alert_id if 'alert_id' in locals() else 'unknown',
                alert=alert,
                alert_data=alert_data if 'alert_data' in locals() else {},
                error=f"Exception in handle_new_alert: {str(e)}",
                reason=f"Alert has {alert.ev_percent:.2f}% EV (>= {filter_ev_min if 'filter_ev_min' in locals() else 'unknown'}% threshold) but exception occurred during processing: {str(e)}",
                ticker=None,
                side=None,
                ev_percent=alert.ev_percent,
                odds=getattr(alert, 'odds', 'N/A'),
                filter_name=alert_filter_name if 'alert_filter_name' in locals() else None
            )


async def heartbeat_task():
    """Background task that sends Telegram heartbeat every 30 minutes"""
    import time
    global monitor_running
    while monitor_running:
        try:
            await asyncio.sleep(1800)  # 30 minutes = 1800 seconds
            
            # Get current balance and positions value
            cash_balance_dollars = 0
            positions_value_dollars = 0
            portfolio_value_dollars = 0
            status = "Running"
            
            try:
                portfolio = await kalshi_client.get_portfolio()
                if portfolio:
                    # Portfolio data structure: {'value': {'a': cash_cents, 'v': positions_value_cents, ...}}
                    value_data = portfolio.get('value', {})
                    if not value_data:
                        # Fallback: check if data is at root level (different API response format)
                        cash_cents = portfolio.get('balance', 0) or 0
                        positions_cents = portfolio.get('portfolio_value', 0) or 0
                    else:
                        # Standard nested format
                        cash_cents = value_data.get('a', 0) or 0  # Available cash
                        positions_cents = value_data.get('v', 0) or 0  # Positions value
                    
                    cash_balance_dollars = cash_cents / 100.0 if cash_cents else 0
                    positions_value_dollars = positions_cents / 100.0 if positions_cents else 0
                    portfolio_value_dollars = cash_balance_dollars + positions_value_dollars
                    
                    # If portfolio returned 0, try fallback: get balance and positions separately
                    if cash_balance_dollars == 0 and positions_value_dollars == 0:
                        print(f"[HEARTBEAT] ⚠️  Portfolio returned 0 values, trying fallback methods...")
                        try:
                            # Try getting balance directly
                            balance_data = await kalshi_client.get_portfolio_balance_fallback()
                            if balance_data:
                                cash_cents = balance_data.get('balance', 0) or balance_data.get('balance_cents', 0) or 0
                                positions_cents = balance_data.get('portfolio_value', 0) or 0
                                cash_balance_dollars = cash_cents / 100.0 if cash_cents else 0
                                positions_value_dollars = positions_cents / 100.0 if positions_cents else 0
                                portfolio_value_dollars = cash_balance_dollars + positions_value_dollars
                                print(f"[HEARTBEAT] ✅ Fallback balance fetch successful: Cash=${cash_balance_dollars:.2f}, Positions=${positions_value_dollars:.2f}")
                        except Exception as fallback_e:
                            print(f"[HEARTBEAT] ⚠️  Fallback balance fetch also failed: {fallback_e}")
                else:
                    print(f"[HEARTBEAT] ⚠️  get_portfolio() returned None - trying fallback...")
                    try:
                        # Try fallback method
                        balance_data = await kalshi_client.get_portfolio_balance_fallback()
                        if balance_data:
                            cash_cents = balance_data.get('balance', 0) or balance_data.get('balance_cents', 0) or 0
                            positions_cents = balance_data.get('portfolio_value', 0) or 0
                            cash_balance_dollars = cash_cents / 100.0 if cash_cents else 0
                            positions_value_dollars = positions_cents / 100.0 if positions_cents else 0
                            portfolio_value_dollars = cash_balance_dollars + positions_value_dollars
                            print(f"[HEARTBEAT] ✅ Fallback balance fetch successful: Cash=${cash_balance_dollars:.2f}, Positions=${positions_value_dollars:.2f}")
                        else:
                            status = "Error: No portfolio data"
                    except Exception as fallback_e:
                        print(f"[HEARTBEAT] ⚠️  Fallback also failed: {fallback_e}")
                        status = "Error: No portfolio data"
            except Exception as e:
                print(f"[HEARTBEAT] ❌ Error fetching portfolio: {e}")
                import traceback
                traceback.print_exc()
                status = f"Error: {str(e)[:50]}"
            
            # Get warm cache ticker count
            warm_tickers_count = len(kalshi_client.warm_cache_tickers) if kalshi_client else 0
            
            # Format message
            message = f"""🤖 <b>Kalshi Bot Status</b>

💰 Cash Balance: ${cash_balance_dollars:.2f}
📊 Positions Value: ${positions_value_dollars:.2f}
💼 Portfolio Value: ${portfolio_value_dollars:.2f}
🟢 Status: {status}
🔥 Warm Tickers: {warm_tickers_count}
⏰ {time.strftime('%Y-%m-%d %H:%M:%S')}"""
            
            send_telegram_message(message)
            print(f"[HEARTBEAT] Sent status: Cash=${cash_balance_dollars:.2f}, Positions=${positions_value_dollars:.2f}, Portfolio=${portfolio_value_dollars:.2f}, Status={status}")
        except Exception as e:
            print(f"[HEARTBEAT] Error in heartbeat task: {e}")
            # Continue running even if one heartbeat fails
            await asyncio.sleep(60)  # Wait 1 minute before retrying


async def monitor_status_heartbeat():
    """Log monitor status every 60 seconds: active filters, last poll time, auto-bet readiness."""
    global odds_ev_monitors, selected_auto_bettor_filters, auto_bet_enabled, monitor_running
    await asyncio.sleep(15)  # First status after 15s so "we're live" appears quickly; then every 60s
    while monitor_running:
        try:
            parts = []
            auto_bet_filters_with_running = []
            for filter_name, monitor in list(odds_ev_monitors.items()):
                if not getattr(monitor, 'running', False):
                    continue
                last = getattr(monitor, 'last_poll_time', None)
                last_str = datetime.fromtimestamp(last).strftime("%H:%M:%S") if last else "never"
                parts.append(f"{filter_name} (last poll {last_str})")
                if filter_name in (selected_auto_bettor_filters or []):
                    auto_bet_filters_with_running.append(filter_name)
            filters_line = ", ".join(parts) if parts else "(no monitors running)"
            ready = auto_bet_enabled and bool(auto_bet_filters_with_running)
            auto_bet_line = "ON, ready" if ready else ("ON, no auto-bet filters" if auto_bet_enabled else "OFF")
            print(f"[MONITOR STATUS] Active filters: {filters_line} | Auto-bet: {auto_bet_line}")
            sys.stdout.flush()
        except Exception as e:
            print(f"[MONITOR STATUS] Error: {e}")
        await asyncio.sleep(60)


def run_api_loop():
    """Run a dedicated event loop for dashboard/portfolio Kalshi API calls only.
    This ensures the monitor loop is NEVER used for page loads or API calls,
    so monitoring and auto-betting are never interrupted."""
    global api_kalshi_client, api_loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        api_kalshi_client = KalshiClient()
        loop.run_until_complete(api_kalshi_client.init())
        api_loop = loop
        print("[API LOOP] Dedicated Kalshi API loop started - portfolio/display calls will not touch monitor")
        loop.run_forever()
    except Exception as e:
        print(f"[API LOOP] Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if api_loop:
            api_loop.close()


def run_monitor_loop():
    """Run the monitor loop in a separate thread"""
    global monitor_running, monitor_loop
    # Flush immediately so we see thread output (stdout can be buffered when not a TTY, e.g. systemd)
    print("[MONITOR THREAD] run_monitor_loop() entered")
    sys.stdout.flush()
    sys.stderr.flush()

    async def async_monitor():
        global odds_ev_monitor, monitor_running, kalshi_client, monitor_loop, positions_loaded
        
        # Initialize the auto-bet lock in this event loop
        global auto_bet_lock
        auto_bet_lock = asyncio.Lock()
        
        monitor_running = True
        print(f"[MONITOR] monitor_running set to True")
        
        # CRITICAL: Initialize Kalshi client session in THIS event loop BEFORE exposing monitor_loop.
        # If we set monitor_loop first, Flask could schedule portfolio/etc on this loop while
        # kalshi_client.session still belongs to a different (dead) loop -> "attached to a different loop".
        print("[MONITOR] Initializing Kalshi client session...")
        await kalshi_client.init()
        print("[MONITOR] Kalshi client session initialized")
        # Now safe for Flask to use this loop for Kalshi calls
        monitor_loop = asyncio.get_running_loop()
        # DISABLED: Proactive warm cache loop - we only warm tickers when alerts come in
        # This prevents warming random/old tickers that aren't relevant
        # Warm cache now happens automatically in handle_new_alert() when alerts are received
        # asyncio.create_task(kalshi_client.warm_cache_loop())
        print("[MONITOR] Warm cache: Disabled proactive loop - will only warm tickers when alerts arrive")
        
        # Start heartbeat task (Telegram status every 30 minutes)
        asyncio.create_task(heartbeat_task())
        # Start monitor status heartbeat (log active filters + last poll + auto-bet readiness every 60s)
        asyncio.create_task(monitor_status_heartbeat())
        
        # CRITICAL: Start global lock watchdog to detect and kill stuck locks
        async def global_lock_watchdog():
            """Global watchdog that checks for stuck locks every 5 seconds and force-releases them"""
            global auto_bet_lock_holder, auto_bet_lock_acquired_at  # CRITICAL: Declare global
            while True:
                await asyncio.sleep(5.0)  # Check every 5 seconds
                try:
                    if auto_bet_lock and auto_bet_lock.locked() and auto_bet_lock_holder and auto_bet_lock_acquired_at:
                        lock_held_duration = (time.time() - auto_bet_lock_acquired_at) * 1000
                        if lock_held_duration > 2000:  # Lock held >2 seconds (user requirement: <2s or dead)
                            print(f"[AUTO-BET] 🚨🚨🚨 GLOBAL WATCHDOG: Lock held for {lock_held_duration:.1f}ms (>10s) by {auto_bet_lock_holder} - FORCING RELEASE!")
                            try:
                                # CRITICAL: Save lock holder BEFORE clearing it (so we can find the task)
                                stuck_lock_holder = auto_bet_lock_holder
                                stuck_lock_acquired_at = auto_bet_lock_acquired_at
                                
                                # Force release the lock FIRST
                                if auto_bet_lock.locked():
                                    auto_bet_lock.release()
                                    print(f"[AUTO-BET] 🚨🚨🚨 GLOBAL WATCHDOG: FORCED LOCK RELEASE - lock is now free")
                                
                                # Clear lock holder tracking
                                auto_bet_lock_holder = None
                                auto_bet_lock_acquired_at = None
                                
                                # Try to cancel the stuck task
                                # Find the task that matches the lock holder
                                task_cancelled = False
                                for submarket_key_stuck, task in auto_bet_submarket_tasks.items():
                                    if task and not task.done():
                                        # Extract alert_id from lock holder string (format: "Alert 12345 ((ticker, side))")
                                        if stuck_lock_holder:
                                            # Check if submarket_key matches the lock holder
                                            if str(submarket_key_stuck) in str(stuck_lock_holder):
                                                print(f"[AUTO-BET] 🚨🚨🚨 GLOBAL WATCHDOG: Cancelling stuck task for {submarket_key_stuck}")
                                                task.cancel()
                                                task_cancelled = True
                                                # Clean up tracking
                                                auto_bet_processing_submarkets.discard(submarket_key_stuck)
                                                auto_bet_submarket_to_alert_id.pop(submarket_key_stuck, None)
                                                auto_bet_submarket_tasks.pop(submarket_key_stuck, None)
                                                if submarket_key_stuck in auto_bet_submarkets:
                                                    auto_bet_submarkets.discard(submarket_key_stuck)
                                                break
                                
                                if not task_cancelled:
                                    print(f"[AUTO-BET] 🚨🚨🚨 GLOBAL WATCHDOG: Could not find matching task for lock holder: {stuck_lock_holder}")
                                    # Try to cancel ALL stuck tasks as fallback
                                    stuck_count = 0
                                    for submarket_key_stuck, task in list(auto_bet_submarket_tasks.items()):
                                        if task and not task.done():
                                            print(f"[AUTO-BET] 🚨🚨🚨 GLOBAL WATCHDOG: Cancelling ALL stuck task: {submarket_key_stuck}")
                                            task.cancel()
                                            stuck_count += 1
                                            auto_bet_processing_submarkets.discard(submarket_key_stuck)
                                            auto_bet_submarket_to_alert_id.pop(submarket_key_stuck, None)
                                            auto_bet_submarket_tasks.pop(submarket_key_stuck, None)
                                    if stuck_count > 0:
                                        print(f"[AUTO-BET] 🚨🚨🚨 GLOBAL WATCHDOG: Cancelled {stuck_count} stuck task(s) as fallback")
                            except Exception as e:
                                print(f"[AUTO-BET] 🚨🚨🚨 GLOBAL WATCHDOG ERROR: {e}")
                                import traceback
                                traceback.print_exc()
                except Exception as e:
                    print(f"[AUTO-BET] GLOBAL WATCHDOG ERROR: {e}")
        
        asyncio.create_task(global_lock_watchdog())
        print("[MONITOR] Background tasks started (including global lock watchdog)")
        
        print("Kalshi client session initialized in monitor loop")
        
        # Load existing positions BEFORE starting monitors (needed for reverse middles, event maxes).
        # Use a timeout so we don't hang forever when API is slow or returns 0 positions slowly.
        print("[MONITOR] About to load existing positions...")
        sys.stdout.flush()
        try:
            print("[MONITOR] Calling load_existing_positions_to_tracking (45s timeout)...")
            sys.stdout.flush()
            await asyncio.wait_for(load_existing_positions_to_tracking(), timeout=45.0)
            print("[MONITOR] Existing positions loaded successfully")
        except asyncio.TimeoutError:
            print("[MONITOR] Position load timed out (45s); proceeding with positions_loaded=True so monitors can start")
            positions_loaded = True  # global set above
        except Exception as e:
            print(f"[MONITOR] ERROR loading positions: {e}")
            import traceback
            traceback.print_exc()
            positions_loaded = True  # Allow monitors to start; position check loop will retry
        
        # Start all selected monitors
        print("[MONITOR] About to start all selected monitors...")
        global odds_ev_monitors, selected_dashboard_filters, selected_auto_bettor_filters
        print(f"[MONITOR] selected_dashboard_filters={selected_dashboard_filters}")
        print(f"[MONITOR] selected_auto_bettor_filters={selected_auto_bettor_filters}")
        # AUDIT: Log each filter's config so we can verify CBB vs Kalshi 3 sports
        for fn in list(set(selected_dashboard_filters + selected_auto_bettor_filters)):
            payload = saved_filters.get(fn, {})
            leagues = payload.get('leagues', [])
            devig = payload.get('devigFilter', {})
            min_ev = devig.get('minEv', 'N/A')
            min_sharps = devig.get('minSharpBooks', 'N/A')
            min_roi = payload.get('minRoi', 'N/A')
            print(f"[MONITOR] FILTER AUDIT: name={fn} | leagues={leagues} | minRoi={min_roi} | devig.minEv={min_ev} | minSharpBooks={min_sharps}")
        monitors_to_start = []
        
        # Combine dashboard and auto-bettor filters (deduplicated)
        all_selected_filters = list(set(selected_dashboard_filters + selected_auto_bettor_filters))
        print(f"[MONITOR] all_selected_filters={all_selected_filters}")
        
        # Start monitors for all selected filters (dashboard + auto-bettor)
        for filter_name in all_selected_filters:
            if filter_name in odds_ev_monitors:
                monitor = odds_ev_monitors[filter_name]
                monitors_to_start.append((filter_name, monitor))
            else:
                # Create monitor if it doesn't exist
                filter_payload = saved_filters.get(filter_name)
                if filter_payload:
                    monitor = EvMonitorImpl(auth_token=None)
                    monitor.set_filter(filter_payload)
                    monitor.poll_interval = monitor_poll_seconds()
                    odds_ev_monitors[filter_name] = monitor
                    monitors_to_start.append((filter_name, monitor))
        
        # If no monitors selected, that's OK - user may have intentionally disabled all filters
        # Don't start any monitors if none are selected
        if not monitors_to_start:
            print(f"No filters selected - no monitors will be started (dashboard: {selected_dashboard_filters}, auto-bettor: {selected_auto_bettor_filters})")
        
        # Start all monitors (only if monitor_running is True)
        started_monitors = []  # Track which monitors were successfully started (defined outside if block)
        print(f"[MONITOR] About to start monitors: monitor_running={monitor_running}, monitors_to_start={len(monitors_to_start)}")

        async def handle_removed_alerts(removed_hashes):
            """When Odds-API stops listing an alert, remove matching cards from the dashboard."""
            to_remove = []
            for alert_id, alert_data in list(active_alerts.items()):
                event_ticker = alert_data.get("event_ticker", "") or alert_data.get("ticker", "")
                alert_hash = f"{event_ticker}|{alert_data.get('pick', '')}|{alert_data.get('qualifier', '')}|{alert_data.get('odds', '')}"
                if alert_hash in removed_hashes:
                    to_remove.append(alert_id)
            if len(to_remove) >= len(active_alerts) * 0.5 or len(removed_hashes) >= len(active_alerts):
                all_ids = list(active_alerts.keys())
                active_alerts.clear()
                for alert_id in all_ids:
                    socketio.emit("remove_alert", {"id": str(alert_id)})
                print(f"Cleared all {len(all_ids)} alerts (feed empty or bulk remove)")
            else:
                for alert_id in to_remove:
                    del active_alerts[alert_id]
                    socketio.emit("remove_alert", {"id": str(alert_id)})
                    print(f"Removed alert (disappeared from feed): {alert_id}")

        if monitor_running:
            for filter_name, monitor in monitors_to_start:
                print(f"[MONITOR] Attempting to start monitor for filter: {filter_name}")
                success = await monitor.start()
                print(f"[MONITOR] monitor.start() returned: {success}, monitor.running={monitor.running}")
                if not success:
                    print(f"❌ Failed to start monitor for filter: {filter_name}")
                else:
                    print(f"✅ Started monitor for filter: {filter_name}")
                    started_monitors.append((filter_name, monitor))
                    print(f"[DEBUG] ✅ Added {filter_name} to started_monitors (total: {len(started_monitors)})")
                    # Create a wrapper callback that tags alerts with filter_name
                    # Use a lambda with default argument to capture filter_name correctly
                    async def filtered_callback(alert: EvAlert, fn=filter_name):
                        # CRITICAL: Check if this filter is still selected before processing
                        # This prevents alerts from being processed after filter is deselected
                        global selected_dashboard_filters, selected_auto_bettor_filters
                        all_selected = set(selected_dashboard_filters + selected_auto_bettor_filters)
                        if fn not in all_selected:
                            print(f"[MONITOR] SKIP: Filter '{fn}' no longer selected, ignoring alert: {alert.teams} - {alert.pick}")
                            return
                        
                        # Tag alert with filter name
                        alert.filter_name = fn
                        await handle_new_alert(alert)
                    
                    # Add callback for new alerts (tagged with filter_name)
                    monitor.add_alert_callback(filtered_callback)
                    
                    # Create a wrapper callback for updated alerts that tags them with filter_name
                    async def filtered_updated_callback(alert: EvAlert, fn=filter_name):
                        # CRITICAL: Check if this filter is still selected before processing
                        global selected_dashboard_filters, selected_auto_bettor_filters
                        all_selected = set(selected_dashboard_filters + selected_auto_bettor_filters)
                        if fn not in all_selected:
                            return  # Skip if filter no longer selected
                        
                        # Tag alert with filter name (CRITICAL: must tag before create_alert_id)
                        alert.filter_name = fn
                        await handle_updated_alert(alert)
                    
                    # Register updated alert callback on THIS monitor (not just legacy)
                    monitor.updated_alert_callbacks.append(filtered_updated_callback)
                    monitor.removed_alert_callbacks.append(handle_removed_alerts)
        else:
            print("[MONITOR] ⚠️ monitor_running is False - monitors will NOT start")
        
        # Register callback for updated alerts (same alert, new EV/liquidity)
        # NOTE: This is a fallback for legacy monitor - per-filter monitors have their own callbacks above
        async def handle_updated_alert(alert: EvAlert):
            """Handle an alert that was updated (same hash, new EV/liquidity)"""
            global active_alerts
            
            # Find existing alert by hash
            alert_id = create_alert_id(alert)
            
            if alert_id in active_alerts:
                # Update existing alert data
                alert_data = active_alerts[alert_id]
                alert_data['ev_percent'] = alert.ev_percent
                alert_data['expected_profit'] = alert.expected_profit
                alert_data['liquidity'] = alert.liquidity
                alert_data['odds'] = alert.odds
                alert_data['book_price'] = alert.book_price
                alert_data['fair_odds'] = alert.fair_odds
                alert_data['display_books'] = getattr(alert, 'display_books', {})
                alert_data['devig_books'] = getattr(alert, 'devig_books', [])
                if hasattr(alert, 'strict_pass'):
                    alert_data['strict_pass'] = alert.strict_pass
                # Update last_seen timestamp for stale alert detection
                alert_data['last_seen'] = time.time()
                # CRITICAL: Preserve filter_name - use existing if new alert doesn't have it, otherwise update
                if hasattr(alert, 'filter_name') and alert.filter_name:
                    alert_data['filter_name'] = alert.filter_name
                # If new alert doesn't have filter_name, preserve the existing one (don't overwrite with None)
                
                # Update price if available
                if hasattr(alert, 'price_cents') and alert.price_cents:
                    alert_data['price_cents'] = alert.price_cents
                    # Convert to American odds
                    alert_data['american_odds'] = price_to_american_odds(alert.price_cents)
                    alert_data['book_price'] = alert_data['american_odds']
                
                # Filter by dashboard min EV before emitting update
                global dashboard_min_ev
                show_update = alert.ev_percent >= dashboard_min_ev or not getattr(alert, 'strict_pass', True)
                if show_update:
                    # Check if values actually changed (avoid logging every refresh)
                    old_ev = alert_data.get('ev_percent', 0)
                    old_liq = alert_data.get('liquidity', 0)
                    ev_changed = abs(old_ev - alert.ev_percent) > 0.01
                    liq_changed = abs(old_liq - alert.liquidity) > 0.01
                    
                    # Emit update to frontend (always emit for real-time updates, but only log if changed)
                    socketio.emit('alert_update', {
                        'id': alert_id,
                        'ev_percent': alert.ev_percent,
                        'expected_profit': alert.expected_profit,
                        'liquidity': alert.liquidity,
                        'odds': alert.odds,
                        'book_price': alert_data.get('book_price', alert.book_price),
                        'american_odds': alert_data.get('american_odds', 'N/A'),
                        'price_cents': alert_data.get('price_cents', 0),
                        'filter_name': alert_data.get('filter_name', getattr(alert, 'filter_name', None)),  # CRITICAL: Preserve filter_name from alert_data
                        'display_books': alert_data.get('display_books', getattr(alert, 'display_books', {})),
                        'sharp_books': alert_data.get('sharp_books', []),  # Preserve sharp books from filter
                        'devig_books': alert_data.get('devig_books', getattr(alert, 'devig_books', [])),
                        'strict_pass': alert_data.get('strict_pass', getattr(alert, 'strict_pass', True)),
                    })
                    # Only log if EV or liquidity actually changed (not just reappearing)
                    if ev_changed or liq_changed:
                        print(f"Updated alert: {alert.teams} - {alert.pick} (EV: {alert.ev_percent:.2f}%)")
                else:
                    # EV dropped below threshold — remove unless diagnostic display candidate
                    if getattr(alert, 'strict_pass', True) and alert_id in active_alerts:
                        del active_alerts[alert_id]
                        socketio.emit('remove_alert', {'id': alert_id})
                        print(f"Removed alert (EV {alert.ev_percent:.2f}% < min {dashboard_min_ev:.2f}%): {alert.teams} - {alert.pick}")
                
                # CRITICAL: Re-check auto-bet criteria when alert updates (EV/odds may have changed)
                # This ensures alerts that move into range get bet automatically
                # BUT: Only create task if not already processing this alert_id (prevents race conditions)
                # ALSO: Check if submarket is already being processed (prevents duplicate tasks for same submarket with different alert IDs)
                # CRITICAL: Use lock to make check-and-mark atomic - mark submarket as processing HERE to prevent duplicate tasks
                ticker = alert_data.get('ticker') or ''
                side = alert_data.get('side') or ''
                submarket_key_for_check = (ticker.upper() if ticker else '', side.lower() if side else '')
                if submarket_key_for_check[0] and submarket_key_for_check[1]:
                    # CRITICAL: Use lock to atomically check AND mark submarket as processing
                    # This prevents race conditions where two tasks both see "not processing" and both create tasks
                    # We mark it here so that if another task checks before this task runs, it will see it's already processing
                    if auto_bet_lock:
                        async def create_task_safely():
                            async with auto_bet_lock:
                                # CRITICAL: Check if already bet FIRST (most important check)
                                # This prevents reappearing alerts from creating duplicate tasks
                                if submarket_key_for_check in auto_bet_submarkets:
                                    print(f"[AUTO-BET] SKIP: Alert {alert_id} - Submarket {submarket_key_for_check} already bet, skipping duplicate task (reappear protection)")
                                    # CRITICAL: Log high-EV alerts (>= 10%) that are blocked because submarket was already bet
                                    # This helps identify when alerts increase above threshold after initial bet
                                    alert_ev = alert_data.get('ev_percent', 0) if alert_data else (alert.ev_percent if alert else 0)
                                    if alert_ev >= 10.0:
                                        alert_filter_name = alert_data.get('filter_name') if alert_data else (getattr(alert, 'filter_name', None) if alert else None)
                                        store_failed_auto_bet(
                                            alert_id=alert_id,
                                            alert=alert,
                                            alert_data=alert_data if alert_data else {},
                                            error="Submarket already bet - duplicate protection",
                                            reason=f"Alert has {alert_ev:.2f}% EV (>= 10.0% threshold) but submarket {submarket_key_for_check} was already bet. This may be an alert that increased above threshold after initial bet.",
                                            ticker=ticker if ticker else None,
                                            side=side if side else None,
                                            ev_percent=alert_ev,
                                            odds=alert_data.get('american_odds') if alert_data else 'N/A',
                                            filter_name=alert_filter_name
                                        )
                                    return
                                
                                # CRITICAL: Check if alert_id is already being processed (within lock to prevent race condition)
                                # This must be checked INSIDE the lock, not outside
                                if alert_id in auto_bet_processing_alert_ids:
                                    print(f"[AUTO-BET] SKIP: Alert {alert_id} already being processed, skipping duplicate task")
                                    return
                                
                                # Check if submarket is already being processed
                                if submarket_key_for_check in auto_bet_processing_submarkets:
                                    # Check if the task is still running - if not, clean up and allow this one
                                    existing_task = auto_bet_submarket_tasks.get(submarket_key_for_check)
                                    if existing_task and not existing_task.done():
                                        print(f"[AUTO-BET] SKIP: Alert {alert_id} - Submarket {submarket_key_for_check} already being processed (task still running), skipping duplicate task")
                                        return
                                    else:
                                        # Task is done or doesn't exist - clean up and allow this one
                                        print(f"[AUTO-BET] WARNING: Submarket {submarket_key_for_check} marked as processing but task is done/missing - cleaning up and allowing new task")
                                        auto_bet_processing_submarkets.discard(submarket_key_for_check)
                                        auto_bet_submarket_to_alert_id.pop(submarket_key_for_check, None)
                                        auto_bet_submarket_tasks.pop(submarket_key_for_check, None)
                                        # Continue to create new task below
                                # CRITICAL: Mark submarket as processing HERE (within lock) to prevent duplicate tasks
                                # Also track which alert_id is processing it so we can verify in check_and_auto_bet
                                auto_bet_processing_submarkets.add(submarket_key_for_check)
                                auto_bet_submarket_to_alert_id[submarket_key_for_check] = alert_id
                                # Mark alert_id as processing (prevents duplicate tasks for same alert)
                                auto_bet_processing_alert_ids.add(alert_id)
                                # Create task and track it so we can verify if it's still running
                                task = asyncio.create_task(check_and_auto_bet(alert_id, alert_data, alert))
                                auto_bet_submarket_tasks[submarket_key_for_check] = task
                                print(f"[AUTO-BET] Created task for alert {alert_id}, submarket {submarket_key_for_check} marked as processing")
                        
                        asyncio.create_task(create_task_safely())
                    else:
                        # Fallback if lock not initialized yet (shouldn't happen, but be safe)
                        if alert_id not in auto_bet_processing_alert_ids:
                            if submarket_key_for_check not in auto_bet_processing_submarkets and submarket_key_for_check not in auto_bet_submarkets:
                                auto_bet_processing_submarkets.add(submarket_key_for_check)
                                auto_bet_submarket_to_alert_id[submarket_key_for_check] = alert_id
                                auto_bet_processing_alert_ids.add(alert_id)
                                task = asyncio.create_task(check_and_auto_bet(alert_id, alert_data, alert))
                                auto_bet_submarket_tasks[submarket_key_for_check] = task
                            else:
                                print(f"[AUTO-BET] SKIP: Alert {alert_id} - Submarket {submarket_key_for_check} already processing/bet, skipping duplicate task")
                        else:
                            print(f"[AUTO-BET] SKIP: Alert {alert_id} already being processed, skipping duplicate task")
                else:
                    print(f"[AUTO-BET] SKIP: Alert {alert_id} - Invalid submarket key: {submarket_key_for_check}")
                    # Log this as a failure - side determination failed
                    if high_ev_should_trigger:
                        filter_ev_min = auto_bet_settings_by_filter.get(alert_filter_name, {}).get('ev_min', auto_bet_ev_min) if alert_filter_name else auto_bet_ev_min
                        if alert.ev_percent >= filter_ev_min:
                            store_failed_auto_bet(
                                alert_id=alert_id,
                                alert=alert,
                                alert_data=alert_data,
                                error="Side determination failed - invalid submarket key",
                                reason=f"Alert has {alert.ev_percent:.2f}% EV (>= {filter_ev_min}% threshold) but side determination failed. Ticker: '{ticker}', Side: '{side}'. This usually means the market subtitles are N/A and ticker suffix matching failed.",
                                ticker=ticker if ticker else None,
                                side=side if side else None,
                                ev_percent=alert.ev_percent,
                                odds=alert_data.get('american_odds'),
                                filter_name=alert_filter_name
                            )
            else:
                # Alert not in active_alerts - treat as new (it disappeared and came back)
                # This ensures perfect mirroring — if the monitor still has it, show it
                # Only log if it's actually reappearing (was removed, now back)
                # Don't log if it's just the first time we see it
                await handle_new_alert(alert)
        
        # Start stale alert cleanup loop - remove alerts that haven't been seen in 5+ seconds
        async def stale_alert_cleanup_loop():
            """Periodically remove alerts that haven't been updated in 5+ seconds"""
            await asyncio.sleep(5)  # Wait 5 seconds before first check
            while True:
                try:
                    await asyncio.sleep(2)  # Check every 2 seconds
                    current_time = time.time()
                    stale_threshold = 5.0  # Remove alerts not seen in 5 seconds
                    to_remove = []
                    
                    for alert_id, alert_data in list(active_alerts.items()):
                        last_seen = alert_data.get('last_seen', 0)
                        if current_time - last_seen > stale_threshold:
                            to_remove.append(alert_id)
                    
                    if to_remove:
                        for alert_id in to_remove:
                            del active_alerts[alert_id]
                            socketio.emit('remove_alert', {'id': str(alert_id)})
                        print(f"[STALE CLEANUP] Removed {len(to_remove)} stale alert(s) (not seen in {stale_threshold}s)")
                except Exception as e:
                    print(f"[STALE CLEANUP] Warning: Error in stale alert cleanup: {e}")
                    await asyncio.sleep(2)
        
        # Start stale alert cleanup as background task
        asyncio.create_task(stale_alert_cleanup_loop())
        print("[STALE CLEANUP] Stale alert cleanup loop started")
        
        # Start position check loop as a background task in the same event loop
        async def position_check_loop():
            """Periodically check positions and update CSV with win/loss results
            Also serves as fallback if WebSocket position updates don't work.
            """
            await asyncio.sleep(60)  # Wait 60 seconds before first check
            while True:
                try:
                    await asyncio.sleep(60)  # Check every 60 seconds
                    # Check for new positions and update tracking (silently)
                    positions_before = len(auto_bet_submarkets)
                    # Call load function silently (it will only log on initial load or if positions change)
                    await load_existing_positions_to_tracking()
                    positions_after = len(auto_bet_submarkets)
                    if positions_after != positions_before:
                        print(f"[POSITION CHECK] Positions changed: {positions_after} (was {positions_before})")
                    await check_and_update_bet_results()
                except Exception as e:
                    print(f"[POSITION CHECK] Warning: Error in position check loop: {e}")
                    await asyncio.sleep(60)
        
        # Start position check as background task
        asyncio.create_task(position_check_loop())
        print("[POSITION CHECK] Position check loop started in monitor event loop")
        
        # Run monitor loops for all selected monitors in parallel
        async def run_all_monitors():
            """Run all monitor loops in parallel"""
            print(f"[MONITOR] ========================================")
            print(f"[MONITOR] run_all_monitors() called")
            print(f"[MONITOR] started_monitors has {len(started_monitors)} monitor(s)")
            tasks = []
            # Use started_monitors (monitors that were successfully started) instead of monitors_to_start
            for filter_name, monitor in started_monitors:
                print(f"[MONITOR] Checking monitor '{filter_name}': monitor.running={monitor.running}")
                if monitor.running:
                    print(f"[MONITOR] Creating task for monitor loop: {filter_name}")
                    task = asyncio.create_task(monitor.monitor_loop())
                    tasks.append(task)
                    print(f"✅ Started monitor loop task for filter: {filter_name}")
                else:
                    print(f"❌ [WARNING] Monitor {filter_name} is not running, skipping monitor loop")
            
            if tasks:
                # Wait for all monitors to complete (they run indefinitely)
                print(f"[MONITOR] ========================================")
                print(f"[MONITOR] 🚀 Running {len(tasks)} monitor loop(s) in parallel...")
                print(f"[MONITOR] ========================================")
                await asyncio.gather(*tasks)
            else:
                print(f"[MONITOR] ❌ No monitors to run - started_monitors={len(started_monitors)}, tasks={len(tasks)}")
                print(f"[MONITOR] This means monitors are not polling for alerts!")
        
        print(f"[MONITOR] About to call run_all_monitors() (monitors will poll for alerts; status heartbeat every 60s)...")
        await run_all_monitors()
    
    # Run async function
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        print("[MONITOR THREAD] Starting async_monitor()...")
        sys.stdout.flush()
        sys.stderr.flush()
        loop.run_until_complete(async_monitor())
    except Exception as e:
        print(f"[MONITOR THREAD] CRITICAL ERROR in async_monitor(): {e}")
        import traceback
        traceback.print_exc()
        monitor_running = False


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    # If already authenticated, redirect to dashboard
    if session.get('authenticated'):
        return redirect('/')
    
    # Handle form submission
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        
        if check_auth(username, password):
            # Set session
            session['authenticated'] = True
            session['username'] = username
            return redirect('/')
        else:
            return render_template('login.html', error='Invalid username or password')
    
    return render_template('login.html')

@app.route('/')
@requires_auth
def index():
    """Main dashboard page"""
    corr = uuid.uuid4().hex[:12]
    html = render_template(
        'dashboard.html',
        dashboard_ui_build=DASHBOARD_UI_BUILD,
        request_correlation=corr,
    )
    if 'app-main-view-switcher' not in html or 'tab-btn-odds' not in html:
        print(
            f"[DASHBOARD] FATAL: rendered HTML missing tab UI (len={len(html)}). "
            f"template_folder={app.template_folder}"
        )
        return Response(
            'Dashboard template error: tab UI missing in rendered HTML. '
            f'Check template_folder={app.template_folder!r}',
            status=500,
            mimetype='text/plain',
        )
    if len(html) < 8000:
        print(f"[DASHBOARD] WARN: rendered HTML unusually short ({len(html)} bytes); expected ~10k+ with tabs.")
    print(f"[DASHBOARD] GET / served html_bytes={len(html)} build={DASHBOARD_UI_BUILD} corr={corr}")
    response = app.make_response(html)
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    response.headers['X-Kalshi-Dashboard-UI'] = DASHBOARD_UI_BUILD
    response.headers['X-Request-Id'] = corr
    return response


@app.route('/live-odds')
@requires_auth
def live_odds_page():
    """Legacy URL: odds grid is on the home dashboard (Odds tab)."""
    return redirect('/?tab=odds')


@app.route('/api/live_odds')
@requires_auth
def api_live_odds():
    """JSON snapshot: events × books moneyline (auto-refresh from the browser).

    Odds-API.io's shared async client is bound to the monitor event loop; scheduling
    snapshot work on api_loop caused broken/empty responses. We use monitor_loop
    when available, otherwise a one-shot isolated client + asyncio.run().

    Query ``league`` narrows to major leagues: ``mlb``, ``nba``, ``nhl``, ``nfl``, or ``all``.
    If ``league`` is omitted, it defaults to the major league matching ``sport`` when
    ``sport`` is baseball / basketball / icehockey / americanfootball (so "MLB" is not all baseball).
    """
    global monitor_loop
    sport = (request.args.get('sport') or 'all').strip().lower()
    timing = (request.args.get('timing') or 'live').strip().lower()
    date_filter = (request.args.get('date') or 'all').strip().lower()
    if "league" in request.args and (request.args.get("league") or "").strip() != "":
        league_focus = (request.args.get("league") or "all").strip().lower()
    else:
        league_focus = {
            "baseball": "mlb",
            "basketball": "nba",
            "icehockey": "nhl",
            "americanfootball": "nfl",
        }.get(sport, "all")
    books = _live_odds_display_books()
    loop = monitor_loop if (monitor_loop and not monitor_loop.is_closed()) else None
    try:
        if loop is not None:
            fut = asyncio.run_coroutine_threadsafe(
                _live_odds_build_snapshot(sport, timing, books, date_filter, league_focus),
                loop,
            )
            data = fut.result(timeout=90)
        else:
            data = asyncio.run(
                _live_odds_build_snapshot_isolated(sport, timing, books, date_filter, league_focus)
            )
    except Exception as e:
        return jsonify(
            {
                "ok": False,
                "error": str(e),
                "books": books,
                "events": [],
                "hint": "If this persists, confirm the monitor thread started (Odds-API polling).",
            }
        ), 500
    return jsonify(data)


@app.route('/logos/<path:filename>')
def serve_logo(filename):
    """Serve logo images from the logos folder"""
    logos_dir = os.path.join(os.path.dirname(__file__), 'logos')
    return send_from_directory(logos_dir, filename)


@app.route('/api/alerts', methods=['GET'])
def get_alerts():
    """Get all active alerts"""
    return jsonify({
        'alerts': list(active_alerts.values()),
        'count': len(active_alerts)
    })


def price_to_american_odds(price_cents):
    """Convert price in cents to American odds format"""
    if price_cents <= 0 or price_cents >= 100:
        return "N/A"
    
    price = price_cents / 100.0
    
    if price >= 0.5:
        # Favorite (negative odds)
        odds = -100 * price / (1 - price)
        return f"{int(odds)}"
    else:
        # Underdog (positive odds)
        odds = 100 * (1 - price) / price
        return f"+{int(odds)}"


def american_odds_to_int(american_odds_str):
    """Convert American odds string (e.g., '+150', '-150') to integer"""
    if not american_odds_str or american_odds_str == "N/A":
        return None
    
    # Remove + sign if present
    odds_str = str(american_odds_str).replace('+', '').strip()
    try:
        return int(odds_str)
    except (ValueError, TypeError):
        return None


async def check_and_auto_bet(alert_id, alert_data, alert):
    """Check if alert matches auto-bet criteria and place bet automatically - OPTIMIZED FOR SPEED"""
    global auto_bet_enabled, auto_bet_ev_min, auto_bet_ev_max, auto_bet_odds_min, auto_bet_odds_max, auto_bet_amount, auto_bet_submarkets, auto_bet_submarket_data, auto_bet_processing_submarkets, auto_bet_games, positions_loaded, nhl_over_bet_amount, px_novig_multiplier, auto_bet_lock, auto_bet_processing_alert_ids, auto_bet_submarket_to_alert_id, auto_bet_submarket_tasks, auto_bet_lock_holder, auto_bet_lock_acquired_at, auto_bet_settings_by_filter, per_event_max_bet, auto_bet_event_totals
    
    import time

    strict_ok = True
    if alert is not None:
        strict_ok = getattr(alert, 'strict_pass', True)
    elif isinstance(alert_data, dict):
        strict_ok = alert_data.get('strict_pass', True)
    if strict_ok is False:
        return
    
    # CRITICAL: Check EV threshold FIRST before any processing/logging to avoid wasting time on low-EV alerts
    # Get filter-specific EV threshold
    alert_filter_name = getattr(alert, 'filter_name', None) or alert_data.get('filter_name')
    if alert_filter_name and alert_filter_name in auto_bet_settings_by_filter:
        current_ev_min = auto_bet_settings_by_filter[alert_filter_name].get('ev_min', auto_bet_ev_min)
        current_ev_max = auto_bet_settings_by_filter[alert_filter_name].get('ev_max', auto_bet_ev_max)
    else:
        current_ev_min = auto_bet_ev_min
        current_ev_max = auto_bet_ev_max
    
    ev_percent = alert.ev_percent if alert else alert_data.get('ev_percent', 0)
    
    # FAST EV CHECK - Do this FIRST before any processing
    # Get ticker and side from alert_data for logging (may be None if not matched yet)
    ticker = alert_data.get('ticker')
    side = alert_data.get('side')
    
    if ev_percent < current_ev_min:
        # EV too low - skip immediately without any processing
        # BUT: Log high-EV alerts (>= 10%) that are being skipped due to EV threshold
        if ev_percent >= 10.0:
            store_failed_auto_bet(
                alert_id=alert_id,
                alert=alert,
                alert_data=alert_data,
                error=f"EV {ev_percent:.2f}% below filter minimum {current_ev_min}%",
                reason=f"Alert has {ev_percent:.2f}% EV but below filter minimum threshold {current_ev_min}%",
                ticker=ticker,
                side=side,
                ev_percent=ev_percent,
                odds=alert_data.get('american_odds'),
                filter_name=alert_filter_name
            )
        return
    if ev_percent > current_ev_max:
        # EV too high - skip immediately without any processing
        # BUT: Log high-EV alerts that are being skipped due to EV max
        if ev_percent >= 10.0:
            store_failed_auto_bet(
                alert_id=alert_id,
                alert=alert,
                alert_data=alert_data,
                error=f"EV {ev_percent:.2f}% above filter maximum {current_ev_max}%",
                reason=f"Alert has {ev_percent:.2f}% EV but above filter maximum threshold {current_ev_max}%",
                ticker=ticker,
                side=side,
                ev_percent=ev_percent,
                odds=alert_data.get('american_odds'),
                filter_name=alert_filter_name
            )
        return
    
    # EV passed - now start trade lifecycle tracking
    trade_start_time = time.time()  # Track entire trade lifecycle timing
    
    # CRITICAL: Determine submarket_key early so we can clean it up on early returns
    submarket_key = None
    ticker = alert_data.get('ticker')
    side = alert_data.get('side')
    if ticker and side:
        submarket_key = (ticker.upper(), side.lower())
    
    # CRITICAL: Initialize lock timing variables at function start (for successful bet logging)
    lock_held_start = None  # Will be set when lock is acquired
    
    # Helper function to clean up submarket (used on early returns and in finally)
    async def cleanup_submarket():
        """Clean up submarket tracking - CRITICAL: Must not acquire lock if already held (deadlock prevention)"""
        if submarket_key and submarket_key[0] and submarket_key[1]:
            # CRITICAL: Check if lock is already held by this task to prevent deadlock
            # If we're in the finally block, the lock should already be released, but be safe
            lock_already_held = auto_bet_lock and auto_bet_lock.locked()
            
            if auto_bet_lock and not lock_already_held:
                # Lock not held - acquire it with timeout to prevent hanging
                try:
                    await asyncio.wait_for(auto_bet_lock.acquire(), timeout=1.0)
                    try:
                        auto_bet_processing_submarkets.discard(submarket_key)
                        alert_id_to_remove = auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                        auto_bet_submarket_tasks.pop(submarket_key, None)
                        # CRITICAL: Also remove alert_id from processing set
                        if alert_id_to_remove:
                            auto_bet_processing_alert_ids.discard(alert_id_to_remove)
                            print(f"[AUTO-BET] Cleaned up alert_id {alert_id_to_remove} from processing set")
                    finally:
                        auto_bet_lock.release()
                except asyncio.TimeoutError:
                    # Lock acquisition timed out - log but don't fail (cleanup is best-effort)
                    print(f"[AUTO-BET] ⚠️  WARNING: cleanup_submarket() timeout acquiring lock for {submarket_key} - skipping cleanup")
                    # Still try to clean up without lock (best effort)
                    auto_bet_processing_submarkets.discard(submarket_key)
                    alert_id_to_remove = auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                    auto_bet_submarket_tasks.pop(submarket_key, None)
                    if alert_id_to_remove:
                        auto_bet_processing_alert_ids.discard(alert_id_to_remove)
            else:
                # Lock already held or no lock - clean up directly (we're already in lock context)
                auto_bet_processing_submarkets.discard(submarket_key)
                alert_id_to_remove = auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                auto_bet_submarket_tasks.pop(submarket_key, None)
                if alert_id_to_remove:
                    auto_bet_processing_alert_ids.discard(alert_id_to_remove)
    
    # CRITICAL: Check EV threshold FIRST before any processing/logging to avoid wasting time on low-EV alerts
    # Get filter-specific EV threshold
    alert_filter_name = getattr(alert, 'filter_name', None) or alert_data.get('filter_name')
    if alert_filter_name and alert_filter_name in auto_bet_settings_by_filter:
        current_ev_min = auto_bet_settings_by_filter[alert_filter_name].get('ev_min', auto_bet_ev_min)
        current_ev_max = auto_bet_settings_by_filter[alert_filter_name].get('ev_max', auto_bet_ev_max)
    else:
        current_ev_min = auto_bet_ev_min
        current_ev_max = auto_bet_ev_max
    
    ev_percent = alert.ev_percent if alert else alert_data.get('ev_percent', 0)
    
    # FAST EV CHECK - Do this FIRST before any processing
    if ev_percent < current_ev_min or ev_percent > current_ev_max:
        # EV out of range - skip immediately without any processing
        return
    
    # EV passed - now start trade lifecycle tracking and logging
    ev_pct = ev_percent
    teams = alert.teams if alert else alert_data.get('teams', 'N/A')
    pick = alert.pick if alert else alert_data.get('pick', 'N/A')
    
    print(f"[AUTO-BET] ========== TRADE LIFECYCLE START ==========")
    print(f"[AUTO-BET] Alert ID: {alert_id}")
    print(f"[AUTO-BET] Market: {teams} - {pick}")
    print(f"[AUTO-BET] EV: {ev_pct:.2f}%")
    print(f"[AUTO-BET] Filter: {alert_filter_name or 'N/A'}")
    print(f"[AUTO-BET] Ticker: {ticker}, Side: {side}")
    print(f"[AUTO-BET] Expected Price: {alert_data.get('price_cents', 'N/A')}¢")
    print(f"[AUTO-BET] Start Time: {time.strftime('%H:%M:%S.%f', time.localtime(trade_start_time))}")
    print(f"[AUTO-BET] =========================================")
    
    # Wrap main logic in try-except-finally for error handling
    try:
        if not auto_bet_enabled:
            print(f"[AUTO-BET] SKIP: Alert {alert_id} - Auto-bet disabled")
            await cleanup_submarket()
            return
        
        # NOTE: positions_loaded check removed - we can proceed even if positions aren't loaded yet
        # Reverse middle detection will still work using bets placed during this session (auto_bet_submarkets)
        # If positions aren't loaded, we just won't check against existing Kalshi positions, which is acceptable
        # The positions_loaded flag is mainly for logging/debugging purposes
        
        # Read settings RIGHT BEFORE using them (ensures latest values)
        # CRITICAL: Read all settings atomically to avoid race conditions
        # Multiple concurrent tasks might be reading while settings are being updated
        # Get filter-specific settings if available, otherwise use global defaults
        # CRITICAL: Use alert_data as source of truth for filter_name (preserved from original alert)
        if alert_filter_name and alert_filter_name in auto_bet_settings_by_filter:
            filter_settings = auto_bet_settings_by_filter[alert_filter_name]
            current_ev_min = filter_settings.get('ev_min', auto_bet_ev_min)
            current_ev_max = filter_settings.get('ev_max', auto_bet_ev_max)
            current_odds_min = filter_settings.get('odds_min', auto_bet_odds_min)
            current_odds_max = filter_settings.get('odds_max', auto_bet_odds_max)
            current_amount = filter_settings.get('amount', auto_bet_amount)
            current_enabled = filter_settings.get('enabled', True) and auto_bet_enabled
            print(f"[AUTO-BET] Using filter-specific settings for '{alert_filter_name}': EV={current_ev_min}%-{current_ev_max}%, Odds={current_odds_min}-{current_odds_max}, Amount=${current_amount:.2f}")
        else:
            # Fallback to global settings
            current_ev_min = auto_bet_ev_min
            current_ev_max = auto_bet_ev_max
            current_odds_min = auto_bet_odds_min
            current_odds_max = auto_bet_odds_max
            current_amount = auto_bet_amount
            current_enabled = auto_bet_enabled
            if alert_filter_name:
                print(f"[AUTO-BET] No filter-specific settings for '{alert_filter_name}', using global defaults")
        
        # Log the exact settings being used for this check
        print(f"[AUTO-BET] Using settings: EV={current_ev_min}%-{current_ev_max}%, Odds={current_odds_min}-{current_odds_max}, Enabled={current_enabled}, Amount=${current_amount:.2f}")
        
        # EV check already done at function start - just log that it passed
        print(f"[AUTO-BET] ✅ EV check PASSED: {ev_percent:.2f}% (range: {current_ev_min}%-{current_ev_max}%)")
        
        # Fast odds check
        american_odds_str = alert_data.get('american_odds', 'N/A')
        print(f"[AUTO-BET] Odds check: '{american_odds_str}' (range: {current_odds_min}-{current_odds_max})")
        american_odds_int = american_odds_to_int(american_odds_str)
        if american_odds_int is None:
            print(f"[AUTO-BET] SKIP: Alert {alert_id} - Could not parse American odds '{american_odds_str}' [ODDS PARSE ERROR]")
            # Log high-EV alerts that fail due to odds parse error
            if ev_percent >= current_ev_min:
                store_failed_auto_bet(
                    alert_id=alert_id,
                    alert=alert,
                    alert_data=alert_data,
                    error=f"Could not parse American odds '{american_odds_str}'",
                    reason=f"Alert has {ev_percent:.2f}% EV (>= {current_ev_min}% threshold) but odds parsing failed",
                    ticker=ticker,
                    side=side,
                    ev_percent=ev_percent,
                    odds=american_odds_str,
                    filter_name=alert_filter_name
                )
            await cleanup_submarket()
            return
        print(f"[AUTO-BET] Parsed odds: {american_odds_int}")
        if american_odds_int < current_odds_min or american_odds_int > current_odds_max:
            print(f"[AUTO-BET] SKIP: Alert {alert_id} Odds {american_odds_int} outside range ({current_odds_min}-{current_odds_max}) [ODDS OUT OF RANGE]")
            print(f"[AUTO-BET]    📊 Odds: {american_odds_int} not in range [{current_odds_min}, {current_odds_max}]")
            # Log high-EV alerts that are blocked due to odds out of range
            if ev_percent >= current_ev_min:
                store_failed_auto_bet(
                    alert_id=alert_id,
                    alert=alert,
                    alert_data=alert_data,
                    error=f"Odds {american_odds_int} outside range ({current_odds_min}-{current_odds_max})",
                    reason=f"Alert has {ev_percent:.2f}% EV (>= {current_ev_min}% threshold) but odds {american_odds_int} outside configured range [{current_odds_min}, {current_odds_max}]",
                    ticker=ticker,
                    side=side,
                    ev_percent=ev_percent,
                    odds=american_odds_int,
                    filter_name=alert_filter_name
                )
            decision_path_so_far['final_decision'] = 'SKIPPED'
            decision_path_so_far['skip_reason'] = f'Odds {american_odds_int} outside range ({current_odds_min}-{current_odds_max})'
            log_alert_passed_threshold(alert_id, alert, alert_data, filter_settings_dict, decision_path_so_far)
            await cleanup_submarket()
            return
        print(f"[AUTO-BET] Odds check PASSED: {american_odds_int}")
        
        # Get expected price (ticker and side already determined above)
        expected_price_cents = alert_data.get('price_cents')
        
        if not ticker or not side:
            print(f"[AUTO-BET] SKIP: Alert {alert_id} - Missing ticker or side (ticker={ticker}, side={side}) [MISSING DATA]")
            # Log high-EV alerts that are blocked due to missing data
            if ev_percent >= current_ev_min:
                store_failed_auto_bet(
                    alert_id=alert_id,
                    alert=alert,
                    alert_data=alert_data,
                    error=f"Missing ticker or side (ticker={ticker}, side={side})",
                    reason=f"Alert has {ev_percent:.2f}% EV (>= {current_ev_min}% threshold) but missing required data for bet placement",
                    ticker=ticker,
                    side=side,
                    ev_percent=ev_percent,
                    odds=alert_data.get('american_odds'),
                    filter_name=alert_filter_name
                )
            await cleanup_submarket()
            return
        
        print(f"[AUTO-BET] Passed checks: EV={ev_percent:.2f}%, Odds={american_odds_int}, Ticker={ticker}, Side={side}")
        if expected_price_cents:
            print(f"[AUTO-BET] Expected price: {expected_price_cents}¢ ({expected_price_cents/100:.2f}¢)")
        print(f"[AUTO-BET] Continuing to NHL check and duplicate check...")
        
        # LOG: Alert passed initial thresholds (EV, odds, matching) - log for analysis
        # This logs alerts that pass thresholds, even if they don't end up placing a bet
        # Initialize decision path tracking - will be updated as checks progress
        decision_path_so_far = {
            'ev_check': 'PASSED',
            'ev_percent': ev_percent,
            'ev_threshold': current_ev_min,
            'odds_check': 'PASSED',
            'odds': american_odds_int,
            'odds_range': f"{current_odds_min}-{current_odds_max}",
            'matching_check': 'PASSED',
            'ticker': ticker,
            'side': side,
        }
        filter_settings_dict = {
            'filter_name': alert_filter_name,
            'ev_min': current_ev_min,
            'ev_max': current_ev_max,
            'odds_min': current_odds_min,
            'odds_max': current_odds_max,
            'amount': current_amount,
            'enabled': current_enabled,
        }
        # Don't log yet - wait until we know final decision (bet placed or skipped)
        
        # NHL exclusions (fast check) - Block ALL NHL moneylines, spreads, and unders (manual only)
        if ticker.upper().startswith('KXNHL'):
            print(f"[AUTO-BET] NHL ticker detected: {ticker}")
            market_type = alert.market_type or alert_data.get('market_type', '')
            pick = (alert.pick or alert_data.get('pick', '') or '').upper()
            # Block: Moneylines, Spreads, Puck Lines, and ALL Unders (regardless of EV)
            is_moneyline = market_type.upper() == 'MONEYLINE' or 'MONEYLINE' in market_type.upper()
            is_spread = 'SPREAD' in market_type.upper() or 'PUCK LINE' in market_type.upper()
            is_under = pick == 'UNDER' or 'UNDER' in pick
            if is_moneyline or is_spread or is_under:
                print(f"[AUTO-BET] SKIP: Alert {alert_id} - NHL exclusion: {market_type} {pick} [NHL EXCLUSION - Manual only]")
                # Log high-EV NHL alerts that are blocked (user might want to know about these)
                if ev_percent >= current_ev_min:
                    store_failed_auto_bet(
                        alert_id=alert_id,
                        alert=alert,
                        alert_data=alert_data,
                        error=f"NHL exclusion: {market_type} {pick} (manual only)",
                        reason=f"Alert has {ev_percent:.2f}% EV (>= {current_ev_min}% threshold) but NHL {market_type} {pick} bets are excluded from auto-betting (manual only)",
                        ticker=ticker,
                        side=side,
                        ev_percent=ev_percent,
                        odds=alert_data.get('american_odds'),
                        filter_name=alert_filter_name
                    )
                await cleanup_submarket()
                return
        
        # EPL/UCL exclusions (same as NHL) - Block moneylines, spreads, and unders; auto-bet overs only
        if ticker.upper().startswith('KXEPL') or ticker.upper().startswith('KXUCL'):
            market_type = alert.market_type or alert_data.get('market_type', '')
            pick = (alert.pick or alert_data.get('pick', '') or '').upper()
            is_moneyline = market_type.upper() == 'MONEYLINE' or 'MONEYLINE' in market_type.upper()
            is_spread = 'SPREAD' in market_type.upper()
            is_under = pick == 'UNDER' or 'UNDER' in pick
            if is_moneyline or is_spread or is_under:
                league = "EPL" if ticker.upper().startswith('KXEPL') else "UCL"
                print(f"[AUTO-BET] SKIP: Alert {alert_id} - {league} exclusion: {market_type} {pick} [Soccer overs only - Manual for spreads/moneylines/unders]")
                # Log high-EV soccer alerts that are blocked
                if ev_percent >= current_ev_min:
                    store_failed_auto_bet(
                        alert_id=alert_id,
                        alert=alert,
                        alert_data=alert_data,
                        error=f"{league} exclusion: {market_type} {pick} (overs only)",
                        reason=f"Alert has {ev_percent:.2f}% EV (>= {current_ev_min}% threshold) but {league} {market_type} {pick} bets are excluded from auto-betting (overs only)",
                        ticker=ticker,
                        side=side,
                        ev_percent=ev_percent,
                        odds=alert_data.get('american_odds'),
                        filter_name=alert_filter_name
                    )
                decision_path_so_far['final_decision'] = 'SKIPPED'
                decision_path_so_far['skip_reason'] = f'{league} exclusion: {market_type} {pick}'
                log_alert_passed_threshold(alert_id, alert, alert_data, filter_settings_dict, decision_path_so_far)
                await cleanup_submarket()
                return
        
        # Fast duplicate check with lock (OLD SIMPLE APPROACH - just wait for lock, no timeout)
        submarket_key = (ticker.upper(), side.lower())
        print(f"[AUTO-BET] Submarket key: {submarket_key}")
        
        # CRITICAL: Acquire lock BEFORE any checks to ensure atomicity
        if not auto_bet_lock:
            print(f"[AUTO-BET] SKIP: Alert {alert_id} - Auto-bet lock not initialized")
            await cleanup_submarket()
            return
            
        # CRITICAL: Acquire lock and perform atomic check-and-mark
        # Add timeout to prevent infinite waiting (lock backlog issue)
        lock_acquisition_start = time.time()
        lock_waiters_before = len(auto_bet_lock._waiters) if (hasattr(auto_bet_lock, '_waiters') and auto_bet_lock._waiters is not None) else 'unknown'
        print(f"[AUTO-BET] [LOCK] Alert {alert_id} attempting to acquire lock for {submarket_key} (waiters before: {lock_waiters_before})")
        try:
            # Timeout after 5 seconds - if lock is held that long, something is wrong
            # This prevents infinite waiting if lock is stuck or backlogged
            await asyncio.wait_for(auto_bet_lock.acquire(), timeout=5.0)
            lock_acquisition_time = (time.time() - lock_acquisition_start) * 1000
            print(f"[AUTO-BET] [LOCK] Alert {alert_id} acquired lock in {lock_acquisition_time:.1f}ms")
        except asyncio.TimeoutError:
            lock_acquisition_time = (time.time() - lock_acquisition_start) * 1000
            lock_waiters_after = len(auto_bet_lock._waiters) if (hasattr(auto_bet_lock, '_waiters') and auto_bet_lock._waiters is not None) else 'unknown'
            print(f"[AUTO-BET] ⚠️  TIMEOUT: Alert {alert_id} waited {lock_acquisition_time:.1f}ms (>5s) for lock - lock may be stuck or backlogged")
            print(f"[AUTO-BET]   Lock state: locked={auto_bet_lock.locked()}, waiters before: {lock_waiters_before}, waiters after: {lock_waiters_after}")
            
            # DIAGNOSTIC: List all running tasks that might be holding the lock
            stuck_tasks = []
            for submarket_key_stuck, task in auto_bet_submarket_tasks.items():
                if task and not task.done():
                    stuck_tasks.append(f"{submarket_key_stuck}: {task}")
            if stuck_tasks:
                print(f"[AUTO-BET]   ⚠️  Found {len(stuck_tasks)} potentially stuck task(s) holding lock: {stuck_tasks[:5]}")
            
            # CRITICAL: Show which task is currently holding the lock
            if auto_bet_lock_holder and auto_bet_lock_acquired_at:
                lock_held_duration = (time.time() - auto_bet_lock_acquired_at) * 1000
                print(f"[AUTO-BET]   🚨 LOCK HELD BY: {auto_bet_lock_holder} for {lock_held_duration:.1f}ms (LOCK IS STUCK!)")
            else:
                print(f"[AUTO-BET]   ⚠️  Lock holder tracking: holder={auto_bet_lock_holder}, acquired_at={auto_bet_lock_acquired_at}")
            
            await cleanup_submarket()
            return
        
        # CRITICAL: Set lock_held_start when lock is acquired (for successful bet logging)
        lock_held_start = time.time()
        # CRITICAL: Track which task is holding the lock (for debugging stuck locks)
        auto_bet_lock_holder = f"Alert {alert_id} ({submarket_key})"
        auto_bet_lock_acquired_at = lock_held_start
        
        # CRITICAL: Add aggressive watchdog to force release lock if held >2 seconds
        # User requirement: "anything that doesn't work after 2 seconds we are dead"
        # This prevents a single stuck task from blocking all bets
        # Store reference to the actual check_and_auto_bet task so watchdog can cancel it
        check_and_auto_bet_task = asyncio.current_task()
        
        async def lock_watchdog():
            """Force release lock if held >2 seconds - prevents stuck tasks from blocking all bets"""
            global auto_bet_lock_holder, auto_bet_lock_acquired_at
            await asyncio.sleep(2.0)  # Wait 2 seconds (user requirement: <2s or dead)
            if auto_bet_lock.locked() and auto_bet_lock_holder == f"Alert {alert_id} ({submarket_key})":
                lock_held_duration = (time.time() - lock_held_start) * 1000
                print(f"[AUTO-BET] 🚨🚨🚨 CRITICAL WATCHDOG: Lock held for {lock_held_duration:.1f}ms (>2s) - FORCING RELEASE!")
                print(f"[AUTO-BET] 🚨🚨🚨 Lock holder: {auto_bet_lock_holder}")
                print(f"[AUTO-BET] 🚨🚨🚨 Cancelling stuck task and force-releasing lock to prevent blocking all bets")
                try:
                    # Cancel the actual check_and_auto_bet task (not the watchdog itself)
                    if check_and_auto_bet_task and not check_and_auto_bet_task.done():
                        check_and_auto_bet_task.cancel()
                        print(f"[AUTO-BET] 🚨🚨🚨 CANCELLED STUCK TASK: Alert {alert_id}")
                    
                    # Force release the lock
                    if auto_bet_lock.locked():
                        auto_bet_lock.release()
                        print(f"[AUTO-BET] 🚨🚨🚨 FORCED LOCK RELEASE - lock is now free")
                    
                    # Clear lock holder tracking
                    if auto_bet_lock_holder == f"Alert {alert_id} ({submarket_key})":
                        auto_bet_lock_holder = None
                        auto_bet_lock_acquired_at = None
                    
                    # Clean up tracking
                    auto_bet_processing_submarkets.discard(submarket_key)
                    auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                    auto_bet_submarket_tasks.pop(submarket_key, None)
                    if submarket_key in auto_bet_submarkets:
                        auto_bet_submarkets.discard(submarket_key)
                        # Subtract from event total if we reserved it
                        if event_base:
                            current_total = auto_bet_event_totals.get(event_base, 0.0)
                            auto_bet_event_totals[event_base] = max(0.0, current_total - bet_amount)
                except Exception as e:
                    print(f"[AUTO-BET] 🚨🚨🚨 ERROR in watchdog: {e}")
                    import traceback
                    traceback.print_exc()
        
        lock_watchdog_task = asyncio.create_task(lock_watchdog())
        
        try:
            # Lock acquired - perform ALL operations (atomic checks, reverse middle, order placement)
            # Safety first: Lock must be held during reverse middle checks (reads shared state)
            # Speed: Order placement is fast (quick API call), so lock can stay held
            print(f"[AUTO-BET] [LOCK] Alert {alert_id} acquired lock for {submarket_key} at {time.strftime('%H:%M:%S.%f', time.localtime(lock_held_start))}")
            # CRITICAL: Atomic check-and-mark to prevent race conditions
            # All checks and marks must happen within the lock to be atomic
            # CRITICAL: Check if already bet (but DON'T add to auto_bet_submarkets yet - only add after successful bet)
            # We only add to auto_bet_submarkets AFTER the bet succeeds (see line ~5027)
            was_already_bet = submarket_key in auto_bet_submarkets
            
            in_processing_set = submarket_key in auto_bet_processing_submarkets
            print(f"[AUTO-BET] [LOCK] Alert {alert_id} - Was already bet: {was_already_bet}, In processing set: {in_processing_set}")
            
            if was_already_bet:
                # Check if we should allow topping up (if previous bet was below target)
                submarket_data = auto_bet_submarket_data.get(submarket_key, {})
                previous_cost = submarket_data.get('actual_cost', 0.0)
                target_amount = submarket_data.get('target_amount', bet_amount)
                
                # Allow topping up if previous bet was less than 80% of target (to account for small fills)
                if previous_cost > 0 and previous_cost < (target_amount * 0.8):
                    remaining_to_target = target_amount - previous_cost
                    print(f"[AUTO-BET] ✅ TOPPING UP: Previous bet was ${previous_cost:.2f} (target: ${target_amount:.2f}), allowing top-up of ${remaining_to_target:.2f}")
                    # Remove from auto_bet_submarkets to allow the new bet
                    auto_bet_submarkets.discard(submarket_key)
                    # Update event total: subtract previous cost (we'll add new cost after bet succeeds)
                    if event_base:
                        current_total = auto_bet_event_totals.get(event_base, 0.0)
                        auto_bet_event_totals[event_base] = max(0.0, current_total - previous_cost)
                        print(f"[AUTO-BET] Subtracted previous cost ${previous_cost:.2f} from event total for {event_base}: ${auto_bet_event_totals[event_base]:.2f} / ${per_event_max_bet:.2f}")
                    # Continue with the bet (don't return)
                else:
                    if in_processing_set:
                        print(f"[AUTO-BET] WARNING: Submarket {submarket_key} in both sets - cleaning up processing set (bet already placed)")
                        auto_bet_processing_submarkets.discard(submarket_key)
                        auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                        auto_bet_submarket_tasks.pop(submarket_key, None)
                    print(f"[AUTO-BET] SKIP: Alert {alert_id} - Already bet {ticker} {side} [DUPLICATE] (previous cost: ${previous_cost:.2f}, target: ${target_amount:.2f})")
                    # CRITICAL: Log high-EV alerts (>= 10%) that are blocked because submarket was already bet
                    # This helps identify when alerts increase above threshold after initial bet
                    if ev_percent >= 10.0:
                        store_failed_auto_bet(
                            alert_id=alert_id,
                            alert=alert,
                            alert_data=alert_data,
                            error="Submarket already bet - duplicate protection",
                            reason=f"Alert has {ev_percent:.2f}% EV (>= 10.0% threshold) but submarket {submarket_key} was already bet (previous cost: ${previous_cost:.2f}, target: ${target_amount:.2f}). This may be an alert that increased above threshold after initial bet.",
                            ticker=ticker,
                            side=side,
                            ev_percent=ev_percent,
                            odds=alert_data.get('american_odds'),
                            filter_name=alert_filter_name
                        )
                    # Release lock before returning (duplicate detected, no bet needed)
                    auto_bet_lock.release()
                    if auto_bet_lock_holder and auto_bet_lock_holder == f"Alert {alert_id} ({submarket_key})":
                        auto_bet_lock_holder = None
                        auto_bet_lock_acquired_at = None
                    return
            
            # If we get here, it's not already bet - we can proceed
            print(f"[AUTO-BET] [LOCK] Alert {alert_id} - Submarket {submarket_key} not already bet, proceeding with checks")
        
            # Check if already processing (second check)
            # NOTE: If we marked it as processing in create_task_safely, we'll see it here
            # But we should continue because we're the one that marked it
            # However, if another task marked it, we should skip
            # Use the mapping to check if we're the one processing it
            if in_processing_set:
                # Check if this alert_id is the one that marked it as processing
                processing_alert_id = auto_bet_submarket_to_alert_id.get(submarket_key)
                if processing_alert_id == alert_id:
                    # We're the one that marked it, so continue processing
                    print(f"[AUTO-BET] [LOCK] Alert {alert_id} - Submarket already processing (we marked it), continuing...")
                else:
                    # Another task marked it, skip
                    print(f"[AUTO-BET] SKIP: Alert {alert_id} - Already processing {ticker} {side} (alert {processing_alert_id} is processing it) [DUPLICATE]")
                    decision_path_so_far['final_decision'] = 'SKIPPED'
                    decision_path_so_far['skip_reason'] = f'Already processing (alert {processing_alert_id})'
                    log_alert_passed_threshold(alert_id, alert, alert_data, filter_settings_dict, decision_path_so_far)
                    # Release lock before returning
                    auto_bet_lock.release()
                    if auto_bet_lock_holder and auto_bet_lock_holder == f"Alert {alert_id} ({submarket_key})":
                        auto_bet_lock_holder = None
                        auto_bet_lock_acquired_at = None
                    return
            
            # PER-EVENT MAX BET CHECK: Must happen INSIDE lock to be atomic
            # This prevents two bets from both passing the check before either updates the total
            # Note: per_event_max_bet and auto_bet_event_totals are already declared as global at function start
            def extract_event_base(ticker_str):
                """Extract event base from ticker (e.g., KXNBAGAME-26JAN12LALSAC-LAL -> KXNBAGAME-26JAN12LALSAC)"""
                if not ticker_str:
                    return None
                parts = ticker_str.split('-')
                if len(parts) >= 3:
                    # Format: SERIES-DATE-TEAMS-SUFFIX -> SERIES-DATE-TEAMS
                    return '-'.join(parts[:-1])
                return None
            
            # CRITICAL: Define bet_amount before using it in per-event max check
            # Use current_amount from settings (read earlier, outside lock for speed)
            bet_amount = current_amount
            
            event_base = extract_event_base(ticker)
            if event_base:
                current_total = auto_bet_event_totals.get(event_base, 0.0)
                if current_total + bet_amount > per_event_max_bet:
                    print(f"[AUTO-BET] [LOCK] Alert {alert_id} - Event {event_base} has reached max bet limit (${current_total:.2f} + ${bet_amount:.2f} > ${per_event_max_bet:.2f}) [PER-EVENT MAX]")
                    auto_bet_processing_submarkets.discard(submarket_key)
                    auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                    auto_bet_submarkets.discard(submarket_key)
                    error_msg = f"Per-event max bet reached: ${current_total:.2f} + ${bet_amount:.2f} > ${per_event_max_bet:.2f}"
                    # Always log per-event max failures (regardless of EV threshold) for debugging
                    store_failed_auto_bet(
                        alert_id=alert_id,
                        alert=alert,
                        alert_data=alert_data,
                        error=error_msg,
                        reason=f"Alert has {ev_percent:.2f}% EV but per-event max bet limit reached for {event_base} (${current_total:.2f} + ${bet_amount:.2f} > ${per_event_max_bet:.2f})",
                        ticker=ticker,
                        side=side,
                        ev_percent=ev_percent,
                        odds=alert_data.get('american_odds'),
                        filter_name=alert_filter_name
                    )
                    socketio.emit('auto_bet_failed', {
                        'alert_id': alert_id,
                        'error': error_msg,
                        'market': f"{alert.teams} - {alert.pick}"
                    })
                    # Release lock before returning
                    auto_bet_lock.release()
                    if auto_bet_lock_holder and auto_bet_lock_holder == f"Alert {alert_id} ({submarket_key})":
                        auto_bet_lock_holder = None
                        auto_bet_lock_acquired_at = None
                    await cleanup_submarket()
                    return
                
                # CRITICAL: Update event total IMMEDIATELY after passing the check (inside lock)
                # This prevents race conditions where two tasks both pass the check before either updates the total
                # If the bet fails later, we'll subtract it from the total in the error handler
                auto_bet_event_totals[event_base] = current_total + bet_amount
                print(f"[AUTO-BET] [LOCK] Updated event total for {event_base}: ${auto_bet_event_totals[event_base]:.2f} / ${per_event_max_bet:.2f} (reserved ${bet_amount:.2f})")
            
            # CRITICAL: Mark as processing IMMEDIATELY after passing all checks
            # This must happen atomically with the checks above (lock ensures this)
            # Only add to processing_submarkets here, NOT to auto_bet_submarkets
            # We'll add to auto_bet_submarkets ONLY after the bet succeeds
            # NOTE: This add() happens WITHIN the lock, so it's atomic with the checks above
            # BUT: If it's already in processing (we marked it in create_task_safely), don't add again
            if not in_processing_set:
                print(f"[AUTO-BET] [LOCK] Alert {alert_id} marking as processing: {submarket_key}")
                auto_bet_processing_submarkets.add(submarket_key)
            else:
                print(f"[AUTO-BET] [LOCK] Alert {alert_id} - Submarket already marked as processing (we marked it in create_task_safely), continuing...")
            
            # CRITICAL: Re-check IMMEDIATELY after adding to catch any race conditions
            # If another task somehow added it between our check and our add, we should skip
            # This is a defensive double-check that should never trigger if the lock works correctly
            in_processing_after_add = submarket_key in auto_bet_processing_submarkets
            if not in_processing_after_add:
                print(f"[AUTO-BET] [LOCK] ERROR: Alert {alert_id} - Failed to add {submarket_key} to processing set!")
                # Release lock before returning
                auto_bet_lock.release()
                if auto_bet_lock_holder and auto_bet_lock_holder == f"Alert {alert_id} ({submarket_key})":
                    auto_bet_lock_holder = None
                    auto_bet_lock_acquired_at = None
                # Log this critical error
                store_failed_auto_bet(
                    alert_id=alert_id,
                    alert=alert,
                    alert_data=alert_data,
                    error="Failed to add submarket to processing set",
                    reason=f"Alert has {ev_percent:.2f}% EV (>= {current_ev_min}% threshold) but failed to add {submarket_key} to processing set after lock acquisition - race condition or bug",
                    ticker=ticker,
                    side=side,
                    ev_percent=ev_percent,
                    odds=alert_data.get('american_odds'),
                    filter_name=alert_filter_name,
                    additional_logs=[
                        f"========== CRITICAL ERROR ==========",
                        f"Alert ID: {alert_id}",
                        f"Submarket: {submarket_key}",
                        f"Was in processing set before: {in_processing_set}",
                        f"Was in processing set after: {in_processing_after_add}",
                        f"Lock held for: {(time.time() - lock_held_start) * 1000:.1f}ms",
                        f"This should never happen - indicates a race condition or bug",
                        f"====================================="
                    ]
                )
                await cleanup_submarket()
                return
            
            # Verify we're the only one processing this (defensive check)
            # This should always be True since we just added it and we hold the lock
            print(f"[AUTO-BET] [LOCK] Alert {alert_id} verified in processing set after add: {in_processing_after_add}")
            
            # CRITICAL: Do NOT add to auto_bet_submarkets here!
            # We only add to auto_bet_submarkets AFTER the bet succeeds (see line ~2649)
            # Adding it here would mark it as "bet" before the bet is actually placed,
            # which could cause issues if the bet fails or if two bets are placed simultaneously.
            # We use auto_bet_processing_submarkets to track what's being processed,
            # and only add to auto_bet_submarkets after successful bet placement.
            
            # CRITICAL: Keep lock held through entire bet placement process
            # Lock must remain until we know success/fail to prevent duplicate bets
            # The entire process (reverse middle check + order placement) should be <0.5s
            # Lock will be released in finally block after success/fail is determined
            print(f"[AUTO-BET] [LOCK] Alert {alert_id} keeping lock held through bet placement (will release after success/fail)")
            
            # TIMING: Track each major step to identify bottlenecks
            step_timings = {}
            step_start = time.time()
            
            # Get game_name and market_type for checks
            game_name = alert.teams
            market_type = alert.market_type or alert_data.get('market_type', '')
            step_timings['setup'] = (time.time() - step_start) * 1000
            
            # NOTE: Duplicate check already done above (lines 1042-1052) before marking as processing
            # No need to check again here - we've already verified it's not a duplicate
            
            # ============================================================================
            # REVERSE MIDDLE PREVENTION: Check for reverse middles across all market types
            # ============================================================================
            # Use market_matcher's check_reverse_middle function for comprehensive checking
            # Extract line from qualifier (e.g., "215.5", "+11.5", "-11.5")
            # CRITICAL: Line is required for totals and spreads to detect reverse middles
            current_line = None
            try:
                qualifier = alert.qualifier or alert_data.get('qualifier', '')
                if qualifier:
                    qualifier_clean = qualifier.replace('+', '').replace('*', '').strip()
                    current_line = float(qualifier_clean)
            except (ValueError, AttributeError):
                pass
            
            # VALIDATION: Require line for totals and spreads (needed for reverse middle detection)
            market_type_lower = (alert.market_type or alert_data.get('market_type', '')).lower()
            is_total = 'total' in market_type_lower or 'TOTAL' in ticker
            is_spread = 'spread' in market_type_lower or 'SPREAD' in ticker
            
            # CRITICAL: Normalize market type early (needed for reverse middle check)
            if 'TOTAL' in ticker or 'Total Points' in (alert.market_type or ''):
                normalized_market_type = 'Total Points'
            elif 'SPREAD' in ticker or 'Point Spread' in (alert.market_type or ''):
                normalized_market_type = 'Point Spread'
            elif 'GAME' in ticker or 'Moneyline' in (alert.market_type or ''):
                normalized_market_type = 'Moneyline'
            else:
                normalized_market_type = alert.market_type or 'Unknown'
            
            # BLOCK SPREAD NO BETS: Analysis shows -30.44% ROI (statistically significant, p=0.001)
            # Removing saves $3,446.13 in losses while maintaining 81.1% of bet volume
            if normalized_market_type == 'Point Spread' and side.lower() == 'no':
                print(f"[AUTO-BET] SKIP: Alert {alert_id} - SPREAD NO BET BLOCKED (historical ROI: -30.44%, statistically significant)")
                print(f"[AUTO-BET]   Market: {alert.teams} - {alert.pick} | {ticker} {side}")
                print(f"[AUTO-BET]   Reason: Spread NO bets have shown consistent losses across all filters and sports")
                error_msg = 'Spread NO bets are blocked due to poor historical performance (-30.44% ROI)'
                # Log this as an intentional block (not a failure, but we want to track it)
                # Use a different error type so it's clear this is intentional
                store_failed_auto_bet(
                    alert_id=alert_id,
                    alert=alert,
                    alert_data=alert_data,
                    error="Spread NO bet blocked (intentional)",
                    reason=f"Spread NO bets are intentionally blocked due to poor historical performance (-30.44% ROI). Alert has {ev_percent:.2f}% EV but spread NO bets have shown consistent losses.",
                    ticker=ticker,
                    side=side,
                    ev_percent=ev_percent,
                    odds=alert_data.get('american_odds'),
                    filter_name=alert_filter_name
                )
                socketio.emit('auto_bet_failed', {
                    'alert_id': alert_id,
                    'error': error_msg,
                    'market': f"{alert.teams} - {alert.pick}"
                })
                await cleanup_submarket()
                return
            
            if (is_total or is_spread) and current_line is None:
                print(f"[AUTO-BET] SKIP: Alert {alert_id} - Missing line value for {market_type_lower} market (qualifier: '{alert.qualifier}') - cannot verify reverse middle safety")
                decision_path_so_far['final_decision'] = 'SKIPPED'
                decision_path_so_far['skip_reason'] = f'Missing line value for {market_type_lower} market (qualifier: {alert.qualifier})'
                log_alert_passed_threshold(alert_id, alert, alert_data, filter_settings_dict, decision_path_so_far)
                error_msg = f"Missing line value for {market_type_lower} market - required for safety checks"
                # Log high-EV alerts that are blocked due to missing line
                if ev_percent >= current_ev_min:
                    store_failed_auto_bet(
                        alert_id=alert_id,
                        alert=alert,
                        alert_data=alert_data,
                        error=error_msg,
                        reason=f"Alert has {ev_percent:.2f}% EV (>= {current_ev_min}% threshold) but missing line value for {market_type_lower} market (qualifier: '{alert.qualifier}') - required for reverse middle safety checks",
                        ticker=ticker,
                        side=side,
                        ev_percent=ev_percent,
                        odds=alert_data.get('american_odds'),
                        filter_name=alert_filter_name
                    )
                socketio.emit('auto_bet_failed', {
                    'alert_id': alert_id,
                    'error': error_msg,
                    'market': f"{alert.teams} - {alert.pick}"
                })
                # Release lock before returning
                auto_bet_lock.release()
                if auto_bet_lock_holder and auto_bet_lock_holder == f"Alert {alert_id} ({submarket_key})":
                    auto_bet_lock_holder = None
                    auto_bet_lock_acquired_at = None
                await cleanup_submarket()
                return
            
            # Build existing positions list for reverse middle check
            # CRITICAL: Use auto_bet_submarket_data which is updated frequently by position_check_loop (every 60s)
            # This data is kept fresh by the separate manager process, so reverse middle check is instant
            # No API calls needed - just read from in-memory data structure
            existing_positions_for_check = []
            # Fast iteration - auto_bet_submarket_data is already populated with all position data
            for existing_key, existing_data in auto_bet_submarket_data.items():
                existing_positions_for_check.append({
                    'line': existing_data.get('line'),
                    'pick': existing_data.get('pick', ''),
                    'market_type': existing_data.get('market_type', ''),
                    'teams': existing_data.get('teams', ''),
                    'side': existing_data.get('side', ''),  # Include side for same-team reverse middle detection
                    'raw_pick': existing_data.get('raw_pick', existing_data.get('pick', ''))  # Original pick before NO transformation
                })
            
            # CRITICAL: Transform line and pick for NO bets BEFORE reverse middle check
            # For spreads with NO side, we need to flip the team and line
            effective_line_for_rm_check = current_line
            effective_pick_for_rm_check = alert.pick or alert_data.get('pick', '')
            
            if normalized_market_type == 'Point Spread' and side.lower() == 'no':
                # Extract teams to find opposing team
                teams_str = alert.teams or alert_data.get('teams', '')
                if teams_str:
                    import re
                    parts = re.split(r'\s*[@]\s*|\s*VS\s*', teams_str, maxsplit=1, flags=re.IGNORECASE)
                    if len(parts) == 2:
                        team1 = parts[0].strip()
                        team2 = parts[1].strip()
                        pick_upper = effective_pick_for_rm_check.upper()
                        
                        # Determine which team is the pick
                        pick_is_team1 = any(word in pick_upper for word in team1.split() if len(word) > 3) or pick_upper in team1.upper() or team1.upper() in pick_upper
                        pick_is_team2 = any(word in pick_upper for word in team2.split() if len(word) > 3) or pick_upper in team2.upper() or team2.upper() in pick_upper
                        
                        # If betting NO on pick team, we're effectively betting the opponent
                        if pick_is_team1:
                            effective_pick_for_rm_check = team2  # Betting NO on team1 = betting team2
                        elif pick_is_team2:
                            effective_pick_for_rm_check = team1  # Betting NO on team2 = betting team1
                        
                        # Flip the line sign for NO bets
                        if effective_line_for_rm_check is not None:
                            effective_line_for_rm_check = -effective_line_for_rm_check
                            print(f"[AUTO-BET] [REVERSE MIDDLE] Spread NO bet: Alert pick '{alert.pick}' -> Effective pick '{effective_pick_for_rm_check}', line {current_line} -> {effective_line_for_rm_check}")
            
            # Use market_matcher's check_reverse_middle function
            # Pass both effective line (for different-team checks) and original line (for same-team checks)
            # Temporarily add side and original_line to alert for check_reverse_middle to access
            alert_side_backup = getattr(alert, 'side', None)
            alert.side = side  # Add side to alert for comprehensive check
            alert.original_line = current_line  # Add original line (before NO transformation) for same-team detection
            try:
                is_reverse_middle, reason = market_matcher.check_reverse_middle(alert, effective_line_for_rm_check, existing_positions_for_check)
            finally:
                # Restore original side (or remove if it didn't exist)
                if alert_side_backup is not None:
                    alert.side = alert_side_backup
                elif hasattr(alert, 'side'):
                    delattr(alert, 'side')
                # Remove original_line if we added it
                if hasattr(alert, 'original_line'):
                    delattr(alert, 'original_line')
            if is_reverse_middle:
                print(f"[AUTO-BET] SKIP: Alert {alert_id} - {reason}")
                decision_path_so_far['final_decision'] = 'SKIPPED'
                decision_path_so_far['skip_reason'] = reason
                log_alert_passed_threshold(alert_id, alert, alert_data, filter_settings_dict, decision_path_so_far)
                # Clean up (lock already released, but clean up sets)
                auto_bet_processing_submarkets.discard(submarket_key)
                auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                auto_bet_submarket_tasks.pop(submarket_key, None)
                auto_bet_submarkets.discard(submarket_key)
                # Subtract from event total if we reserved it
                if event_base:
                    current_total = auto_bet_event_totals.get(event_base, 0.0)
                    auto_bet_event_totals[event_base] = max(0.0, current_total - bet_amount)
                await cleanup_submarket()
                return
            
            # Continue with existing fast reverse middle check as backup (for speed)
            # game_name and market_type already defined above for logging
            
            # Get pick (Over/Under/Team name) and calculate EFFECTIVE pick for reverse middle check
            current_pick_raw = (alert.pick or alert_data.get('pick', '')).upper()
            is_over = 'OVER' in current_pick_raw or current_pick_raw == 'OVER'
            is_under = 'UNDER' in current_pick_raw or current_pick_raw == 'UNDER'
            
            # CRITICAL: Calculate effective pick and line BEFORE reverse middle check
            # For NO bets on spreads, we need to flip the team and line for proper reverse middle detection
            current_pick = current_pick_raw
            effective_line_for_check = current_line
            
            # For spreads with NO side, transform to effective pick/line BEFORE checking
            if normalized_market_type == 'Point Spread' and side.lower() == 'no' and not is_over and not is_under:
                # Extract teams to find opposing team
                teams_str = alert.teams or alert_data.get('teams', '')
                if teams_str:
                    import re
                    parts = re.split(r'\s*[@]\s*|\s*VS\s*', teams_str, maxsplit=1, flags=re.IGNORECASE)
                    if len(parts) == 2:
                        team1 = parts[0].strip()
                        team2 = parts[1].strip()
                        pick_upper = current_pick_raw
                        
                        # Determine which team is the pick
                        pick_is_team1 = any(word in pick_upper for word in team1.split() if len(word) > 3) or pick_upper in team1.upper() or team1.upper() in pick_upper
                        pick_is_team2 = any(word in pick_upper for word in team2.split() if len(word) > 3) or pick_upper in team2.upper() or team2.upper() in pick_upper
                        
                        # If betting NO on pick team, we're effectively betting the opponent
                        if pick_is_team1:
                            current_pick = team2.upper()  # Betting NO on team1 = betting team2
                        elif pick_is_team2:
                            current_pick = team1.upper()  # Betting NO on team2 = betting team1
                        
                        # Flip the line sign for NO bets
                        if effective_line_for_check is not None:
                            effective_line_for_check = -effective_line_for_check
                            print(f"[AUTO-BET] [REVERSE MIDDLE CHECK] Spread NO bet: Alert pick '{alert.pick}' -> Effective pick '{current_pick}', line {current_line} -> {effective_line_for_check}")
            
            # Calculate effective pick for totals (accounting for side)
            if ('TOTAL' in ticker or 'Total Points' in market_type) and side.lower() == 'no':
                if is_over:
                    current_pick = 'UNDER'  # NO on Over = effectively Under
                elif is_under:
                    current_pick = 'OVER'  # NO on Under = effectively Over
            
            # FAST REVERSE MIDDLE CHECK - Use ticker-based matching (lightning fast, instant)
            # CRITICAL: auto_bet_submarket_data is updated frequently by position_check_loop (separate manager)
            # This check is instant - no API calls, just in-memory data read
            
            # Check all existing bets on the same game
            # CRITICAL: Use ticker prefix to identify same game (more reliable than team names)
            # Tickers like KXNBAGAME-26JAN09HOUPOR-POR, KXNBATOTAL-26JAN09HOUPOR-220, KXNBASPREAD-26JAN09HOUPOR-HOU4
            # All share the same base event identifier: 26JAN09HOUPOR (date + team codes)
            def extract_event_base(ticker_str):
                """Extract base event identifier from ticker (e.g., '26JAN09HOUPOR' from 'KXNBAGAME-26JAN09HOUPOR-POR')"""
                if '-' in ticker_str:
                    parts = ticker_str.split('-')
                    if len(parts) >= 2:
                        # Return the middle part (date + team codes)
                        return parts[1]
                return None
            
            current_event_base = extract_event_base(ticker)
            
            # FAST REVERSE MIDDLE CHECK - Use ticker-based matching only (lightning fast, instant)
            # Read directly from auto_bet_submarket_data (updated by position_check_loop every 60s)
            reverse_middle_check_start = time.time()
            print(f"[AUTO-BET] [TIMING] [{time.strftime('%H:%M:%S.%f', time.localtime(reverse_middle_check_start))}] ⚡ Starting reverse middle check")
            reverse_middle_checked_count = 0
            for existing_key, existing_data in auto_bet_submarket_data.items():
                reverse_middle_checked_count += 1
                existing_ticker, existing_side = existing_key
                existing_event_base = extract_event_base(existing_ticker)
                existing_game = existing_data.get('teams', '')
                existing_market_type = existing_data.get('market_type', '')
                existing_line = existing_data.get('line')
                existing_pick = existing_data.get('pick', '').upper()
                
                # Fast ticker match only (skip expensive team name matching for speed)
                if not (current_event_base and existing_event_base and current_event_base == existing_event_base):
                    continue
                
                # 1. TOTALS REVERSE MIDDLE (fast check)
                if ('TOTAL' in ticker or 'Total Points' in market_type) and \
                   ('TOTAL' in existing_ticker or 'Total Points' in existing_market_type):
                    existing_is_over = 'OVER' in existing_pick or existing_pick == 'OVER'
                    existing_is_under = 'UNDER' in existing_pick or existing_pick == 'UNDER'
                    
                    if current_line is not None and existing_line is not None:
                        opposite = (is_over and existing_is_under) or (is_under and existing_is_over)
                        if opposite:
                            # Same line = block
                            if abs(current_line - existing_line) < 0.01:
                                print(f"[AUTO-BET] SKIP: Alert {alert_id} - TOTALS SAME LINE! {current_pick} {current_line} vs {existing_pick} {existing_line} [REVERSE MIDDLE]")
                                auto_bet_processing_submarkets.discard(submarket_key)
                                auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                                auto_bet_submarket_tasks.pop(submarket_key, None)
                                auto_bet_submarkets.discard(submarket_key)
                                return
                            # Reverse middle: Over X and Under Y where X > Y
                            if (is_over and existing_is_under and current_line > existing_line) or \
                               (is_under and existing_is_over and current_line < existing_line):
                                print(f"[AUTO-BET] SKIP: Alert {alert_id} - TOTALS REVERSE MIDDLE! {current_pick} {current_line} vs {existing_pick} {existing_line} [REVERSE MIDDLE]")
                                auto_bet_processing_submarkets.discard(submarket_key)
                                auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                                auto_bet_submarket_tasks.pop(submarket_key, None)
                                auto_bet_submarkets.discard(submarket_key)
                                return
                
                # 2. SPREAD REVERSE MIDDLE (fast check)
                if ('SPREAD' in ticker or 'Point Spread' in market_type) and \
                   ('SPREAD' in existing_ticker or 'Point Spread' in existing_market_type):
                    # Get original sides to detect same-team reverse middles
                    current_side_original = side.lower()
                    existing_side_original = existing_data.get('side', '').lower()
                    
                    # Get original picks (before NO bet transformation) for same-team detection
                    current_pick_original = (alert.pick or alert_data.get('pick', '')).upper()
                    existing_pick_original = existing_data.get('raw_pick', existing_pick).upper()
                    
                    # Use effective_line_for_check (already transformed for NO bets) instead of current_line
                    line_to_check = effective_line_for_check if effective_line_for_check is not None else current_line
                    if line_to_check is not None and existing_line is not None:
                        current_team = current_pick if not is_over and not is_under else None
                        existing_team = existing_pick if 'OVER' not in existing_pick and 'UNDER' not in existing_pick else None
                        
                        # Check for SAME TEAM reverse middle (NO on lower line + YES on higher line)
                        # Example: NO on "wins by over 1.5" + YES on "wins by over 4.5" = gap 2-4 where all lose
                        if current_team and existing_team and current_team == existing_team:
                            # Same team - check for NO + YES pattern with gap
                            # NO on "wins by over X" means wins by ≤X
                            # YES on "wins by over Y" means wins by >Y
                            # If Y > X, gap exists (X+0.5 to Y-0.5) where all lose
                            
                            # Get original lines (before NO transformation)
                            current_line_original = current_line  # Original line from alert
                            existing_line_original = existing_line  # Stored line (may be transformed)
                            
                            # If existing was a NO bet, reverse the transformation to get original line
                            # NO bets are stored with flipped sign (e.g., NO on 1.5 stored as -1.5)
                            if existing_side_original == 'no' and existing_line_original is not None:
                                existing_line_original = abs(existing_line_original)  # Reverse: -1.5 -> 1.5
                            
                            # If current is a NO bet, it's already the original (from alert)
                            # But ensure it's positive for comparison
                            if current_side_original == 'no' and current_line_original is not None:
                                current_line_original = abs(current_line_original)
                            
                            if current_line_original is not None and existing_line_original is not None:
                                # Check for NO on lower line + YES on higher line
                                if current_side_original == 'no' and existing_side_original == 'yes':
                                    # Current: NO on X, Existing: YES on Y
                                    # Gap exists if Y > X (e.g., NO on 1.5 + YES on 4.5 = gap 2-4)
                                    if existing_line_original > current_line_original:
                                        gap_start = current_line_original + 0.5
                                        gap_end = existing_line_original - 0.5
                                        print(f"[AUTO-BET] SKIP: Alert {alert_id} - SAME TEAM SPREAD REVERSE MIDDLE! NO on {current_team} {current_line_original} + YES on {existing_team} {existing_line_original} (gap: {gap_start:.1f}-{gap_end:.1f} where all lose)")
                                        auto_bet_processing_submarkets.discard(submarket_key)
                                        auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                                        auto_bet_submarkets.discard(submarket_key)
                                        return
                                elif current_side_original == 'yes' and existing_side_original == 'no':
                                    # Current: YES on Y, Existing: NO on X
                                    # Gap exists if Y > X
                                    if current_line_original > existing_line_original:
                                        gap_start = existing_line_original + 0.5
                                        gap_end = current_line_original - 0.5
                                        print(f"[AUTO-BET] SKIP: Alert {alert_id} - SAME TEAM SPREAD REVERSE MIDDLE! YES on {current_team} {current_line_original} + NO on {existing_team} {existing_line_original} (gap: {gap_start:.1f}-{gap_end:.1f} where all lose)")
                                        auto_bet_processing_submarkets.discard(submarket_key)
                                        auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                                        auto_bet_submarkets.discard(submarket_key)
                                        return
                        
                        # Check for DIFFERENT TEAM reverse middle (existing logic)
                        positive_line = line_to_check if line_to_check > 0 else existing_line if existing_line > 0 else None
                        negative_line = line_to_check if line_to_check < 0 else existing_line if existing_line < 0 else None
                        
                        if positive_line is not None and negative_line is not None:
                            if positive_line < abs(negative_line):
                                # Reverse middle - block it if teams are different
                                if current_team and existing_team and current_team != existing_team:
                                    print(f"[AUTO-BET] SKIP: Alert {alert_id} - SPREAD REVERSE MIDDLE! {current_pick} {line_to_check} vs {existing_pick} {existing_line} (gap: {abs(negative_line) - positive_line:.1f} - both can lose) [REVERSE MIDDLE]")
                                    auto_bet_processing_submarkets.discard(submarket_key)
                                    auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                                    auto_bet_submarkets.discard(submarket_key)
                                    return
                
                # 3. MONEYLINE + MONEYLINE REVERSE MIDDLE (fast check)
                # CRITICAL: Betting both teams' moneylines on the same event is ALWAYS a reverse middle
                is_moneyline = 'GAME' in ticker or 'Moneyline' in market_type
                existing_is_moneyline = 'GAME' in existing_ticker or 'Moneyline' in existing_market_type
                
                if is_moneyline and existing_is_moneyline:
                    current_team = current_pick if not is_over and not is_under else None
                    existing_team = existing_pick if 'OVER' not in existing_pick and 'UNDER' not in existing_pick else None
                    
                    if current_team and existing_team and current_team != existing_team:
                        # Different teams on same event - ALWAYS a reverse middle for moneylines
                        print(f"[AUTO-BET] 🚨 REVERSE MIDDLE DETECTED: Alert {alert_id} - MONEYLINE + MONEYLINE REVERSE MIDDLE!")
                        print(f"[AUTO-BET]   Current: {current_team} ML on {current_event_base}")
                        print(f"[AUTO-BET]   Existing: {existing_team} ML on {existing_event_base}")
                        print(f"[AUTO-BET]   Event match: {current_event_base == existing_event_base}")
                        print(f"[AUTO-BET]   SKIP: Only one team can win - both bets can't win [REVERSE MIDDLE]")
                        auto_bet_processing_submarkets.discard(submarket_key)
                        auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                        auto_bet_submarkets.discard(submarket_key)
                        return
                
                # 4. MONEYLINE + SPREAD REVERSE MIDDLE (fast check)
                # REVERSE MIDDLE: Team A ML + Team B -X (favorite spread) where teams are DIFFERENT
                # If Team B (favorite) wins by 1 to |X|-1, both bets lose
                # Example: Oklahoma ML + Alabama -2.5 = REVERSE MIDDLE (if Alabama wins by 1-2, both lose)
                # Example: Oklahoma ML + Alabama +2.5 = NOT reverse middle (if Oklahoma wins by 1-2, both win)
                is_spread = 'SPREAD' in ticker or 'Point Spread' in market_type
                existing_is_spread = 'SPREAD' in existing_ticker or 'Point Spread' in existing_market_type
                
                if (is_moneyline and existing_is_spread) or (is_spread and existing_is_moneyline):
                    current_team = current_pick if not is_over and not is_under else None
                    existing_team = existing_pick if 'OVER' not in existing_pick and 'UNDER' not in existing_pick else None
                    
                    # CRITICAL: Reverse middle occurs when DIFFERENT teams AND spread is negative (favorite)
                    # Team A ML + Team B -X: If Team B wins by 1 to |X|-1, both lose
                    if current_team and existing_team and current_team != existing_team:
                        # New ML + Existing negative spread (favorite)
                        if is_moneyline and existing_is_spread and existing_line is not None and existing_line < 0:
                            # Current: Team A ML, Existing: Team B -X (favorite)
                            # If Team B wins by 1 to |X|-1, both lose = REVERSE MIDDLE
                            max_loss_margin = abs(existing_line) - 1
                            print(f"[AUTO-BET] SKIP: Alert {alert_id} - ML+SPREAD REVERSE MIDDLE: {current_team} ML vs {existing_team} {existing_line} (if {existing_team} wins by 1-{max_loss_margin:.1f}, both lose)")
                            auto_bet_processing_submarkets.discard(submarket_key)
                            auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                            auto_bet_submarkets.discard(submarket_key)
                            return
                        # New negative spread (favorite) + Existing ML
                        if is_spread and existing_is_moneyline and current_line is not None and current_line < 0:
                            # Current: Team A -X (favorite), Existing: Team B ML
                            # If Team A wins by 1 to |X|-1, both lose = REVERSE MIDDLE
                            max_loss_margin = abs(current_line) - 1
                            print(f"[AUTO-BET] SKIP: Alert {alert_id} - ML+SPREAD REVERSE MIDDLE: {current_team} {current_line} vs {existing_team} ML (if {current_team} wins by 1-{max_loss_margin:.1f}, both lose)")
                            auto_bet_processing_submarkets.discard(submarket_key)
                            auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                            auto_bet_submarkets.discard(submarket_key)
                            return
                        # If spread is positive (underdog), it's NOT a reverse middle - both can win
                        # Example: Oklahoma ML + Alabama +2.5 = OK (if Oklahoma wins by 1-2, both win)
            
            # PER-MARKET-TYPE LIMIT: Check how many bets we've placed on this game for this market type AND pick direction
            # Max 2 bets per pick direction per market type per game
            # Examples:
            #   - Over 224.5 and Over 221.5 = 2 overs (OK)
            #   - Under 227.5 and Under 225.5 = 2 unders (OK)
            #   - Over 224.5, Over 221.5, AND Under 227.5 = 2 overs + 1 under (OK, true middle)
            #   - Over 224.5, Over 221.5, Over 218.5 = 3 overs (BLOCKED)
            game_name = alert.teams
            market_type = alert.market_type or alert_data.get('market_type', '')
            
            # Normalize market type for grouping
            if 'TOTAL' in ticker or 'Total Points' in market_type or 'Total Goals' in market_type:
                normalized_market_type = 'Total Points'
            elif 'SPREAD' in ticker or 'Point Spread' in market_type:
                normalized_market_type = 'Point Spread'
            elif 'GAME' in ticker or 'Moneyline' in market_type:
                normalized_market_type = 'Moneyline'
            else:
                normalized_market_type = market_type
            
            # Determine pick direction
            current_pick = (alert.pick or alert_data.get('pick', '')).upper()
            is_over = 'OVER' in current_pick or current_pick == 'OVER'
            is_under = 'UNDER' in current_pick or current_pick == 'UNDER'
            
            # CRITICAL: Calculate effective pick_direction based on side
            # For totals: NO on "Over" = effectively "Under", NO on "Under" = effectively "Over"
            if normalized_market_type == 'Total Points':
                # For totals, side determines effective direction
                if side.lower() == 'no':
                    # NO on Over = Under, NO on Under = Over
                    if is_over:
                        pick_direction = 'Under'  # Effective direction
                    elif is_under:
                        pick_direction = 'Over'  # Effective direction
                    else:
                        pick_direction = 'Unknown'
                else:
                    # YES side: direction matches pick
                    if is_over:
                        pick_direction = 'Over'
                    elif is_under:
                        pick_direction = 'Under'
                    else:
                        pick_direction = 'Unknown'
            else:
                # For spreads/moneylines, track by team name (for reverse middle detection)
                pick_direction = current_pick if not is_over and not is_under else 'Unknown'
            
            # Per-pick-direction limit check (fast)
            if game_name not in auto_bet_games:
                auto_bet_games[game_name] = {}
            if normalized_market_type not in auto_bet_games[game_name]:
                auto_bet_games[game_name][normalized_market_type] = {}
            if pick_direction not in auto_bet_games[game_name][normalized_market_type]:
                auto_bet_games[game_name][normalized_market_type][pick_direction] = []
            
            # CRITICAL: Check limit BEFORE adding to list (to prevent race conditions)
            # Count only successfully placed bets (those in auto_bet_submarkets)
            # This ensures we don't count failed attempts or tasks that are still processing
            existing_bets = [
                key for key in auto_bet_games[game_name][normalized_market_type][pick_direction]
                if key in auto_bet_submarkets  # Only count bets that actually succeeded
            ]
            
            if len(existing_bets) >= 2:
                print(f"[AUTO-BET] SKIP: Alert {alert_id} - Limit reached: {len(existing_bets)} {normalized_market_type} {pick_direction} bets on {game_name} [LIMIT]")
                auto_bet_processing_submarkets.discard(submarket_key)
                auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                auto_bet_submarket_tasks.pop(submarket_key, None)
                auto_bet_submarkets.discard(submarket_key)
                return
            
            # CRITICAL: Add to auto_bet_games BEFORE placing order (same as auto_bet_submarkets)
            # This ensures the limit check works correctly for concurrent bets
            # If order fails, we'll remove it in the error handler
            if submarket_key not in auto_bet_games[game_name][normalized_market_type][pick_direction]:
                auto_bet_games[game_name][normalized_market_type][pick_direction].append(submarket_key)
                print(f"[AUTO-BET] Added {submarket_key} to {game_name} -> {normalized_market_type} -> {pick_direction} (before order)")
            
            # Store line data for reverse middle detection
            # CRITICAL: For spreads, we need the SIGNED line (+X or -X) and the CORRECT TEAM
            # Alert feed: pick = team name, qualifier/line = signed line (e.g., "+4.5" or "-7.5")
            # Example: "New York +4.5" means New York gets 4.5 points
            # Example: "Phoenix -7.5" means Phoenix must win by 7.5+
            line_value = None
            try:
                # Prefer alert.line if available (may have sign already)
                if hasattr(alert, 'line') and alert.line is not None:
                    line_value = float(alert.line)
                else:
                    # Fallback to qualifier
                    qualifier = alert.qualifier or alert_data.get('qualifier', '')
                    if qualifier:
                        # For spreads, preserve the sign (+X or -X) - CRITICAL for reverse middle detection
                        # For totals, the sign doesn't matter (just the number)
                        if 'SPREAD' in ticker or 'Point Spread' in market_type:
                            # Keep the sign for spreads (e.g., "+4.5" or "-7.5")
                            qualifier_clean = qualifier.replace('*', '').strip()
                            line_value = float(qualifier_clean)
                        else:
                            # For totals, remove + sign (just need the number)
                            qualifier_clean = qualifier.replace('+', '').replace('*', '').strip()
                            line_value = float(qualifier_clean)
            except (ValueError, AttributeError, TypeError):
                pass
            
            # Normalize market type for grouping (already done above, but need it here too)
            if 'TOTAL' in ticker or 'Total Points' in market_type or 'Total Goals' in market_type:
                normalized_market_type = 'Total Points'
            elif 'SPREAD' in ticker or 'Point Spread' in market_type:
                normalized_market_type = 'Point Spread'
            elif 'GAME' in ticker or 'Moneyline' in market_type:
                normalized_market_type = 'Moneyline'
            else:
                normalized_market_type = market_type
            
            # Determine pick direction (already done above, but need it here too)
            current_pick = (alert.pick or alert_data.get('pick', '')).upper()
            is_over = 'OVER' in current_pick or current_pick == 'OVER'
            is_under = 'UNDER' in current_pick or current_pick == 'UNDER'
            
            # CRITICAL: Calculate effective pick_direction based on side (same logic as above)
            if normalized_market_type == 'Total Points':
                # For totals, side determines effective direction
                if side.lower() == 'no':
                    # NO on Over = Under, NO on Under = Over
                    if is_over:
                        pick_direction = 'Under'  # Effective direction
                    elif is_under:
                        pick_direction = 'Over'  # Effective direction
                    else:
                        pick_direction = 'Unknown'
                else:
                    # YES side: direction matches pick
                    pick_direction = 'Over' if is_over else ('Under' if is_under else 'Unknown')
            else:
                pick_direction = current_pick if not is_over and not is_under else 'Unknown'
            
            # Store data for future reverse middle checks (use effective pick)
            effective_pick = alert.pick or alert_data.get('pick', '')
            
            # CRITICAL: For spreads with NO side, we're betting the OPPOSING team
            # Example: Alert "Lakers -10.5", betting NO = betting Hawks +10.5
            # We need to store "Hawks" as the pick, not "Lakers", for reverse middle detection
            if normalized_market_type == 'Point Spread' and side.lower() == 'no' and not is_over and not is_under:
                # Extract teams from alert.teams (format: "Team A @ Team B" or "Team A VS Team B")
                teams_str = alert.teams or alert_data.get('teams', '')
                if teams_str:
                    import re
                    parts = re.split(r'\s*[@]\s*|\s*VS\s*', teams_str, maxsplit=1, flags=re.IGNORECASE)
                    if len(parts) == 2:
                        team1 = parts[0].strip()
                        team2 = parts[1].strip()
                        pick_upper = effective_pick.upper()
                        
                        # Determine which team is the pick
                        pick_is_team1 = any(word in pick_upper for word in team1.split() if len(word) > 3) or pick_upper in team1.upper() or team1.upper() in pick_upper
                        pick_is_team2 = any(word in pick_upper for word in team2.split() if len(word) > 3) or pick_upper in team2.upper() or team2.upper() in pick_upper
                        
                        # If betting NO on pick team, we're effectively betting the opponent
                        if pick_is_team1:
                            effective_pick = team2  # Betting NO on team1 = betting team2
                        elif pick_is_team2:
                            effective_pick = team1  # Betting NO on team2 = betting team1
                        print(f"[AUTO-BET] [REVERSE MIDDLE] Spread NO bet: Alert pick '{alert.pick}' -> Effective pick '{effective_pick}' (opposing team)")
            
            if normalized_market_type == 'Total Points' and side.lower() == 'no':
                # Calculate effective pick for totals with NO side
                if is_over:
                    effective_pick = 'Under'
                elif is_under:
                    effective_pick = 'Over'
            
            # Store data for future reverse middle checks
            try:
                qualifier = alert.qualifier or alert_data.get('qualifier', '')
                if qualifier:
                    if 'SPREAD' in ticker or 'Point Spread' in market_type:
                        qualifier_clean = qualifier.replace('*', '').strip()
                        line_value = float(qualifier_clean)
                        # CRITICAL: For NO bets on spreads, flip the sign
                        # Example: Alert "Lakers -10.5", betting NO = betting Hawks +10.5
                        if side.lower() == 'no':
                            line_value = -line_value  # Flip sign: -10.5 -> +10.5
                            print(f"[AUTO-BET] [REVERSE MIDDLE] Spread NO bet: Flipped line from {qualifier_clean} to {line_value}")
                    else:
                        qualifier_clean = qualifier.replace('+', '').replace('*', '').strip()
                        line_value = float(qualifier_clean)
                else:
                    line_value = None
            except:
                line_value = None
            
            auto_bet_submarket_data[submarket_key] = {
                'line': line_value,
                'pick': effective_pick,  # Store effective pick (already calculated above)
                'qualifier': qualifier,
                'market_type': normalized_market_type,
                'teams': game_name,
                'pick_direction': pick_direction,  # Already effective direction
                'raw_pick': alert.pick or alert_data.get('pick', ''),  # Keep raw for reference
                'side': side,  # Store side for reference
                'actual_cost': None,  # Will be set after successful bet
                'target_amount': bet_amount  # Store target amount for topping up logic
            }
            # CRITICAL: Do NOT add to auto_bet_games here - wait until bet succeeds
            # This prevents failed attempts from counting toward the limit
            # We'll add it after the bet succeeds (see line ~1504)
            
            # Determine bet amount based on market type, then apply special rules
            # Priority: NHL overs > Market type > PX+Novig multiplier
            # Use the amount we read at the start (current_amount) to ensure consistency
            global nhl_over_bet_amount, px_novig_multiplier, moneyline_bet_amount, total_bet_amount, spread_bet_amount
            bet_amount = current_amount
            
            # Check for NHL overs first (takes precedence)
            is_nhl_over = False
            if ticker and ticker.upper().startswith('KXNHL'):
                current_pick = (alert.pick or alert_data.get('pick', '')).upper()
                if 'OVER' in current_pick and ('total' in market_type.lower() or 'TOTAL' in ticker):
                    bet_amount = nhl_over_bet_amount
                    is_nhl_over = True
            
            # Apply market-type specific bet amounts (only if not NHL over)
            if not is_nhl_over:
                if normalized_market_type == 'Moneyline':
                    bet_amount = moneyline_bet_amount
                    print(f"[AUTO-BET] Moneyline detected - using ${moneyline_bet_amount:.2f} bet amount (35.75% ROI justifies larger bets)")
                elif normalized_market_type == 'Total Points' or normalized_market_type == 'Total Goals':
                    bet_amount = total_bet_amount
                    print(f"[AUTO-BET] Total detected - using ${total_bet_amount:.2f} bet amount")
                elif normalized_market_type == 'Point Spread':
                    bet_amount = spread_bet_amount
                    print(f"[AUTO-BET] Point Spread detected - using ${spread_bet_amount:.2f} bet amount (1.10% ROI - reduced bet size)")
            
            # Apply multiplier if both ProphetX and Novig are in devig books (only if not NHL over)
            # This multiplies the market-type specific amount
            if not is_nhl_over:
                devig_books = getattr(alert, 'devig_books', []) or alert_data.get('devig_books', [])
                if devig_books and 'ProphetX' in devig_books and 'Novig' in devig_books:
                    base_amount = bet_amount  # Store before multiplier
                    bet_amount = bet_amount * px_novig_multiplier
                    print(f"[AUTO-BET] ProphetX + Novig detected - applying {px_novig_multiplier}x multiplier: ${base_amount:.2f} -> ${bet_amount:.2f}")
            
            # Calculate contracts and place bet (minimal logging for speed)
            contracts = market_matcher.calculate_contracts_from_dollars(bet_amount, expected_price_cents or 50)
            print(f"[AUTO-BET] Placing bet: {alert.teams} - {alert.pick} | {ticker} {side} | ${bet_amount:.2f} ({contracts} contracts @ {expected_price_cents}¢)")
            
            # Place the bet (lock is still held)
            # Strategy: Taker limit order with 1 second expiration - guarantees execution at BB price
            # ========== COMPREHENSIVE AUTO-BET LOGGING ==========
            print(f"\n{'='*80}")
            print(f"[AUTO-BET] ========== AUTO-BET ORDER PLACEMENT STARTED ==========")
            print(f"[AUTO-BET] Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
            print(f"[AUTO-BET] Alert ID: {alert_id}")
            print(f"[AUTO-BET] Submarket Key: {submarket_key}")
            
            # COMPREHENSIVE ALERT LOGGING
            print(f"[AUTO-BET] ========== ALERT DETAILS ==========")
            print(f"[AUTO-BET]   Teams: {alert.teams or 'N/A'}")
            print(f"[AUTO-BET]   Pick: {alert.pick or 'N/A'}")
            print(f"[AUTO-BET]   Qualifier: {alert.qualifier or 'N/A'}")
            print(f"[AUTO-BET]   Market Type: {alert.market_type or 'N/A'}")
            print(f"[AUTO-BET]   EV: {alert.ev_percent:.2f}%")
            print(f"[AUTO-BET]   Alert Odds: {alert.odds or 'N/A'}")
            print(f"[AUTO-BET]   Expected Profit: ${alert.expected_profit:.2f}")
            print(f"[AUTO-BET]   Alert Liquidity: ${alert.liquidity:.2f}")
            
            # COMPREHENSIVE MARKET LOGGING
            print(f"[AUTO-BET] ========== MARKET DETAILS ==========")
            print(f"[AUTO-BET]   Ticker: {ticker}")
            print(f"[AUTO-BET]   Event Ticker: {alert_data.get('event_ticker', 'N/A')}")
            print(f"[AUTO-BET]   Side Determined: {side}")
            # CRITICAL: Log both BB fields to confirm we're using the correct one
            bb_odds = alert.odds or 'N/A'
            bb_price_cents = expected_price_cents
            bb_price_american_odds = price_to_american_odds(bb_price_cents) if bb_price_cents else 'N/A'
            print(f"[AUTO-BET] ========== ALERT PRICE DATA ==========")
            print(f"[AUTO-BET]   BB 'odds' field: {bb_odds} (effective price AFTER fees - BB calculates EV based on this)")
            print(f"[AUTO-BET]   BB 'price' field: {bb_price_cents}¢ = {bb_price_american_odds} American odds (order price BEFORE fees)")
            print(f"[AUTO-BET]   ✅ LIMIT ORDER PRICE: {bb_price_cents}¢ ({bb_price_american_odds}) - Place order here to get {bb_odds} after fees")
            print(f"[AUTO-BET]   Expected Price: {expected_price_cents} cents ({expected_price_cents/100:.2f}¢)")
            print(f"[AUTO-BET]   Requested Contracts: {contracts}")
            print(f"[AUTO-BET]   Bet Amount: ${bet_amount:.2f}")
            
            # Try to get market subtitles for validation logging
            try:
                market_data = await kalshi_client.get_market_by_ticker(ticker)
                if market_data:
                    # Market data is nested: {'market': {...}}
                    market_info = market_data.get('market', {}) if isinstance(market_data, dict) else market_data
                    yes_subtitle = market_info.get('yes_sub_title', 'N/A') if isinstance(market_info, dict) else 'N/A'
                    no_subtitle = market_info.get('no_sub_title', 'N/A') if isinstance(market_info, dict) else 'N/A'
                    market_title = market_info.get('title', 'N/A') if isinstance(market_info, dict) else 'N/A'
                    print(f"[AUTO-BET]   Market Title: {market_title}")
                    print(f"[AUTO-BET]   YES Subtitle: {yes_subtitle}")
                    print(f"[AUTO-BET]   NO Subtitle: {no_subtitle}")
                    
                    # CRITICAL VALIDATION: Double-check side before placing bet
                    print(f"[AUTO-BET] ========== SIDE VALIDATION ==========")
                    pick_upper = (alert.pick or '').upper()
                    market_type_lower = (alert.market_type or '').lower()
                    
                    # Extract team names for validation
                    teams_str = (alert.teams or '').upper()
                    team1 = None
                    team2 = None
                    if teams_str:
                        import re
                        parts = re.split(r'\s*[@]\s*|\s*VS\s*', teams_str, maxsplit=1, flags=re.IGNORECASE)
                        if len(parts) == 2:
                            team1 = parts[0].strip()
                            team2 = parts[1].strip()
                    
                    # Check if pick is in subtitles
                    pick_words = [w for w in pick_upper.split() if len(w) > 3] if len(pick_upper.split()) > 1 else [pick_upper]
                    yes_contains_pick = any(word in yes_subtitle.upper() for word in pick_words) or pick_upper in yes_subtitle.upper() if yes_subtitle != 'N/A' else False
                    no_contains_pick = any(word in no_subtitle.upper() for word in pick_words) or pick_upper in no_subtitle.upper() if no_subtitle != 'N/A' else False
                    
                    # Enhanced team-based matching for moneylines
                    if market_type_lower == 'moneyline' and team1 and team2:
                        pick_is_team1 = any(word in pick_upper for word in team1.split() if len(word) > 3) or pick_upper in team1 or team1 in pick_upper
                        pick_is_team2 = any(word in pick_upper for word in team2.split() if len(word) > 3) or pick_upper in team2 or team2 in pick_upper
                        
                        if pick_is_team1:
                            team1_in_yes = any(word in yes_subtitle.upper() for word in team1.split() if len(word) > 3) or team1 in yes_subtitle.upper()
                            team1_in_no = any(word in no_subtitle.upper() for word in team1.split() if len(word) > 3) or team1 in no_subtitle.upper()
                            if team1_in_yes and not team1_in_no:
                                yes_contains_pick = True
                                no_contains_pick = False
                            elif team1_in_no and not team1_in_yes:
                                yes_contains_pick = False
                                no_contains_pick = True
                        elif pick_is_team2:
                            team2_in_yes = any(word in yes_subtitle.upper() for word in team2.split() if len(word) > 3) or team2 in yes_subtitle.upper()
                            team2_in_no = any(word in no_subtitle.upper() for word in team2.split() if len(word) > 3) or team2 in no_subtitle.upper()
                            if team2_in_yes and not team2_in_no:
                                yes_contains_pick = True
                                no_contains_pick = False
                            elif team2_in_no and not team2_in_yes:
                                yes_contains_pick = False
                                no_contains_pick = True
                    
                    print(f"[AUTO-BET]   Pick '{alert.pick or 'N/A'}' in YES subtitle: {yes_contains_pick}")
                    print(f"[AUTO-BET]   Pick '{alert.pick or 'N/A'}' in NO subtitle: {no_contains_pick}")
                    
                    # SPECIAL HANDLING FOR TOTALS: Over/Under logic is simple - trust the side determination
                    # Kalshi subtitles are often buggy (both say "Over X"), so we can't rely on them
                    is_total_market = 'total' in market_type_lower or 'over' in market_type_lower or 'under' in market_type_lower
                    if is_total_market:
                        pick_upper_check = (alert.pick or '').upper()
                        if pick_upper_check == 'OVER' and side == 'yes':
                            # Over = YES, this is always correct regardless of subtitle
                            yes_contains_pick = True
                            print(f"[AUTO-BET]   ✅ TOTAL MARKET: Over → YES (trusting side determination, ignoring buggy subtitles)")
                        elif pick_upper_check == 'UNDER' and side == 'no':
                            # Under = NO, this is always correct regardless of subtitle
                            no_contains_pick = True
                            print(f"[AUTO-BET]   ✅ TOTAL MARKET: Under → NO (trusting side determination, ignoring buggy subtitles)")
                        elif pick_upper_check == 'UNDER' and side == 'yes':
                            # This shouldn't happen, but if it does, it's wrong
                            print(f"[AUTO-BET]   🚨 CRITICAL: Under pick but side is YES - REJECTING!")
                            error_msg = "CRITICAL: Under pick but side is YES - wrong side!"
                            store_failed_auto_bet(
                                alert_id=alert_id,
                                alert=alert,
                                alert_data=alert_data,
                                error=error_msg,
                                reason="Side determination error: Under pick should be NO, not YES",
                                ticker=ticker,
                                side=side,
                                ev_percent=ev_percent,
                                odds=alert_data.get('american_odds'),
                                filter_name=getattr(alert, 'filter_name', None)
                            )
                            socketio.emit('auto_bet_failed', {
                                'alert_id': alert_id,
                                'error': error_msg,
                                'market': f"{alert.teams} - {alert.pick}"
                            })
                            await cleanup_submarket()
                            return
                        elif pick_upper_check == 'OVER' and side == 'no':
                            # This shouldn't happen, but if it does, it's wrong
                            print(f"[AUTO-BET]   🚨 CRITICAL: Over pick but side is NO - REJECTING!")
                            error_msg = "CRITICAL: Over pick but side is NO - wrong side!"
                            store_failed_auto_bet(
                                alert_id=alert_id,
                                alert=alert,
                                alert_data=alert_data,
                                error=error_msg,
                                reason="Side determination error: Over pick should be YES, not NO",
                                ticker=ticker,
                                side=side,
                                ev_percent=ev_percent,
                                odds=alert_data.get('american_odds'),
                                filter_name=getattr(alert, 'filter_name', None)
                            )
                            socketio.emit('auto_bet_failed', {
                                'alert_id': alert_id,
                                'error': error_msg,
                                'market': f"{alert.teams} - {alert.pick}"
                            })
                            await cleanup_submarket()
                            return
                    
                    # SPECIAL HANDLING FOR MONEYLINES: When subtitles are N/A or buggy, trust ticker-based side determination
                    # For moneylines, we already determined the side using the ticker (most reliable method)
                    is_moneyline = market_type_lower == 'moneyline' or 'game' in market_type_lower
                    if is_moneyline and (yes_subtitle == 'N/A' or no_subtitle == 'N/A' or yes_subtitle == no_subtitle):
                        # Trust the ticker-based side determination (already done in determine_side)
                        pass  # Silently trust ticker-based determination
                        yes_contains_pick = True if side == 'yes' else False
                        no_contains_pick = True if side == 'no' else False
                    
                    # CRITICAL: REJECT bet if side is wrong (prevents betting wrong side)
                    # BUT: Skip this check for totals/moneylines/spreads if subtitles are N/A or buggy (Kalshi bug)
                    is_spread = market_type_lower in ['point spread', 'spread', 'puck line']
                    subtitles_buggy = (yes_subtitle == 'N/A' or no_subtitle == 'N/A' or yes_subtitle == no_subtitle)
                    
                    if side == 'yes' and not yes_contains_pick:
                        # For totals/moneylines/spreads with N/A or buggy subtitles, trust the side determination
                        if (is_total_market or is_moneyline or is_spread) and subtitles_buggy:
                            pass  # Silently trust ticker-based determination
                        else:
                            print(f"[AUTO-BET]   🚨 CRITICAL: YES side does NOT contain pick - REJECTING BET to prevent wrong side!")
                            print(f"[AUTO-BET]   YES subtitle: '{yes_subtitle}', NO subtitle: '{no_subtitle}', Pick: '{alert.pick}'")
                            if no_contains_pick:
                                print(f"[AUTO-BET]   ✅ CORRECTION: NO subtitle contains pick - would change to NO, but REJECTING for safety")
                            error_msg = f"CRITICAL: Side validation failed. YES subtitle doesn't contain pick '{alert.pick}'"
                            store_failed_auto_bet(
                                alert_id=alert_id,
                                alert=alert,
                                alert_data=alert_data,
                                error=error_msg,
                                reason=f"YES subtitle: '{yes_subtitle}', NO subtitle: '{no_subtitle}', Pick: '{alert.pick}'",
                                ticker=ticker,
                                side=side,
                                ev_percent=ev_percent,
                                odds=alert_data.get('american_odds'),
                                filter_name=getattr(alert, 'filter_name', None)
                            )
                            socketio.emit('auto_bet_failed', {
                                'alert_id': alert_id,
                                'error': error_msg,
                                'market': f"{alert.teams} - {alert.pick}"
                            })
                            await cleanup_submarket()
                            return
                    elif side == 'no' and not no_contains_pick:
                        # For moneylines with N/A subtitles, trust ticker-based side determination
                        if is_moneyline and (yes_subtitle == 'N/A' or no_subtitle == 'N/A' or yes_subtitle == no_subtitle):
                            pass  # Silently trust ticker-based determination
                            no_contains_pick = True
                        else:
                            # For non-tie sports, NO on opponent is valid (equivalent to YES on pick)
                            sport = determine_sport_from_ticker(ticker)
                            is_non_tie_sport = sport in ["NBA", "NHL", "MLB", "NCAA Men's Basketball"]
                            opponent_in_subtitle = False
                            
                            if is_non_tie_sport and team1 and team2:
                                pick_is_team1 = any(word in pick_upper for word in team1.split() if len(word) > 3) or pick_upper in team1 or team1 in pick_upper
                                pick_is_team2 = any(word in pick_upper for word in team2.split() if len(word) > 3) or pick_upper in team2 or team2 in pick_upper
                                
                                if pick_is_team1:
                                    opponent = team2
                                elif pick_is_team2:
                                    opponent = team1
                                else:
                                    opponent = None
                                
                                if opponent:
                                    opponent_words = [w for w in opponent.split() if len(w) > 3]
                                    opponent_in_no = any(word in no_subtitle.upper() for word in opponent_words) or opponent in no_subtitle.upper()
                                    if opponent_in_no:
                                        opponent_in_subtitle = True
                                        print(f"[AUTO-BET]   ✅ VALID: For non-tie sport ({sport}), NO on opponent '{opponent}' = YES on pick '{alert.pick}'")
                            
                            if not opponent_in_subtitle and not no_contains_pick:
                                # For totals/moneylines with N/A subtitles, trust the side determination
                                if (is_total_market or is_moneyline) and (yes_subtitle == 'N/A' or no_subtitle == 'N/A' or yes_subtitle == no_subtitle):
                                    print(f"[AUTO-BET]   ⚠️  Market with N/A/buggy subtitles - trusting side determination (side={side})")
                                else:
                                    print(f"[AUTO-BET]   🚨 CRITICAL: NO side does NOT contain pick - REJECTING BET to prevent wrong side!")
                                    print(f"[AUTO-BET]   YES subtitle: '{yes_subtitle}', NO subtitle: '{no_subtitle}', Pick: '{alert.pick}'")
                                    if yes_contains_pick:
                                        print(f"[AUTO-BET]   ✅ CORRECTION: YES subtitle contains pick - would change to YES, but REJECTING for safety")
                                    error_msg = f"CRITICAL: Side validation failed. NO subtitle doesn't contain pick '{alert.pick}'"
                                    store_failed_auto_bet(
                                        alert_id=alert_id,
                                        alert=alert,
                                        alert_data=alert_data,
                                        error=error_msg,
                                        reason=f"YES subtitle: '{yes_subtitle}', NO subtitle: '{no_subtitle}', Pick: '{alert.pick}'",
                                        ticker=ticker,
                                        side=side,
                                        ev_percent=ev_percent,
                                        odds=alert_data.get('american_odds'),
                                        filter_name=getattr(alert, 'filter_name', None)
                                    )
                                    socketio.emit('auto_bet_failed', {
                                        'alert_id': alert_id,
                                        'error': error_msg,
                                        'market': f"{alert.teams} - {alert.pick}"
                                    })
                                    await cleanup_submarket()
                                    return
                    
                    # Log validation result
                    if side == 'yes':
                        print(f"[AUTO-BET]   ✅ Side validation PASSED: YES side contains pick")
                    elif side == 'no':
                        print(f"[AUTO-BET]   ✅ Side validation PASSED: NO side contains pick (or opponent for non-tie sport)")
            except Exception as e:
                print(f"[AUTO-BET]   ⚠️  Could not fetch market data for logging: {e}")
            
            print(f"[AUTO-BET] ==========================================")
            # ========== END COMPREHENSIVE LOGGING ==========
            
            # PER-EVENT MAX BET CHECK: Already done inside lock above (atomic check)
            # This check is now redundant but kept for logging/debugging
            # The actual check happens inside the lock to prevent race conditions
            
            # NOTE: Submarket was already marked as "bet" at line 1855 (after duplicate check)
            # This ensures only one task can proceed past the duplicate check
            # The lock is still held from the async with block starting at line 1833
            
            # Strategy: Taker limit order with 1 second expiration - guarantees we trade at BB price
            # We use the BB 'price' field for the order, which after fees nets to the 'odds' field shown
            # This ensures we execute at the price BB calculated, maintaining the EV shown
            import time as time_module
            # CRITICAL: 1 second expiration - orders must fill immediately or be cancelled
            # This prevents orders sitting in the orderbook and getting filled at non-EV prices
            # Kalshi expects expiration_ts in SECONDS (Unix timestamp), not milliseconds
            expiration_ts = int(time_module.time()) + 1  # 1 second expiration
            
            # Track order placement timing
            order_placement_start = time.time()
            
            try:
                # CRITICAL: expected_price_cents is the limit price from the alert (e.g. 63¢)
                # The 'odds' field (-183) is the effective price AFTER fees (what BB uses for EV calculation)
                # Example: price=63¢ (-170) = order price BEFORE fees
                #          odds=-183 = effective price AFTER Kalshi fees
                #          BB calculates EV based on -183 (after fees), but we place order at 63¢ (before fees)
                # ========== PRE-ORDER PLACEMENT SUMMARY ==========
                # Get event_base and current_total for logging (event_base was already calculated earlier in the lock section)
                # event_base is already defined in the lock section above, but we need to recalculate it here for logging
                # since we're outside the lock now
                def extract_event_base_for_logging(ticker_str):
                    if not ticker_str:
                        return None
                    parts = ticker_str.split('-')
                    if len(parts) >= 3:
                        # Format: SERIES-DATE-TEAMS-SUFFIX -> SERIES-DATE-TEAMS
                        return '-'.join(parts[:-1])
                    return None
                
                event_base_for_logging = extract_event_base_for_logging(ticker)
                event_total_bet = auto_bet_event_totals.get(event_base_for_logging, 0.0) if event_base_for_logging else 0.0
                
                print(f"[AUTO-BET] ========== ALL CHECKS PASSED - READY TO PLACE ORDER ==========")
                print(f"[AUTO-BET] ✅ EV Check: {ev_percent:.2f}% (>= {current_ev_min}%, <= {current_ev_max}%)")
                print(f"[AUTO-BET] ✅ Odds Check: {american_odds_str} (in range {current_odds_min}-{current_odds_max})")
                print(f"[AUTO-BET] ✅ Duplicate Check: Submarket {submarket_key} not already bet")
                print(f"[AUTO-BET] ✅ Reverse Middle Check: No conflicting positions detected")
                print(f"[AUTO-BET] ✅ Per-Event Max: ${event_total_bet:.2f} / ${per_event_max_bet:.2f}")
                print(f"[AUTO-BET] ✅ Liquidity Check: Sufficient liquidity available")
                print(f"[AUTO-BET] ✅ Price Check: Expected {expected_price_cents}¢, will validate at order time")
                print(f"[AUTO-BET] ========== ORDER PARAMETERS ==========")
                print(f"[AUTO-BET]   Ticker: {ticker}")
                print(f"[AUTO-BET]   Side: {side}")
                print(f"[AUTO-BET]   Contracts: {contracts}")
                print(f"[AUTO-BET]   Bet Amount: ${bet_amount:.2f}")
                print(f"[AUTO-BET]   Expected Price: {expected_price_cents}¢")
                print(f"[AUTO-BET]   Price Tolerance: ±2¢ better allowed, ±1¢ worse allowed")
                print(f"[AUTO-BET] ========== CALLING place_order() NOW ==========")
                print(f"[AUTO-BET] [TIMING] Order placement started at {time.strftime('%H:%M:%S.%f', time.localtime(order_placement_start))}")
                print(f"[AUTO-BET] [TIMING] Step timings so far: {step_timings}")
                
                # TIMING: Track order placement
                order_api_start = time.time()
                print(f"[AUTO-BET] [TIMING] [{time.strftime('%H:%M:%S.%f', time.localtime(order_api_start))}] ⚡ Starting order placement API call")
                result = await kalshi_client.place_order(
                    ticker=ticker,
                    side=side,
                    count=contracts,
                    validate_odds=True,  # Keep validation to prevent bad fills from wrong submarket matches
                    expected_price_cents=expected_price_cents,  # BB 'price' field (63¢) - order price BEFORE fees
                    max_liquidity_dollars=bet_amount,
                    post_only=False,  # Taker order - instant fill at BB price
                    expiration_ts=expiration_ts  # 1 second expiration
                )
                
                order_api_end = time.time()
                order_api_duration_ms = (order_api_end - order_api_start) * 1000
                step_timings['place_order_api'] = order_api_duration_ms
                print(f"[AUTO-BET] [TIMING] ⚡ Order API call completed in {order_api_duration_ms:.1f}ms")
                
                order_placement_end = time.time()
                order_placement_duration_ms = (order_placement_end - order_placement_start) * 1000
                step_timings['order_placement_total'] = order_placement_duration_ms
                print(f"[AUTO-BET] [TIMING] ⚡ Order placement total (including validation) completed in {order_placement_duration_ms:.1f}ms")
                print(f"[AUTO-BET] [RESULT] Order result received: success={result.get('success') if result else 'None'}")
                
                # CRITICAL: Check for price delta errors IMMEDIATELY and release lock fast
                # Price delta errors mean wrong submarket - no need to hold lock for cleanup
                if result and not result.get('success'):
                    error = result.get('error', '')
                    reason = result.get('reason', '')
                    if 'Price delta too large' in reason or ('delta' in result and result.get('delta', 0) > 2):
                        # Price delta error - release lock IMMEDIATELY and return early
                        # Don't do any slow operations while holding lock
                        price_delta = result.get('delta', 0)
                        expected_price = result.get('expected', 0)
                        current_price = result.get('current', 0)
                        print(f"[AUTO-BET] ⚠️  PRICE DELTA ERROR: {price_delta}¢ delta - releasing lock IMMEDIATELY")
                        print(f"[AUTO-BET]   Expected: {expected_price}¢, Current: {current_price}¢")
                        # Release lock immediately (will be released in finally, but log it)
                        # Remove from bet set immediately
                        if submarket_key and submarket_key in auto_bet_submarkets:
                            auto_bet_submarkets.discard(submarket_key)
                            if event_base:
                                current_total = auto_bet_event_totals.get(event_base, 0.0)
                                auto_bet_event_totals[event_base] = max(0.0, current_total - bet_amount)
                        # Clean up processing sets immediately
                        auto_bet_processing_submarkets.discard(submarket_key)
                        auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                        auto_bet_submarket_tasks.pop(submarket_key, None)
                        # Log to failed-bets (but do it after lock is released)
                        error_msg = result.get('error', 'Unknown error')
                        full_error_msg = f"{error_msg}: {reason}" if reason else error_msg
                        # Store failure details for logging after lock release
                        failure_details = {
                            'error': full_error_msg,
                            'reason': f"Order placement failed: {error_msg}" + (f" - {reason}" if reason else ""),
                            'expected_price': expected_price,
                            'current_price': current_price,
                            'price_delta': price_delta
                        }
                        # Return early - lock will be released in finally block
                        # We'll log the failure after lock is released
                        result = failure_details  # Store for logging after lock release
                    else:
                        # Other error - continue with normal error handling
                        if result.get('success'):
                            print(f"[AUTO-BET] ✅ ORDER SUCCESS: Order ID {result.get('order_id', 'N/A')}")
                        else:
                            print(f"[AUTO-BET] ❌ ORDER FAILED: {result.get('error', 'Unknown error')}")
                            if result.get('reason'):
                                print(f"[AUTO-BET]   Reason: {result.get('reason')}")
                            if result.get('expected') is not None and result.get('current') is not None:
                                price_delta = abs(result.get('expected', 0) - result.get('current', 0))
                                print(f"[AUTO-BET]   Price: Expected {result.get('expected')}¢, Got {result.get('current')}¢ (delta: {price_delta}¢)")
                elif result and result.get('success'):
                    print(f"[AUTO-BET] ✅ ORDER SUCCESS: Order ID {result.get('order_id', 'N/A')}")
            except asyncio.TimeoutError:
                print(f"[AUTO-BET] ❌ Timeout: {ticker} {side}")
                auto_bet_processing_submarkets.discard(submarket_key)
                auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                auto_bet_submarket_tasks.pop(submarket_key, None)
                if submarket_key in auto_bet_submarkets:
                    auto_bet_submarkets.remove(submarket_key)
                    
                    # CRITICAL: Subtract reserved bet_amount from event total (we reserved it earlier)
                    if event_base:
                        current_total = auto_bet_event_totals.get(event_base, 0.0)
                        auto_bet_event_totals[event_base] = max(0.0, current_total - bet_amount)
                        print(f"[AUTO-BET] Subtracted reserved ${bet_amount:.2f} from event total for {event_base}: ${auto_bet_event_totals[event_base]:.2f} / ${per_event_max_bet:.2f}")
                    
                    submarket_data = auto_bet_submarket_data.pop(submarket_key, None)
                    if submarket_data:
                        game_name = submarket_data.get('teams', alert.teams)
                        market_type = submarket_data.get('market_type', '')
                        pick_direction = submarket_data.get('pick_direction', '')
                        if (game_name in auto_bet_games and 
                            market_type in auto_bet_games[game_name] and
                            pick_direction in auto_bet_games[game_name][market_type]):
                            if submarket_key in auto_bet_games[game_name][market_type][pick_direction]:
                                auto_bet_games[game_name][market_type][pick_direction].remove(submarket_key)
                error_msg = "Order placement timed out"
                store_failed_auto_bet(
                    alert_id=alert_id,
                    alert=alert,
                    alert_data=alert_data,
                    error=error_msg,
                    reason="Order placement took longer than expected timeout",
                    ticker=ticker,
                    side=side,
                    expected_price=expected_price_cents,
                    ev_percent=ev_percent,
                    odds=alert_data.get('american_odds'),
                    filter_name=getattr(alert, 'filter_name', None)
                )
                socketio.emit('auto_bet_failed', {
                    'alert_id': alert_id,
                    'error': error_msg,
                    'market': f"{alert.teams} - {alert.pick}"
                })
                return
            except Exception as order_error:
                print(f"[AUTO-BET] ❌ Exception: {str(order_error)} | {ticker} {side}")
                import traceback
                traceback.print_exc()
                auto_bet_processing_submarkets.discard(submarket_key)
                auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                auto_bet_submarket_tasks.pop(submarket_key, None)
                if submarket_key in auto_bet_submarkets:
                    auto_bet_submarkets.remove(submarket_key)
                    
                    # CRITICAL: Subtract reserved bet_amount from event total (we reserved it earlier)
                    if event_base:
                        current_total = auto_bet_event_totals.get(event_base, 0.0)
                        auto_bet_event_totals[event_base] = max(0.0, current_total - bet_amount)
                        print(f"[AUTO-BET] Subtracted reserved ${bet_amount:.2f} from event total for {event_base}: ${auto_bet_event_totals[event_base]:.2f} / ${per_event_max_bet:.2f}")
                    
                    submarket_data = auto_bet_submarket_data.pop(submarket_key, None)
                    if submarket_data:
                        game_name = submarket_data.get('teams', alert.teams)
                        market_type = submarket_data.get('market_type', '')
                        pick_direction = submarket_data.get('pick_direction', '')
                        if (game_name in auto_bet_games and 
                            market_type in auto_bet_games[game_name] and
                            pick_direction in auto_bet_games[game_name][market_type]):
                            if submarket_key in auto_bet_games[game_name][market_type][pick_direction]:
                                auto_bet_games[game_name][market_type][pick_direction].remove(submarket_key)
                trade_end_time = time.time()
                total_trade_duration = (trade_end_time - trade_start_time) * 1000  # ms
                order_placement_duration = (trade_end_time - order_placement_start) * 1000  # ms
                error_msg = str(order_error)
                import traceback
                traceback_str = traceback.format_exc()
                store_failed_auto_bet(
                    alert_id=alert_id,
                    alert=alert,
                    alert_data=alert_data,
                    error=error_msg,
                    reason="Exception during order placement",
                    ticker=ticker,
                    side=side,
                    expected_price=expected_price_cents,
                    ev_percent=ev_percent,
                    odds=alert_data.get('american_odds'),
                    filter_name=getattr(alert, 'filter_name', None),
                    additional_logs=[
                        f"========== TRADE LIFECYCLE ==========",
                        f"Alert ID: {alert_id}",
                        f"Market: {teams} - {pick}",
                        f"EV: {ev_percent:.2f}%",
                        f"Filter: {alert_filter_name}",
                        f"Ticker: {ticker}, Side: {side}",
                        f"Expected price: {expected_price_cents}¢" if expected_price_cents else "Expected price: N/A",
                        f"Total trade time: {total_trade_duration:.1f}ms (from alert to exception)",
                        f"Order placement time: {order_placement_duration:.1f}ms",
                        f"Exception: {error_msg}",
                        f"Traceback:",
                        traceback_str,
                        f"====================================="
                    ]
                )
                socketio.emit('auto_bet_failed', {
                    'alert_id': alert_id,
                    'error': error_msg,
                    'market': f"{alert.teams} - {alert.pick}"
                })
                return
            
            # CRITICAL: Check for price delta errors - if found, clean up and return immediately
            # Lock already released earlier, so we can do cleanup and logging without blocking
            if result and not result.get('success'):
                error = result.get('error', '')
                reason = result.get('reason', '')
                if 'Price delta too large' in reason or ('delta' in result and result.get('delta', 0) > 2):
                    # Price delta error - clean up and log with comprehensive details
                    price_delta = result.get('delta', 0)
                    expected_price = result.get('expected', 0)
                    current_price = result.get('current', 0)
                    print(f"[AUTO-BET] ⚠️  PRICE DELTA ERROR: {price_delta}¢ delta - cleaning up immediately")
                    print(f"[AUTO-BET]   Expected: {expected_price}¢, Current: {current_price}¢")
                    # Clean up immediately (lock already released)
                    if submarket_key and submarket_key in auto_bet_submarkets:
                        auto_bet_submarkets.discard(submarket_key)
                        if event_base:
                            current_total = auto_bet_event_totals.get(event_base, 0.0)
                            auto_bet_event_totals[event_base] = max(0.0, current_total - bet_amount)
                    auto_bet_processing_submarkets.discard(submarket_key)
                    auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                    auto_bet_submarket_tasks.pop(submarket_key, None)
                    
                    # Log failure with comprehensive details
                    error_msg = result.get('error', 'Unknown error')
                    full_error_msg = f"{error_msg}: {reason}" if reason else error_msg
                    trade_end_time = time.time()
                    total_trade_duration = (trade_end_time - trade_start_time) * 1000
                    order_placement_duration = (trade_end_time - order_placement_start) * 1000
                    
                    # Comprehensive logging for price delta errors
                    additional_logs = [
                        f"========== PRICE DELTA ERROR ==========",
                        f"Alert ID: {alert_id}",
                        f"Market: {alert.teams} - {alert.pick}",
                        f"EV: {ev_percent:.2f}%",
                        f"Filter: {alert_filter_name}",
                        f"Ticker: {ticker}",
                        f"Side: {side}",
                        f"Expected Price: {expected_price}¢",
                        f"Current Price: {current_price}¢",
                        f"Price Delta: {price_delta}¢",
                        f"Total trade time: {total_trade_duration:.1f}ms",
                        f"Order placement time: {order_placement_duration:.1f}ms",
                        f"",
                        f"Error: {full_error_msg}",
                        f"Reason: Price delta > 2¢ indicates likely wrong submarket match",
                        f"",
                        f"========== DIAGNOSTICS ==========",
                        f"Submarket key: {submarket_key}",
                        f"In processing set: {submarket_key in auto_bet_processing_submarkets}",
                        f"In bet set: {submarket_key in auto_bet_submarkets}",
                        f"Processing alert_id: {auto_bet_submarket_to_alert_id.get(submarket_key, 'N/A')}",
                        f"Task exists: {submarket_key in auto_bet_submarket_tasks}",
                        f"Task done: {auto_bet_submarket_tasks.get(submarket_key).done() if submarket_key in auto_bet_submarket_tasks and auto_bet_submarket_tasks.get(submarket_key) else 'N/A'}",
                        f"",
                        f"Lock state: locked={auto_bet_lock.locked() if auto_bet_lock else 'N/A'}",
                        f"Lock waiters: {len(auto_bet_lock._waiters) if (auto_bet_lock and hasattr(auto_bet_lock, '_waiters') and auto_bet_lock._waiters is not None) else 'unknown'}",
                        f"Lock holder: {auto_bet_lock_holder}",
                        f"Lock acquired at: {auto_bet_lock_acquired_at}",
                        f"",
                        f"Event base: {event_base}",
                        f"Event total bet: ${auto_bet_event_totals.get(event_base, 0.0):.2f} / ${per_event_max_bet:.2f}",
                        f"Bet amount: ${bet_amount:.2f}",
                        f"Contracts: {contracts}",
                        f"",
                        f"Match result: {'N/A (not available in check_and_auto_bet scope)'}",
                        f"Match confidence: {'N/A (not available in check_and_auto_bet scope)'}",
                        f"Match method: {'N/A (not available in check_and_auto_bet scope)'}",
                        f"",
                        f"====================================="
                    ]
                    
                    store_failed_auto_bet(
                        alert_id=alert_id,
                        alert=alert,
                        alert_data=alert_data,
                        error=full_error_msg,
                        reason=f"Order placement failed: {error_msg}" + (f" - {reason}" if reason else ""),
                        ticker=ticker,
                        side=side,
                        expected_price=expected_price,
                        current_price=current_price,
                        price_delta=price_delta,
                        ev_percent=ev_percent,
                        odds=alert_data.get('american_odds'),
                        filter_name=alert_filter_name,
                        additional_logs=additional_logs
                    )
                    socketio.emit('auto_bet_failed', {
                        'alert_id': alert_id,
                        'error': full_error_msg,
                        'market': f"{alert.teams} - {alert.pick}"
                    })
                    # Return immediately - lock already released
                    await cleanup_submarket()
                    return
            
            # CRITICAL: Log result details for debugging
            order_placement_time = time.time()
            order_placement_duration = (order_placement_time - order_placement_start) * 1000  # ms
            
            if result:
                if result.get('success'):
                    print(f"[AUTO-BET] ✅ Order placed successfully!")
                    print(f"[AUTO-BET] Order placement time: {order_placement_duration:.1f}ms")
                else:
                    error_msg = result.get('error', 'Unknown error')
                    error_reason = result.get('reason', '')
                    print(f"[AUTO-BET] ❌ Order placement FAILED: {error_msg}")
                    if error_reason:
                        print(f"[AUTO-BET]   Reason: {error_reason}")
                    print(f"[AUTO-BET]   📊 FAILURE DETAILS:")
                    print(f"[AUTO-BET]      Alert: {alert.teams} - {alert.pick}")
                    print(f"[AUTO-BET]      Ticker: {ticker}, Side: {side}")
                    print(f"[AUTO-BET]      Expected price: {expected_price_cents}¢" if expected_price_cents else "[AUTO-BET]      Expected price: N/A")
                    if 'expected' in result:
                        print(f"[AUTO-BET]      Expected: {result.get('expected')}¢")
                    if 'current' in result:
                        print(f"[AUTO-BET]      Current: {result.get('current')}¢")
                    if 'delta' in result:
                        print(f"[AUTO-BET]      Delta: {result.get('delta')}¢")
                    
                    # CRITICAL: Log order placement failures to failed-bets with complete trade lifecycle
                    trade_end_time = time.time()
                    total_trade_duration = (trade_end_time - trade_start_time) * 1000  # ms
                    full_error_msg = f"{error_msg}: {error_reason}" if error_reason else error_msg
                    
                    # Build comprehensive logs including orderbook details
                    additional_logs = [
                        f"========== TRADE LIFECYCLE ==========",
                        f"Alert ID: {alert_id}",
                        f"Market: {teams} - {pick}",
                        f"EV: {ev_percent:.2f}%",
                        f"Filter: {alert_filter_name}",
                        f"Ticker: {ticker}, Side: {side}",
                        f"Expected price: {expected_price_cents}¢" if expected_price_cents else "Expected price: N/A",
                        f"Current price: {result.get('current', 'N/A')}¢",
                        f"Price delta: {result.get('delta', 'N/A')}¢",
                        f"Total trade time: {total_trade_duration:.1f}ms (from alert to failure)",
                        f"Order placement time: {order_placement_duration:.1f}ms",
                    ]
                    
                    # Add liquidity details if available
                    print(f"[AUTO-BET] DEBUG: Result keys: {list(result.keys())}")
                    if 'available_liquidity' in result:
                        additional_logs.append(f"Available liquidity: ${result.get('available_liquidity', 'N/A')}")
                        print(f"[AUTO-BET] DEBUG: Available liquidity: ${result.get('available_liquidity', 'N/A')}")
                    if 'minimum_required' in result:
                        additional_logs.append(f"Minimum required: ${result.get('minimum_required', 'N/A')}")
                    if 'best_ask' in result:
                        additional_logs.append(f"Best ask: {result.get('best_ask')}¢")
                        print(f"[AUTO-BET] DEBUG: Best ask: {result.get('best_ask')}¢")
                    if 'best_ask_size' in result:
                        additional_logs.append(f"Best ask size: {result.get('best_ask_size')} contracts")
                        print(f"[AUTO-BET] DEBUG: Best ask size: {result.get('best_ask_size')} contracts")
                    if 'orderbook_asks' in result:
                        orderbook_asks = result.get('orderbook_asks', [])
                        print(f"[AUTO-BET] DEBUG: Orderbook asks found: {len(orderbook_asks)} levels, type={type(orderbook_asks)}")
                        if orderbook_asks:
                            additional_logs.append(f"")
                            additional_logs.append(f"========== ORDERBOOK DETAILS ==========")
                            additional_logs.append(f"Orderbook asks (top {min(10, len(orderbook_asks))} levels):")
                            for i, ask in enumerate(orderbook_asks[:10]):
                                # Handle both dict and list formats
                                if isinstance(ask, dict):
                                    ask_price = ask.get('price', 0)
                                    ask_size = ask.get('quantity', 0)
                                elif isinstance(ask, (list, tuple)) and len(ask) >= 2:
                                    ask_price = ask[0] if isinstance(ask[0], (int, float)) else 0
                                    ask_size = ask[1] if len(ask) > 1 else 0
                                else:
                                    print(f"[AUTO-BET] DEBUG: Unknown ask format: {ask}, type={type(ask)}")
                                    continue
                                
                                ask_price_cents = int(ask_price * 100) if ask_price else 0
                                ask_liquidity = (ask_size * ask_price * 100) / 100.0 if ask_price else 0
                                additional_logs.append(f"  Level {i+1}: {ask_price_cents}¢ ({ask_price:.4f}) - {ask_size} contracts = ${ask_liquidity:.2f}")
                                print(f"[AUTO-BET] DEBUG: Level {i+1}: {ask_price_cents}¢ ({ask_price:.4f}) - {ask_size} contracts = ${ask_liquidity:.2f}")
                            additional_logs.append(f"=====================================")
                        else:
                            additional_logs.append(f"")
                            additional_logs.append(f"⚠️  orderbook_asks is empty list")
                    else:
                        print(f"[AUTO-BET] DEBUG: No orderbook_asks key in result. Result keys: {list(result.keys())}")
                        additional_logs.append(f"")
                        additional_logs.append(f"⚠️  No orderbook_asks key in error result")
                        additional_logs.append(f"   Available keys: {', '.join(result.keys())}")
                    
                    additional_logs.extend([
                        f"",
                        f"Error: {full_error_msg}",
                        f"====================================="
                    ])
                    
                    store_failed_auto_bet(
                        alert_id=alert_id,
                        alert=alert,
                        alert_data=alert_data,
                        error=full_error_msg,
                        reason=f"Order placement failed: {error_msg}" + (f" - {error_reason}" if error_reason else ""),
                        ticker=ticker,
                        side=side,
                        expected_price=expected_price_cents,
                        current_price=result.get('current'),
                        price_delta=result.get('delta'),
                        ev_percent=ev_percent,
                        odds=alert_data.get('american_odds'),
                        filter_name=alert_filter_name,
                        additional_logs=additional_logs
                    )
                    
                    socketio.emit('auto_bet_failed', {
                        'alert_id': alert_id,
                        'error': full_error_msg,
                        'market': f"{alert.teams} - {alert.pick}",
                        'ticker': ticker,
                        'side': side,
                        'expected_price': expected_price_cents,
                        'current_price': result.get('current'),
                        'price_delta': result.get('delta')
                    })
                    await cleanup_submarket()
                    return
            
            if result and result.get('success'):
                fill_count = result.get('fill_count', 0)
                initial_count = result.get('initial_count', result.get('count', fill_count))
                executed_price_cents = result.get('executed_price_cents') or result.get('price_cents', 0)
                limit_price_cents = result.get('price_cents', 0)  # Limit price (requested)
                total_cost_cents = result.get('total_cost_cents') or 0  # Handle None case (when fill_count=0)
                order_id = result.get('order_id', 'N/A')
                order_status = result.get('status', 'executed')
                fee_type = result.get('fee_type', 'taker')  # 'maker' or 'taker'
                taker_fees_cents = result.get('taker_fees_cents', 0)
                maker_fees_cents = result.get('maker_fees_cents', 0)
                total_fees_cents = result.get('total_fees_cents', 0)
                
                # CRITICAL SAFETY CHECK: If order filled immediately, verify price is reasonable
                # This catches wrong submarket matches without adding pre-order latency
                if fill_count > 0 and executed_price_cents > 0 and expected_price_cents:
                    price_delta = abs(executed_price_cents - expected_price_cents)
                    # If price is off by more than 5 cents, it's likely wrong submarket
                    if price_delta > 5:
                        print(f"[AUTO-BET] 🚨 CRITICAL: Executed price ({executed_price_cents}¢) differs significantly from expected ({expected_price_cents}¢) - delta: {price_delta}¢")
                        print(f"[AUTO-BET]   ⚠️  This suggests possible wrong submarket match! Order ID: {order_id}")
                        print(f"[AUTO-BET]   ⚠️  Alert: {alert.teams} - {alert.pick} | Ticker: {ticker}")
                        # Don't cancel - order already filled, but log for investigation
                        # In future, we could add logic to cancel if delta is extreme (>10 cents)
                
                # COMPREHENSIVE ORDER RESULT LOGGING
                print(f"[AUTO-BET] ========== ORDER RESULT ==========")
                print(f"[AUTO-BET]   Success: ✅")
                print(f"[AUTO-BET]   Order ID: {order_id}")
                print(f"[AUTO-BET]   Order Status: {order_status}")
                print(f"[AUTO-BET]   Filled: {fill_count}/{initial_count} contracts")
                print(f"[AUTO-BET]   Limit Price (requested): {limit_price_cents} cents ({limit_price_cents/100:.2f}¢)")
                print(f"[AUTO-BET]   Executed Price (actual): {executed_price_cents} cents ({executed_price_cents/100:.2f}¢)")
                if executed_price_cents != limit_price_cents and limit_price_cents > 0:
                    slippage_cents = executed_price_cents - limit_price_cents
                    slippage_pct = (slippage_cents / limit_price_cents * 100) if limit_price_cents > 0 else 0
                    print(f"[AUTO-BET]   Slippage: {slippage_cents:+.1f}¢ ({slippage_pct:+.2f}%)")
                print(f"[AUTO-BET]   Total Cost: ${total_cost_cents/100:.2f}" if total_cost_cents else "[AUTO-BET]   Total Cost: $0.00 (no fills)")
                print(f"[AUTO-BET]   Fee Type: {fee_type}")
                print(f"[AUTO-BET]   Total Fees: ${total_fees_cents/100:.2f} (taker: ${taker_fees_cents/100:.2f}, maker: ${maker_fees_cents/100:.2f})")
                
                # Calculate effective price (including fees)
                if fill_count > 0:
                    effective_price_decimal = (total_cost_cents / 100.0) / fill_count
                    effective_price_cents = int(effective_price_decimal * 100)
                    if effective_price_decimal >= 0.5:
                        odds = -100 * effective_price_decimal / (1 - effective_price_decimal)
                        executed_american_odds = f"{int(odds)}"
                    else:
                        odds = 100 * (1 - effective_price_decimal) / effective_price_decimal
                        executed_american_odds = f"+{int(odds)}"
                    print(f"[AUTO-BET]   Effective Price (with fees): {effective_price_cents}¢ ({effective_price_decimal:.4f}), American odds: {executed_american_odds}")
                
                # Alert vs Execution comparison
                alert_price_cents = expected_price_cents
                if alert_price_cents:
                    price_delta = executed_price_cents - alert_price_cents
                    price_delta_pct = (price_delta / alert_price_cents * 100) if alert_price_cents > 0 else 0
                    print(f"[AUTO-BET]   Alert Price: {alert_price_cents} cents ({alert_price_cents/100:.2f}¢)")
                    print(f"[AUTO-BET]   Price Change: {price_delta:+.1f}¢ ({price_delta_pct:+.2f}%)")
                
                # COMPREHENSIVE TRADE LIFECYCLE SUMMARY
                trade_end_time = time.time()
                total_trade_duration = (trade_end_time - trade_start_time) * 1000  # ms
                fill_duration = (trade_end_time - order_placement_time) * 1000  # ms
                print(f"[AUTO-BET] ========== TRADE LIFECYCLE SUMMARY ==========")
                print(f"[AUTO-BET] Total Trade Time: {total_trade_duration:.1f}ms (from alert to completion)")
                print(f"[AUTO-BET] Order Placement Time: {order_placement_duration:.1f}ms")
                print(f"[AUTO-BET] Fill Time: {fill_duration:.1f}ms")
                print(f"[AUTO-BET] Final Result: ✅ SUCCESS")
                print(f"[AUTO-BET]   Filled: {fill_count}/{initial_count} contracts")
                print(f"[AUTO-BET]   Cost: ${total_cost_cents/100:.2f}")
                print(f"[AUTO-BET]   Expected Price (BB): {expected_price_cents}¢")
                print(f"[AUTO-BET]   Limit Price (requested): {limit_price_cents}¢")
                print(f"[AUTO-BET]   Executed Price (actual): {executed_price_cents}¢")
                if executed_price_cents != expected_price_cents:
                    slippage_vs_expected = executed_price_cents - expected_price_cents
                    slippage_pct_vs_expected = (slippage_vs_expected / expected_price_cents * 100) if expected_price_cents > 0 else 0
                    print(f"[AUTO-BET]   Slippage vs Expected: {slippage_vs_expected:+.1f}¢ ({slippage_pct_vs_expected:+.2f}%)")
                if executed_price_cents != limit_price_cents and limit_price_cents > 0:
                    slippage_vs_limit = executed_price_cents - limit_price_cents
                    slippage_pct_vs_limit = (slippage_vs_limit / limit_price_cents * 100) if limit_price_cents > 0 else 0
                    print(f"[AUTO-BET]   Slippage vs Limit: {slippage_vs_limit:+.1f}¢ ({slippage_pct_vs_limit:+.2f}%)")
                print(f"[AUTO-BET] ==========================================")
                print(f"[AUTO-BET] ========== END AUTO-BET LOGGING ==========")
                print(f"{'='*80}\n")
                
                # CRITICAL: If taker order didn't fill completely (still open/resting), cancel immediately
                # Taker orders should fill instantly, but if they don't, we cancel to prevent late fills at non-EV prices
                # Also check for 'partial' status which means order is still open
                if order_status in ['open', 'resting', 'pending', 'queued', 'partial'] and fill_count < initial_count:
                    remaining = initial_count - fill_count
                    print(f"[AUTO-BET] 🚨 CRITICAL: Taker order still {order_status} ({fill_count}/{initial_count} filled) - cancelling IMMEDIATELY to prevent late fill...")
                    
                    if order_id and order_id != 'N/A':
                        # Retry cancellation up to 3 times if it fails
                        max_cancel_retries = 3
                        cancelled = False
                        for retry in range(max_cancel_retries):
                            cancel_result = await kalshi_client.cancel_order(order_id)
                            if cancel_result.get('success'):
                                print(f"[AUTO-BET] ✅ Successfully cancelled {order_status} taker order ({remaining} contracts remaining)")
                                cancelled = True
                                break
                            else:
                                error_msg = cancel_result.get('error', 'unknown')
                                print(f"[AUTO-BET] ⚠️  Cancel attempt {retry + 1}/{max_cancel_retries} failed for order {order_id}: {error_msg}")
                                if retry < max_cancel_retries - 1:
                                    await asyncio.sleep(0.5)  # Wait 500ms before retry
                        
                        if not cancelled:
                            print(f"[AUTO-BET] ❌ FAILED to cancel order {order_id} after {max_cancel_retries} attempts - order may still be resting!")
                            # Emit warning to frontend
                            socketio.emit('auto_bet_warning', {
                                'alert_id': alert_id,
                                'warning': f"Order {order_id} is {order_status} and could not be cancelled - may fill at non-EV price",
                                'order_id': order_id,
                                'status': order_status
                            })
                # Also check if order is resting even when fully filled (Kalshi sometimes reports executed but order is actually resting)
                elif order_status == 'resting' and fill_count == initial_count:
                    print(f"[AUTO-BET] 🚨 WARNING: Order {order_id} is 'resting' but reports fully filled - attempting cancellation to be safe...")
                    if order_id and order_id != 'N/A':
                        cancel_result = await kalshi_client.cancel_order(order_id)
                        if cancel_result.get('success'):
                            print(f"[AUTO-BET] ✅ Cancelled resting order that reported as fully filled")
                        else:
                            print(f"[AUTO-BET] ⚠️  Could not cancel resting order {order_id}: {cancel_result.get('error', 'unknown')}")
                
                # Log fee type (always taker now)
                total_fees_cents = result.get('total_fees_cents', 0)
                print(f"[AUTO-BET] Taker fill (fees: ${total_fees_cents/100:.2f} | taker: ${taker_fees_cents/100:.2f})")
                
                # Handle partial fills - taker orders should fill completely, but log if partial
                if fill_count > 0 and fill_count < initial_count:
                    remaining = initial_count - fill_count
                    print(f"[AUTO-BET] Partial fill: {fill_count}/{initial_count} contracts. Remaining: {remaining} (order should have filled completely as taker)")
                
                # Check minimum bet size
                actual_cost = total_cost_cents / 100.0
                MIN_BET_SIZE_DOLLARS = 20.0
                if actual_cost < MIN_BET_SIZE_DOLLARS and fill_count > 0:
                    print(f"[AUTO-BET] ⚠️  WARNING: Fill ${actual_cost:.2f} < ${MIN_BET_SIZE_DOLLARS:.2f} min (only {fill_count} contracts filled)")
                
                # Only process if order filled (resting orders handled later)
                if fill_count == 0:
                    print(f"[AUTO-BET] ⚠️  Order {order_id} had fill_count=0, returning early (no emit)")
                    auto_bet_processing_submarkets.discard(submarket_key)
                    auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                    auto_bet_submarket_tasks.pop(submarket_key, None)
                    return
                
                print(f"[AUTO-BET] ✅ Order {order_id} filled {fill_count} contracts, proceeding to emit notification")
                
                # Calculate win amount (payout - cost)
                payout = fill_count * 1.0  # Each contract pays $1 if it wins
                cost = total_cost_cents / 100.0 if total_cost_cents else (fill_count * executed_price_cents / 100.0)
                win_amount = payout - cost
                
                # CRITICAL: Update submarket data with actual cost (for topping up logic)
                if submarket_key in auto_bet_submarket_data:
                    previous_cost = auto_bet_submarket_data[submarket_key].get('actual_cost') or 0.0
                    # Handle None explicitly (in case it was initialized as None)
                    if previous_cost is None:
                        previous_cost = 0.0
                    # Accumulate cost if this is a top-up
                    auto_bet_submarket_data[submarket_key]['actual_cost'] = previous_cost + cost
                    print(f"[AUTO-BET] Updated submarket data: actual_cost = ${auto_bet_submarket_data[submarket_key]['actual_cost']:.2f} (previous: ${previous_cost:.2f}, this bet: ${cost:.2f})")
                else:
                    # Shouldn't happen, but create entry if missing
                    auto_bet_submarket_data[submarket_key] = {
                        'actual_cost': cost,
                        'target_amount': bet_amount
                    }
                
                # CRITICAL: Add to auto_bet_submarkets ONLY after bet succeeds (prevents duplicates)
                # This marks the submarket as "bet" so future alerts for the same submarket are skipped
                # UNLESS they're topping up (handled above)
                if submarket_key not in auto_bet_submarkets:
                    auto_bet_submarkets.add(submarket_key)
                    print(f"[AUTO-BET] Marked {submarket_key} as bet (successful order)")
                
                # CRITICAL: Calculate American odds from EFFECTIVE price (cost per contract including fees)
                # This matches what the user actually paid and what the win amount reflects
                # Use exact decimal price (don't round) for accurate odds conversion
                if fill_count > 0 and cost > 0:
                    effective_price_decimal = cost / fill_count  # Cost per contract in dollars (includes fees)
                    # Convert to American odds using exact decimal (not rounded cents)
                    if effective_price_decimal >= 0.5:
                        # Favorite (negative odds)
                        odds = -100 * effective_price_decimal / (1 - effective_price_decimal)
                        effective_american_odds = f"{int(odds)}"
                    else:
                        # Underdog (positive odds)
                        odds = 100 * (1 - effective_price_decimal) / effective_price_decimal
                        effective_american_odds = f"+{int(odds)}"
                    # Check if effective odds (after fees) are outside range
                    effective_odds_int = american_odds_to_int(effective_american_odds)
                    if effective_odds_int is not None:
                        if effective_odds_int < auto_bet_odds_min or effective_odds_int > auto_bet_odds_max:
                            print(f"[AUTO-BET] ⚠️  WARNING: Effective odds {effective_odds_int} outside range ({auto_bet_odds_min}-{auto_bet_odds_max}) after fees")
                else:
                    # Fallback to executed price if we can't calculate effective price
                    effective_american_odds = price_to_american_odds(executed_price_cents)
                
                # Get market/submarket names for display
                market_name = alert_data.get('market_name', alert.teams)
                submarket_name = alert_data.get('submarket_name', f"{alert.pick} {alert.qualifier or ''}".strip())
                qualifier = alert.qualifier or alert_data.get('qualifier', '')
                
                # Prepare CSV record
                sport = determine_sport_from_ticker(ticker)
                taker_fees_cents = result.get('taker_fees_cents', 0)
                total_fees_cents = result.get('total_fees_cents', 0)
                
                # Get devig books and their odds from alert
                devig_books = getattr(alert, 'devig_books', []) or alert_data.get('devig_books', [])
                display_books = getattr(alert, 'display_books', {}) or alert_data.get('display_books', {})
                
                # Build devig books string with odds (e.g., "Pinnacle:-206, SportTrade:-207, ProphetX:-212, BookMaker:-212")
                devig_books_str = ''
                if devig_books and display_books:
                    our_selection = alert.pick
                    our_books = display_books.get(our_selection, [])
                    
                    # Create a map of book name to odds for quick lookup
                    book_odds_map = {book.get('book', ''): book.get('odds', 0) for book in our_books}
                    
                    # Build list of devig books with their odds
                    devig_books_list = []
                    for book_name in devig_books:
                        if book_name in book_odds_map:
                            odds = book_odds_map[book_name]
                            devig_books_list.append(f"{book_name}:{odds}")
                    
                    devig_books_str = ', '.join(devig_books_list)
                
                bet_record = {
                    'timestamp': datetime.now().isoformat(),
                    'order_id': order_id,
                    'ticker': ticker,
                    'side': side,
                    'teams': alert.teams,
                    'market_type': alert.market_type or alert_data.get('market_type', ''),
                    'pick': alert.pick,
                    'qualifier': qualifier,
                    'ev_percent': f"{ev_percent:.2f}",
                    'expected_price_cents': str(expected_price_cents),
                    'executed_price_cents': str(executed_price_cents),
                    'american_odds': effective_american_odds,
                    'contracts': str(fill_count),
                    'cost': f"{cost:.2f}",
                    'payout': f"{payout:.2f}",
                    'win_amount': f"{win_amount:.2f}",
                    'fee_type': fee_type,  # 'maker' or 'taker'
                    'taker_fees_cents': str(taker_fees_cents),  # Taker fees in cents (for Telegram alert)
                    'taker_fees': f"{taker_fees_cents/100:.2f}",  # Taker fees in dollars
                    'total_fees': f"{total_fees_cents/100:.2f}",  # Total fees in dollars
                    'sport': sport,
                    'status': 'executed',
                    'result': 'OPEN',  # Will be updated when market settles
                    'pnl': '0.00',
                    'settled': 'FALSE',
                    'devig_books': devig_books_str,  # Books used for devigging with their odds
                    'kalshi_odds': effective_american_odds,  # Kalshi odds we bet at (net of fees)
                    'filter_name': getattr(alert, 'filter_name', '') or alert_data.get('filter_name', '')  # Filter name that triggered this bet (with fallback to alert_data)
                }
                
                # Update decision path with bet execution info
                decision_path_so_far['final_decision'] = 'BET_PLACED'
                decision_path_so_far['order_id'] = order_id
                decision_path_so_far['executed_price_cents'] = executed_price_cents
                decision_path_so_far['contracts'] = fill_count
                decision_path_so_far['cost'] = cost
                log_alert_passed_threshold(alert_id, alert, alert_data, filter_settings_dict, decision_path_so_far)
                
                # Write to Google Sheets (with CSV fallback)
                # FINAL COMPREHENSIVE SUMMARY LOG
                print(f"[AUTO-BET] ========== FINAL BET SUMMARY ==========")
                print(f"[AUTO-BET]   Alert: {alert.teams} - {alert.pick} {alert.qualifier or ''}")
                print(f"[AUTO-BET]   Market: {ticker} | Side: {side}")
                print(f"[AUTO-BET]   Contracts: {fill_count} @ {executed_price_cents}¢ (effective: {effective_price_cents}¢ with fees)")
                print(f"[AUTO-BET]   Cost: ${cost:.2f} | Payout if win: ${payout:.2f} | Win amount: ${win_amount:.2f}")
                print(f"[AUTO-BET]   Alert EV: {alert.ev_percent:.2f}% | Alert Price: {expected_price_cents}¢")
                print(f"[AUTO-BET]   Executed Price: {executed_price_cents}¢ | Effective Price: {effective_price_cents}¢")
                print(f"[AUTO-BET]   Fee Type: {fee_type} | Fees: ${total_fees_cents/100:.2f}")
                print(f"[AUTO-BET]   Order ID: {order_id}")
                print(f"[AUTO-BET] ==========================================")
                
                write_auto_bet_to_sheets(bet_record)
                
                # CRITICAL: Store successful bet with comprehensive timing for comparison with failures
                print(f"[AUTO-BET] 📝 About to log successful bet: Alert {alert_id}, Ticker: {ticker}, Side: {side}, Fill: {fill_count}")
                trade_end_time = time.time()
                total_trade_duration = (trade_end_time - trade_start_time) * 1000  # ms
                price_delta = executed_price_cents - expected_price_cents if (executed_price_cents and expected_price_cents) else None
                print(f"[AUTO-BET] 📝 Timing: Total={total_trade_duration:.1f}ms, Price delta={price_delta}")
                
                # Build comprehensive timing logs
                lock_held_duration_ms = 'N/A'
                if lock_held_start is not None:
                    lock_held_duration_ms = (time.time() - lock_held_start) * 1000
                
                success_timings = {
                    'total_trade_duration_ms': total_trade_duration,
                    'lock_acquisition_ms': step_timings.get('lock_acquisition', 'N/A'),
                    'lock_held_duration_ms': lock_held_duration_ms,
                    'reverse_middle_check_ms': step_timings.get('reverse_middle_total', 'N/A'),
                    'order_placement_api_ms': step_timings.get('place_order_api', 'N/A'),
                    'order_placement_total_ms': step_timings.get('order_placement_total', 'N/A'),
                }
                
                success_logs = [
                    f"========== SUCCESSFUL TRADE ==========",
                    f"Alert ID: {alert_id}",
                    f"Market: {alert.teams} - {alert.pick}",
                    f"EV: {ev_percent:.2f}%",
                    f"Filter: {alert_filter_name}",
                    f"Ticker: {ticker}, Side: {side}",
                    f"Expected Price: {expected_price_cents}¢",
                    f"Executed Price: {executed_price_cents}¢",
                    f"Price Delta: {price_delta}¢" if price_delta is not None else "Price Delta: N/A",
                    f"",
                    f"========== TIMING BREAKDOWN ==========",
                    f"Total trade time: {total_trade_duration:.1f}ms (from alert to completion)",
                    f"Lock acquisition: {success_timings.get('lock_acquisition_ms', 'N/A')}ms",
                    f"Lock held duration: {success_timings.get('lock_held_duration_ms', 'N/A')}ms",
                    f"Reverse middle check: {success_timings.get('reverse_middle_check_ms', 'N/A')}ms",
                    f"Order placement API: {success_timings.get('order_placement_api_ms', 'N/A')}ms",
                    f"Order placement total: {success_timings.get('order_placement_total_ms', 'N/A')}ms",
                    f"",
                    f"Contracts: {fill_count}",
                    f"Cost: ${cost:.2f}",
                    f"Order ID: {order_id}",
                    f"====================================="
                ]
                
                print(f"[AUTO-BET] 📝 Calling store_successful_auto_bet with alert_id={alert_id}")
                try:
                    store_successful_auto_bet(
                        alert_id=alert_id,
                        alert=alert,
                        alert_data=alert_data,
                        ticker=ticker,
                        side=side,
                        expected_price=expected_price_cents,
                        executed_price=executed_price_cents,
                        price_delta=price_delta,
                        ev_percent=ev_percent,
                        odds=effective_american_odds,
                        filter_name=alert_filter_name,
                        trade_timings=success_timings,
                        additional_logs=success_logs
                    )
                    print(f"[AUTO-BET] ✅ Successfully called store_successful_auto_bet")
                except Exception as e:
                    print(f"[AUTO-BET] ❌ ERROR calling store_successful_auto_bet: {e}")
                    import traceback
                    traceback.print_exc()
                
                # Update per-event bet total after successful bet
                # CRITICAL: We already reserved bet_amount earlier, so we need to replace it with actual cost
                # Formula: new_total = (current_total - reserved_bet_amount) + actual_cost
                # This ensures we don't double-count
                if event_base:
                    current_reserved = auto_bet_event_totals.get(event_base, 0.0)
                    # Check if this is a top-up (previous cost exists)
                    submarket_data = auto_bet_submarket_data.get(submarket_key, {})
                    previous_cost = submarket_data.get('actual_cost', 0.0) - cost  # Subtract current cost to get previous
                    if previous_cost > 0:
                        # This is a top-up - we already subtracted previous_cost above, so just add new cost
                        auto_bet_event_totals[event_base] = current_reserved - bet_amount + cost
                        print(f"[AUTO-BET] Updated event total for {event_base} (TOP-UP): ${auto_bet_event_totals[event_base]:.2f} / ${per_event_max_bet:.2f} (previous: ${previous_cost:.2f}, new: ${cost:.2f}, reserved: ${bet_amount:.2f})")
                    else:
                        # First bet - replace reserved amount with actual cost
                        auto_bet_event_totals[event_base] = current_reserved - bet_amount + cost
                        print(f"[AUTO-BET] Updated event total for {event_base}: ${auto_bet_event_totals[event_base]:.2f} / ${per_event_max_bet:.2f} (replaced reserved ${bet_amount:.2f} with actual ${cost:.2f})")
                
                # Store in memory for quick access
                auto_bet_records.append(bet_record)
                
                # Send Telegram alert for successful bet
                send_auto_bet_alert(bet_record)
                
                # Emit auto-bet notification to frontend (with full details for popup)
                print(f"[AUTO-BET] 📡 Preparing to emit auto_bet_placed event: order_id={order_id}, fill_count={fill_count}, cost=${cost:.2f}")
                try:
                    socketio.emit('auto_bet_placed', {
                        'alert_id': alert_id,
                        'order_id': order_id,
                        'fill_count': fill_count,
                        'initial_count': fill_count,  # For auto-bet, fill_count = initial_count
                        'status': 'executed',  # Auto-bets are always executed (or they fail)
                        'cost': cost,
                        'american_odds': effective_american_odds,  # Use effective odds (with fees)
                        'payout': payout,
                        'win_amount': win_amount,
                        'market': f"{alert.teams} - {alert.pick}",
                        'market_name': market_name,
                        'submarket_name': submarket_name,
                        'ticker': ticker,
                        'side': side,
                        'price_cents': executed_price_cents,
                        'executed_price_cents': executed_price_cents,  # Explicit field
                        'ev_percent': ev_percent,
                        'teams': alert.teams,
                        'pick': alert.pick,
                        'qualifier': qualifier,  # Add qualifier for popup display
                        'fee_type': fee_type,  # 'maker' or 'taker' - for UI display
                        'filter_name': getattr(alert, 'filter_name', '') or alert_data.get('filter_name', ''),  # Filter name (with fallback to alert_data)
                        'message': f'Auto-bet placed: {fill_count} contracts at {price_to_american_odds(executed_price_cents)} ({fee_type})'
                    })
                    print(f"[AUTO-BET] ✅ Emitted auto_bet_placed event to frontend")
                except Exception as e:
                    print(f"[AUTO-BET] ⚠️  Failed to emit auto_bet_placed event: {e}")
                
                print(f"[AUTO-BET] OK: Successfully placed auto-bet: {fill_count} contracts, ${total_cost_cents/100:.2f}")
                
                # CRITICAL: Submarket already marked as "bet" before order placement (above) to prevent race conditions
                # This is just a defensive check - it should already be in the set
                if submarket_key not in auto_bet_submarkets:
                    async with auto_bet_lock:
                        auto_bet_submarkets.add(submarket_key)
                        print(f"[AUTO-BET] WARNING: Had to add {submarket_key} to bet set after order (should have been added before)")
                else:
                    print(f"[AUTO-BET] Submarket {submarket_key} already marked as bet (marked before order placement)")
                submarket_data = auto_bet_submarket_data.get(submarket_key)
                if submarket_data:
                    game_name = submarket_data.get('teams', alert.teams)
                    market_type = submarket_data.get('market_type', '')
                    pick_direction = submarket_data.get('pick_direction', '')
                    # Normalize market type (same logic as above)
                    ticker = alert_data.get('ticker', '')
                    if 'TOTAL' in ticker or 'Total Points' in market_type or 'Total Goals' in market_type:
                        normalized_market_type = 'Total Points'
                    elif 'SPREAD' in ticker or 'Point Spread' in market_type:
                        normalized_market_type = 'Point Spread'
                    elif 'GAME' in ticker or 'Moneyline' in market_type:
                        normalized_market_type = 'Moneyline'
                    else:
                        normalized_market_type = market_type
                    # Verify it's already in the list (defensive check)
                    if (game_name in auto_bet_games and
                        normalized_market_type in auto_bet_games[game_name] and
                        pick_direction in auto_bet_games[game_name][normalized_market_type] and
                        submarket_key not in auto_bet_games[game_name][normalized_market_type][pick_direction]):
                        # Shouldn't happen, but add it if missing (defensive)
                        auto_bet_games[game_name][normalized_market_type][pick_direction].append(submarket_key)
                        print(f"[AUTO-BET] WARNING: Had to add {submarket_key} to auto_bet_games after order (should have been added before)")
                
                # Clean up processing set (bet succeeded, keep in auto_bet_submarkets permanently)
                # Lock is still held from above, so we can directly modify
                auto_bet_processing_submarkets.discard(submarket_key)
                auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                auto_bet_submarket_tasks.pop(submarket_key, None)
                # Lock will be released in finally block
            else:
                error = result.get('error', 'Unknown error') if result else 'No result returned'
                print(f"[AUTO-BET] ❌ Failed to place auto-bet: {error}")
                if result:
                    print(f"[AUTO-BET] Full result: {result}")
                
                # Check if this is a "trading paused" error (Kalshi-side issue)
                is_trading_paused = 'trading paused' in error.lower() or 'trading_is_paused' in error.lower()
                
                if is_trading_paused:
                    print(f"[AUTO-BET] 🛑 TRADING PAUSED: Kalshi has paused trading on this market.")
                    print(f"[AUTO-BET]    This is a Kalshi-side issue - not a bot error.")
                    print(f"[AUTO-BET]    Trading will resume automatically when Kalshi unpauses.")
                    print(f"[AUTO-BET]    The alert will be retried when trading resumes (if alert is still active).")
                    # Don't increment retry count for trading paused - it's not our fault
                    # The alert can be retried later when trading resumes
                else:
                    # CRITICAL: Increment retry count to prevent infinite loops
                    import time
                    current_time = time.time()
                    retry_count = auto_bet_submarket_retry_count.get(submarket_key, 0) + 1
                    auto_bet_submarket_retry_count[submarket_key] = retry_count
                    auto_bet_submarket_last_retry[submarket_key] = current_time
                    
                    if retry_count >= MAX_RETRIES_PER_SUBMARKET:
                        print(f"[AUTO-BET] 🚨 MAX RETRIES REACHED: {submarket_key} has failed {retry_count} times - BLOCKING further retries for {RETRY_COOLDOWN_SECONDS}s to prevent infinite loop")
                        # CRITICAL: Mark as "permanently failed" to prevent any more retries
                        # Add to a blacklist that prevents task creation
                        if submarket_key not in auto_bet_submarkets:  # Only if not already bet
                            # Keep it in retry tracking so retry check blocks it
                            print(f"[AUTO-BET] 🚨 BLOCKING: {submarket_key} is now blocked from further auto-bet attempts")
                    else:
                        print(f"[AUTO-BET] Retry count for {submarket_key}: {retry_count}/{MAX_RETRIES_PER_SUBMARKET}")
                
                # Bet failed - remove from bet set so it can be retried if alert comes back
                # CRITICAL: Remove from auto_bet_submarkets since bet failed (we added it before order)
                if submarket_key and submarket_key in auto_bet_submarkets:
                    print(f"[AUTO-BET] Removing {submarket_key} from bet set (order failed)")
                    auto_bet_submarkets.discard(submarket_key)
                    
                    # CRITICAL: Subtract reserved bet_amount from event total (we reserved it earlier)
                    # This ensures the limit check works correctly for future bets
                    if event_base:
                        current_total = auto_bet_event_totals.get(event_base, 0.0)
                        auto_bet_event_totals[event_base] = max(0.0, current_total - bet_amount)
                        print(f"[AUTO-BET] Subtracted reserved ${bet_amount:.2f} from event total for {event_base}: ${auto_bet_event_totals[event_base]:.2f} / ${per_event_max_bet:.2f}")
                
                # Bet failed - remove from all sets so it can be retried if alert comes back
                # Lock is still held from above, so we can directly modify
                auto_bet_processing_submarkets.discard(submarket_key)
                auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                auto_bet_submarket_tasks.pop(submarket_key, None)
                # CRITICAL: Also remove from auto_bet_games (we added it before order)
                if submarket_key:
                    # Get game_name, market_type, pick_direction from stored data or alert
                    submarket_data = auto_bet_submarket_data.get(submarket_key)
                    if submarket_data:
                        game_name = submarket_data.get('teams', alert.teams)
                        market_type = submarket_data.get('market_type', '')
                        pick_direction = submarket_data.get('pick_direction', '')
                    else:
                        game_name = alert.teams
                        market_type = alert.market_type or alert_data.get('market_type', '')
                        # Determine pick_direction
                        ticker = alert_data.get('ticker', '')
                        current_pick = (alert.pick or alert_data.get('pick', '')).upper()
                        is_over = 'OVER' in current_pick or current_pick == 'OVER'
                        is_under = 'UNDER' in current_pick or current_pick == 'UNDER'
                        if 'TOTAL' in ticker or 'Total Points' in market_type:
                            pick_direction = 'Over' if is_over else ('Under' if is_under else 'Unknown')
                        else:
                            pick_direction = current_pick if not is_over and not is_under else 'Unknown'
                    
                    # Normalize market type (same logic as limit check)
                    ticker = alert_data.get('ticker', '')
                    if 'TOTAL' in ticker or 'Total Points' in market_type or 'Total Goals' in market_type:
                        normalized_market_type = 'Total Points'
                    elif 'SPREAD' in ticker or 'Point Spread' in market_type:
                        normalized_market_type = 'Point Spread'
                    elif 'GAME' in ticker or 'Moneyline' in market_type:
                        normalized_market_type = 'Moneyline'
                    else:
                        normalized_market_type = market_type
                    
                    # Remove from auto_bet_games using normalized_market_type
                    if (game_name in auto_bet_games and 
                        normalized_market_type in auto_bet_games[game_name] and
                        pick_direction in auto_bet_games[game_name][normalized_market_type] and
                        submarket_key in auto_bet_games[game_name][normalized_market_type][pick_direction]):
                        auto_bet_games[game_name][normalized_market_type][pick_direction].remove(submarket_key)
                        print(f"[AUTO-BET] Removed {submarket_key} from {game_name} -> {normalized_market_type} -> {pick_direction} (order failed)")
                    
                    # Also remove from submarket_data
                    if submarket_key in auto_bet_submarket_data:
                        auto_bet_submarket_data.pop(submarket_key, None)
                
                store_failed_auto_bet(
                    alert_id=alert_id,
                    alert=alert,
                    alert_data=alert_data,
                    error=error,
                    reason="Order placement failed",
                    ticker=alert_data.get('ticker'),
                    side=alert_data.get('side'),
                    expected_price=alert_data.get('price_cents'),
                    ev_percent=alert_data.get('ev_percent'),
                    odds=alert_data.get('american_odds'),
                    filter_name=getattr(alert, 'filter_name', None)
                )
                socketio.emit('auto_bet_failed', {
                    'alert_id': alert_id,
                    'error': error,
                    'market': f"{alert.teams} - {alert.pick}"
                })
        
        except Exception as e:
            print(f"[AUTO-BET] ERROR: Exception during auto-bet: {e}")
            import traceback
            traceback_str = traceback.format_exc()
            traceback.print_exc()
            
            # CRITICAL: Log ALL exceptions to failed-bets, especially high-EV alerts
            ev_pct = alert.ev_percent if alert else alert_data.get('ev_percent', 0)
            trade_end_time = time.time()
            total_trade_duration = (trade_end_time - trade_start_time) * 1000  # ms
            
            # Always log exceptions, but especially for high-EV alerts (10%+)
            if ev_pct >= 10.0:
                print(f"[AUTO-BET] 🚨 CRITICAL: High-EV alert ({ev_pct:.2f}%) failed with exception - logging to failed-bets")
            
            store_failed_auto_bet(
            alert_id=alert_id,
            alert=alert,
            alert_data=alert_data,
            error=f"Exception during auto-bet: {str(e)}",
            reason=f"An exception occurred while processing this alert. This may indicate a bug or unexpected condition.",
            ticker=ticker,
            side=side,
            expected_price=alert_data.get('price_cents'),
            ev_percent=ev_pct,
            odds=alert_data.get('american_odds'),
            filter_name=alert_filter_name,
            additional_logs=[
                f"========== TRADE LIFECYCLE ==========",
                f"Alert ID: {alert_id}",
                f"Market: {teams} - {pick}",
                f"EV: {ev_pct:.2f}%",
                f"Filter: {alert_filter_name}",
                f"Ticker: {ticker}, Side: {side}",
                f"Expected price: {alert_data.get('price_cents', 'N/A')}¢",
                f"Total trade time: {total_trade_duration:.1f}ms (from alert to exception)",
                f"",
                f"Exception: {str(e)}",
                f"Traceback:",
                traceback_str,
                f"====================================="
            ]
            )
            
            socketio.emit('auto_bet_failed', {
                'alert_id': alert_id,
                'error': f"Exception: {str(e)}",
                'market': f"{teams} - {pick}"
            })
            
            # Clean up processing set if we got here (exception before bet placement)
            try:
                await cleanup_submarket()
            except:
                pass  # Ignore errors during cleanup
        
            finally:
                # CRITICAL: Cancel lock watchdog FIRST (lock is being released normally)
                try:
                    if 'lock_watchdog_task' in locals() and lock_watchdog_task and not lock_watchdog_task.done():
                        lock_watchdog_task.cancel()
                        try:
                            await lock_watchdog_task
                        except asyncio.CancelledError:
                            pass
                except:
                    pass
                
                # CRITICAL: Always release lock when done (even on exceptions)
                # This MUST happen even if watchdog already released it
                if auto_bet_lock.locked():
                    lock_held_duration = (time.time() - lock_held_start) * 1000
                    try:
                        auto_bet_lock.release()
                        lock_waiters_after_release = len(auto_bet_lock._waiters) if (hasattr(auto_bet_lock, '_waiters') and auto_bet_lock._waiters is not None) else 'unknown'
                        print(f"[AUTO-BET] [CHECK_AND_AUTO_BET] [LOCK] Alert {alert_id} released lock after {lock_held_duration:.1f}ms (waiters now: {lock_waiters_after_release})")
                    except Exception as e:
                        print(f"[AUTO-BET] [CHECK_AND_AUTO_BET] [LOCK] ERROR releasing lock: {e}")
                    # Clear lock holder tracking
                    if auto_bet_lock_holder and auto_bet_lock_holder == f"Alert {alert_id} ({submarket_key})":
                        auto_bet_lock_holder = None
                        auto_bet_lock_acquired_at = None
                else:
                    # Lock already released (either by watchdog or earlier)
                    lock_held_duration = (time.time() - lock_held_start) * 1000
                    if lock_held_duration > 10000:
                        print(f"[AUTO-BET] [CHECK_AND_AUTO_BET] [LOCK] Alert {alert_id} - lock was already released (held for {lock_held_duration:.1f}ms - watchdog may have released it)")
                    else:
                        print(f"[AUTO-BET] [CHECK_AND_AUTO_BET] [LOCK] Alert {alert_id} - lock was not locked in finally block (already released?)")
    
    finally:
        # CRITICAL: Always clean up alert_id from processing set when done
        try:
            if alert_id in auto_bet_processing_alert_ids:
                auto_bet_processing_alert_ids.discard(alert_id)
            await cleanup_submarket()
        except:
            pass


async def _fetch_event_details(client, event_ticker, event_group):
    """Fetch event details to get market name"""
    try:
        event_data = await client.get_event_by_ticker(event_ticker)
        if event_data:
            event_group['market_name'] = event_data.get('title', event_ticker)
    except Exception as e:
        print(f"Warning: Could not fetch event details for {event_ticker}: {e}")


async def _fetch_position_details(client, ticker, pos, loop):
    """Fetch market and orderbook details for a position"""
    try:
        market_data = await client.get_market_by_ticker(ticker)
        if market_data:
            position_count = pos.get('position', 0) or pos.get('count', 0)
            if position_count > 0:
                pos['submarket_name'] = market_data.get('yes_sub_title', ticker)
            else:
                pos['submarket_name'] = market_data.get('no_sub_title', ticker)
        
        orderbook = await client.fetch_orderbook(ticker)
        if orderbook:
            position_count = pos.get('position', 0) or pos.get('count', 0)
            if position_count > 0:
                # We're long YES
                if 'yes' in orderbook:
                    best_ask = orderbook['yes'].get('best_ask')
                    if best_ask:
                        current_price_cents = int((1.0 - best_ask) * 100)
                        pos['current_price_cents'] = current_price_cents
                        pos['current_odds'] = price_to_american_odds(current_price_cents)
            elif position_count < 0:
                # We're long NO
                if 'no' in orderbook:
                    best_ask = orderbook['no'].get('best_ask')
                    if best_ask:
                        current_price_cents = int((1.0 - best_ask) * 100)
                        pos['current_price_cents'] = current_price_cents
                        pos['current_odds'] = price_to_american_odds(current_price_cents)
    except Exception as e:
        print(f"Warning: Could not fetch position details for {ticker}: {e}")


@app.route('/api/portfolio', methods=['GET'])
def get_portfolio():
    """Get portfolio balance and positions
    
    CRITICAL: Uses dedicated api_loop (NOT monitor_loop) so dashboard/API calls
    never touch the monitor thread. Monitoring and auto-betting are never interrupted.
    """
    global api_loop, api_kalshi_client, monitor_loop, kalshi_client
    client = api_kalshi_client or kalshi_client
    loop = api_loop if (api_loop and not api_loop.is_closed()) else (monitor_loop if (monitor_loop and not monitor_loop.is_closed()) else None)
    
    if not client:
        return jsonify({'error': 'Kalshi client not initialized', 'cash': 0, 'portfolio_value': 0, 'positions': []}), 500
    
    try:
        # Prefer dedicated API loop so we NEVER block or touch the monitor thread
        if loop:
            try:
                portfolio_future = asyncio.run_coroutine_threadsafe(client.get_portfolio(), loop)
                portfolio = portfolio_future.result(timeout=2)  # 2s timeout - fail fast
            except Exception as e:
                portfolio = None
            
            try:
                positions_future = asyncio.run_coroutine_threadsafe(client.get_positions(), loop)
                positions = positions_future.result(timeout=2)  # 2s timeout - fail fast
            except Exception as e:
                positions = []
        else:
            portfolio = None
            positions = []
        
        # Parse portfolio response
        # current_value endpoint returns: {"value": {"a": 79994, "v": 19703, "cumulative_deposits": 98000}}
        # a = available cash (cents)
        # v = positions value (cents)
        # cumulative_deposits = total deposits (cents)
        
        cash_cents = 0
        positions_value_cents = 0
        cumulative_deposits_cents = 0
        
        if portfolio:
            value_data = portfolio.get('value', {})
            if value_data:
                # New current_value format
                cash_cents = value_data.get('a', 0)  # Available cash
                positions_value_cents = value_data.get('v', 0)  # Positions value
                cumulative_deposits_cents = value_data.get('cumulative_deposits', 0)
            else:
                # Fallback to old balance format (from get_portfolio_balance_fallback)
                # Balance response has: {'balance': 79412, 'portfolio_value': 21535, ...}
                # 'balance' = cash, 'portfolio_value' = positions value
                cash_cents = portfolio.get('balance', 0) or portfolio.get('balance_cents', 0) or portfolio.get('cash_balance', 0)
                if isinstance(portfolio, (int, float)):
                    cash_cents = portfolio
                # Use portfolio_value from balance response as positions value
                if positions_value_cents == 0:
                    positions_value_cents = portfolio.get('portfolio_value', 0)
        
        # Also calculate positions value from positions list if we have it
        if positions:
            calculated_positions_value_cents = 0
            for pos in positions:
                # Positions from get_positions() have 'value_cents' or 'value'
                value_cents = pos.get('value_cents', 0)
                if not value_cents:
                    value_dollars = pos.get('value', 0)
                    if value_dollars:
                        value_cents = int(value_dollars * 100)
                    else:
                        # Fallback: calculate from count * price
                        count = pos.get('count', 0) or pos.get('position', 0)
                        price_cents = pos.get('average_price_cents', 0) or (pos.get('average_price', 0) * 100)
                        value_cents = count * price_cents
                calculated_positions_value_cents += value_cents
            
            # Use calculated value if we don't have it from portfolio
            if positions_value_cents == 0 and calculated_positions_value_cents > 0:
                positions_value_cents = calculated_positions_value_cents
        
        cash_dollars = cash_cents / 100.0 if cash_cents else 0
        positions_value_dollars = positions_value_cents / 100.0 if positions_value_cents else 0
        portfolio_value_dollars = cash_dollars + positions_value_dollars
        cumulative_deposits_dollars = cumulative_deposits_cents / 100.0 if cumulative_deposits_cents else 0
        
        # FALLBACK: If API doesn't return cumulative_deposits, use stored initial deposit
        # This allows P/L calculation even when current_value endpoint fails
        if cumulative_deposits_dollars == 0:
            cumulative_deposits_dollars = INITIAL_DEPOSIT_DOLLARS
            # Portfolio logging removed
        
        # Calculate P/L: Portfolio Value - Cumulative Deposits
        # This shows your total profit/loss since you started depositing
        pnl_dollars = portfolio_value_dollars - cumulative_deposits_dollars
        
        pnl_display = f"${pnl_dollars:+.2f}" if pnl_dollars is not None else "N/A"
        # Portfolio logging removed - keep functionality but reduce noise
        
        # Enhance positions with market details, current odds, and P/L
        # Group positions by event/market
        enhanced_positions_by_event = {}
        if positions:
            for pos in positions:
                ticker = pos.get('ticker', '')
                if not ticker:
                    continue
                
                # Extract event ticker from submarket ticker
                # Format: KXNHLGAME-26JAN05DETOTT-DET -> KXNHLGAME-26JAN05DETOTT
                event_ticker = ticker.rsplit('-', 1)[0] if '-' in ticker else ticker
                
                # Calculate entry price (average price per contract)
                # CRITICAL: Use Kalshi's API average_price_cents if available (most accurate)
                # Otherwise fall back to calculating from total_traded
                position_count = pos.get('position', 0) or pos.get('count', 0)
                total_traded = pos.get('total_traded', 0)  # in dollars
                fees_paid = pos.get('fees_paid', 0)  # in dollars (from fees_paid_dollars field)
                
                # Try to get average price from API first (matches mobile app)
                entry_price_cents = pos.get('average_price_cents', 0) or pos.get('entry_price_cents', 0)
                if entry_price_cents == 0 and pos.get('average_price'):
                    entry_price_cents = int(pos.get('average_price', 0) * 100)
                
                # Fallback: calculate from total_traded (may not match Kalshi's calculation exactly)
                if entry_price_cents == 0 and position_count > 0:
                    entry_price_cents = int((total_traded / position_count * 100))
                
                # Cost = total_traded + fees_paid (matches Kalshi's "Cost" column)
                # Kalshi shows the total cost including fees, not just the trade amount
                cost = total_traded + fees_paid
                # Market value = current mark-to-market value
                # Start with market_exposure from API, but we'll update from orderbook if available
                market_exposure = pos.get('market_exposure', 0)  # Already in dollars from API
                # Initial market value - will be updated from orderbook if we can fetch it
                # Note: market_exposure often equals cost when position hasn't changed, so we need orderbook
                market_value = market_exposure if market_exposure > 0 else (pos.get('value', 0) or (pos.get('value_cents', 0) / 100.0))
                # Total return will be recalculated after orderbook fetch (if successful)
                total_return = 0
                total_return_pct = 0
                
                # Try to fetch market name synchronously (with timeout)
                market_name = event_ticker  # Fallback to event ticker
                submarket_name = ticker  # Fallback to ticker
                current_price_cents = None
                
                # Fetch event details to get market name
                # Use alert loop with SHORT timeout - if it fails, we use fallback
                try:
                    if loop.is_running():
                        event_future = asyncio.run_coroutine_threadsafe(
                            kalshi_client.get_event_by_ticker(event_ticker),
                            loop
                        )
                        event_data = event_future.result(timeout=1.0)  # 1s timeout - fail fast
                        if event_data:
                            # get_event_by_ticker returns a dict with event data nested under 'event' key
                            if isinstance(event_data, dict):
                                # Event data is nested: {'event': {...}, 'markets': [...]}
                                event_info = event_data.get('event', {})
                                if isinstance(event_info, dict):
                                    # Try different possible field names for event title
                                    market_name = (event_info.get('title') or 
                                                 event_info.get('event_title') or
                                                 event_info.get('name') or
                                                 event_info.get('event_ticker') or 
                                                 event_ticker)
                                    # Portfolio logging removed
                                    pass
                                else:
                                    market_name = event_ticker
                            else:
                                market_name = event_ticker
                        # Portfolio logging removed
                        pass
                    else:
                        # Fallback: use run_until_complete
                        try:
                            event_data = loop.run_until_complete(kalshi_client.get_event_by_ticker(event_ticker))
                        except:
                            event_data = None
                        
                        if event_data:
                            if isinstance(event_data, dict):
                                # Event data is nested: {'event': {...}, 'markets': [...]}
                                event_info = event_data.get('event', {})
                                if isinstance(event_info, dict):
                                    market_name = (event_info.get('title') or 
                                                 event_info.get('event_title') or
                                                 event_info.get('name') or
                                                 event_info.get('event_ticker') or 
                                                 event_ticker)
                                    # Portfolio logging removed
                                    pass
                                else:
                                    market_name = event_ticker
                            else:
                                market_name = event_ticker
                except Exception as e:
                    # Silently fail - use fallback
                    pass
                
                # Fetch market details for submarket name
                # Use alert loop with SHORT timeout - if it fails, we use fallback
                try:
                    if loop.is_running():
                        market_future = asyncio.run_coroutine_threadsafe(
                            kalshi_client.get_market_by_ticker(ticker),
                            loop
                        )
                        market_data = market_future.result(timeout=1.0)  # 1s timeout - fail fast
                        if market_data:
                            # Market data is nested: {'market': {...}}
                            market_info = market_data.get('market', {})
                            if isinstance(market_info, dict):
                                # Market data has yes_sub_title and no_sub_title fields
                                if position_count > 0:
                                    # Long YES position - use yes subtitle
                                    submarket_name = market_info.get('yes_sub_title') or market_info.get('subtitle') or ticker
                                    # Portfolio logging removed
                                    pass
                                else:
                                    # Long NO position - use no subtitle
                                    submarket_name = market_info.get('no_sub_title') or market_info.get('subtitle') or ticker
                                    # Portfolio logging removed
                                    pass
                        # Portfolio logging removed
                        pass
                    else:
                        # Fallback: use run_until_complete
                        try:
                            market_data = loop.run_until_complete(kalshi_client.get_market_by_ticker(ticker))
                        except:
                            market_data = None
                        
                        if market_data:
                            # Market data is nested: {'market': {...}}
                            market_info = market_data.get('market', {})
                            if isinstance(market_info, dict):
                                if position_count > 0:
                                    submarket_name = market_info.get('yes_sub_title') or market_info.get('subtitle') or ticker
                                else:
                                    submarket_name = market_info.get('no_sub_title') or market_info.get('subtitle') or ticker
                except Exception as e:
                    # Silently fail - use fallback
                    pass
                
                # Fetch current orderbook for current odds and market value
                # Use alert loop with SHORT timeout - if it fails, we use market_exposure
                # Alerts are the priority - positions can wait
                try:
                    if loop.is_running():
                        orderbook_future = asyncio.run_coroutine_threadsafe(
                            kalshi_client.fetch_orderbook(ticker),
                            loop
                        )
                        # Use shorter timeout (1.5s) - if it times out, we'll use market_exposure
                        orderbook = orderbook_future.result(timeout=1.5)  # 1.5s timeout - fail fast
                    else:
                        # Fallback: use run_until_complete
                        try:
                            orderbook = loop.run_until_complete(kalshi_client.fetch_orderbook(ticker))
                        except:
                            orderbook = None
                    
                    if orderbook:
                        # Portfolio logging removed
                        # Orderbook structure: {'yes': {'best_bid': 0.65, 'best_ask': 0.66, ...}, 'no': {...}}
                        if position_count > 0:
                            # We're long YES - use MID-PRICE (average of bid and ask) to match Kalshi's mark-to-market
                            # Kalshi uses mid-price or last traded, not just bid (which is conservative)
                            yes_data = orderbook.get('yes', {})
                            no_data = orderbook.get('no', {})
                            if isinstance(yes_data, dict) and isinstance(no_data, dict):
                                yes_best_bid = yes_data.get('best_bid')
                                no_best_bid = no_data.get('best_bid')
                                
                                if yes_best_bid is not None and yes_best_bid > 0 and no_best_bid is not None:
                                    # YES ask = 1 - NO bid
                                    yes_best_ask = 1.0 - no_best_bid
                                    # Mid-price = average of bid and ask (matches Kalshi's valuation)
                                    yes_mid_price = (yes_best_bid + yes_best_ask) / 2.0
                                    current_price_cents = int(yes_mid_price * 100)
                                    # Calculate market value using mid-price
                                    actual_market_value = abs(position_count) * yes_mid_price
                                    if actual_market_value > 0:
                                        market_value = actual_market_value
                                        # Portfolio logging removed
                                elif yes_best_bid is not None and yes_best_bid > 0:
                                    # Fallback to bid if we can't calculate mid-price
                                    current_price_cents = int(yes_best_bid * 100)
                                    actual_market_value = abs(position_count) * yes_best_bid
                                    if actual_market_value > 0:
                                        market_value = actual_market_value
                                        # Portfolio logging removed
                                # Portfolio logging removed
                                pass
                        elif position_count < 0:
                            # We're long NO - use MID-PRICE (average of bid and ask) to match Kalshi's mark-to-market
                            yes_data = orderbook.get('yes', {})
                            no_data = orderbook.get('no', {})
                            if isinstance(yes_data, dict) and isinstance(no_data, dict):
                                no_best_bid = no_data.get('best_bid')
                                yes_best_bid = yes_data.get('best_bid')
                                
                                if no_best_bid is not None and no_best_bid > 0 and yes_best_bid is not None:
                                    # NO ask = 1 - YES bid
                                    no_best_ask = 1.0 - yes_best_bid
                                    # Mid-price = average of bid and ask (matches Kalshi's valuation)
                                    no_mid_price = (no_best_bid + no_best_ask) / 2.0
                                    current_price_cents = int(no_mid_price * 100)
                                    # Calculate market value using mid-price
                                    actual_market_value = abs(position_count) * no_mid_price
                                    if actual_market_value > 0:
                                        market_value = actual_market_value
                                        # Portfolio logging removed
                                elif no_best_bid is not None and no_best_bid > 0:
                                    # Fallback to bid if we can't calculate mid-price
                                    current_price_cents = int(no_best_bid * 100)
                                    actual_market_value = abs(position_count) * no_best_bid
                                    if actual_market_value > 0:
                                        market_value = actual_market_value
                                        # Portfolio logging removed
                                # Portfolio logging removed
                                pass
                    # Portfolio logging removed
                    pass
                except asyncio.TimeoutError:
                    # Portfolio logging removed
                    pass
                except Exception as e:
                    # Portfolio logging removed
                    pass
                
                # Enhance position data
                enhanced_pos = pos.copy()
                enhanced_pos['event_ticker'] = event_ticker
                enhanced_pos['market_name'] = market_name
                enhanced_pos['submarket_name'] = submarket_name
                enhanced_pos['entry_price_cents'] = entry_price_cents
                enhanced_pos['entry_odds'] = price_to_american_odds(entry_price_cents) if entry_price_cents > 0 else "N/A"
                enhanced_pos['current_price_cents'] = current_price_cents
                enhanced_pos['current_odds'] = price_to_american_odds(current_price_cents) if current_price_cents else "N/A"
                enhanced_pos['cost'] = cost
                enhanced_pos['market_value'] = market_value
                enhanced_pos['market_exposure'] = market_exposure  # Include market_exposure for frontend
                # Recalculate total return with updated market_value (from orderbook if available)
                total_return = market_value - cost
                total_return_pct = (total_return / cost * 100) if cost > 0 else 0
                enhanced_pos['total_return'] = total_return
                enhanced_pos['total_return_pct'] = total_return_pct
                
                # Group by event
                if event_ticker not in enhanced_positions_by_event:
                    enhanced_positions_by_event[event_ticker] = {
                        'event_ticker': event_ticker,
                        'market_name': market_name,  # Will be updated if fetch succeeds
                        'positions': []
                    }
                else:
                    # Update market name if we got a better one (not just ticker)
                    if market_name != event_ticker and market_name != ticker and len(market_name) > len(event_ticker):
                        enhanced_positions_by_event[event_ticker]['market_name'] = market_name
                enhanced_positions_by_event[event_ticker]['positions'].append(enhanced_pos)
        
        # Convert grouped positions to list format
        enhanced_positions = list(enhanced_positions_by_event.values())
        
        # Portfolio logging removed - keep functionality but reduce noise
        
        response_data = {
            'cash': cash_dollars,
            'cash_cents': int(cash_cents),
            'positions_value': positions_value_dollars,
            'positions_value_cents': int(positions_value_cents),
            'portfolio_value': portfolio_value_dollars,
            'cumulative_deposits': cumulative_deposits_dollars,
            'pnl': pnl_dollars,
            'positions': enhanced_positions,  # Now grouped by event
            # Legacy fields for backward compatibility
            'balance': cash_dollars,
            'balance_cents': int(cash_cents),
            'position_value': positions_value_dollars
        }
        
        return jsonify(response_data)
    except Exception as e:
        print(f"Error fetching portfolio: {e}")
        # Return default values on error
        return jsonify({
            'error': str(e),
            'balance': 0,
            'balance_cents': 0,
            'positions': [],
            'position_value': 0
        })


@app.route('/api/set_max_bet', methods=['POST'])
def set_max_bet():
    """Set user's max bet amount"""
    global user_max_bet_amount
    
    data = request.json
    max_amount = float(data.get('max_amount', 200.0))
    
    if max_amount <= 0:
        return jsonify({'error': 'Max bet amount must be positive'}), 400
    
    user_max_bet_amount = max_amount
    return jsonify({'success': True, 'max_bet_amount': user_max_bet_amount})


@app.route('/api/get_max_bet', methods=['GET'])
def get_max_bet():
    """Get user's max bet amount"""
    return jsonify({'max_bet_amount': user_max_bet_amount})


@app.route("/api/broad_scan_pregame", methods=["GET", "POST"])
def broad_scan_pregame():
    """Read/update whether diagnostic broad scan merges pregame MLB/NBA/NHL /events (class-level, no .env)."""
    from odds_ev_monitor import OddsEVMonitor

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        OddsEVMonitor.broad_scan_include_pregame = bool(data.get("include", True))
    return jsonify({"include": OddsEVMonitor.broad_scan_include_pregame})


@app.route('/api/get_auto_bet', methods=['GET'])
def get_auto_bet():
    """Get auto-bet settings (per-filter)"""
    global auto_bet_enabled, auto_bet_ev_min, auto_bet_ev_max, auto_bet_odds_min, auto_bet_odds_max, auto_bet_amount, nhl_over_bet_amount, px_novig_multiplier, auto_bet_settings_by_filter, saved_filters, selected_auto_bettor_filters
    return jsonify({
        'enabled': auto_bet_enabled,
        'nhl_over_amount': nhl_over_bet_amount,
        'nhl_overs_amount': nhl_over_bet_amount,  # For frontend compatibility
        'px_novig_multiplier': px_novig_multiplier,  # Multiplier for ProphetX + Novig bets
        'settings_by_filter': auto_bet_settings_by_filter,  # Per-filter settings
        'available_filters': list(saved_filters.keys()),  # All available filters
        'selected_auto_bettor_filters': selected_auto_bettor_filters,  # Which filters are enabled for auto-betting
        # Legacy fields for backward compatibility
        'ev_min': auto_bet_ev_min,
        'ev_max': auto_bet_ev_max,
        'odds_min': auto_bet_odds_min,
        'odds_max': auto_bet_odds_max,
        'amount': auto_bet_amount
    })


@app.route("/api/alert_feed_prefs", methods=["GET", "POST"])
def alert_feed_prefs():
    """Read/set pipeline toggle: include pregame Kalshi value-bets (relaxes ODDS_API_LIVE_ONLY)."""
    if request.method == "GET":
        return jsonify(
            {
                "include_pregame_value_bets": bool(
                    getattr(EvMonitorImpl, "include_pregame_value_bets", False)
                )
            }
        )
    data = request.get_json(silent=True) or {}
    if "include_pregame_value_bets" not in data:
        return jsonify({"error": "include_pregame_value_bets required"}), 400
    EvMonitorImpl.include_pregame_value_bets = bool(data["include_pregame_value_bets"])
    print(
        f"[DASHBOARD] Alert feed prefs: include_pregame_value_bets={EvMonitorImpl.include_pregame_value_bets}"
    )
    return jsonify(
        {
            "success": True,
            "include_pregame_value_bets": EvMonitorImpl.include_pregame_value_bets,
        }
    )


@app.route("/token-update")
@requires_auth
def token_update_page():
    """Legacy route; Odds-API.io uses ODDS_API_KEY in .env (not edited by the app)."""
    return redirect("/")

@app.route('/control')
@requires_auth
def control_panel():
    """Bot control panel page"""
    return render_template('control_panel.html')

@app.route('/logs')
@requires_auth
def logs_page():
    """View bot logs"""
    return render_template('logs.html')

@app.route('/failed-bets')
@requires_auth
def failed_bets():
    """Display failed auto-bet attempts with full details"""
    return render_template('failed_bets.html')

@app.route('/api/failed_bets', methods=['GET'])
@requires_auth
def api_failed_bets():
    """API endpoint to get failed and successful auto-bet attempts (for comparison)"""
    global failed_auto_bets, successful_auto_bets
    # Return most recent first
    failed_list = list(reversed(failed_auto_bets))
    successful_list = list(reversed(successful_auto_bets))
    print(f"[API] /api/failed_bets called - returning {len(failed_list)} failed, {len(successful_list)} successful")
    return jsonify({
        'failed_bets': failed_list,
        'successful_bets': successful_list
    })

@app.route('/api/clear_failed_bets', methods=['POST'])
@requires_auth
def api_clear_failed_bets():
    """Clear all failed and successful auto-bet records"""
    global failed_auto_bets, successful_auto_bets
    failed_auto_bets.clear()
    successful_auto_bets.clear()
    return jsonify({'success': True, 'message': 'Failed and successful bets cleared'})

@app.route('/api/bot_status', methods=['GET'])
@requires_auth
def bot_status():
    """Get bot status (running, stopped, etc.)"""
    global monitor_thread, monitor_running, auto_bet_enabled
    
    is_running = monitor_thread is not None and monitor_thread.is_alive() and monitor_running
    
    # Check systemd service status
    service_running = False
    try:
        import subprocess
        result = subprocess.run(
            ['/usr/bin/systemctl', 'is-active', 'kalshi-bot.service'],
            capture_output=True,
            text=True,
            timeout=5
        )
        service_running = (result.returncode == 0 and result.stdout.strip() == 'active')
    except:
        pass  # If we can't check, assume it's running if monitor is running
    
    return jsonify({
        'running': is_running,
        'service_running': service_running,
        'auto_bet_enabled': auto_bet_enabled,
        'monitor_thread_alive': monitor_thread is not None and monitor_thread.is_alive(),
        'monitor_running': monitor_running
    })

@app.route('/api/bot_control', methods=['POST'])
@requires_auth
def bot_control():
    """Control bot - can control auto-betting or the monitoring loop"""
    global auto_bet_enabled, monitor_running, monitor_thread, monitor_loop, odds_ev_monitors
    
    data = request.json
    action = data.get('action', '').lower()
    
    if action == 'enable_auto_bet':
        auto_bet_enabled = True
        return jsonify({'success': True, 'message': 'Auto-betting enabled', 'auto_bet_enabled': True})
    elif action == 'disable_auto_bet':
        auto_bet_enabled = False
        return jsonify({'success': True, 'message': 'Auto-betting disabled', 'auto_bet_enabled': False})
    elif action == 'stop_service' or action == 'stop_bot':
        # Stop the monitoring loop (but keep Flask server running)
        try:
            monitor_running = False
            
            # Stop all monitors
            async def stop_all_monitors():
                global odds_ev_monitors
                for filter_name, monitor in odds_ev_monitors.items():
                    try:
                        await monitor.stop()
                        print(f"[CONTROL] Stopped monitor for filter: {filter_name}")
                    except Exception as e:
                        print(f"[CONTROL] Error stopping monitor {filter_name}: {e}")
            
            # Use the monitor's event loop if available
            if monitor_loop and not monitor_loop.is_closed():
                # Schedule the stop on the monitor's event loop
                asyncio.run_coroutine_threadsafe(stop_all_monitors(), monitor_loop)
            else:
                # If no loop, try to stop synchronously (monitors will stop on next check)
                print("[CONTROL] Monitor loop not available, monitors will stop on next check")
            
            return jsonify({'success': True, 'message': 'Bot monitoring stopped (web interface still running)', 'running': False})
        except Exception as e:
            return jsonify({'success': False, 'error': f'Error stopping bot: {str(e)}'}), 500
    elif action == 'start_service' or action == 'start_bot':
        # Start the monitoring loop
        try:
            
            # If monitor thread is not running, start it
            if monitor_thread is None or not monitor_thread.is_alive():
                print("[CONTROL] Starting monitor thread...")
                monitor_running = True
                monitor_thread = threading.Thread(target=run_monitor_loop, daemon=True)
                monitor_thread.start()
                return jsonify({'success': True, 'message': 'Bot monitoring started', 'running': True})
            elif not monitor_running:
                # Thread exists but monitor is stopped - restart monitors
                print("[CONTROL] Restarting monitors...")
                monitor_running = True
                
                # Restart all monitors using the monitor's event loop
                async def restart_all_monitors():
                    global odds_ev_monitors, selected_dashboard_filters, selected_auto_bettor_filters, saved_filters
                    all_selected_filters = list(set(selected_dashboard_filters + selected_auto_bettor_filters))
                    
                    for filter_name in all_selected_filters:
                        if filter_name in odds_ev_monitors:
                            monitor = odds_ev_monitors[filter_name]
                            if not monitor.running:
                                success = await monitor.start()
                                if success:
                                    print(f"[CONTROL] Restarted monitor for filter: {filter_name}")
                        else:
                            # Create new monitor
                            filter_payload = saved_filters.get(filter_name)
                            if filter_payload:
                                monitor = EvMonitorImpl(auth_token=None)
                                monitor.set_filter(filter_payload)
                                monitor.poll_interval = monitor_poll_seconds()
                                odds_ev_monitors[filter_name] = monitor
                                success = await monitor.start()
                                if success:
                                    print(f"[CONTROL] Started new monitor for filter: {filter_name}")
                
                if monitor_loop and not monitor_loop.is_closed():
                    asyncio.run_coroutine_threadsafe(restart_all_monitors(), monitor_loop)
                    return jsonify({'success': True, 'message': 'Bot monitoring restarted', 'running': True})
                else:
                    return jsonify({'success': False, 'error': 'Monitor loop not available. Please restart the service.'}), 500
            else:
                return jsonify({'success': True, 'message': 'Bot is already running', 'running': True})
        except Exception as e:
            return jsonify({'success': False, 'error': f'Error starting bot: {str(e)}'}), 500
    elif action == 'restart_service' or action == 'restart_bot':
        # Restart the monitoring loop
        try:
            # First stop
            monitor_running = False
            old_thread = monitor_thread

            async def stop_all_monitors():
                global odds_ev_monitors
                for filter_name, monitor in odds_ev_monitors.items():
                    try:
                        await monitor.stop()
                    except Exception as e:
                        print(f"[CONTROL] Error stopping monitor {filter_name}: {e}")

            if monitor_loop and not monitor_loop.is_closed():
                asyncio.run_coroutine_threadsafe(stop_all_monitors(), monitor_loop)

            # Wait for old thread to fully exit so we don't have two loops (avoids "attached to a different loop")
            if old_thread and old_thread.is_alive():
                old_thread.join(timeout=5.0)
                if old_thread.is_alive():
                    print("[CONTROL] Warning: old monitor thread did not exit within 5s")

            # Restart
            monitor_thread = threading.Thread(target=run_monitor_loop, daemon=True)
            monitor_thread.start()
            monitor_running = True

            return jsonify({'success': True, 'message': 'Bot monitoring restarted', 'running': True})
        except Exception as e:
            return jsonify({'success': False, 'error': f'Error restarting bot: {str(e)}'}), 500
    else:
        return jsonify({'success': False, 'error': f'Unknown action: {action}'}), 400

@app.route('/api/logs', methods=['GET'])
@requires_auth
def get_logs():
    """Get recent bot logs - shows what you'd see in PowerShell.
    Note: Opening dashboard, portfolio, or refreshing logs does NOT stop the
    monitor; it runs in a separate thread. If you see no new [MONITOR] lines after clicking
    around, the log buffer may show the latest HTTP requests first; monitoring is still active."""
    try:
        import subprocess
        
        # Try multiple formats, starting with the cleanest
        formats_to_try = [
            ('cat', 'cat'),  # Pure messages only
            ('short-precise', 'short-precise'),  # Timestamp + message
            ('short', 'short'),  # Standard format
            ('default', None),  # Default format
        ]
        
        for format_name, format_option in formats_to_try:
            try:
                # Use full path to journalctl (not in PATH when running from Python)
                cmd = ['/usr/bin/journalctl', '-u', 'kalshi-bot.service', '-n', '1000', '--no-pager']
                if format_option:
                    cmd.extend(['-o', format_option])
                
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                
                if result.returncode == 0 and result.stdout and result.stdout.strip():
                    logs = result.stdout.split('\n')
                    
                    # For 'cat' format, just filter empty lines and Flask HTTP requests
                    if format_name == 'cat':
                        # Filter out Flask HTTP request logs (they're just noise)
                        filtered_logs = []
                        for log in logs:
                            log = log.strip()
                            if not log:
                                continue
                            # Skip Flask HTTP request logs
                            if 'GET /api/logs HTTP/1.1' in log or 'GET /logs HTTP/1.1' in log:
                                continue
                            if 'GET /style.css' in log or 'GET /script.js' in log:
                                continue
                            if 'GET /socket.io/' in log or 'POST /socket.io/' in log:
                                continue
                            if 'GET /favicon.ico' in log:
                                continue
                            filtered_logs.append(log)
                        if filtered_logs:
                            filtered_logs = filtered_logs[-500:] if len(filtered_logs) > 500 else filtered_logs
                            return jsonify({'success': True, 'logs': filtered_logs})
                    else:
                        # For other formats, extract messages
                        cleaned_logs = []
                        for log in logs:
                            log = log.strip()
                            if not log:
                                continue
                            
                            # Skip systemd status lines
                            if any(skip in log for skip in [
                                'Main PID:',
                                'Tasks:',
                                'Memory:',
                                'CPU:',
                                'CGroup:',
                                'Consumed',
                                'systemd[1]: Started Kalshi Betting Bot',
                                'systemd[1]: Stopped Kalshi Betting Bot',
                            ]):
                                continue
                            
                            # Skip Flask HTTP request logs (they're just noise)
                            if 'GET /api/logs HTTP/1.1' in log or 'GET /logs HTTP/1.1' in log:
                                continue
                            if 'GET /style.css' in log or 'GET /script.js' in log:
                                continue
                            if 'GET /socket.io/' in log or 'POST /socket.io/' in log:
                                continue
                            if 'GET /favicon.ico' in log:
                                continue
                            
                            # Extract message from journalctl format
                            # Format: "Jan 23 17:01:20 kalshi-bot python[PID]: message"
                            if 'python[' in log and ']:' in log:
                                # Split on ']:' to get message
                                parts = log.split(']:', 1)
                                if len(parts) > 1:
                                    message = parts[1].strip()
                                    if message:
                                        cleaned_logs.append(message)
                            elif 'python ' in log and ': ' in log:
                                # Alternative format
                                parts = log.split(': ', 1)
                                if len(parts) > 1:
                                    message = parts[1].strip()
                                    if message:
                                        cleaned_logs.append(message)
                            else:
                                # Keep line as-is if we can't parse
                                cleaned_logs.append(log)
                        
                        if cleaned_logs:
                            cleaned_logs = cleaned_logs[-500:] if len(cleaned_logs) > 500 else cleaned_logs
                            return jsonify({'success': True, 'logs': cleaned_logs})
            except subprocess.TimeoutExpired:
                continue  # Try next format
            except Exception as e:
                # Silently try next format - don't spam logs with errors
                continue  # Try next format
        
        # If all formats failed, check service status
        try:
            # Use full path to systemctl
            status_result = subprocess.run(
                ['/usr/bin/systemctl', 'is-active', 'kalshi-bot.service'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if status_result.returncode == 0 and status_result.stdout.strip() == 'active':
                return jsonify({
                    'success': True, 
                    'logs': [
                        'Bot is running but no log output found.',
                        '',
                        'This might mean:',
                        '1. Bot just started (wait a few seconds and refresh)',
                        '2. Bot is waiting for alerts (this is normal)',
                        '3. Logs might be in a different format',
                        '',
                        'Try: journalctl -u kalshi-bot.service -n 50 (via SSH)',
                        'to see what format the logs are in.'
                    ]
                })
            else:
                return jsonify({
                    'success': True,
                    'logs': [
                        'Bot service may not be running.',
                        '',
                        'To check status:',
                        'systemctl status kalshi-bot.service',
                        '',
                        'To start bot:',
                        'systemctl start kalshi-bot.service'
                    ]
                })
        except Exception as e:
            print(f"[LOGS] Error checking service status: {e}")
            
        # Final fallback
        return jsonify({
            'success': False,
            'error': 'Could not fetch logs. Check server logs for details.'
        }), 500
            
    except FileNotFoundError:
        return jsonify({'success': False, 'error': 'journalctl command not found. This should not happen on a properly configured server.'}), 500
    except Exception as e:
        # Don't spam logs with errors - just return a clean error message
        return jsonify({'success': False, 'error': f'Error fetching logs: {str(e)}'}), 500

@app.route("/api/token_status", methods=["GET"])
@requires_auth
def token_status():
    """Odds-API.io has no bearer token; frontend may poll this for compatibility."""
    return jsonify({"expired": False, "mode": "odds-api"})


@app.route("/api/update_token", methods=["POST"])
@requires_auth
def update_token():
    """Not used with Odds-API.io (configure ODDS_API_KEY in .env and restart)."""
    return (
        jsonify(
            {
                "success": False,
                "message": "Token API disabled. Set ODDS_API_KEY (and optional ODDS_API_BOOKMAKERS) in .env, then restart the dashboard.",
            }
        ),
        410,
    )


@app.route('/api/set_nhl_over_amount', methods=['POST'])
def set_nhl_over_amount():
    """Set NHL over bet amount (for frontend real-time updates)"""
    global nhl_over_bet_amount
    
    data = request.json
    if 'amount' not in data:
        return jsonify({'error': 'amount is required'}), 400
    
    try:
        new_amount = float(data['amount'])
        if new_amount <= 0:
            return jsonify({'error': 'Amount must be positive'}), 400
        
        old_amount = nhl_over_bet_amount
        nhl_over_bet_amount = new_amount
        print(f"[AUTO-BET] NHL Over bet amount updated: ${old_amount:.2f} -> ${nhl_over_bet_amount:.2f}")
        
        return jsonify({
            'success': True,
            'nhl_over_amount': nhl_over_bet_amount,
            'message': f'NHL Over bet amount updated to ${nhl_over_bet_amount:.2f}'
        })
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid amount'}), 400


@app.route('/api/set_auto_bet', methods=['POST'])
def set_auto_bet():
    """Set auto-bet settings"""
    global auto_bet_enabled, auto_bet_ev_min, auto_bet_ev_max, auto_bet_odds_min, auto_bet_odds_max, auto_bet_amount, nhl_over_bet_amount, px_novig_multiplier, auto_bet_submarkets, auto_bet_settings_by_filter, selected_auto_bettor_filters
    
    data = request.json
    if not data:
        print(f"[AUTO-BET] ERROR: set_auto_bet called with no data")
        return jsonify({'error': 'No data provided'}), 400
    
    # Log the incoming request for debugging
    print(f"[AUTO-BET] Received settings update: {data}")
    
    # Handle per-filter settings update
    if 'settings_by_filter' in data:
        for filter_name, settings in data['settings_by_filter'].items():
            if filter_name in auto_bet_settings_by_filter:
                auto_bet_settings_by_filter[filter_name].update(settings)
                print(f"[AUTO-BET] Updated settings for filter '{filter_name}': {settings}")
            else:
                # Create new settings for this filter
                auto_bet_settings_by_filter[filter_name] = settings
                print(f"[AUTO-BET] Created new settings for filter '{filter_name}': {settings}")
    
    # Handle selected auto-bettor filters
    if 'selected_auto_bettor_filters' in data:
        selected_auto_bettor_filters = data['selected_auto_bettor_filters']
        print(f"[AUTO-BET] Updated selected auto-bettor filters: {selected_auto_bettor_filters}")
    
    old_enabled = auto_bet_enabled
    old_ev_min = auto_bet_ev_min
    old_ev_max = auto_bet_ev_max
    old_odds_min = auto_bet_odds_min
    old_odds_max = auto_bet_odds_max
    old_amount = auto_bet_amount
    
    if 'enabled' in data:
        auto_bet_enabled = bool(data.get('enabled', False))
        # Send Telegram alert if state changed
        if old_enabled != auto_bet_enabled:
            send_auto_bet_state_change(auto_bet_enabled)
    
    if 'ev_min' in data:
        new_ev_min = float(data['ev_min'])
        if new_ev_min != old_ev_min:
            print(f"[AUTO-BET] EV min changed: {old_ev_min}% -> {new_ev_min}%")
        auto_bet_ev_min = new_ev_min
    if 'ev_max' in data:
        new_ev_max = float(data['ev_max'])
        if new_ev_max != old_ev_max:
            print(f"[AUTO-BET] EV max changed: {old_ev_max}% -> {new_ev_max}%")
        auto_bet_ev_max = new_ev_max
    if 'odds_min' in data:
        new_odds_min = int(data['odds_min'])
        if new_odds_min != old_odds_min:
            print(f"[AUTO-BET] Odds min changed: {old_odds_min} -> {new_odds_min}")
        auto_bet_odds_min = new_odds_min
    if 'odds_max' in data:
        new_odds_max = int(data['odds_max'])
        if new_odds_max != old_odds_max:
            print(f"[AUTO-BET] Odds max changed: {old_odds_max} -> {new_odds_max}")
        auto_bet_odds_max = new_odds_max
    if 'amount' in data:
        new_amount = float(data['amount'])
        if new_amount != old_amount:
            print(f"[AUTO-BET] Amount changed: ${old_amount:.2f} -> ${new_amount:.2f}")
        auto_bet_amount = new_amount
    # Accept both 'nhl_over_amount' and 'nhl_overs_amount' for compatibility
    if 'nhl_over_amount' in data or 'nhl_overs_amount' in data:
        old_nhl_amount = nhl_over_bet_amount
        new_nhl_amount = float(data.get('nhl_over_amount') or data.get('nhl_overs_amount'))
        nhl_over_bet_amount = new_nhl_amount
        # Only log if value actually changed
        if new_nhl_amount != old_nhl_amount:
            print(f"[AUTO-BET] NHL Over bet amount updated: ${old_nhl_amount:.2f} -> ${nhl_over_bet_amount:.2f}")
    
    # Handle ProphetX + Novig multiplier
    if 'px_novig_multiplier' in data:
        old_multiplier = px_novig_multiplier
        new_multiplier = float(data['px_novig_multiplier'])
        if new_multiplier < 1.0:
            return jsonify({'error': 'Multiplier must be >= 1.0'}), 400
        px_novig_multiplier = new_multiplier
        if new_multiplier != old_multiplier:
            print(f"[AUTO-BET] ProphetX + Novig multiplier updated: {old_multiplier}x -> {px_novig_multiplier}x")
    
    # Clear duplicate tracking if explicitly requested
    if data.get('clear_duplicates', False):
        auto_bet_submarkets.clear()
        print(f"Auto-bet duplicate tracking cleared")
    
    print(f"Auto-bet settings updated: enabled={auto_bet_enabled}, EV={auto_bet_ev_min}%-{auto_bet_ev_max}%, Odds={auto_bet_odds_min}-{auto_bet_odds_max}, Amount=${auto_bet_amount:.2f}, NHL Over=${nhl_over_bet_amount:.2f}, PX+Novig Multiplier={px_novig_multiplier}x, Tracked submarkets: {len(auto_bet_submarkets)}")
    
    return jsonify({
        'success': True,
        'enabled': auto_bet_enabled,
        'ev_min': auto_bet_ev_min,
        'ev_max': auto_bet_ev_max,
        'odds_min': auto_bet_odds_min,
        'odds_max': auto_bet_odds_max,
        'amount': auto_bet_amount,
        'nhl_over_amount': nhl_over_bet_amount,
        'px_novig_multiplier': px_novig_multiplier,
        'tracked_submarkets': len(auto_bet_submarkets)
    })


def handle_telegram_callback(callback_data: str, callback_id: str = None):
    """Handle Telegram callback query (button clicks)"""
    global auto_bet_enabled, auto_bet_ev_min, auto_bet_ev_max, auto_bet_odds_min, auto_bet_odds_max, auto_bet_amount, auto_bet_submarkets
    
    try:
        if callback_data == 'start_auto_bet':
            old_enabled = auto_bet_enabled
            auto_bet_enabled = True
            if old_enabled != auto_bet_enabled:
                send_auto_bet_state_change(auto_bet_enabled)
            # Answer callback query
            if callback_id:
                answer_callback_query(callback_id, "✅ Auto-betting started!")
            return True
        
        elif callback_data == 'stop_auto_bet':
            old_enabled = auto_bet_enabled
            auto_bet_enabled = False
            if old_enabled != auto_bet_enabled:
                send_auto_bet_state_change(auto_bet_enabled)
            # Answer callback query
            if callback_id:
                answer_callback_query(callback_id, "⛔ Auto-betting stopped!")
            return True
        
        elif callback_data == 'status':
            status_icon = "✅ ACTIVE" if auto_bet_enabled else "⛔ INACTIVE"
            response_text = f"""{status_icon}

EV Range: {auto_bet_ev_min}% - {auto_bet_ev_max}%
Odds Range: {auto_bet_odds_min} to {auto_bet_odds_max}
Bet Amount: ${auto_bet_amount:.2f}
Tracked Submarkets: {len(auto_bet_submarkets)}"""
            
            keyboard = {
                'inline_keyboard': [[
                    {'text': '✅ Start' if not auto_bet_enabled else '⛔ Stop', 
                     'callback_data': 'start_auto_bet' if not auto_bet_enabled else 'stop_auto_bet'},
                    {'text': '📊 Refresh Status', 'callback_data': 'status'}
                ]]
            }
            send_telegram_message(response_text, reply_markup=keyboard)
            if callback_id:
                answer_callback_query(callback_id, "Status updated!")
            return True
        
        elif callback_data == 'clear_duplicates':
            count = len(auto_bet_submarkets)
            auto_bet_submarkets.clear()
            send_telegram_message(f"✅ Cleared {count} tracked submarkets")
            if callback_id:
                answer_callback_query(callback_id, f"Cleared {count} duplicates!")
            return True
        
        elif callback_data.startswith('set_bet_amount_'):
            # Extract amount from callback_data (e.g., 'set_bet_amount_50' -> 50)
            try:
                amount_str = callback_data.replace('set_bet_amount_', '')
                new_amount = float(amount_str)
                old_amount = auto_bet_amount  # auto_bet_amount already declared as global at function start
                auto_bet_amount = new_amount
                send_telegram_message(f"✅ Bet amount changed from ${old_amount:.2f} to ${new_amount:.2f}")
                if callback_id:
                    answer_callback_query(callback_id, f"Set to ${new_amount:.2f}!")
                return True
            except (ValueError, AttributeError) as e:
                if callback_id:
                    answer_callback_query(callback_id, "Error: Invalid amount")
                return False
        
        elif callback_data == 'change_bet_amount':
            # Show quick bet amount options
            current_amount = auto_bet_amount
            message = f"""💰 <b>CHANGE BET AMOUNT</b>

Current: ${current_amount:.2f}

Select new amount:"""
            
            keyboard = {
                'inline_keyboard': [[
                    {'text': '$50', 'callback_data': 'set_bet_amount_50'},
                    {'text': '$100', 'callback_data': 'set_bet_amount_100'},
                    {'text': '$200', 'callback_data': 'set_bet_amount_200'}
                ], [
                    {'text': '$25', 'callback_data': 'set_bet_amount_25'},
                    {'text': '$75', 'callback_data': 'set_bet_amount_75'},
                    {'text': '$150', 'callback_data': 'set_bet_amount_150'}
                ], [
                    {'text': '📊 Status', 'callback_data': 'status'}
                ]]
            }
            send_telegram_message(message, reply_markup=keyboard)
            if callback_id:
                answer_callback_query(callback_id, "Select amount")
            return True
        
        elif callback_data == 'stats':
            stats = get_auto_bet_stats()
            stats_text = f"""📊 <b>AUTO-BET STATISTICS</b>

🎲 Total Bets: {stats['total_bets']}
💰 Total Cost: ${stats['total_cost']:.2f}
📈 Total P&L: ${stats['total_pnl']:+.2f}
📊 ROI: {stats['roi']:+.2f}%

✅ Wins: {stats['wins']}
❌ Losses: {stats['losses']}
⏳ Pending: {stats['pending']}

📈 Win Rate: {stats['win_rate']:.1f}%
⚡ Avg EV: {stats['avg_ev']:.2f}%"""
            
            keyboard = {
                'inline_keyboard': [[
                    {'text': '🔄 Refresh Stats', 'callback_data': 'stats'},
                    {'text': '📊 Status', 'callback_data': 'status'}
                ]]
            }
            send_telegram_message(stats_text, reply_markup=keyboard)
            if callback_id:
                answer_callback_query(callback_id, "Stats updated!")
            return True
        
        return False
    
    except Exception as e:
        print(f"[TELEGRAM] Error handling callback: {e}")
        return False


def answer_callback_query(callback_query_id: str, text: str):
    """Answer a Telegram callback query"""
    global telegram_bot_token, telegram_chat_id
    if not telegram_bot_token:
        return
    
    try:
        import requests
        url = f"https://api.telegram.org/bot{telegram_bot_token}/answerCallbackQuery"
        payload = {
            'callback_query_id': callback_query_id,
            'text': text,
            'show_alert': False
        }
        requests.post(url, json=payload, timeout=3)
    except Exception as e:
        print(f"[TELEGRAM] Error answering callback: {e}")


@app.route('/api/telegram/webhook', methods=['POST'])
def telegram_webhook():
    """Handle Telegram webhook for bot commands and callbacks"""
    global auto_bet_enabled, auto_bet_ev_min, auto_bet_ev_max, auto_bet_odds_min, auto_bet_odds_max, auto_bet_amount, auto_bet_submarkets, telegram_chat_id
    
    try:
        data = request.json
        
        # Handle callback queries (button clicks)
        if 'callback_query' in data:
            callback_query = data['callback_query']
            callback_data = callback_query.get('data', '')
            callback_id = callback_query.get('id', '')
            message = callback_query.get('message', {})
            chat_id = message.get('chat', {}).get('id')
            
            # Update chat_id if not set
            if not telegram_chat_id and chat_id:
                telegram_chat_id = str(chat_id)
                print(f"[TELEGRAM] Chat ID set to: {telegram_chat_id}")
            
            # Handle the callback
            handle_telegram_callback(callback_data, callback_id)
            # Answer the callback query
            answer_callback_query(callback_id, "Processing...")
            return jsonify({'success': True})
        
        # Handle regular messages
        message = data.get('message', {})
        if not message:
            return jsonify({'success': False})
        
        text = message.get('text', '').strip()
        chat_id = message.get('chat', {}).get('id')
        
        if not text or not chat_id:
            return jsonify({'success': False})
        
        # Update chat_id if not set
        if not telegram_chat_id:
            telegram_chat_id = str(chat_id)
            print(f"[TELEGRAM] Chat ID set to: {telegram_chat_id}")
        
        # Handle commands (only send response if state actually changed to avoid duplicates)
        response_text = None
        keyboard = None
        
        if text.lower() in ['/start', '/start_auto_bet', 'start']:
            old_enabled = auto_bet_enabled
            auto_bet_enabled = True
            if old_enabled != auto_bet_enabled:
                send_auto_bet_state_change(auto_bet_enabled)
                return jsonify({'success': True})  # Don't send duplicate message
            else:
                response_text = "✅ Auto-betting is already active!"
                keyboard = {
                    'inline_keyboard': [[
                        {'text': '⛔ Stop Auto-Bet', 'callback_data': 'stop_auto_bet'},
                        {'text': '📊 Status', 'callback_data': 'status'}
                    ]]
                }
        
        elif text.lower() in ['/stop', '/stop_auto_bet', 'stop']:
            old_enabled = auto_bet_enabled
            auto_bet_enabled = False
            if old_enabled != auto_bet_enabled:
                send_auto_bet_state_change(auto_bet_enabled)
                return jsonify({'success': True})  # Don't send duplicate message
            else:
                response_text = "⛔ Auto-betting is already inactive!"
                keyboard = {
                    'inline_keyboard': [[
                        {'text': '✅ Start Auto-Bet', 'callback_data': 'start_auto_bet'},
                        {'text': '📊 Status', 'callback_data': 'status'}
                    ]]
                }
        
        elif text.lower() in ['/status', 'status']:
            status_icon = "✅ ACTIVE" if auto_bet_enabled else "⛔ INACTIVE"
            response_text = f"""{status_icon}

EV Range: {auto_bet_ev_min}% - {auto_bet_ev_max}%
Odds Range: {auto_bet_odds_min} to {auto_bet_odds_max}
Bet Amount: ${auto_bet_amount:.2f}
Tracked Submarkets: {len(auto_bet_submarkets)}"""
            keyboard = {
                'inline_keyboard': [[
                    {'text': '✅ Start' if not auto_bet_enabled else '⛔ Stop', 
                     'callback_data': 'start_auto_bet' if not auto_bet_enabled else 'stop_auto_bet'},
                    {'text': '📊 Refresh', 'callback_data': 'status'}
                ], [
                    {'text': '📈 Stats', 'callback_data': 'stats'},
                    {'text': '🗑️ Clear Duplicates', 'callback_data': 'clear_duplicates'}
                ]]
            }
        
        elif text.lower() in ['/stats', 'stats']:
            stats = get_auto_bet_stats()
            response_text = f"""📊 <b>AUTO-BET STATISTICS</b>

🎲 Total Bets: {stats['total_bets']}
💰 Total Cost: ${stats['total_cost']:.2f}
📈 Total P&L: ${stats['total_pnl']:+.2f}
📊 ROI: {stats['roi']:+.2f}%

✅ Wins: {stats['wins']}
❌ Losses: {stats['losses']}
⏳ Pending: {stats['pending']}

📈 Win Rate: {stats['win_rate']:.1f}%
⚡ Avg EV: {stats['avg_ev']:.2f}%"""
            keyboard = {
                'inline_keyboard': [[
                    {'text': '🔄 Refresh Stats', 'callback_data': 'stats'},
                    {'text': '📊 Status', 'callback_data': 'status'}
                ]]
            }
        
        elif text.lower() in ['/help', 'help']:
                    response_text = """🤖 <b>Kalshi Auto-Bet Bot Commands</b>

<b>Text Commands (just type these):</b>
/start or "start" - Start auto-betting
/stop or "stop" - Stop auto-betting
/status or "status" - Check current status
/stats or "stats" - View auto-bet statistics
/help or "help" - Show this help message

<b>Or use the buttons below messages!</b>"""
                    keyboard = {
                        'inline_keyboard': [[
                            {'text': '✅ Start Auto-Bet', 'callback_data': 'start_auto_bet'},
                            {'text': '⛔ Stop Auto-Bet', 'callback_data': 'stop_auto_bet'}
                        ], [
                            {'text': '📊 Status', 'callback_data': 'status'},
                            {'text': '📈 Stats', 'callback_data': 'stats'}
                        ]]
                    }
        
        # Send response if we have one
        if response_text:
            send_telegram_message(response_text, reply_markup=keyboard)
        
        return jsonify({'success': True})
    
    except Exception as e:
        print(f"[TELEGRAM] Error handling webhook: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/clear_auto_bet_duplicates', methods=['POST'])
def clear_auto_bet_duplicates():
    """Clear the duplicate bet tracking (allows re-betting same submarkets)"""
    global auto_bet_submarkets
    count = len(auto_bet_submarkets)
    auto_bet_submarkets.clear()
    print(f"Cleared {count} tracked auto-bet submarkets")
    return jsonify({
        'success': True,
        'cleared_count': count,
        'message': f'Cleared {count} tracked submarkets'
    })


@app.route('/api/remove_alert', methods=['POST'])
def remove_alert():
    """Manually remove an alert from the dashboard"""
    global active_alerts
    
    data = request.json
    alert_id = data.get('alert_id')
    
    if not alert_id:
        return jsonify({'error': 'alert_id required'}), 400
    
    alert_id = str(alert_id)  # Ensure string
    
    # Try exact match first
    if alert_id in active_alerts:
        del active_alerts[alert_id]
        socketio.emit('remove_alert', {'id': alert_id})
        print(f"REMOVED: Manually removed alert: {alert_id}")
        return jsonify({'success': True, 'message': f'Alert {alert_id} removed'})
    
    # Try to find by string conversion of all IDs
    for aid in list(active_alerts.keys()):
        if str(aid) == alert_id:
            del active_alerts[aid]
            socketio.emit('remove_alert', {'id': str(aid)})
            print(f"REMOVED: Manually removed alert: {aid} (matched as string)")
            return jsonify({'success': True, 'message': f'Alert {aid} removed'})
    
    # If still not found, force remove from frontend anyway
    socketio.emit('remove_alert', {'id': alert_id})
    print(f"REMOVED: Force removed alert from frontend: {alert_id} (not found in backend)")
    return jsonify({'success': True, 'message': f'Alert {alert_id} removed from frontend'})


@app.route('/api/get_filters', methods=['GET'])
def get_filters():
    """Get all saved filters and current selections"""
    global dashboard_min_ev, user_max_bet_amount, per_event_max_bet
    return jsonify({
        'saved_filters': saved_filters,
        'selected_dashboard_filters': selected_dashboard_filters,
        'selected_auto_bettor_filters': selected_auto_bettor_filters,
        'default_filter': DEFAULT_FILTER_NAME,
        'dashboard_min_ev': dashboard_min_ev,
        'max_bet_amount': user_max_bet_amount,
        'per_event_max_bet': per_event_max_bet
    })

@app.route('/api/set_filters', methods=['POST'])
def set_filters():
    """Set dashboard filter settings (min EV, etc.)"""
    global dashboard_min_ev, user_max_bet_amount, per_event_max_bet
    data = request.json
    
    if 'min_ev' in data:
        new_min_ev = float(data['min_ev'])
        if new_min_ev < 0:
            return jsonify({'error': 'Min EV must be >= 0'}), 400
        old_min_ev = dashboard_min_ev
        dashboard_min_ev = new_min_ev
        print(f"[DASHBOARD] Min EV updated: {old_min_ev}% -> {dashboard_min_ev}%")
    
    if 'max_bet_amount' in data:
        new_max_bet = float(data['max_bet_amount'])
        if new_max_bet <= 0:
            return jsonify({'error': 'Max bet amount must be > 0'}), 400
        old_max_bet = user_max_bet_amount
        user_max_bet_amount = new_max_bet
        print(f"[DASHBOARD] Max bet amount updated: ${old_max_bet:.2f} -> ${user_max_bet_amount:.2f}")
    
    if 'per_event_max_bet' in data:
        new_per_event_max = float(data['per_event_max_bet'])
        if new_per_event_max <= 0:
            return jsonify({'error': 'Per-event max bet must be > 0'}), 400
        old_per_event_max = per_event_max_bet
        per_event_max_bet = new_per_event_max
        print(f"[DASHBOARD] Per-event max bet updated: ${old_per_event_max:.2f} -> ${per_event_max_bet:.2f}")
    
    # Handle nested filter structure from frontend (for backward compatibility)
    if 'filters' in data and 'devigFilter' in data['filters']:
        if 'minEv' in data['filters']['devigFilter']:
            new_min_ev = float(data['filters']['devigFilter']['minEv'])
            if new_min_ev < 0:
                return jsonify({'error': 'Min EV must be >= 0'}), 400
            old_min_ev = dashboard_min_ev
            dashboard_min_ev = new_min_ev
            print(f"[DASHBOARD] Min EV updated (nested): {old_min_ev}% -> {dashboard_min_ev}%")
    
    return jsonify({
        'success': True,
        'dashboard_min_ev': dashboard_min_ev,
        'max_bet_amount': user_max_bet_amount,
        'per_event_max_bet': per_event_max_bet
    })


@app.route('/api/save_filter', methods=['POST'])
def save_filter():
    """Save a new filter or update an existing one"""
    global saved_filters
    data = request.json
    filter_name = data.get('name')
    filter_payload = data.get('payload')
    
    if not filter_name or not filter_payload:
        return jsonify({'error': 'Name and payload required'}), 400
    
    saved_filters[filter_name] = filter_payload
    print(f"Saved filter: {filter_name}")
    return jsonify({'success': True, 'saved_filters': saved_filters})


@app.route('/api/delete_filter', methods=['POST'])
def delete_filter():
    """Delete a saved filter"""
    global saved_filters, selected_dashboard_filters, selected_auto_bettor_filters
    data = request.json
    filter_name = data.get('name')
    
    if not filter_name:
        return jsonify({'error': 'Filter name required'}), 400
    
    if filter_name == DEFAULT_FILTER_NAME or filter_name == CBB_FILTER_NAME:
        return jsonify({'error': 'Cannot delete default filters'}), 400
    
    if filter_name in saved_filters:
        del saved_filters[filter_name]
        # Remove from selections if selected
        if filter_name in selected_dashboard_filters:
            selected_dashboard_filters.remove(filter_name)
        if filter_name in selected_auto_bettor_filters:
            selected_auto_bettor_filters.remove(filter_name)
        # Stop monitor if running
        if filter_name in odds_ev_monitors:
            odds_ev_monitors[filter_name].running = False
            del odds_ev_monitors[filter_name]
        print(f"Deleted filter: {filter_name}")
        return jsonify({'success': True, 'saved_filters': saved_filters})
    
    return jsonify({'error': 'Filter not found'}), 404


@app.route('/api/set_selected_filters', methods=['POST'])
def set_selected_filters():
    """Set which filters are selected for dashboard and/or auto-bettor"""
    global selected_dashboard_filters, selected_auto_bettor_filters
    data = request.json
    dashboard_filters = data.get('dashboard_filters', [])
    auto_bettor_filters = data.get('auto_bettor_filters', [])
    
    # Validate that all selected filters exist
    for filter_name in dashboard_filters + auto_bettor_filters:
        if filter_name not in saved_filters:
            return jsonify({'error': f'Filter not found: {filter_name}'}), 400
    
    selected_dashboard_filters = dashboard_filters
    selected_auto_bettor_filters = auto_bettor_filters
    
    print(f"Selected dashboard filters: {selected_dashboard_filters}")
    print(f"Selected auto-bettor filters: {selected_auto_bettor_filters}")
    
    # Stop monitors that are no longer selected
    all_selected = set(dashboard_filters + auto_bettor_filters)
    monitors_to_stop = []
    for filter_name, monitor in list(odds_ev_monitors.items()):
        if filter_name not in all_selected:
            print(f"Stopping monitor for deselected filter: {filter_name}")
            monitor.running = False
            monitors_to_stop.append(filter_name)
    
    # Remove stopped monitors from dict
    for filter_name in monitors_to_stop:
        if filter_name in odds_ev_monitors:
            del odds_ev_monitors[filter_name]
    
    # Restart monitors with new selections (will be handled by monitor restart logic)
    return jsonify({
        'success': True,
        'selected_dashboard_filters': selected_dashboard_filters,
        'selected_auto_bettor_filters': selected_auto_bettor_filters
    })


@app.route('/api/place_bet', methods=['POST'])
def place_bet():
    """Place a bet via API - WITH EXTENSIVE LOGGING FOR DEBUGGING"""
    import time
    start_time = time.time()
    
    if not kalshi_client:
        print(f"[BET] ERROR: Kalshi client not initialized")
        return jsonify({'error': 'Kalshi client not initialized'}), 500
    
    data = request.json
    alert_id = data.get('alert_id')
    bet_amount = float(data.get('bet_amount', 0))
    bet_max = data.get('bet_max', False)
    
    print(f"\n{'='*80}")
    print(f"[BET] ========== MANUAL BET PLACEMENT STARTED ==========")
    print(f"[BET] Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
    print(f"[BET] Alert ID: {alert_id}")
    print(f"[BET] Bet Amount: ${bet_amount:.2f}")
    print(f"[BET] Bet Max: {bet_max}")
    
    if alert_id not in active_alerts:
        print(f"[BET] ERROR: Alert not found in active_alerts")
        return jsonify({'error': 'Alert not found'}), 404
    
    alert = active_alerts[alert_id]
    ticker = alert['ticker']
    side = alert['side']
    expected_price_cents = alert.get('price_cents')
    
    # COMPREHENSIVE ALERT LOGGING
    print(f"[BET] ========== ALERT DETAILS ==========")
    print(f"[BET]   Teams: {alert.get('teams', 'N/A')}")
    print(f"[BET]   Pick: {alert.get('pick', 'N/A')}")
    print(f"[BET]   Qualifier: {alert.get('qualifier', 'N/A')}")
    print(f"[BET]   Market Type: {alert.get('market_type', 'N/A')}")
    print(f"[BET]   EV: {alert.get('ev_percent', 0):.2f}%")
    print(f"[BET]   Alert Odds: {alert.get('odds', 'N/A')}")
    print(f"[BET]   Book Price (American): {alert.get('book_price', 'N/A')}")
    print(f"[BET]   Fair Odds: {alert.get('fair_odds', 'N/A')}")
    print(f"[BET]   Expected Profit: ${alert.get('expected_profit', 0):.2f}")
    print(f"[BET]   Alert Liquidity: ${alert.get('liquidity', 0):.2f}")
    
    # COMPREHENSIVE MARKET LOGGING
    print(f"[BET] ========== MARKET DETAILS ==========")
    print(f"[BET]   Ticker: {ticker}")
    print(f"[BET]   Event Ticker: {alert.get('event_ticker', 'N/A')}")
    print(f"[BET]   Side Determined: {side}")
    print(f"[BET]   Expected Price: {expected_price_cents} cents ({expected_price_cents/100:.2f}¢)")
    
    # Try to get market subtitles if available (from active_alerts or fetch)
    try:
        # Check if we have market data cached
        market_data = alert.get('market_data')
        if not market_data:
            # Try to fetch market data for logging
            loop = get_or_create_event_loop()
            if loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    kalshi_client.get_market_by_ticker(ticker),
                    loop
                )
                market_data = future.result(timeout=2)
            else:
                market_data = loop.run_until_complete(kalshi_client.get_market_by_ticker(ticker))
        
        if market_data:
            yes_subtitle = market_data.get('yes_sub_title', 'N/A')
            no_subtitle = market_data.get('no_sub_title', 'N/A')
            market_title = market_data.get('title', 'N/A')
            print(f"[BET]   Market Title: {market_title}")
            print(f"[BET]   YES Subtitle: {yes_subtitle}")
            print(f"[BET]   NO Subtitle: {no_subtitle}")
            
            # VALIDATION LOGGING
            print(f"[BET] ========== SIDE VALIDATION ==========")
            pick_upper = (alert.get('pick', '') or '').upper()
            if alert.get('market_type', '').lower() == 'moneyline':
                yes_contains_pick = pick_upper in yes_subtitle.upper() if yes_subtitle != 'N/A' else False
                no_contains_pick = pick_upper in no_subtitle.upper() if no_subtitle != 'N/A' else False
                print(f"[BET]   Pick '{alert.get('pick', 'N/A')}' in YES subtitle: {yes_contains_pick}")
                print(f"[BET]   Pick '{alert.get('pick', 'N/A')}' in NO subtitle: {no_contains_pick}")
                if side == 'yes':
                    print(f"[BET]   ✅ Side validation: YES side contains pick: {yes_contains_pick}")
                elif side == 'no':
                    print(f"[BET]   ✅ Side validation: NO side contains pick: {no_contains_pick}")
    except Exception as e:
        print(f"[BET]   ⚠️  Could not fetch market data for logging: {e}")
    
    print(f"[BET] ==========================================")
    
    # FALLBACK MATCHING: If alert failed to match initially, try again for manual bets
    if not ticker or not side:
        event_ticker = alert.get('event_ticker')
        market_type = alert.get('market_type')
        qualifier = alert.get('qualifier')
        pick = alert.get('pick')
        teams = alert.get('teams')
        
        if event_ticker and market_type and pick:
            print(f"[BET] ⚠️  Alert not matched initially - attempting fallback matching for manual bet...")
            print(f"[BET]   Event Ticker: {event_ticker}")
            print(f"[BET]   Market Type: {market_type}")
            print(f"[BET]   Line/Qualifier: {qualifier}")
            print(f"[BET]   Pick: {pick}")
            
            try:
                loop = get_or_create_event_loop()
                if loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        kalshi_client.find_submarket(
                            event_ticker=event_ticker,
                            market_type=market_type,
                            line=qualifier if qualifier else None,
                            selection=pick,
                            teams_str=teams
                        ),
                        loop
                    )
                    match_result = future.result(timeout=5)
                else:
                    match_result = loop.run_until_complete(
                        kalshi_client.find_submarket(
                            event_ticker=event_ticker,
                            market_type=market_type,
                            line=qualifier if qualifier else None,
                            selection=pick,
                            teams_str=teams
                        )
                    )
                
                if match_result and match_result.get('ticker'):
                    ticker = match_result['ticker']
                    side = match_result.get('side')
                    print(f"[BET] ✅ Fallback matching SUCCESS: ticker={ticker}, side={side}")
                    
                    # Update alert data with matched ticker/side
                    alert['ticker'] = ticker
                    alert['side'] = side
                    active_alerts[alert_id] = alert
                    
                    # Try to get price if available
                    if match_result.get('price_cents'):
                        expected_price_cents = match_result['price_cents']
                        alert['price_cents'] = expected_price_cents
                else:
                    print(f"[BET] ❌ Fallback matching FAILED: Could not find market")
                    return jsonify({'error': 'Could not find matching market on Kalshi'}), 404
            except Exception as e:
                print(f"[BET] ❌ Fallback matching ERROR: {e}")
                import traceback
                traceback.print_exc()
                return jsonify({'error': f'Failed to match market: {str(e)}'}), 500
    
    if not ticker or not side:
        print(f"[BET] ERROR: Invalid alert data - ticker={ticker}, side={side}")
        return jsonify({'error': 'Invalid alert data - market not found'}), 400
    
    # SPEED OPTIMIZATION: Skip portfolio check - Kalshi API will reject if insufficient balance
    # This saves 100-500ms per bet, critical for fast-moving edges
    loop = get_or_create_event_loop()
    setup_time = time.time() - start_time
    print(f"[BET] Setup time: {setup_time*1000:.1f}ms")
    
    # Calculate contracts
    calc_start = time.time()
    liquidity = alert.get('liquidity', 0)
    if bet_max:
        # Bet up to user's max bet amount, capped by available liquidity
        # Use minimum of: user max, alert liquidity, orderbook liquidity (checked in place_order)
        max_bet_dollars = min(user_max_bet_amount, liquidity) if liquidity > 0 else user_max_bet_amount
        if max_bet_dollars <= 0:
            print(f"[BET] ERROR: No liquidity available (max_bet_dollars={max_bet_dollars})")
            return jsonify({'error': 'No liquidity available'}), 400
        contracts = market_matcher.calculate_max_contracts(max_bet_dollars, expected_price_cents or 50)
        print(f"[BET] BET MAX mode: max_bet_dollars=${max_bet_dollars:.2f}, contracts={contracts}")
    else:
        if bet_amount <= 0:
            print(f"[BET] ERROR: Invalid bet amount: ${bet_amount:.2f}")
            return jsonify({'error': 'Invalid bet amount'}), 400
        contracts = market_matcher.calculate_contracts_from_dollars(bet_amount, expected_price_cents or 50)
        max_bet_dollars = None
        print(f"[BET] Fixed amount mode: bet_amount=${bet_amount:.2f}, contracts={contracts}")
    
    calc_time = time.time() - calc_start
    print(f"[BET] Contract calculation time: {calc_time*1000:.1f}ms")
    print(f"[BET] Requested contracts: {contracts}")
    
    # SPEED OPTIMIZATION: Place order with minimal validation (run async in event loop)
    # Use cached orderbook if available (from pre-fetch), otherwise fetch fresh
    # This saves 100-200ms if we pre-fetched
    order_start = time.time()
    print(f"[BET] Calling place_order()...")
    
    # CRITICAL: Handle event loop properly - Flask runs in a thread, so we need to use thread-safe approach
    if loop.is_running():
        # Loop is already running - use thread-safe approach
        future = asyncio.run_coroutine_threadsafe(
            kalshi_client.place_order(
                ticker=ticker,
                side=side,
                count=contracts,
                validate_odds=True,
                expected_price_cents=expected_price_cents,
                max_liquidity_dollars=max_bet_dollars if bet_max else None,
                skip_duplicate_check=True  # Manual bet: allow multiple clicks; duplicate detection only for auto-bettor
            ),
            loop
        )
        result = future.result(timeout=10)  # 10 second timeout
    else:
        # Loop not running - use run_until_complete
        result = loop.run_until_complete(kalshi_client.place_order(
                ticker=ticker,
                side=side,
                count=contracts,
                validate_odds=True,
                expected_price_cents=expected_price_cents,
                max_liquidity_dollars=max_bet_dollars if bet_max else None,
                skip_duplicate_check=True  # Manual bet: allow multiple clicks on same alert; duplicate detection is only for auto-bettor
            ))
    
    order_time = time.time() - order_start
    print(f"[BET] place_order() completed in {order_time*1000:.1f}ms")
    
    # Log result details
    total_time = time.time() - start_time
    print(f"[BET] Order Result:")
    print(f"[BET]   Success: {result.get('success', False)}")
    if result.get('success'):
        final_count = result.get('fill_count', result.get('count', 0))
        # Use executed price (actual fill price) if available, otherwise fall back to limit price
        executed_price_cents = result.get('executed_price_cents') or result.get('price_cents', 0)
        limit_price_cents = result.get('price_cents', 0)  # Limit price (requested)
        total_cost_cents = result.get('total_cost_cents')
        requested_count = result.get('requested_count', contracts)
        
        # Calculate actual cost from executed price or use total_cost_cents if available
        if total_cost_cents:
            bet_amount_actual = total_cost_cents / 100.0
        else:
            bet_amount_actual = final_count * (executed_price_cents / 100.0)
        
        print(f"[BET]   Final Contracts: {final_count} (requested: {requested_count})")
        print(f"[BET]   Limit Price: {limit_price_cents} cents ({limit_price_cents/100:.2f}¢) (requested)")
        print(f"[BET]   Executed Price: {executed_price_cents} cents ({executed_price_cents/100:.2f}¢) (actual)")
        print(f"[BET]   Bet Amount: ${bet_amount_actual:.2f}")
        if executed_price_cents != limit_price_cents:
            slippage_cents = executed_price_cents - limit_price_cents
            slippage_pct = (slippage_cents / limit_price_cents * 100) if limit_price_cents > 0 else 0
            print(f"[BET]   Slippage: {slippage_cents:+.1f}¢ ({slippage_pct:+.2f}%)")
        if final_count < requested_count:
            print(f"[BET]   WARNING: Partial fill! {final_count}/{requested_count} contracts")
        if 'order' in result:
            order_data = result.get('order', {})
            print(f"[BET]   Order ID: {order_data.get('order_id', 'N/A')}")
            print(f"[BET]   Order Status: {order_data.get('status', 'N/A')}")
    else:
        error = result.get('error', 'Unknown error')
        print(f"[BET]   ERROR: {error}")
        if 'expected' in result and 'current' in result:
            print(f"[BET]   Expected Price: {result.get('expected')} cents")
            print(f"[BET]   Current Price: {result.get('current')} cents")
            print(f"[BET]   Price Delta: {abs(result.get('expected', 0) - result.get('current', 0))} cents")
    
    print(f"[BET] Total execution time: {total_time*1000:.1f}ms")
    print(f"[BET] Breakdown:")
    print(f"[BET]   Setup: {setup_time*1000:.1f}ms")
    print(f"[BET]   Contract calc: {calc_time*1000:.1f}ms")
    print(f"[BET]   Order placement: {order_time*1000:.1f}ms")
    print(f"[BET]   Overhead: {(total_time - setup_time - calc_time - order_time)*1000:.1f}ms")
    
    # FINAL COMPREHENSIVE SUMMARY
    if result.get('success'):
        print(f"[BET] ========== FINAL BET SUMMARY ==========")
        print(f"[BET]   Alert: {alert.get('teams', 'N/A')} - {alert.get('pick', 'N/A')} {alert.get('qualifier', '')}")
        print(f"[BET]   Market: {ticker} | Side: {side}")
        if result.get('fill_count'):
            final_count = result.get('fill_count', 0)
            executed_price = result.get('executed_price_cents') or result.get('price_cents', 0)
            total_cost_cents = result.get('total_cost_cents')
            total_cost = (total_cost_cents / 100.0) if total_cost_cents is not None else (final_count * executed_price / 100.0)
            print(f"[BET]   Contracts: {final_count} @ {executed_price}¢")
            print(f"[BET]   Total Cost: ${total_cost:.2f}")
            print(f"[BET]   Order ID: {result.get('order_id', 'N/A')}")
            print(f"[BET]   Status: {result.get('status', 'N/A')}")
        print(f"[BET] ==========================================")
    
    print(f"{'='*80}\n")
    
    # Emit enhanced bet confirmation to client
    if result.get('success'):
        fill_count = result.get('fill_count', result.get('count', 0))
        initial_count = result.get('initial_count', result.get('count', 0))
        order_id = result.get('order_id', 'N/A')
        order_status = result.get('status', 'filled' if fill_count == initial_count else 'partial')
        
        # Use executed price (actual fill price) if available, otherwise fall back to limit price
        executed_price_cents = result.get('executed_price_cents') or result.get('price_cents', expected_price_cents)
        total_cost_cents = result.get('total_cost_cents')
        
        # Calculate cost from executed price or use total_cost_cents if available
        if total_cost_cents:
            cost = total_cost_cents / 100.0  # Use actual total cost from Kalshi (includes fees)
        else:
            cost = fill_count * (executed_price_cents / 100.0)  # Calculate from executed price
        
        payout = fill_count * 1.0  # Payout if right: 1 dollar per contract
        win_amount = payout - cost  # Win amount (payout less cost)
        
        # CRITICAL: Calculate American odds from EFFECTIVE price (cost per contract including fees)
        # This matches what the user actually paid and what the win amount reflects
        # Use exact decimal price (don't round) for accurate odds conversion
        if fill_count > 0 and cost > 0:
            effective_price_decimal = cost / fill_count  # Cost per contract in dollars (includes fees)
            # Convert to American odds using exact decimal (not rounded cents)
            if effective_price_decimal >= 0.5:
                # Favorite (negative odds)
                odds = -100 * effective_price_decimal / (1 - effective_price_decimal)
                executed_american_odds = f"{int(odds)}"
            else:
                # Underdog (positive odds)
                odds = 100 * (1 - effective_price_decimal) / effective_price_decimal
                executed_american_odds = f"+{int(odds)}"
            effective_price_cents = int(effective_price_decimal * 100)  # For display only
            print(f"[BET] Effective price (with fees): {effective_price_cents}¢ ({effective_price_decimal:.4f}), American odds: {executed_american_odds}")
        else:
            # Fallback to executed price if we can't calculate effective price
            executed_american_odds = price_to_american_odds(executed_price_cents)
        
        # Get market and submarket info from alert for bet tracking
        market_name = alert.get('market_name', '')
        submarket_name = alert.get('submarket_name', '')
        teams = alert.get('teams', '')
        pick = alert.get('pick', '')
        
        # Get qualifier from alert for popup display
        qualifier = alert.get('qualifier', '')
        
        # Emit bet_success with order details for frontend popup display
        socketio.emit('bet_success', {
            'alert_id': alert_id,
            'order_id': order_id,
            'fill_count': fill_count,
            'initial_count': initial_count,
            'status': order_status,
            'message': f'Bet placed successfully: {fill_count}/{initial_count} contracts filled',
            'ticker': ticker,
            'side': side,
            'price_cents': executed_price_cents,
            'executed_price_cents': executed_price_cents,  # Explicit field for popup
            'cost': cost,
            'american_odds': executed_american_odds,
            'payout': payout,
            'win_amount': win_amount,
            'market_name': market_name,
            'submarket_name': submarket_name,
            'teams': teams,
            'pick': pick,
            'qualifier': qualifier  # Add qualifier for popup display
        })
        
        # Use cost for confirmation message (already calculated above)
        socketio.emit('bet_confirmation', {
            'alert_id': alert_id,
            'status': 'success',
            'message': f'Bet placed: {fill_count}/{initial_count} contracts (${cost:.2f})',
            'result': result
        })
    else:
        socketio.emit('bet_error', {
            'alert_id': alert_id,
            'error': result.get('error', 'Unknown error'),
            'result': result
        })
        
        socketio.emit('bet_confirmation', {
            'alert_id': alert_id,
            'status': 'failed',
            'message': result.get('error', 'Unknown error'),
            'result': result
        })
    
    # Also emit bet_result for backward compatibility
    socketio.emit('bet_result', {
        'alert_id': alert_id,
        'result': result
    })
    
    return jsonify(result)


@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    print(f"Client connected: {request.sid}")
    # Send current alerts to new client
    emit('alerts_update', {'alerts': list(active_alerts.values())})


@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    print(f"Client disconnected: {request.sid}")


@socketio.on('request_orderbook')
def handle_orderbook_request(data):
    """Handle orderbook update request"""
    ticker = data.get('ticker')
    if not ticker or not kalshi_client:
        return
    
    loop = get_or_create_event_loop()
    orderbook = loop.run_until_complete(kalshi_client.fetch_orderbook(ticker))
    
    if orderbook:
        emit('orderbook_update', {
            'ticker': ticker,
            'orderbook': orderbook
        })


async def load_existing_positions_to_tracking():
    """Load existing positions from Kalshi and FULLY populate all tracking structures
    This is CRITICAL - we must know all positions before auto-betting starts to prevent:
    - Duplicate bets
    - Reverse middles
    - Over-betting (3+ of same pick direction)
    """
    global auto_bet_submarkets, auto_bet_submarket_data, auto_bet_games, kalshi_client, positions_loaded
    
    if not kalshi_client:
        print("[AUTO-BET] ERROR: Kalshi client not initialized, cannot load positions")
        return
    
    print("[MONITOR] load_existing_positions_to_tracking: entered")
    try:
        print("[MONITOR] get_positions() calling (10s timeout)...")
        positions = await asyncio.wait_for(kalshi_client.get_positions(), timeout=10.0)
        print(f"[MONITOR] get_positions() returned, count={len(positions) if positions else 0}")
    except asyncio.TimeoutError:
        print("[AUTO-BET] get_positions() timed out (10s); treating as 0 positions so monitors can start")
        positions_loaded = True
        if not hasattr(load_existing_positions_to_tracking, '_initial_load_done'):
            load_existing_positions_to_tracking._initial_load_done = True
        return
    
    try:
        # CRITICAL: Set positions_loaded = True even if there are 0 positions
        # This allows auto-betting to proceed when user has no open positions
        # The check is mainly for reverse middle detection, which can still work
        # using bets placed during this session (auto_bet_submarkets)
        if not positions:
            positions_loaded = True
            if not hasattr(load_existing_positions_to_tracking, '_initial_load_done'):
                print(f"[AUTO-BET] ✅ POSITIONS LOADED: positions_loaded={positions_loaded}, loaded_count=0 (no open positions)")
                load_existing_positions_to_tracking._initial_load_done = True
            return
        
        # Silent loading - only log if positions change
        
        loaded_count = 0
        for pos in positions:
            ticker = pos.get('ticker', '')
            position = pos.get('position', 0)  # Positive = YES, Negative = NO
            
            if not ticker:
                continue
            
            # Determine side from position
            # Positive position = YES side, Negative position = NO side
            if position > 0:
                side = 'yes'
            elif position < 0:
                side = 'no'
            else:
                continue  # Zero position, skip
            
            submarket_key = (ticker.upper(), side.lower())
            
            # Skip if already loaded
            if submarket_key in auto_bet_submarkets:
                continue
            
            # Fetch market data to get full information
            try:
                market_data = await kalshi_client.get_market_by_ticker(ticker)
                if not market_data:
                    # Fallback: add to set without full data (better than nothing)
                    auto_bet_submarkets.add(submarket_key)
                    loaded_count += 1
                    # Silent - no logging for positions without market data
                    continue
                
                # Extract market info
                market_info = market_data.get('market', {}) if isinstance(market_data, dict) else market_data
                
                # Get market type from ticker or market data
                market_type = ''
                if 'TOTAL' in ticker:
                    market_type = 'Total Points'
                elif 'SPREAD' in ticker:
                    market_type = 'Point Spread'
                elif 'GAME' in ticker:
                    market_type = 'Moneyline'
                
                # Get pick and qualifier from market subtitles
                pick = ''
                qualifier = ''
                line_value = None
                
                if side == 'yes':
                    subtitle = market_info.get('yes_sub_title', '') or market_info.get('subtitle', '')
                else:
                    subtitle = market_info.get('no_sub_title', '') or market_info.get('subtitle', '')
                
                # Parse subtitle to extract pick and line
                # Examples: "Over 215.5 points scored", "Team A wins by over 4.5 Points", "Team A"
                if 'OVER' in subtitle.upper() or 'UNDER' in subtitle.upper():
                    # Totals market
                    if 'OVER' in subtitle.upper():
                        pick = 'Over'
                    elif 'UNDER' in subtitle.upper():
                        pick = 'Under'
                    # Extract line number
                    import re
                    line_match = re.search(r'(\d+\.?\d*)', subtitle)
                    if line_match:
                        qualifier = line_match.group(1)
                        try:
                            line_value = float(qualifier)
                        except:
                            pass
                else:
                    # Spread or moneyline - pick is the team name
                    pick = subtitle
                    team_name_in_subtitle = None  # Will be used for spread mapping
                    
                    # For spreads, extract line and determine sign AND team based on side
                    # CRITICAL: "Team A wins by over X Points"
                    # - YES side = Team A -X (Team A must win by X+)
                    # - NO side = Opposite Team +X (Opposite team gets X points, or Team A wins by less than X)
                    # Example: "Phoenix wins by over 4.5 Points" NO = New York +4.5
                    # Example: "Phoenix wins by over 7.5 Points" YES = Phoenix -7.5
                    if market_type == 'Point Spread':
                        import re
                        line_match = re.search(r'(\d+\.?\d*)', subtitle)
                        if line_match:
                            qualifier = line_match.group(1)
                            try:
                                line_num = float(qualifier)
                                # Extract team name from subtitle (e.g., "Phoenix wins by over 4.5")
                                # Pattern: Team name is usually before "wins by" or similar
                                if 'WINS BY' in subtitle.upper():
                                    # Extract team name before "wins by"
                                    team_match = re.match(r'^(.+?)\s+wins\s+by', subtitle, re.IGNORECASE)
                                    if team_match:
                                        team_name_in_subtitle = team_match.group(1).strip()
                                
                                # Determine sign and team: YES on "wins by over X" = same team -X, NO = opposite team +X
                                if 'WINS BY OVER' in subtitle.upper() or 'WINS BY' in subtitle.upper():
                                    if side == 'yes':
                                        # YES: Same team gets negative line (must win by X+)
                                        line_value = -line_num
                                        pick = team_name_in_subtitle if team_name_in_subtitle else pick
                                    else:
                                        # NO: Opposite team gets positive line (gets X points)
                                        line_value = line_num
                                        # We'll determine opposite team below after fetching event
                                        pick = None  # Will be set to opposite team
                                else:
                                    # Fallback: assume positive if we can't determine
                                    line_value = line_num
                            except:
                                pass
                
                # Get teams from event (need to fetch event)
                event_ticker = '-'.join(ticker.split('-')[:-1]) if '-' in ticker else ticker
                teams = event_ticker  # Fallback
                try:
                    event_data = await kalshi_client.get_event_by_ticker(event_ticker)
                    if event_data:
                        event_info = event_data.get('event', {}) if isinstance(event_data, dict) else event_data
                        teams = event_info.get('title', event_ticker) or event_ticker
                        
                        # For spreads with NO side, find the opposite team
                        if market_type == 'Point Spread' and side == 'no' and pick is None and team_name_in_subtitle:
                            # Parse teams from event title (e.g., "New York at Phoenix" or "Phoenix @ New York")
                            # Find the team that's NOT in the subtitle
                            event_title = teams
                            # Try different separators
                            if ' @ ' in event_title or ' at ' in event_title:
                                separator = ' @ ' if ' @ ' in event_title else ' at '
                                team1, team2 = event_title.split(separator, 1)
                                team1 = team1.strip()
                                team2 = team2.strip()
                                
                                # Find the opposite team
                                if team_name_in_subtitle.upper() in team1.upper():
                                    pick = team2
                                elif team_name_in_subtitle.upper() in team2.upper():
                                    pick = team1
                except:
                    pass
                
                # CRITICAL: Calculate Effective Direction based on side
                # For totals: NO on "Over" = effectively "Under", NO on "Under" = effectively "Over"
                # This ensures reverse middle detection works correctly
                pick_upper = pick.upper() if pick else ''
                is_over = 'OVER' in pick_upper
                is_under = 'UNDER' in pick_upper
                
                # Calculate effective pick and pick_direction
                effective_pick = pick
                effective_pick_direction = None
                
                if market_type == 'Total Points':
                    # For totals, side determines effective direction
                    if side == 'no':
                        # NO on Over = Under, NO on Under = Over
                        if is_over:
                            effective_pick = 'Under'
                            effective_pick_direction = 'Under'
                        elif is_under:
                            effective_pick = 'Over'
                            effective_pick_direction = 'Over'
                        else:
                            effective_pick_direction = 'Unknown'
                    else:
                        # YES side: direction matches market
                        if is_over:
                            effective_pick_direction = 'Over'
                        elif is_under:
                            effective_pick_direction = 'Under'
                        else:
                            effective_pick_direction = 'Unknown'
                else:
                    # For spreads/moneylines, pick_direction is the team name
                    effective_pick_direction = pick if pick else 'Unknown'
                
                # Add to ALL tracking structures using EFFECTIVE values
                auto_bet_submarkets.add(submarket_key)
                auto_bet_submarket_data[submarket_key] = {
                    'line': line_value,
                    'pick': effective_pick,  # Store effective pick
                    'qualifier': qualifier,
                    'market_type': market_type,
                    'teams': teams,
                    'pick_direction': effective_pick_direction,  # Store effective direction
                    'raw_pick': pick,  # Keep raw pick for reference
                    'side': side  # Store side for reference
                }
                auto_bet_games[teams][market_type][effective_pick_direction].append(submarket_key)
                
                loaded_count += 1
                # Show stored values with effective direction clearly marked
                line_display = f"{line_value:+.1f}" if line_value is not None else "N/A"
                effective_note = f" (via {side.upper()} contracts)" if side == 'no' and market_type == 'Total Points' else ""
                # Silent - positions loaded quietly
            except Exception as e:
                # Fallback: add to set without full data
                auto_bet_submarkets.add(submarket_key)
                loaded_count += 1
                # Silent - errors loading position details
        
        # CRITICAL: Mark positions as loaded - auto-betting can now proceed
        positions_loaded = True
        # Only log on initial load or if positions changed
        if not hasattr(load_existing_positions_to_tracking, '_initial_load_done'):
            print(f"[AUTO-BET] ✅ POSITIONS LOADED: positions_loaded={positions_loaded}, loaded_count={loaded_count}")
            if loaded_count > 0:
                print(f"[AUTO-BET] Loaded {loaded_count} positions into tracking")
            load_existing_positions_to_tracking._initial_load_done = True
        # For periodic checks, only log if positions changed (handled by position_check_loop)
        # For periodic checks, only log if positions changed (handled by position_check_loop)
    
    except Exception as e:
        print(f"[AUTO-BET] CRITICAL ERROR: Could not load existing positions: {e}")
        import traceback
        traceback.print_exc()
        # Don't start auto-betting if we can't load positions - too dangerous
        positions_loaded = False
        raise


def initialize_dashboard():
    """Initialize dashboard components"""
    global kalshi_client, market_matcher, odds_ev_monitor, telegram_bot_token, telegram_chat_id, odds_ev_monitors, api_kalshi_client, api_loop, api_loop_thread
    
    # Initialize Kalshi client (used by monitor thread for alerts, positions, orders)
    kalshi_client = KalshiClient()
    
    # Start dedicated API loop thread for dashboard/portfolio - NEVER uses monitor_loop
    # so monitoring and auto-betting are never interrupted by page loads or API calls
    if api_loop_thread is None or not api_loop_thread.is_alive():
        api_loop_thread = threading.Thread(target=run_api_loop, daemon=True)
        api_loop_thread.start()
        # Brief wait so api_loop and api_kalshi_client are set before first request
        for _ in range(50):
            if api_loop is not None and api_kalshi_client is not None:
                break
            time.sleep(0.1)
    
    # Initialize market matcher (team mappings are now static in market_matcher.py)
    # Run abbreviation_finder.py periodically to update mappings
    market_matcher = MarketMatcher(kalshi_client)
    
    # Note: Existing positions will be loaded in the monitor loop AFTER client is initialized
    # This ensures the client session is ready before fetching positions
    
    # Initialize Telegram bot (if configured)
    telegram_bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID')
    
    if telegram_bot_token:
        print(f"[TELEGRAM] Telegram bot configured (token: {telegram_bot_token[:10]}...)")
        if telegram_chat_id:
            print(f"[TELEGRAM] Telegram chat ID: {telegram_chat_id}")
            # Don't send state change message on startup - only send when user actually changes state
            # This prevents spam when Flask restarts in debug mode
            # if auto_bet_enabled:
            #     send_auto_bet_state_change(True)  # Disabled on startup
        else:
            print("[TELEGRAM] Warning: Telegram chat ID not set. Send any message to bot to set it automatically.")
    else:
        print("[TELEGRAM] Info: Telegram bot not configured (set TELEGRAM_BOT_TOKEN in .env)")
    
    # Initialize Google Sheets (if configured)
    try:
        init_google_sheets()
    except Exception as e:
        print(f"[GOOGLE SHEETS] Warning: Could not initialize Google Sheets: {e}")
        print("[GOOGLE SHEETS] Will use CSV fallback")
    
    # Odds-API.io (ODDS_API_KEY in .env); polling interval from ODDS_POLL_INTERVAL_SECONDS
    k = os.getenv("ODDS_API_KEY", "").strip()
    if not k:
        print("[MONITOR] Warning: ODDS_API_KEY missing — no Odds-API alerts until set in .env")
    else:
        print(f"[MONITOR] Odds-API.io (poll {monitor_poll_seconds()}s); key prefix {k[:6]}...")
    poll_sec = monitor_poll_seconds()
    _bm = odds_api_master_bookmakers()
    n_books = len(_bm)
    print(
        f" Production Mode — Odds-API.io {n_books} book(s): {', '.join(_bm)} — {poll_sec:g}s live polling — Kalshi 3-sharp POWER devig"
    )

    _pre_ui = os.getenv("ODDS_UI_INCLUDE_PREGAME_VALUE_BETS", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    EvMonitorImpl.include_pregame_value_bets = _pre_ui
    if _pre_ui:
        print(
            "[DASHBOARD] ODDS_UI_INCLUDE_PREGAME_VALUE_BETS enabled — "
            "pregame Kalshi value-bets pass the live-only gate (override in Settings)"
        )

    # Initialize monitors for all selected dashboard filters
    for filter_name in selected_dashboard_filters:
        if filter_name not in odds_ev_monitors:
            filter_payload = saved_filters.get(filter_name)
            if filter_payload:
                monitor = EvMonitorImpl(auth_token=None)
                monitor.set_filter(filter_payload)
                monitor.poll_interval = monitor_poll_seconds()
                odds_ev_monitors[filter_name] = monitor
                print(f"Initialized monitor for filter: {filter_name}")
    
    # Set legacy monitor to first selected filter (for backward compatibility)
    if selected_dashboard_filters and selected_dashboard_filters[0] in odds_ev_monitors:
        odds_ev_monitor = odds_ev_monitors[selected_dashboard_filters[0]]
    elif odds_ev_monitors:
        # Fallback to first monitor if no dashboard filters selected
        odds_ev_monitor = list(odds_ev_monitors.values())[0]
    else:
        # Fallback to default monitor
        odds_ev_monitor = EvMonitorImpl(auth_token=None)
        odds_ev_monitor.set_filter(DEFAULT_FILTER_PAYLOAD)
        odds_ev_monitor.poll_interval = monitor_poll_seconds()
    
    # Connect WebSocket for real-time updates
    async def connect_ws():
        # CRITICAL: Set callbacks BEFORE connecting WebSocket
        # This ensures the connection handler can see the callbacks
        
        # Set callback for WebSocket updates
        async def ws_update_callback(ticker, orderbook):
            """Update alerts with real-time orderbook data"""
            # Find all alerts for this ticker
            updated_count = 0
            for alert_id, alert_data in list(active_alerts.items()):
                if alert_data.get('ticker', '').upper() == ticker.upper():
                    # Get current best price and liquidity from orderbook
                    side = alert_data.get('side', 'yes')
                    bids = orderbook.get('bids', [])
                    asks = orderbook.get('asks', [])
                    
                    # Get best price for the side we're betting
                    if side == 'yes':
                        best_price_cents = asks[0].get('price', 0) if asks else 0
                        # Calculate liquidity at best price
                        liquidity_dollars = 0
                        for ask in asks:
                            if ask.get('price') == best_price_cents:
                                liquidity_dollars += (ask.get('size', 0) * ask.get('price', 0)) / 100.0
                    else:  # no
                        best_price_cents = bids[0].get('price', 0) if bids else 0
                        # Calculate liquidity at best price
                        liquidity_dollars = 0
                        for bid in bids:
                            if bid.get('price') == best_price_cents:
                                liquidity_dollars += (bid.get('size', 0) * bid.get('price', 0)) / 100.0
                    
                    # Recalculate EV if we have fair odds from the alert
                    original_ev = alert_data.get('ev_percent', 0)
                    original_fair_odds = alert_data.get('fair_odds', '')
                    
                    # Update alert data
                    alert_data['price_cents'] = best_price_cents
                    alert_data['liquidity'] = liquidity_dollars
                    # Convert to American odds
                    american_odds = price_to_american_odds(best_price_cents)
                    alert_data['american_odds'] = american_odds
                    alert_data['book_price'] = american_odds
                    
                    # Recalculate expected profit if EV is available
                    if original_ev and liquidity_dollars > 0:
                        alert_data['expected_profit'] = (original_ev / 100.0) * liquidity_dollars
                    
                    # Emit update to frontend (ensure ID is string)
                    socketio.emit('alert_update', {
                        'id': str(alert_id),
                        'price_cents': best_price_cents,
                        'liquidity': liquidity_dollars,
                        'book_price': american_odds,
                        'american_odds': american_odds,
                        'expected_profit': alert_data.get('expected_profit', 0)
                    })
                    updated_count += 1
            
            if updated_count > 0:
                # Also emit raw orderbook for debugging
                socketio.emit('orderbook_update', {'ticker': ticker, 'orderbook': orderbook})
        
        kalshi_client.ws_callback = ws_update_callback
        print(f"[WS] ✅ Orderbook callback registered")
        
        # Set callback for position updates
        async def calculate_effective_direction(market_type: str, side: str, pick: str) -> tuple[str, str]:
            """
            Calculate effective pick and pick_direction based on side.
            For totals: NO on "Over" = effectively "Under", NO on "Under" = effectively "Over"
            For spreads: NO on "Team A wins by X" = effectively "Opposite Team +X"
            
            Returns: (effective_pick, effective_pick_direction)
            """
            pick_upper = (pick or '').upper()
            is_over = 'OVER' in pick_upper
            is_under = 'UNDER' in pick_upper
            
            effective_pick = pick
            effective_pick_direction = None
            
            if market_type == 'Total Points':
                # For totals, side determines effective direction
                if side.lower() == 'no':
                    # NO on Over = Under, NO on Under = Over
                    if is_over:
                        effective_pick = 'Under'
                        effective_pick_direction = 'Under'
                    elif is_under:
                        effective_pick = 'Over'
                        effective_pick_direction = 'Over'
                    else:
                        effective_pick_direction = 'Unknown'
                else:
                    # YES side: direction matches pick
                    if is_over:
                        effective_pick_direction = 'Over'
                    elif is_under:
                        effective_pick_direction = 'Under'
                    else:
                        effective_pick_direction = 'Unknown'
            else:
                # For spreads/moneylines, pick_direction is the team name
                effective_pick_direction = pick if pick else 'Unknown'
            
            return effective_pick, effective_pick_direction
        
        async def ws_positions_callback(positions_data):
            """Handle real-time position updates from WebSocket with effective direction tracking
            This processes YOUR ACTUAL POSITIONS (non-zero holdings) from Kalshi WebSocket
            """
            global auto_bet_submarkets, auto_bet_submarket_data, auto_bet_games
            
            # IMMEDIATE LOG: This confirms the callback is being called
            print(f"[WS-POSITION] 🔔 Callback triggered - processing position update NOW")
            
            # positions_data can be a dict with 'market_positions' array or a list
            market_positions = []
            if isinstance(positions_data, dict):
                market_positions = positions_data.get('market_positions', [])
            elif isinstance(positions_data, list):
                market_positions = positions_data
            
            # Filter to only actual positions (non-zero holdings)
            # Kalshi sends ALL positions including zeros, but we only care about actual holdings
            actual_positions = [pos for pos in market_positions if pos.get('position', 0) != 0]
            
            if not actual_positions:
                # No actual positions, but still emit to frontend for UI updates
                socketio.emit('positions_update', positions_data)
                socketio.emit('portfolio_refresh')
                return
            
            print(f"[WS-POSITION] Real-time update received: Processing {len(actual_positions)} of YOUR actual position(s)")
            
            # Process each position update
            # First, collect all current ACTUAL positions (non-zero) to detect removals
            current_position_tickers = set()
            new_positions_detected = []  # Track new positions for immediate logging
            
            for pos in actual_positions:
                ticker = pos.get('ticker', '')
                position = pos.get('position', 0)  # Positive = YES, Negative = NO
                
                if not ticker or position == 0:
                    continue
                
                # Determine side from position
                side = 'yes' if position > 0 else 'no'
                submarket_key = (ticker.upper(), side.lower())
                
                # Check if this is a NEW position (not already tracked)
                is_new = submarket_key not in auto_bet_submarkets
                if is_new:
                    new_positions_detected.append((ticker, side, position))
                
                # Track actual positions only
                current_position_tickers.add(submarket_key)
            
            # IMMEDIATE LOG: Print new positions detected right away (before async processing)
            # This ensures you see the log within <100ms of placing a manual trade
            for ticker, side, position in new_positions_detected:
                print(f"[WS-POSITION] 🆕 New position detected: {ticker} | Side: {side.upper()} | Total: {abs(position)} contracts")
            
            # Remove positions that are no longer in the update (closed positions)
            positions_to_remove = []
            for submarket_key in list(auto_bet_submarkets):
                ticker, side = submarket_key
                if (ticker, side) not in current_position_tickers:
                    # Position was closed/removed
                    positions_to_remove.append(submarket_key)
            
            for submarket_key in positions_to_remove:
                auto_bet_submarkets.discard(submarket_key)
                position_data = auto_bet_submarket_data.pop(submarket_key, {})
                teams = position_data.get('teams', '')
                market_type = position_data.get('market_type', '')
                pick_direction = position_data.get('pick_direction', '')
                
                # Remove from games tracking
                if teams and market_type and pick_direction and teams in auto_bet_games:
                    if market_type in auto_bet_games[teams]:
                        if pick_direction in auto_bet_games[teams][market_type]:
                            auto_bet_games[teams][market_type][pick_direction] = [
                                k for k in auto_bet_games[teams][market_type][pick_direction]
                                if k != submarket_key
                            ]
                            # Clean up empty lists
                            if not auto_bet_games[teams][market_type][pick_direction]:
                                del auto_bet_games[teams][market_type][pick_direction]
                
                ticker, side = submarket_key
                print(f"[WS-POSITION] Removed: {ticker} {side} | Position closed")
            
            # Now process active positions (only actual holdings, not zero positions)
            for pos in actual_positions:
                ticker = pos.get('ticker', '')
                position = pos.get('position', 0)  # Positive = YES, Negative = NO
                
                if not ticker or position == 0:
                    continue  # Skip zero positions (shouldn't happen since we filtered, but safety check)
                
                # Determine side from position
                if position > 0:
                    side = 'yes'
                elif position < 0:
                    side = 'no'
                else:
                    continue  # Zero position, skip
                
                submarket_key = (ticker.upper(), side.lower())
                
                # Fetch market data to get full information (async)
                try:
                    market_data = await kalshi_client.get_market_by_ticker(ticker)
                    if not market_data:
                        # Still add to tracking even without full data
                        if submarket_key not in auto_bet_submarkets:
                            auto_bet_submarkets.add(submarket_key)
                            print(f"[WS-POSITION] Update received: {ticker} {side} | Effective: Unknown (no market data) | Total: {position} contracts")
                        continue
                    
                    # Extract market info
                    market_info = market_data.get('market', {}) if isinstance(market_data, dict) else market_data
                    
                    # Get market type
                    market_type = ''
                    if 'TOTAL' in ticker:
                        market_type = 'Total Points'
                    elif 'SPREAD' in ticker:
                        market_type = 'Point Spread'
                    elif 'GAME' in ticker:
                        market_type = 'Moneyline'
                    
                    # Get pick from subtitle
                    pick = ''
                    qualifier = ''
                    line_value = None
                    
                    if side == 'yes':
                        subtitle = market_info.get('yes_sub_title', '') or market_info.get('subtitle', '')
                    else:
                        subtitle = market_info.get('no_sub_title', '') or market_info.get('subtitle', '')
                    
                    # Parse subtitle to extract pick and line (similar to load_existing_positions_to_tracking)
                    if 'OVER' in subtitle.upper() or 'UNDER' in subtitle.upper():
                        # Totals market
                        if 'OVER' in subtitle.upper():
                            pick = 'Over'
                        elif 'UNDER' in subtitle.upper():
                            pick = 'Under'
                        # Extract line number
                        import re
                        line_match = re.search(r'(\d+\.?\d*)', subtitle)
                        if line_match:
                            qualifier = line_match.group(1)
                            try:
                                line_value = float(qualifier)
                            except:
                                pass
                    else:
                        # Spread or moneyline - pick is the team name
                        pick = subtitle
                        # For spreads, extract line
                        if market_type == 'Point Spread':
                            import re
                            line_match = re.search(r'(\d+\.?\d*)', subtitle)
                            if line_match:
                                qualifier = line_match.group(1)
                                try:
                                    line_num = float(qualifier)
                                    if 'WINS BY OVER' in subtitle.upper() or 'WINS BY' in subtitle.upper():
                                        if side == 'yes':
                                            line_value = -line_num
                                        else:
                                            line_value = line_num
                                    else:
                                        line_value = line_num
                                except:
                                    pass
                    
                    # Get teams from event
                    event_ticker = '-'.join(ticker.split('-')[:-1]) if '-' in ticker else ticker
                    teams = event_ticker  # Fallback
                    try:
                        event_data = await kalshi_client.get_event_by_ticker(event_ticker)
                        if event_data:
                            event_info = event_data.get('event', {}) if isinstance(event_data, dict) else event_data
                            teams = event_info.get('title', event_ticker) or event_ticker
                    except:
                        pass
                    
                    # Calculate effective direction
                    effective_pick, effective_pick_direction = await calculate_effective_direction(market_type, side, pick)
                    
                    # Update tracking structures
                    was_new = submarket_key not in auto_bet_submarkets
                    auto_bet_submarkets.add(submarket_key)
                    auto_bet_submarket_data[submarket_key] = {
                        'line': line_value,
                        'pick': effective_pick,
                        'qualifier': qualifier,
                        'market_type': market_type,
                        'teams': teams,
                        'pick_direction': effective_pick_direction,
                        'raw_pick': pick,
                        'side': side
                    }
                    
                    # Update games tracking
                    if teams and market_type and effective_pick_direction:
                        if teams not in auto_bet_games:
                            auto_bet_games[teams] = {}
                        if market_type not in auto_bet_games[teams]:
                            auto_bet_games[teams][market_type] = {}
                        if effective_pick_direction not in auto_bet_games[teams][market_type]:
                            auto_bet_games[teams][market_type][effective_pick_direction] = []
                        if submarket_key not in auto_bet_games[teams][market_type][effective_pick_direction]:
                            auto_bet_games[teams][market_type][effective_pick_direction].append(submarket_key)
                    
                    # Log the detailed update (with effective direction after processing)
                    line_display = f"{line_value:+.1f}" if line_value is not None else "N/A"
                    action = "Added" if was_new else "Updated"
                    
                    # For new positions, print the requested format with effective direction
                    if was_new:
                        print(f"[WS-POSITION] ✅ New position detected: {ticker} | Effective: {effective_pick_direction} | Side: {side.upper()} | Total: {abs(position)} contracts")
                    else:
                        print(f"[WS-POSITION] {action}: {ticker} {side} | Effective: {effective_pick_direction} | Side: {side.upper()} | Line: {line_display} | Total: {abs(position)} contracts")
                    
                except Exception as e:
                    print(f"[WS-POSITION] Error processing position update for {ticker}: {e}")
                    # Still add to tracking even on error
                    if submarket_key not in auto_bet_submarkets:
                        auto_bet_submarkets.add(submarket_key)
                        print(f"[WS-POSITION] Update received: {ticker} {side} | Effective: Unknown (error) | Total: {position} contracts")
            
            # Emit position update to frontend
            socketio.emit('positions_update', positions_data)
            # Also trigger a portfolio refresh
            socketio.emit('portfolio_refresh')
        
        kalshi_client.ws_positions_callback = ws_positions_callback
        print(f"[WS] ✅ Position callback registered - real-time position updates will be logged immediately")
        
        # NOW connect WebSocket (callbacks are set, so connection handler will see them)
        print(f"[WS] Connecting WebSocket...")
        await kalshi_client.connect_ws()
        print(f"[WS] WebSocket connection initiated")
    
    # Run WebSocket connection in background
    loop = get_or_create_event_loop()
    try:
        loop.run_until_complete(connect_ws())
    except Exception as e:
        print(f"Warning: WebSocket connection error: {e}")
    
    # Start alert expiry loop
    async def alert_expiry_loop():
        while True:
            try:
                now = datetime.now().timestamp()
                to_remove = []
                for aid, ad in list(active_alerts.items()):
                    expiry = ad.get('expiry', 0)
                    if expiry and expiry < now:
                        to_remove.append(aid)
                
                for aid in to_remove:
                    del active_alerts[aid]
                    socketio.emit('remove_alert', {'id': aid})
                    print(f"Removed expired alert: {aid}")
                
                await asyncio.sleep(5)  # Check every 5 seconds
            except Exception as e:
                print(f"Warning: Error in alert expiry loop: {e}")
                await asyncio.sleep(5)
    
    # Run expiry loop in background thread
    def run_expiry_loop():
        loop = get_or_create_event_loop()
        try:
            loop.run_until_complete(alert_expiry_loop())
        except Exception as e:
            print(f"Error in expiry loop: {e}")
    
    expiry_thread = threading.Thread(target=run_expiry_loop, daemon=True)
    expiry_thread.start()
    
    # Position check loop is now integrated into the monitor loop (same event loop)
    # This prevents "attached to a different loop" errors
    
    # Start Telegram polling for commands (if configured)
    if telegram_bot_token:
        async def telegram_polling_loop():
            """Poll Telegram for commands"""
            import requests
            last_update_id = 0
            while True:
                try:
                    url = f"https://api.telegram.org/bot{telegram_bot_token}/getUpdates"
                    # Use long polling: timeout=10 means wait up to 10 seconds for updates
                    # This reduces API calls and handles timeouts gracefully
                    params = {'offset': last_update_id + 1, 'timeout': 10}
                    response = requests.get(url, params=params, timeout=15)  # 15s timeout for request
                    if response.status_code == 200:
                        data = response.json()
                        if data.get('ok') and data.get('result'):
                            for update in data['result']:
                                last_update_id = update.get('update_id', last_update_id)
                                # Process update (message or callback)
                                loop = get_or_create_event_loop()
                                if loop.is_running():
                                    asyncio.create_task(process_telegram_update(update))
                                else:
                                    loop.run_until_complete(process_telegram_update(update))
                    elif response.status_code == 409:
                        # Conflict - another instance is polling, skip this update
                        # Silently skip - this is normal when multiple instances are running
                        await asyncio.sleep(10)
                    else:
                        # Only log non-409 errors (409 is expected when multiple instances run)
                        if response.status_code != 409:
                            print(f"[TELEGRAM] WARNING: API returned status {response.status_code}")
                        await asyncio.sleep(5)
                except requests.exceptions.Timeout:
                    # Timeout is expected with long polling - just continue
                    continue
                except Exception as e:
                    print(f"[TELEGRAM] Polling error: {e}")
                    await asyncio.sleep(5)
        
        async def process_telegram_update(update_data):
            """Process Telegram update (messages or callbacks)"""
            global auto_bet_enabled, auto_bet_ev_min, auto_bet_ev_max, auto_bet_odds_min, auto_bet_odds_max, auto_bet_amount, auto_bet_submarkets, telegram_chat_id
            
            try:
                # Handle callback queries (button clicks)
                if 'callback_query' in update_data:
                    callback_query = update_data['callback_query']
                    callback_data = callback_query.get('data', '')
                    callback_id = callback_query.get('id', '')
                    message = callback_query.get('message', {})
                    chat_id = message.get('chat', {}).get('id')
                    
                    # Update chat_id if not set
                    if not telegram_chat_id and chat_id:
                        telegram_chat_id = str(chat_id)
                        print(f"[TELEGRAM] Chat ID set to: {telegram_chat_id}")
                    
                    # Handle the callback
                    handle_telegram_callback(callback_data, callback_id)
                    return
                
                # Handle regular messages
                message = update_data.get('message', {})
                if not message:
                    return
                
                text = message.get('text', '').strip()
                chat_id = message.get('chat', {}).get('id')
                
                if not text or not chat_id:
                    return
                
                # Update chat_id if not set
                if not telegram_chat_id:
                    telegram_chat_id = str(chat_id)
                    print(f"[TELEGRAM] Chat ID set to: {telegram_chat_id}")
                
                # Handle commands (only send response if state actually changed to avoid duplicates)
                response_text = None
                keyboard = None
                
                if text.lower() in ['/start', '/start_auto_bet', 'start']:
                    old_enabled = auto_bet_enabled
                    auto_bet_enabled = True
                    if old_enabled != auto_bet_enabled:
                        send_auto_bet_state_change(auto_bet_enabled)
                        return  # Don't send duplicate message
                    else:
                        response_text = "✅ Auto-betting is already active!"
                        keyboard = {
                            'inline_keyboard': [[
                                {'text': '⛔ Stop Auto-Bet', 'callback_data': 'stop_auto_bet'},
                                {'text': '📊 Status', 'callback_data': 'status'}
                            ]]
                        }
                
                elif text.lower() in ['/stop', '/stop_auto_bet', 'stop']:
                    old_enabled = auto_bet_enabled
                    auto_bet_enabled = False
                    if old_enabled != auto_bet_enabled:
                        send_auto_bet_state_change(auto_bet_enabled)
                        return  # Don't send duplicate message
                    else:
                        response_text = "⛔ Auto-betting is already inactive!"
                        keyboard = {
                            'inline_keyboard': [[
                                {'text': '✅ Start Auto-Bet', 'callback_data': 'start_auto_bet'},
                                {'text': '📊 Status', 'callback_data': 'status'}
                            ]]
                        }
                
                elif text.lower() in ['/status', 'status']:
                    status_icon = "✅ ACTIVE" if auto_bet_enabled else "⛔ INACTIVE"
                    response_text = f"""{status_icon}

EV Range: {auto_bet_ev_min}% - {auto_bet_ev_max}%
Odds Range: {auto_bet_odds_min} to {auto_bet_odds_max}
Bet Amount: ${auto_bet_amount:.2f}
Tracked Submarkets: {len(auto_bet_submarkets)}"""
                    keyboard = {
                        'inline_keyboard': [[
                            {'text': '✅ Start' if not auto_bet_enabled else '⛔ Stop', 
                             'callback_data': 'start_auto_bet' if not auto_bet_enabled else 'stop_auto_bet'},
                            {'text': '📊 Refresh', 'callback_data': 'status'}
                        ], [
                            {'text': '📈 Stats', 'callback_data': 'stats'},
                            {'text': '🗑️ Clear Duplicates', 'callback_data': 'clear_duplicates'}
                        ]]
                    }
                
                elif text.lower().startswith('/token ') or text.lower().startswith('token '):
                    response_text = (
                        "ℹ️ <b>Odds-API.io</b> uses <code>ODDS_API_KEY</code> in your <code>.env</code> file.\n\n"
                        "Edit the key on the server and restart the bot — bearer tokens are not used."
                    )
                    keyboard = {
                        'inline_keyboard': [[
                            {'text': '📊 Status', 'callback_data': 'status'}
                        ]]
                    }
                
                elif text.lower() in ['/help', 'help']:
                    response_text = """🤖 <b>Kalshi Auto-Bet Bot Commands</b>

<b>Text Commands (just type these):</b>
/start or "start" - Start auto-betting
/stop or "stop" - Stop auto-betting
/status or "status" - Check current status
/stats or "stats" - View auto-bet statistics
/token - Info: API key is set in .env (ODDS_API_KEY)
/help or "help" - Show this help message

<b>Or use the buttons below messages!</b>"""
                    keyboard = {
                        'inline_keyboard': [[
                            {'text': '✅ Start Auto-Bet', 'callback_data': 'start_auto_bet'},
                            {'text': '⛔ Stop Auto-Bet', 'callback_data': 'stop_auto_bet'}
                        ], [
                            {'text': '📊 Status', 'callback_data': 'status'}
                        ]]
                    }
                
                # Send response if we have one
                if response_text:
                    send_telegram_message(response_text, reply_markup=keyboard)
            
            except Exception as e:
                print(f"[TELEGRAM] Error processing update: {e}")
        
        def run_telegram_polling():
            loop = get_or_create_event_loop()
            try:
                loop.run_until_complete(telegram_polling_loop())
            except Exception as e:
                print(f"Error in Telegram polling loop: {e}")
        
        telegram_polling_thread = threading.Thread(target=run_telegram_polling, daemon=True)
        telegram_polling_thread.start()
        print("[TELEGRAM] OK: Telegram polling started")
    
    _dash_tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "dashboard.html")
    try:
        with open(_dash_tpl, encoding="utf-8") as f:
            _tpl_head = f.read(12000)
        _has_tabs = "app-main-view-switcher" in _tpl_head and "tab-btn-odds" in _tpl_head
        print(f"[DASHBOARD] dashboard.html path: {_dash_tpl}")
        print(f"[DASHBOARD] View tabs in file on disk: {_has_tabs}")
        if not _has_tabs:
            print("[DASHBOARD] WARNING: This dashboard.html has no odds tab markup — wrong folder or stale file.")
    except OSError as e:
        print(f"[DASHBOARD] WARNING: Could not read dashboard.html: {e}")

    print("Dashboard initialized")


def start_monitor():
    """Start OddsEVMonitor (Odds-API.io) polling in a background thread."""
    global monitor_thread
    
    if monitor_thread and monitor_thread.is_alive():
        return
    
    monitor_thread = threading.Thread(target=run_monitor_loop, daemon=True)
    monitor_thread.start()
    print("Odds-API.io monitor thread started (look for [MONITOR THREAD] in logs)")
    sys.stdout.flush()


# Track if shutdown notification was sent (prevent duplicates)
_shutdown_notification_sent = False

def send_shutdown_notification():
    """Send Telegram notification when bot shuts down (only once)"""
    global _shutdown_notification_sent
    
    if _shutdown_notification_sent:
        return  # Already sent, don't send again
    
    _shutdown_notification_sent = True
    
    try:
        send_telegram_message("🛑 <b>BOT STOPPED</b>\n\nKalshi betting bot has been shut down.")
    except Exception as e:
        print(f"[TELEGRAM] Could not send shutdown notification: {e}")


if __name__ == '__main__':
    print()
    print(
        "Note: Using your current 10 books (DK, FD, Betfair Exchange, Circa, Polymarket, MGM, "
        "Bookmaker, Caesars, Kalshi, NoVig). Recommended future swaps when you can reset key: "
        "replace MGM and Caesars with ProphetX and SportTrade for sharper live edges."
    )
    print()

    import signal
    import atexit
    
    # Register shutdown handler
    def shutdown_handler(signum=None, frame=None):
        print("\n[SHUTDOWN] Bot shutting down...")
        send_shutdown_notification()
        sys.exit(0)
    
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    atexit.register(send_shutdown_notification)
    
    initialize_dashboard()
    start_monitor()
    
    port = int(os.environ.get("DASHBOARD_PORT", "5000"))
    print(f"Starting dashboard server on http://localhost:{port}")
    try:
        # debug=False: Werkzeug reloader would spawn a child process and duplicate / disrupt the monitor thread.
        socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        shutdown_handler()
    finally:
        send_shutdown_notification()

