"""
Real-Time Betting Dashboard for Polymarket
Web-based dashboard for instant betting on Polymarket alerts
"""
import asyncio
import json
import os
import sys
import csv
import io

# Fix Unicode encoding for Windows console
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        # Python < 3.7 or encoding not available, use replacement
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import defaultdict
from flask import Flask, render_template, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit
import threading
from dotenv import load_dotenv
from bookiebeats_monitor import BookieBeatsAlert
from Polymarket.bookiebeats_api_monitor_polymarket import BookieBeatsAPIMonitorPolymarket
from Polymarket.market_matcher_polymarket import MarketMatcherPolymarket
from Polymarket.polymarket_client import PolymarketClient

# Load environment variables
load_dotenv()

# Get initial deposit from .env (fallback if API doesn't return cumulative_deposits)
INITIAL_DEPOSIT_DOLLARS = float(os.getenv('INITIAL_DEPOSIT', '980.0'))  # Default to $980 if not set

app = Flask(__name__, 
            static_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static'),
            template_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'templates'))
app.config['SECRET_KEY'] = 'polymarket-live-betting-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Global state
polymarket_client: Optional[PolymarketClient] = None
market_matcher_polymarket: Optional[MarketMatcherPolymarket] = None
bookiebeats_monitor: Optional[BookieBeatsAPIMonitorPolymarket] = None
active_alerts: Dict[str, Dict] = {}  # alert_id -> alert_data
monitor_thread: Optional[threading.Thread] = None
monitor_loop: Optional[asyncio.AbstractEventLoop] = None  # Store the monitor's event loop
monitor_running = False
ALERT_TTL = 30  # Remove alerts after 30 seconds if EV drops
user_max_bet_amount = 100.0  # Default max bet amount in dollars (user-configurable)
dashboard_min_ev = 0.0  # Minimum EV to show on dashboard (for manual betting, default 0% to show all)
per_event_max_bet = 400.0  # Default max bet per event in dollars (user-configurable, default $400)

# Auto-bet settings (global toggle)
auto_bet_enabled = True  # Default to ON - can be disabled via frontend or Telegram

# Per-filter auto-bet settings
# Format: {filter_name: {'ev_min': float, 'ev_max': float, 'odds_min': int, 'odds_max': int, 'amount': float, 'enabled': bool}}
auto_bet_settings_by_filter = {}  # Dict of filter_name -> settings dict
nhl_over_bet_amount = 202.0  # NHL over bet amount in dollars (configurable from frontend, shared across all filters)

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
# Track by: game_name -> market_type -> pick_direction -> list of (ticker, side) tuples
# For totals: pick_direction is "Over" or "Under"
# For spreads/moneylines: pick_direction is the team name (for reverse middle detection)
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
bookiebeats_monitors = {}  # Dict of filter_name -> BookieBeatsAPIMonitorPolymarket instance
bookiebeats_monitor = None  # Legacy single monitor (will be replaced by multi-monitor system)

# Default filter: "Polymarket All Sports (3 Sharps Live)" - More conservative filter requiring 3 sharps and sharp liquidity
DEFAULT_FILTER_NAME = "Polymarket All Sports (3 Sharps Live)"
DEFAULT_FILTER_PAYLOAD = {
    "state": "ND",
    "bettingBooks": ["Polymarket"],
    "displayBooks": ["Polymarket", "Circa", "BookMaker", "Pinnacle", "Novig", "ProphetX", "SportTrade", "FanDuel", "DraftKings"],
    "leagues": ["SOCCER_ALL", "TENNIS_ALL", "BASKETBALL_ALL", "FOOTBALL_ALL", "HOCKEY_ALL", "BASEBALL_ALL", "UFC_ALL"],
    "betTypes": ["GAMELINES"],
    "minRoi": 0,
    "middleStatus": "INCLUDE",
    "middleFilters": [{"sport": "Any", "minHold": 0, "minMiddle": 0}],
    "sortOrder": "ROI",
    "devigFilter": {
        "sharps": ["Circa", "BookMaker", "Pinnacle", "Novig", "ProphetX", "SportTrade", "FanDuel", "DraftKings"],
        "method": "POWER",
        "type": "AVERAGE",
        "minEv": 0,
        "minLimit": 0,
        "minSharpBooks": 3,
        "hold": [{"book": "Any", "max": 8}]
    },
    "oddsRanges": [{"book": "Any", "min": -200, "max": 200}],
    "minLimits": [{"book": "Polymarket", "min": 75}],
    "minSharpLimits": [
        {"book": "BookMaker", "min": 250},
        {"book": "Circa", "min": 250},
        {"book": "Novig", "min": 200},
        {"book": "Pinnacle", "min": 250},
        {"book": "ProphetX", "min": 200},
        {"book": "SportTrade", "min": 200},
        {"book": "DraftKings", "min": 250},
        {"book": "FanDuel", "min": 250}
    ],
    "linkType": "MOBILE_BETSLIP"
}

# Second filter: "CBB EV Filter (Live - Polymarket)" - College basketball specific filter
CBB_FILTER_NAME = "CBB EV Filter (Live - Polymarket)"
CBB_FILTER_PAYLOAD = {
    "state": "ND",
    "bettingBooks": ["Polymarket"],
    "displayBooks": ["BookMaker", "Caesars", "DraftKings", "FanDuel", "Kambi", "Pinnacle", "Rebet", "SportTrade", "HardRock", "EspnBet", "Polymarket", "Pinnacle", "FanDuel", "DraftKings", "BookMaker"],
    "leagues": ["NCAAB"],
    "excludedCategories": ["1st Quarter", "2nd Quarter", "3rd Quarter", "4th Quarter", "1st Half", "2nd Half"],
    "betTypes": ["GAMELINES"],
    "minRoi": 8,
    "middleStatus": "INCLUDE",
    "middleFilters": [{"sport": "Any", "minHold": 8, "minMiddle": 0}],
    "sortOrder": "ROI",
    "devigFilter": {
        "sharps": ["Pinnacle", "FanDuel", "DraftKings", "BookMaker"],
        "method": "WORST_CASE",
        "type": "AVERAGE",
        "minEv": 8,
        "minLimit": 0,
        "minSharpBooks": 2,
        "hold": [{"book": "Any", "max": 8}]
    },
    "oddsRanges": [{"book": "Any", "min": -200, "max": 200}],
    "minLimits": [{"book": "Any", "min": 80}, {"book": "Any", "min": 0}],
    "minSharpLimits": [{"book": "Any", "min": 200}],
    "linkType": "MOBILE_BETSLIP"
}

# Initialize with default filter
saved_filters[DEFAULT_FILTER_NAME] = DEFAULT_FILTER_PAYLOAD
saved_filters[CBB_FILTER_NAME] = CBB_FILTER_PAYLOAD
# By default, both filters should be selected for both dashboard and auto-bettor
selected_dashboard_filters = [DEFAULT_FILTER_NAME, CBB_FILTER_NAME]
selected_auto_bettor_filters = [DEFAULT_FILTER_NAME, CBB_FILTER_NAME]

# Initialize per-filter auto-bet settings with defaults
auto_bet_settings_by_filter[DEFAULT_FILTER_NAME] = {
    'ev_min': 5.0,
    'ev_max': 25.0,
    'odds_min': -200,
    'odds_max': 200,
    'amount': 101.0,
    'enabled': True
}
auto_bet_settings_by_filter[CBB_FILTER_NAME] = {
    'ev_min': 8.0,  # CBB filter has 8% min EV in payload
    'ev_max': 25.0,
    'odds_min': -200,
    'odds_max': 200,
    'amount': 101.0,
    'enabled': True
}

# Auto-bet tracking for Google Sheets export and win/loss analysis
AUTO_BET_CSV_FILE = os.path.join(os.path.dirname(__file__), "auto_bets.csv")  # Backup CSV
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
    balance = "Loading..."
    try:
        if polymarket_client:
            # Try to get balance from cached portfolio data if available
            # Otherwise, fetch it in background (non-blocking)
            loop = get_or_create_event_loop()
            if loop.is_running():
                # Try to get from thread-safe future with short timeout
                try:
                    portfolio_future = asyncio.run_coroutine_threadsafe(polymarket_client.get_portfolio(), loop)
                    portfolio = portfolio_future.result(timeout=1)  # 1s timeout
                    if portfolio:
                        balance_cents = portfolio.get('balance', 0) or portfolio.get('balance_cents', 0) or 0
                        balance_dollars = balance_cents / 100.0 if balance_cents else 0
                        balance = f"${balance_dollars:.2f}"
                except:
                    balance = "N/A"  # Timeout or error
            else:
                portfolio = loop.run_until_complete(polymarket_client.get_portfolio())
                if portfolio:
                    balance_cents = portfolio.get('balance', 0) or portfolio.get('balance_cents', 0) or 0
                    balance_dollars = balance_cents / 100.0 if balance_cents else 0
                    balance = f"${balance_dollars:.2f}"
    except Exception as e:
        balance = "N/A"
    
    # Format fee information for Telegram
    if fee_type == 'maker':
        fee_info = "✅ FEE-FREE (Maker)"
    else:
        fee_info = f"💰 Fees: ${taker_fees_dollars:.2f} (Taker)"
    
    message = f"""🎲 <b>AUTO-BET PLACED</b>

📊 <b>{teams}</b>
🎯 Pick: {pick_display}
📈 EV: {ev_percent}%
💰 Amount: ${cost}
📦 Contracts: {contracts}
⚡ Odds: {american_odds}
{fee_info}
🏆 Sport: {sport}
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
    global polymarket_client
    
    if not polymarket_client:
        return
    
    try:
        # Get current positions from Polymarket
        positions = await polymarket_client.get_positions()
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


def create_alert_id(alert: BookieBeatsAlert) -> str:
    """Create unique ID for an alert (stable, doesn't change with EV or odds)"""
    # Use stable hash without EV or odds - same alert with different EV/odds should have same ID
    # CRITICAL: Use hashlib for stable hash (Python's hash() is not stable across sessions)
    # CRITICAL: Don't include odds in hash - odds can change slightly but it's still the same alert
    # We want to update the existing alert, not create a new one when odds change
    import hashlib
    # Use ticker, pick, qualifier, and market_type to create stable ID
    # Also include filter_name if available to distinguish same alert from different filters
    filter_name = getattr(alert, 'filter_name', '') or ''
    key = f"{alert.ticker}|{alert.pick}|{alert.qualifier}|{alert.market_type}|{filter_name}"
    # Use MD5 hash and take first 10 digits for consistent ID
    hash_obj = hashlib.md5(key.encode('utf-8'))
    hash_hex = hash_obj.hexdigest()
    # Convert hex to int and take modulo to get consistent numeric ID
    hash_int = int(hash_hex[:8], 16)  # Use first 8 hex chars (32 bits)
    return str(hash_int % (10 ** 10))  # Return as string for consistency


async def handle_new_alert(alert: BookieBeatsAlert):
    """Handle a new alert from BookieBeats - OPTIMIZED FOR SPEED (async version)"""
    print(f"[HANDLE ALERT] 📥 Received alert: {alert.teams} - {alert.pick} ({alert.ev_percent:.2f}% EV)")
    global active_alerts
    
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
        
        if not event_ticker:
            print(f"Warning: No ticker found in alert: {alert.market_url}")
            return
        
        # CRITICAL: Find the EXACT submarket within the event
        # The link gives us the EVENT ticker, but we need the SUBMARKET ticker
        # Use line from alert (stored from API), or parse from qualifier as fallback
        line = getattr(alert, 'line', None)
        if line is None and alert.qualifier:
            try:
                # Parse line from qualifier (e.g., "40.5", "+11.5", "-11.5")
                import re
                line_str = alert.qualifier.replace('+', '').replace('*', '').strip()
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
        submarket = await polymarket_client.find_submarket(
            event_ticker=event_ticker,
            market_type=alert.market_type,
            line=line,
            selection=alert.pick
        )
        
        match_result = None
        
        if submarket:
            # Found exact submarket!
            submarket_ticker = submarket.get('ticker', '')
            
            # CRITICAL: Validate that this is a standard market (not MULTIGAMEEXTENDED, etc.)
            excluded_types = ['MULTIGAMEEXTENDED', 'EXTENDED', 'MULTIGAME', 'PARLAY', 'COMBO']
            if any(excluded_type in submarket_ticker.upper() for excluded_type in excluded_types):
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
            
            # Fallback to old method
            match_result = await market_matcher_polymarket.match_alert_to_polymarket(alert)
            if not match_result:
                if is_ncaab:
                    print(f"❌ [NCAAB] Fallback matching also failed for: {alert.teams} - {alert.pick}")
                else:
                    print(f"❌ Fallback matching also failed for: {alert.teams} - {alert.pick}")
        
        if not match_result:
            print(f"Warning: Could not match alert: {alert.teams} - {alert.pick}")
            # Emit match failure to UI
            socketio.emit('alert_match_failed', {
                'teams': alert.teams,
                'pick': alert.pick,
                'market_type': alert.market_type,
                'reason': 'Could not find matching submarket'
            })
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
        side = market_matcher_polymarket.determine_side(alert, market_dict)
        
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
            if side == 'yes' and not yes_contains_pick:
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
        # Don't check subtitles because Polymarket subtitles are often buggy (both say "Over")
        elif 'total' in market_type_lower:
            pick_upper_check = pick_upper
            is_over = 'OVER' in pick_upper_check or pick_upper_check == 'OVER'
            is_under = 'UNDER' in pick_upper_check or pick_upper_check == 'UNDER'
            
            # Trust the side determination from market_matcher_polymarket (Over = YES, Under = NO)
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
        # CRITICAL: We use the 'price' field from BookieBeats API (63¢) for the LIMIT ORDER
        # The 'odds' field (-183) is the effective price AFTER fees (what BB uses for EV calculation)
        # Example: price=63¢ (-170 American odds) = order price BEFORE fees
        #          odds=-183 = effective price AFTER Kalshi fees
        #          BB calculates EV based on -183 (after fees), but we place order at 63¢ (before fees)
        price_cents = getattr(alert, 'price_cents', None)
        if price_cents is None:
            # Fallback: parse from odds if price not available (shouldn't happen with API)
            price_cents = market_matcher_polymarket.parse_odds_to_price_cents(alert.odds)
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
        asyncio.create_task(polymarket_client.fetch_orderbook(match_result['ticker']))
        
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
            'event_ticker': event_ticker,  # EVENT ticker (for hash matching with BookieBeats)
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
            'expiry': (datetime.now() + timedelta(seconds=30)).timestamp()  # TTL: 30 seconds
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
            
            # CRITICAL: Preserve filter_name - use existing if new alert doesn't have it, otherwise update
            if hasattr(alert, 'filter_name') and alert.filter_name:
                existing_alert['filter_name'] = alert.filter_name
                updated = True
            # If new alert doesn't have filter_name, preserve the existing one (don't overwrite with None)
            
            # Emit update to frontend if anything changed
            if updated:
                print(f"[HANDLE ALERT] 🔄 Updated existing alert {alert_id} with new data (EV: {existing_alert.get('ev_percent', 0):.2f}%, Odds: {existing_alert.get('odds', 'N/A')})")
                socketio.emit('alert_update', existing_alert)
            return
        
        # Store with string ID (already converted in create_alert_id)
        active_alerts[alert_id] = alert_data
        
        # Subscribe to WebSocket for real-time orderbook updates
        await polymarket_client.subscribe_orderbook(match_result['ticker'])
        
        # Filter by dashboard min EV AND selected dashboard filters before emitting
        global dashboard_min_ev, selected_dashboard_filters
        alert_filter_name = getattr(alert, 'filter_name', None) or alert_data.get('filter_name')
        
        # Check if this alert's filter is selected for dashboard
        if alert_filter_name and alert_filter_name not in selected_dashboard_filters:
            print(f"[ALERT] SKIP: Alert from filter '{alert_filter_name}' not in selected dashboard filters: {selected_dashboard_filters}")
            # Still store in active_alerts for potential future use, but don't emit to frontend
            return
        
        print(f"[ALERT] Processing alert: {alert.teams} - {alert.pick} ({alert.ev_percent:.2f}% EV, dashboard_min_ev={dashboard_min_ev:.2f}%)")
        if alert.ev_percent >= dashboard_min_ev:
            # Emit to all connected clients IMMEDIATELY (don't wait for orderbook)
            print(f"[ALERT] ✅ Emitting to frontend: EV {alert.ev_percent:.2f}% >= min {dashboard_min_ev:.2f}%")
            socketio.emit('new_alert', alert_data)
            print(f"New alert: {alert.teams} - {alert.pick} ({alert.ev_percent:.2f}% EV)")
        else:
            print(f"❌ Filtered alert (EV {alert.ev_percent:.2f}% < min {dashboard_min_ev:.2f}%): {alert.teams} - {alert.pick}")
        
        # AUTO-BET: Check if alert matches auto-bet criteria and place bet automatically
        # CRITICAL: Only process alerts from filters selected for auto-bettor
        alert_filter_name = getattr(alert, 'filter_name', None)
        if auto_bet_enabled:
            # Check if alert is from a filter selected for auto-bettor
            if alert_filter_name and alert_filter_name not in selected_auto_bettor_filters:
                print(f"[AUTO-BET] SKIP: Alert {alert_id} from filter '{alert_filter_name}' not in selected auto-bettor filters: {selected_auto_bettor_filters}")
                # Don't process for auto-betting (dashboard filtering is handled earlier in handle_new_alert)
                return
            else:
                # CRITICAL: Run in background (don't await) so it doesn't block alert processing or manual betting
                # BUT: Only create task if not already processing this alert_id (prevents race conditions)
                # ALSO: Check if submarket is already being processed (prevents duplicate tasks for same submarket with different alert IDs)
                # CRITICAL: Use lock to make check-and-mark atomic - mark submarket as processing HERE to prevent duplicate tasks
                submarket_key_for_check = (match_result['ticker'].upper(), side.lower())
                print(f"[AUTO-BET] 🔍 Checking if alert {alert_id} should trigger auto-bet:")
                print(f"[AUTO-BET]    ticker={match_result['ticker']}, side={side}, submarket_key={submarket_key_for_check}")
                print(f"[AUTO-BET]    filter={alert_filter_name}, selected_auto_bettor_filters={selected_auto_bettor_filters}")
                print(f"[AUTO-BET]    alert_id in processing: {alert_id in auto_bet_processing_alert_ids}")
                print(f"[AUTO-BET]    submarket already bet: {submarket_key_for_check in auto_bet_submarkets}")
                print(f"[AUTO-BET]    submarket processing: {submarket_key_for_check in auto_bet_processing_submarkets}")
                if alert_id not in auto_bet_processing_alert_ids:
                    # CRITICAL: Use lock to atomically check AND mark submarket as processing
                    # This prevents race conditions where two tasks both see "not processing" and both create tasks
                    # We mark it here so that if another task checks before this task runs, it will see it's already processing
                    if auto_bet_lock:
                        async def create_task_safely():
                            async with auto_bet_lock:
                                # CRITICAL: Check retry count and cooldown FIRST (prevents infinite loops)
                                import time
                                current_time = time.time()
                                retry_count = auto_bet_submarket_retry_count.get(submarket_key_for_check, 0)
                                last_retry = auto_bet_submarket_last_retry.get(submarket_key_for_check, 0)
                                
                                if retry_count >= MAX_RETRIES_PER_SUBMARKET:
                                    time_since_last_retry = current_time - last_retry
                                    if time_since_last_retry < RETRY_COOLDOWN_SECONDS:
                                        print(f"[AUTO-BET] 🚨 BLOCKED: Alert {alert_id} - Submarket {submarket_key_for_check} exceeded max retries ({retry_count}/{MAX_RETRIES_PER_SUBMARKET}) and still in cooldown ({int(time_since_last_retry)}s/{RETRY_COOLDOWN_SECONDS}s), BLOCKING to prevent infinite loop")
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
                                    print(f"[AUTO-BET] SKIP: Alert {alert_id} - Submarket {submarket_key_for_check} already bet, skipping duplicate task (reappear protection)")
                                    return
                                
                                # CRITICAL: Check if submarket is already being processed
                                # This catches the case where a bet is in progress but not yet in auto_bet_submarkets
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
                                
                                # CRITICAL: Re-check if already bet AFTER checking processing (defensive double-check)
                                # This catches the race condition where bet was added between initial check and now
                                if submarket_key_for_check in auto_bet_submarkets:
                                    print(f"[AUTO-BET] SKIP: Alert {alert_id} - Submarket {submarket_key_for_check} already bet (caught in double-check), skipping duplicate task")
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
                                print(f"[AUTO-BET] Created task for alert {alert_id}, submarket {submarket_key_for_check} marked as processing")
                        
                        asyncio.create_task(create_task_safely())
                    else:
                        # Fallback if lock not initialized yet (shouldn't happen, but be safe)
                        # CRITICAL: Check already bet FIRST
                        if submarket_key_for_check in auto_bet_submarkets:
                            print(f"[AUTO-BET] SKIP: Alert {alert_id} - Submarket {submarket_key_for_check} already bet, skipping duplicate task (fallback check)")
                        elif submarket_key_for_check not in auto_bet_processing_submarkets:
                            auto_bet_processing_submarkets.add(submarket_key_for_check)
                            auto_bet_submarket_to_alert_id[submarket_key_for_check] = alert_id
                            auto_bet_processing_alert_ids.add(alert_id)
                            task = asyncio.create_task(check_and_auto_bet(alert_id, alert_data, alert))
                            auto_bet_submarket_tasks[submarket_key_for_check] = task
                        else:
                            print(f"[AUTO-BET] SKIP: Alert {alert_id} - Submarket {submarket_key_for_check} already processing, skipping duplicate task")
                else:
                    print(f"[AUTO-BET] SKIP: Alert {alert_id} already being processed, skipping duplicate task")
    
    except Exception as e:
        print(f"Error handling alert: {e}")
        import traceback
        traceback.print_exc()


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
                portfolio = await polymarket_client.get_portfolio()
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
            except Exception as e:
                print(f"[HEARTBEAT] Error fetching portfolio: {e}")
                import traceback
                traceback.print_exc()
                status = "Error fetching data"
            
            # Get warm cache ticker count
            warm_tickers_count = len(polymarket_client.warm_cache_tickers) if polymarket_client else 0
            
            # Format message
            message = f"""🤖 <b>Polymarket Bot Status</b>

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


def run_monitor_loop():
    """Run the monitor loop in a separate thread"""
    global monitor_running, monitor_loop
    
    async def async_monitor():
        global bookiebeats_monitor, monitor_running, polymarket_client, monitor_loop
        
        # Store the event loop so Flask can use it
        monitor_loop = asyncio.get_running_loop()
        
        # Initialize the auto-bet lock in this event loop
        global auto_bet_lock
        auto_bet_lock = asyncio.Lock()
        
        monitor_running = True
        
        # CRITICAL: Initialize Polymarket client session in THIS event loop
        # This ensures the session uses the correct event loop
        await polymarket_client.init()
        # Start the proactive Warm Cache loop in the background
        asyncio.create_task(polymarket_client.warm_cache_loop())
        
        # Start heartbeat task (Telegram status every 30 minutes)
        asyncio.create_task(heartbeat_task())
        
        print("Polymarket client session initialized in monitor loop")
        
        # Load existing positions to prevent duplicate bets on restart
        # This must happen AFTER client is initialized
        await load_existing_positions_to_tracking()
        
        # Start all selected monitors
        global bookiebeats_monitors, selected_dashboard_filters, selected_auto_bettor_filters
        monitors_to_start = []
        
        # Combine dashboard and auto-bettor filters (deduplicated)
        all_selected_filters = list(set(selected_dashboard_filters + selected_auto_bettor_filters))
        
        # Start monitors for all selected filters (dashboard + auto-bettor)
        for filter_name in all_selected_filters:
            if filter_name in bookiebeats_monitors:
                monitor = bookiebeats_monitors[filter_name]
                monitors_to_start.append((filter_name, monitor))
            else:
                # Create monitor if it doesn't exist
                filter_payload = saved_filters.get(filter_name)
                if filter_payload:
                    monitor = BookieBeatsAPIMonitorPolymarket(auth_token=os.getenv("BOOKIEBEATS_TOKEN"))
                    monitor.set_filter(filter_payload)
                    monitor.poll_interval = 0.5
                    bookiebeats_monitors[filter_name] = monitor
                    monitors_to_start.append((filter_name, monitor))
        
        # If no monitors selected, that's OK - user may have intentionally disabled all filters
        # Don't start any monitors if none are selected
        if not monitors_to_start:
            print(f"No filters selected - no monitors will be started (dashboard: {selected_dashboard_filters}, auto-bettor: {selected_auto_bettor_filters})")
        
        # Start all monitors
        for filter_name, monitor in monitors_to_start:
            success = await monitor.start()
            if not success:
                print(f"Failed to start monitor for filter: {filter_name}")
            else:
                print(f"Started monitor for filter: {filter_name}")
                # Create a wrapper callback that tags alerts with filter_name
                # Use a lambda with default argument to capture filter_name correctly
                async def filtered_callback(alert: BookieBeatsAlert, fn=filter_name):
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
        
        # Register callbacks for updated and removed alerts on ALL monitors
        # Register callback for updated alerts (same alert, new EV/liquidity)
        async def handle_updated_alert(alert: BookieBeatsAlert):
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
                if alert.ev_percent >= dashboard_min_ev:
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
                        'devig_books': alert_data.get('devig_books', getattr(alert, 'devig_books', []))
                    })
                    # Only log if EV or liquidity actually changed (not just reappearing)
                    if ev_changed or liq_changed:
                        print(f"Updated alert: {alert.teams} - {alert.pick} (EV: {alert.ev_percent:.2f}%)")
                else:
                    # EV dropped below threshold - remove from dashboard
                    if alert_id in active_alerts:
                        del active_alerts[alert_id]
                        socketio.emit('remove_alert', {'id': alert_id})
                        print(f"Removed alert (EV {alert.ev_percent:.2f}% < min {dashboard_min_ev:.2f}%): {alert.teams} - {alert.pick}")
                
                # CRITICAL: Re-check auto-bet criteria when alert updates (EV/odds may have changed)
                # This ensures alerts that move into range get bet automatically
                # BUT: Only create task if not already processing this alert_id (prevents race conditions)
                # ALSO: Check if submarket is already being processed (prevents duplicate tasks for same submarket with different alert IDs)
                # CRITICAL: Use lock to make check-and-mark atomic - mark submarket as processing HERE to prevent duplicate tasks
                submarket_key_for_check = (alert_data.get('ticker', '').upper(), alert_data.get('side', '').lower())
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
            else:
                # Alert not in active_alerts - treat as new (it disappeared and came back)
                # This ensures perfect mirroring - if it's in BookieBeats, show it
                # Only log if it's actually reappearing (was removed, now back)
                # Don't log if it's just the first time we see it
                await handle_new_alert(alert)
        
        # Register callback for removed alerts (defined once, registered on all monitors)
        async def handle_removed_alerts(removed_hashes):
            """Handle alerts that disappeared from BookieBeats"""
            # CRITICAL: Use EVENT ticker for hash matching (matches BookieBeats hash)
            # BookieBeats hash uses event_ticker, not submarket ticker
            to_remove = []
            for alert_id, alert_data in list(active_alerts.items()):
                # Reconstruct hash using EVENT ticker (matches BookieBeats hash format)
                event_ticker = alert_data.get('event_ticker', '') or alert_data.get('ticker', '')
                alert_hash = f"{event_ticker}|{alert_data.get('pick', '')}|{alert_data.get('qualifier', '')}|{alert_data.get('odds', '')}"

                if alert_hash in removed_hashes:
                    to_remove.append(alert_id)

            # If removing most/all alerts, clear everything
            if len(to_remove) >= len(active_alerts) * 0.5 or len(removed_hashes) >= len(active_alerts):
                # Clear all alerts - BookieBeats is empty or most alerts removed
                all_ids = list(active_alerts.keys())
                active_alerts.clear()
                for alert_id in all_ids:
                    socketio.emit('remove_alert', {'id': str(alert_id)})
                print(f"Cleared all {len(all_ids)} alerts (BookieBeats empty/most removed)")
            else:
                # Remove individual alerts
                for alert_id in to_remove:
                    del active_alerts[alert_id]
                    socketio.emit('remove_alert', {'id': str(alert_id)})
                    print(f"Removed alert (disappeared from BB): {alert_id}")
        
        # Register updated/removed callbacks on ALL monitors (not just legacy bookiebeats_monitor)
        for filter_name, monitor in monitors_to_start:
            if hasattr(monitor, 'updated_alert_callbacks'):
                monitor.updated_alert_callbacks.append(handle_updated_alert)
            if hasattr(monitor, 'removed_alert_callbacks'):
                monitor.removed_alert_callbacks.append(handle_removed_alerts)
        
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
            tasks = []
            for filter_name, monitor in monitors_to_start:
                if monitor.running:
                    task = asyncio.create_task(monitor.monitor_loop())
                    tasks.append(task)
                    print(f"Started monitor loop for filter: {filter_name}")
            
            if tasks:
                # Wait for all monitors to complete (they run indefinitely)
                await asyncio.gather(*tasks)
            else:
                print("No monitors to run")
        
        await run_all_monitors()
    
    # Run async function
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(async_monitor())


@app.route('/')
def index():
    """Main dashboard page"""
    # Add cache-busting header to force browser to reload
    response = app.make_response(render_template('dashboard_polymarket.html'))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/logos/<path:filename>')
def serve_logo(filename):
    """Serve logo images from the logos folder"""
    logos_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logos')
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
    global auto_bet_enabled, auto_bet_ev_min, auto_bet_ev_max, auto_bet_odds_min, auto_bet_odds_max, auto_bet_amount, auto_bet_submarkets, auto_bet_submarket_data, auto_bet_processing_submarkets, auto_bet_games, positions_loaded, nhl_over_bet_amount, auto_bet_lock, auto_bet_processing_alert_ids, auto_bet_submarket_to_alert_id, auto_bet_submarket_tasks
    
    # CRITICAL: Determine submarket_key early so we can clean it up on early returns
    submarket_key = None
    ticker = alert_data.get('ticker')
    side = alert_data.get('side')
    if ticker and side:
        submarket_key = (ticker.upper(), side.lower())
    
    # Helper function to clean up submarket (used on early returns and in finally)
    async def cleanup_submarket():
        if submarket_key and submarket_key[0] and submarket_key[1]:
            if auto_bet_lock:
                async with auto_bet_lock:
                    auto_bet_processing_submarkets.discard(submarket_key)
                    auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                    auto_bet_submarket_tasks.pop(submarket_key, None)
            else:
                auto_bet_processing_submarkets.discard(submarket_key)
                auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                auto_bet_submarket_tasks.pop(submarket_key, None)
    
    # Log entry for debugging (only for alerts that might pass)
    try:
        ev_pct = alert.ev_percent if alert else alert_data.get('ev_percent', 0)
        teams = alert.teams if alert else alert_data.get('teams', 'N/A')
        pick = alert.pick if alert else alert_data.get('pick', 'N/A')
        # Only log if EV is potentially in range (to reduce spam)
        if ev_pct >= 5.0:  # Log alerts with 5%+ EV
            print(f"[AUTO-BET] Checking: {teams} - {pick} ({ev_pct:.2f}% EV)")
    except:
        pass
    
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
        alert_filter_name = getattr(alert, 'filter_name', None)
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
        
        # Fast EV check
        ev_percent = alert.ev_percent
        print(f"[AUTO-BET] EV check: {ev_percent:.2f}% (range: {current_ev_min}%-{current_ev_max}%)")
        if ev_percent < current_ev_min or ev_percent > current_ev_max:
            print(f"[AUTO-BET] SKIP: Alert {alert_id} EV {ev_percent:.2f}% outside range ({current_ev_min}%-{current_ev_max}%) [EV OUT OF RANGE]")
            await cleanup_submarket()
            return
        print(f"[AUTO-BET] EV check PASSED: {ev_percent:.2f}%")
        
        # Fast odds check
        american_odds_str = alert_data.get('american_odds', 'N/A')
        print(f"[AUTO-BET] Odds check: '{american_odds_str}' (range: {current_odds_min}-{current_odds_max})")
        american_odds_int = american_odds_to_int(american_odds_str)
        if american_odds_int is None:
            print(f"[AUTO-BET] SKIP: Alert {alert_id} - Could not parse American odds '{american_odds_str}'")
            await cleanup_submarket()
            return
        print(f"[AUTO-BET] Parsed odds: {american_odds_int}")
        if american_odds_int < current_odds_min or american_odds_int > current_odds_max:
            print(f"[AUTO-BET] SKIP: Alert {alert_id} Odds {american_odds_int} outside range ({current_odds_min}-{current_odds_max}) [ODDS OUT OF RANGE]")
            await cleanup_submarket()
            return
        print(f"[AUTO-BET] Odds check PASSED: {american_odds_int}")
        
        # Get expected price (ticker and side already determined above)
        expected_price_cents = alert_data.get('price_cents')
        
        if not ticker or not side:
            print(f"[AUTO-BET] SKIP: Alert {alert_id} - Missing ticker or side (ticker={ticker}, side={side}) [MISSING DATA]")
            await cleanup_submarket()
            return
        
        print(f"[AUTO-BET] Passed checks: EV={ev_percent:.2f}%, Odds={american_odds_int}, Ticker={ticker}, Side={side}")
        print(f"[AUTO-BET] Continuing to NHL check and duplicate check...")
        
        # NHL exclusions (fast check) - Block ALL NHL moneylines, spreads, and unders (manual only)
        # Check by league/teams since Polymarket ticker format may differ from Polymarket
        market_type = alert.market_type or alert_data.get('market_type', '')
        pick = (alert.pick or alert_data.get('pick', '') or '').upper()
        teams = (alert.teams or alert_data.get('teams', '') or '').upper()
        
        # Check if this is NHL by checking teams for NHL team names or league
        is_nhl = False
        nhl_teams = ['BRUINS', 'RANGERS', 'ISLANDERS', 'DEVILS', 'FLYERS', 'PENGUINS', 'CAPITALS', 'HURRICANES', 
                     'BLUE JACKETS', 'SABRES', 'RED WINGS', 'PANTHERS', 'LIGHTNING', 'MAPLE LEAFS', 'SENATORS', 'CANADIENS',
                     'BLACKHAWKS', 'AVALANCHE', 'STARS', 'WILD', 'PREDATORS', 'BLUES', 'JETS', 'DUCKS', 'COYOTES', 'FLAMES',
                     'OILERS', 'KINGS', 'SHARKS', 'KRAKEN', 'GOLDEN KNIGHTS', 'CANUCKS', 'MAMMOTH']
        if any(nhl_team in teams for nhl_team in nhl_teams):
            is_nhl = True
        # Also check ticker if it contains NHL indicators (Polymarket format may vary)
        if ticker and ('NHL' in ticker.upper() or 'HOCKEY' in ticker.upper()):
            is_nhl = True
        
        if is_nhl:
            print(f"[AUTO-BET] NHL detected: {teams} - {ticker}")
            # Block: Moneylines, Spreads, Puck Lines, and ALL Unders (regardless of EV)
            is_moneyline = market_type.upper() == 'MONEYLINE' or 'MONEYLINE' in market_type.upper()
            is_spread = 'SPREAD' in market_type.upper() or 'PUCK LINE' in market_type.upper()
            is_under = pick == 'UNDER' or 'UNDER' in pick
            if is_moneyline or is_spread or is_under:
                print(f"[AUTO-BET] SKIP: Alert {alert_id} - NHL exclusion: {market_type} {pick} [NHL EXCLUSION - Manual only]")
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
        print(f"[AUTO-BET] [LOCK] Alert {alert_id} attempting to acquire lock for {submarket_key}")
        async with auto_bet_lock:
            print(f"[AUTO-BET] [LOCK] Alert {alert_id} acquired lock for {submarket_key}")
            # CRITICAL: Atomic check-and-mark to prevent race conditions
            # All checks and marks must happen within the lock to be atomic
            # CRITICAL: Check if already bet AND add atomically in one operation
            # Use set length to detect if item was already present (atomic operation)
            set_size_before = len(auto_bet_submarkets)
            auto_bet_submarkets.add(submarket_key)
            set_size_after = len(auto_bet_submarkets)
            was_already_bet = (set_size_before == set_size_after)  # If size didn't change, it was already in set
            
            in_processing_set = submarket_key in auto_bet_processing_submarkets
            print(f"[AUTO-BET] [LOCK] Alert {alert_id} - Was already bet: {was_already_bet}, In processing set: {in_processing_set}")
            
            if was_already_bet:
                if in_processing_set:
                    print(f"[AUTO-BET] WARNING: Submarket {submarket_key} in both sets - cleaning up processing set (bet already placed)")
                    auto_bet_processing_submarkets.discard(submarket_key)
                    auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                    auto_bet_submarket_tasks.pop(submarket_key, None)
                print(f"[AUTO-BET] SKIP: Alert {alert_id} - Already bet {ticker} {side} [DUPLICATE]")
                # Remove from set since we just added it (it was already there)
                auto_bet_submarkets.discard(submarket_key)
                return
            
            # If we get here, we successfully added it (it wasn't already there)
            print(f"[AUTO-BET] [LOCK] Marked {submarket_key} as bet IMMEDIATELY (atomic check-and-add) to prevent duplicates")
        
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
                    return
            
            # PER-EVENT MAX BET CHECK: Must happen INSIDE lock to be atomic
            # This prevents two bets from both passing the check before either updates the total
            global per_event_max_bet, auto_bet_event_totals
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
                    socketio.emit('auto_bet_failed', {
                        'alert_id': alert_id,
                        'error': f"Per-event max bet reached: ${current_total:.2f} + ${bet_amount:.2f} > ${per_event_max_bet:.2f}",
                        'market': f"{alert.teams} - {alert.pick}"
                    })
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
            
            print(f"[AUTO-BET] [LOCK] Alert {alert_id} marked as processing, continuing to reverse middle check...")
            # NOTE: Lock is still held - it will be released when we exit the async with block
            # Get game_name and market_type for checks
            game_name = alert.teams
            market_type = alert.market_type or alert_data.get('market_type', '')
            
            # NOTE: Duplicate check already done above (lines 1042-1052) before marking as processing
            # No need to check again here - we've already verified it's not a duplicate
            
            # ============================================================================
            # REVERSE MIDDLE PREVENTION: Check for reverse middles across all market types
            # ============================================================================
            # Use market_matcher_polymarket's check_reverse_middle function for comprehensive checking
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
            
            if (is_total or is_spread) and current_line is None:
                print(f"[AUTO-BET] SKIP: Alert {alert_id} - Missing line value for {market_type_lower} market (qualifier: '{alert.qualifier}') - cannot verify reverse middle safety")
                socketio.emit('auto_bet_failed', {
                    'alert_id': alert_id,
                    'error': f"Missing line value for {market_type_lower} market - required for safety checks",
                    'market': f"{alert.teams} - {alert.pick}"
                })
                await cleanup_submarket()
                return
            
            # Build existing positions list for reverse middle check
            existing_positions_for_check = []
            for existing_key, existing_data in auto_bet_submarket_data.items():
                existing_positions_for_check.append({
                    'line': existing_data.get('line'),
                    'pick': existing_data.get('pick', ''),
                    'market_type': existing_data.get('market_type', ''),
                    'teams': existing_data.get('teams', '')
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
            
            # Use market_matcher_polymarket's check_reverse_middle function with effective line
            is_reverse_middle, reason = market_matcher_polymarket.check_reverse_middle(alert, effective_line_for_rm_check, existing_positions_for_check)
            if is_reverse_middle:
                print(f"[AUTO-BET] SKIP: Alert {alert_id} - {reason}")
                auto_bet_processing_submarkets.discard(submarket_key)
                auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                auto_bet_submarket_tasks.pop(submarket_key, None)
                auto_bet_submarkets.discard(submarket_key)
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
            
            # FAST REVERSE MIDDLE CHECK - Use ticker-based matching (lightning fast, no verbose logging)
            
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
            
            # FAST REVERSE MIDDLE CHECK - Use ticker-based matching only (lightning fast)
            for existing_key, existing_data in auto_bet_submarket_data.items():
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
                    # Use effective_line_for_check (already transformed for NO bets) instead of current_line
                    line_to_check = effective_line_for_check if effective_line_for_check is not None else current_line
                    if line_to_check is not None and existing_line is not None:
                        positive_line = line_to_check if line_to_check > 0 else existing_line if existing_line > 0 else None
                        negative_line = line_to_check if line_to_check < 0 else existing_line if existing_line < 0 else None
                        
                        if positive_line is not None and negative_line is not None:
                            if positive_line < abs(negative_line):
                                # Reverse middle - block it ONLY if teams are different
                                current_team = current_pick if not is_over and not is_under else None
                                existing_team = existing_pick if 'OVER' not in existing_pick and 'UNDER' not in existing_pick else None
                                
                                # Only block if teams are DIFFERENT (reverse middle)
                                # Same team with different lines is a true middle (allow)
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
                
                # 4. MONEYLINE + SPREAD REVERSE MIDDLE (fast check - only blocks negative spreads)
                is_spread = 'SPREAD' in ticker or 'Point Spread' in market_type
                existing_is_spread = 'SPREAD' in existing_ticker or 'Point Spread' in existing_market_type
                
                if (is_moneyline and existing_is_spread) or (is_spread and existing_is_moneyline):
                    current_team = current_pick if not is_over and not is_under else None
                    existing_team = existing_pick if 'OVER' not in existing_pick and 'UNDER' not in existing_pick else None
                    
                    if current_team and existing_team and current_team != existing_team:
                        # Only block if spread is negative (favorite)
                        if is_moneyline and existing_is_spread and existing_line is not None and existing_line < 0:
                            print(f"[AUTO-BET] SKIP: Alert {alert_id} - ML+SPREAD REVERSE MIDDLE! {current_team} ML vs {existing_team} {existing_line} [REVERSE MIDDLE]")
                            auto_bet_processing_submarkets.discard(submarket_key)
                            auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                            auto_bet_submarkets.discard(submarket_key)
                            return
                        if is_spread and existing_is_moneyline and current_line is not None and current_line < 0:
                            print(f"[AUTO-BET] SKIP: Alert {alert_id} - ML+SPREAD REVERSE MIDDLE! {current_team} {current_line} vs {existing_team} ML [REVERSE MIDDLE]")
                            auto_bet_processing_submarkets.discard(submarket_key)
                            auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                            auto_bet_submarkets.discard(submarket_key)
                            return
            
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
            # BookieBeats provides: pick = team name, qualifier/line = signed line (e.g., "+4.5" or "-7.5")
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
                'side': side  # Store side for reference
            }
            # CRITICAL: Do NOT add to auto_bet_games here - wait until bet succeeds
            # This prevents failed attempts from counting toward the limit
            # We'll add it after the bet succeeds (see line ~1504)
            
            # Determine bet amount (NHL overs use special amount)
            # Use the amount we read at the start (current_amount) to ensure consistency
            global nhl_over_bet_amount
            bet_amount = current_amount
            # Check if this is NHL (using team-based detection since Polymarket ticker format may differ)
            if is_nhl:
                current_pick = (alert.pick or alert_data.get('pick', '')).upper()
                if 'OVER' in current_pick and ('total' in market_type.lower() or 'TOTAL' in ticker):
                    bet_amount = nhl_over_bet_amount
            
            # Calculate contracts and place bet (minimal logging for speed)
            contracts = market_matcher_polymarket.calculate_contracts_from_dollars(bet_amount, expected_price_cents or 50)
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
            print(f"[AUTO-BET] ========== BOOKIEBEATS PRICE DATA ==========")
            print(f"[AUTO-BET]   BB 'odds' field: {bb_odds} (effective price AFTER fees - BB calculates EV based on this)")
            print(f"[AUTO-BET]   BB 'price' field: {bb_price_cents}¢ = {bb_price_american_odds} American odds (order price BEFORE fees)")
            print(f"[AUTO-BET]   ✅ LIMIT ORDER PRICE: {bb_price_cents}¢ ({bb_price_american_odds}) - Place order here to get {bb_odds} after fees")
            print(f"[AUTO-BET]   Expected Price: {expected_price_cents} cents ({expected_price_cents/100:.2f}¢)")
            print(f"[AUTO-BET]   Requested Contracts: {contracts}")
            print(f"[AUTO-BET]   Bet Amount: ${bet_amount:.2f}")
            
            # Try to get market subtitles for validation logging
            try:
                market_data = await polymarket_client.get_market_by_ticker(ticker)
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
                    # Polymarket subtitles are often buggy (both say "Over X"), so we can't rely on them
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
                            socketio.emit('auto_bet_failed', {
                                'alert_id': alert_id,
                                'error': f"CRITICAL: Under pick but side is YES - wrong side!",
                                'market': f"{alert.teams} - {alert.pick}"
                            })
                            await cleanup_submarket()
                            return
                        elif pick_upper_check == 'OVER' and side == 'no':
                            # This shouldn't happen, but if it does, it's wrong
                            print(f"[AUTO-BET]   🚨 CRITICAL: Over pick but side is NO - REJECTING!")
                            socketio.emit('auto_bet_failed', {
                                'alert_id': alert_id,
                                'error': f"CRITICAL: Over pick but side is NO - wrong side!",
                                'market': f"{alert.teams} - {alert.pick}"
                            })
                            await cleanup_submarket()
                            return
                    
                    # SPECIAL HANDLING FOR MONEYLINES: When subtitles are N/A or buggy, trust ticker-based side determination
                    # For moneylines, we already determined the side using the ticker (most reliable method)
                    is_moneyline = market_type_lower == 'moneyline' or 'game' in market_type_lower
                    if is_moneyline and (yes_subtitle == 'N/A' or no_subtitle == 'N/A' or yes_subtitle == no_subtitle):
                        # Trust the ticker-based side determination (already done in determine_side)
                        print(f"[AUTO-BET]   ✅ MONEYLINE: Subtitles N/A or buggy - trusting ticker-based side determination (side={side})")
                        yes_contains_pick = True if side == 'yes' else False
                        no_contains_pick = True if side == 'no' else False
                    
                    # CRITICAL: REJECT bet if side is wrong (prevents betting wrong side)
                    # BUT: Skip this check for totals/moneylines if subtitles are N/A (Kalshi bug)
                    if side == 'yes' and not yes_contains_pick:
                        # For totals/moneylines with N/A subtitles, trust the side determination
                        if (is_total_market or is_moneyline) and (yes_subtitle == 'N/A' or no_subtitle == 'N/A' or yes_subtitle == no_subtitle):
                            print(f"[AUTO-BET]   ⚠️  Market with N/A/buggy subtitles - trusting side determination (side={side})")
                        else:
                            print(f"[AUTO-BET]   🚨 CRITICAL: YES side does NOT contain pick - REJECTING BET to prevent wrong side!")
                            print(f"[AUTO-BET]   YES subtitle: '{yes_subtitle}', NO subtitle: '{no_subtitle}', Pick: '{alert.pick}'")
                            if no_contains_pick:
                                print(f"[AUTO-BET]   ✅ CORRECTION: NO subtitle contains pick - would change to NO, but REJECTING for safety")
                            socketio.emit('auto_bet_failed', {
                                'alert_id': alert_id,
                                'error': f"CRITICAL: Side validation failed. YES subtitle doesn't contain pick '{alert.pick}'",
                                'market': f"{alert.teams} - {alert.pick}"
                            })
                            await cleanup_submarket()
                            return
                    elif side == 'no' and not no_contains_pick:
                        # For moneylines with N/A subtitles, trust ticker-based side determination
                        if is_moneyline and (yes_subtitle == 'N/A' or no_subtitle == 'N/A' or yes_subtitle == no_subtitle):
                            print(f"[AUTO-BET]   ✅ MONEYLINE: Subtitles N/A or buggy - trusting ticker-based side determination (side={side})")
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
                                    socketio.emit('auto_bet_failed', {
                                        'alert_id': alert_id,
                                        'error': f"CRITICAL: Side validation failed. NO subtitle doesn't contain pick '{alert.pick}'",
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
            
            try:
                # CRITICAL: expected_price_cents is from BookieBeats 'price' field (63¢) - this is the ORDER PRICE
                # The 'odds' field (-183) is the effective price AFTER fees (what BB uses for EV calculation)
                # Example: price=63¢ (-170) = order price BEFORE fees
                #          odds=-183 = effective price AFTER Kalshi fees
                #          BB calculates EV based on -183 (after fees), but we place order at 63¢ (before fees)
                result = await polymarket_client.place_order(
                    ticker=ticker,
                    side=side,
                    count=contracts,
                    validate_odds=True,
                    expected_price_cents=expected_price_cents,  # BB 'price' field (63¢) - order price BEFORE fees
                    max_liquidity_dollars=bet_amount,
                    post_only=False,  # Taker order - instant fill at BB price
                    expiration_ts=expiration_ts  # 1 second expiration
                )
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
                socketio.emit('auto_bet_failed', {
                    'alert_id': alert_id,
                    'error': "Order placement timed out",
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
                socketio.emit('auto_bet_failed', {
                    'alert_id': alert_id,
                    'error': str(order_error),
                    'market': f"{alert.teams} - {alert.pick}"
                })
                return
            
            if result and result.get('success'):
                fill_count = result.get('fill_count', 0)
                initial_count = result.get('initial_count', result.get('count', fill_count))
                executed_price_cents = result.get('executed_price_cents') or result.get('price_cents', 0)
                limit_price_cents = result.get('price_cents', 0)  # Limit price (requested)
                total_cost_cents = result.get('total_cost_cents', 0)
                order_id = result.get('order_id', 'N/A')
                order_status = result.get('status', 'executed')
                fee_type = result.get('fee_type', 'taker')  # 'maker' or 'taker'
                taker_fees_cents = result.get('taker_fees_cents', 0)
                maker_fees_cents = result.get('maker_fees_cents', 0)
                total_fees_cents = result.get('total_fees_cents', 0)
                
                # COMPREHENSIVE ORDER RESULT LOGGING
                print(f"[AUTO-BET] ========== ORDER RESULT ==========")
                print(f"[AUTO-BET]   Success: ✅")
                print(f"[AUTO-BET]   Order ID: {order_id}")
                print(f"[AUTO-BET]   Order Status: {order_status}")
                print(f"[AUTO-BET]   Filled: {fill_count}/{initial_count} contracts")
                print(f"[AUTO-BET]   Limit Price (requested): {limit_price_cents} cents ({limit_price_cents/100:.2f}¢)")
                print(f"[AUTO-BET]   Executed Price (actual): {executed_price_cents} cents ({executed_price_cents/100:.2f}¢)")
                if executed_price_cents != limit_price_cents:
                    slippage_cents = executed_price_cents - limit_price_cents
                    slippage_pct = (slippage_cents / limit_price_cents * 100) if limit_price_cents > 0 else 0
                    print(f"[AUTO-BET]   Slippage: {slippage_cents:+.1f}¢ ({slippage_pct:+.2f}%)")
                print(f"[AUTO-BET]   Total Cost: ${total_cost_cents/100:.2f}")
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
                            cancel_result = await polymarket_client.cancel_order(order_id)
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
                        cancel_result = await polymarket_client.cancel_order(order_id)
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
                    auto_bet_processing_submarkets.discard(submarket_key)
                    auto_bet_submarket_to_alert_id.pop(submarket_key, None)
                    auto_bet_submarket_tasks.pop(submarket_key, None)
                    return
                
                # CRITICAL: Add to auto_bet_submarkets ONLY after bet succeeds (prevents duplicates)
                # This marks the submarket as "bet" so future alerts for the same submarket are skipped
                if submarket_key not in auto_bet_submarkets:
                    auto_bet_submarkets.add(submarket_key)
                    print(f"[AUTO-BET] Marked {submarket_key} as bet (successful order)")
                
                # Calculate win amount (payout - cost)
                payout = fill_count * 1.0  # Each contract pays $1 if it wins
                cost = total_cost_cents / 100.0 if total_cost_cents else (fill_count * executed_price_cents / 100.0)
                win_amount = payout - cost
                
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
                    'filter_name': getattr(alert, 'filter_name', '')  # Filter name that triggered this bet
                }
                
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
                
                # Update per-event bet total after successful bet
                # CRITICAL: We already reserved bet_amount earlier, so we need to replace it with actual cost
                # Formula: new_total = (current_total - reserved_bet_amount) + actual_cost
                # This ensures we don't double-count
                if event_base:
                    current_reserved = auto_bet_event_totals.get(event_base, 0.0)
                    # Subtract the reserved amount and add the actual cost
                    auto_bet_event_totals[event_base] = current_reserved - bet_amount + cost
                    print(f"[AUTO-BET] Updated event total for {event_base}: ${auto_bet_event_totals[event_base]:.2f} / ${per_event_max_bet:.2f} (replaced reserved ${bet_amount:.2f} with actual ${cost:.2f})")
                
                # Store in memory for quick access
                auto_bet_records.append(bet_record)
                
                # Send Telegram alert for successful bet
                send_auto_bet_alert(bet_record)
                
                # Emit auto-bet notification to frontend (with full details for popup)
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
                    'filter_name': getattr(alert, 'filter_name', ''),  # Filter name
                    'message': f'Auto-bet placed: {fill_count} contracts at {price_to_american_odds(executed_price_cents)} ({fee_type})'
                })
                
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
                
                socketio.emit('auto_bet_failed', {
                    'alert_id': alert_id,
                    'error': error,
                    'market': f"{alert.teams} - {alert.pick}"
                })
    
    except Exception as e:
        print(f"[AUTO-BET] ERROR: Exception during auto-bet: {e}")
        import traceback
        traceback.print_exc()
        
        # Clean up processing set if we got here (exception before bet placement)
        try:
            await cleanup_submarket()
        except:
            pass  # Ignore errors during cleanup
    
    finally:
        # CRITICAL: Always clean up alert_id from processing set when done
        try:
            if alert_id in auto_bet_processing_alert_ids:
                auto_bet_processing_alert_ids.discard(alert_id)
            await cleanup_submarket()
        except:
            pass
        # NOTE: async with auto_bet_lock automatically releases the lock
        # The lock is released when we exit the async with block, even on exceptions


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
    
    CRITICAL: Uses the alert loop but with SHORT timeouts to avoid blocking.
    Portfolio updates are not urgent - if they timeout, we return defaults.
    Alerts are processed immediately when they arrive, so portfolio fetches won't delay them.
    """
    if not polymarket_client:
        return jsonify({'error': 'Kalshi client not initialized', 'cash': 0, 'portfolio_value': 0, 'positions': []}), 500
    
    try:
        # Use the alert loop (where session was initialized)
        # But use SHORT timeouts so we fail fast and don't block alerts
        loop = get_or_create_event_loop()
        
        if loop.is_running():
            # Loop is running - use thread-safe approach with SHORT timeout
            # If it times out, we return defaults (portfolio is not urgent)
            try:
                portfolio_future = asyncio.run_coroutine_threadsafe(polymarket_client.get_portfolio(), loop)
                portfolio = portfolio_future.result(timeout=2)  # 2s timeout - fail fast
            except Exception as e:
                # Portfolio fetch failed - silently continue (non-critical)
                portfolio = None
            
            try:
                positions_future = asyncio.run_coroutine_threadsafe(polymarket_client.get_positions(), loop)
                positions = positions_future.result(timeout=2)  # 2s timeout - fail fast
            except Exception as e:
                # Positions fetch failed - silently continue (non-critical)
                positions = []
        else:
            # Loop exists but not running - use run_until_complete
            try:
                portfolio = loop.run_until_complete(polymarket_client.get_portfolio())
            except Exception as e:
                # Portfolio fetch failed - silently continue (non-critical)
                portfolio = None
            try:
                positions = loop.run_until_complete(polymarket_client.get_positions())
            except Exception as e:
                # Positions fetch failed - silently continue (non-critical)
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
                            polymarket_client.get_event_by_ticker(event_ticker),
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
                            event_data = loop.run_until_complete(polymarket_client.get_event_by_ticker(event_ticker))
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
                            polymarket_client.get_market_by_ticker(ticker),
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
                            market_data = loop.run_until_complete(polymarket_client.get_market_by_ticker(ticker))
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
                            polymarket_client.fetch_orderbook(ticker),
                            loop
                        )
                        # Use shorter timeout (1.5s) - if it times out, we'll use market_exposure
                        orderbook = orderbook_future.result(timeout=1.5)  # 1.5s timeout - fail fast
                    else:
                        # Fallback: use run_until_complete
                        try:
                            orderbook = loop.run_until_complete(polymarket_client.fetch_orderbook(ticker))
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


@app.route('/api/get_auto_bet', methods=['GET'])
def get_auto_bet():
    """Get auto-bet settings (per-filter)"""
    global auto_bet_enabled, auto_bet_ev_min, auto_bet_ev_max, auto_bet_odds_min, auto_bet_odds_max, auto_bet_amount, nhl_over_bet_amount, auto_bet_settings_by_filter, saved_filters, selected_auto_bettor_filters
    return jsonify({
        'enabled': auto_bet_enabled,
        'nhl_over_amount': nhl_over_bet_amount,
        'nhl_overs_amount': nhl_over_bet_amount,  # For frontend compatibility
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


@app.route('/api/update_token', methods=['POST'])
def update_token():
    """Update BookieBeats bearer token"""
    global bookiebeats_monitor
    
    data = request.json
    new_token = data.get('token', '').strip()
    
    if not new_token:
        return jsonify({'error': 'Token is required'}), 400
    
    try:
        # Update token in monitor
        if bookiebeats_monitor:
            bookiebeats_monitor.update_token(new_token)
        
        # Update .env file
        env_file = os.path.join(os.path.dirname(__file__), '.env')
        env_lines = []
        token_updated = False
        
        # Read existing .env file
        if os.path.exists(env_file):
            with open(env_file, 'r', encoding='utf-8') as f:
                env_lines = f.readlines()
        
        # Update or add BOOKIEBEATS_TOKEN
        for i, line in enumerate(env_lines):
            if line.strip().startswith('BOOKIEBEATS_TOKEN='):
                env_lines[i] = f'BOOKIEBEATS_TOKEN={new_token}\n'
                token_updated = True
                break
        
        if not token_updated:
            env_lines.append(f'BOOKIEBEATS_TOKEN={new_token}\n')
        
        # Write back to .env
        with open(env_file, 'w', encoding='utf-8') as f:
            f.writelines(env_lines)
        
        # Update environment variable
        os.environ['BOOKIEBEATS_TOKEN'] = new_token
        
        print(f"[TOKEN] Token updated via API (first 20 chars: {new_token[:20]}...)")
        return jsonify({'success': True, 'message': f'Token updated successfully (first 20 chars: {new_token[:20]}...)'})
    
    except Exception as e:
        print(f"[TOKEN] Error updating token: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


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
    global auto_bet_enabled, auto_bet_ev_min, auto_bet_ev_max, auto_bet_odds_min, auto_bet_odds_max, auto_bet_amount, nhl_over_bet_amount, auto_bet_submarkets, auto_bet_settings_by_filter, selected_auto_bettor_filters
    
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
    
    # Clear duplicate tracking if explicitly requested
    if data.get('clear_duplicates', False):
        auto_bet_submarkets.clear()
        print(f"Auto-bet duplicate tracking cleared")
    
    print(f"Auto-bet settings updated: enabled={auto_bet_enabled}, EV={auto_bet_ev_min}%-{auto_bet_ev_max}%, Odds={auto_bet_odds_min}-{auto_bet_odds_max}, Amount=${auto_bet_amount:.2f}, NHL Over=${nhl_over_bet_amount:.2f}, Tracked submarkets: {len(auto_bet_submarkets)}")
    
    return jsonify({
        'success': True,
        'enabled': auto_bet_enabled,
        'ev_min': auto_bet_ev_min,
        'ev_max': auto_bet_ev_max,
        'odds_min': auto_bet_odds_min,
        'odds_max': auto_bet_odds_max,
        'amount': auto_bet_amount,
        'nhl_over_amount': nhl_over_bet_amount,
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
                    response_text = """🤖 <b>Polymarket Auto-Bet Bot Commands</b>

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
        if filter_name in bookiebeats_monitors:
            bookiebeats_monitors[filter_name].running = False
            del bookiebeats_monitors[filter_name]
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
    for filter_name, monitor in list(bookiebeats_monitors.items()):
        if filter_name not in all_selected:
            print(f"Stopping monitor for deselected filter: {filter_name}")
            monitor.running = False
            monitors_to_stop.append(filter_name)
    
    # Remove stopped monitors from dict
    for filter_name in monitors_to_stop:
        if filter_name in bookiebeats_monitors:
            del bookiebeats_monitors[filter_name]
    
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
    
    if not polymarket_client:
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
                    polymarket_client.get_market_by_ticker(ticker),
                    loop
                )
                market_data = future.result(timeout=2)
            else:
                market_data = loop.run_until_complete(polymarket_client.get_market_by_ticker(ticker))
        
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
    
    if not ticker or not side:
        print(f"[BET] ERROR: Invalid alert data - ticker={ticker}, side={side}")
        return jsonify({'error': 'Invalid alert data'}), 400
    
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
        contracts = market_matcher_polymarket.calculate_max_contracts(max_bet_dollars, expected_price_cents or 50)
        print(f"[BET] BET MAX mode: max_bet_dollars=${max_bet_dollars:.2f}, contracts={contracts}")
    else:
        if bet_amount <= 0:
            print(f"[BET] ERROR: Invalid bet amount: ${bet_amount:.2f}")
            return jsonify({'error': 'Invalid bet amount'}), 400
        contracts = market_matcher_polymarket.calculate_contracts_from_dollars(bet_amount, expected_price_cents or 50)
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
            polymarket_client.place_order(
                ticker=ticker,
                side=side,
                count=contracts,
                validate_odds=True,
                expected_price_cents=expected_price_cents,
                max_liquidity_dollars=max_bet_dollars if bet_max else None
            ),
            loop
        )
        result = future.result(timeout=10)  # 10 second timeout
    else:
        # Loop not running - use run_until_complete
        result = loop.run_until_complete(polymarket_client.place_order(
            ticker=ticker,
            side=side,
            count=contracts,
            validate_odds=True,
            expected_price_cents=expected_price_cents,
            max_liquidity_dollars=max_bet_dollars if bet_max else None
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
            total_cost = result.get('total_cost_cents', 0) / 100.0
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
            cost = total_cost_cents / 100.0  # Use actual total cost from Polymarket (includes fees)
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
    if not ticker or not polymarket_client:
        return
    
    loop = get_or_create_event_loop()
    orderbook = loop.run_until_complete(polymarket_client.fetch_orderbook(ticker))
    
    if orderbook:
        emit('orderbook_update', {
            'ticker': ticker,
            'orderbook': orderbook
        })


async def load_existing_positions_to_tracking():
    """Load existing positions from Polymarket and FULLY populate all tracking structures
    This is CRITICAL - we must know all positions before auto-betting starts to prevent:
    - Duplicate bets
    - Reverse middles
    - Over-betting (3+ of same pick direction)
    """
    global auto_bet_submarkets, auto_bet_submarket_data, auto_bet_games, polymarket_client, positions_loaded
    
    if not polymarket_client:
        print("[AUTO-BET] ERROR: Kalshi client not initialized, cannot load positions")
        return
    
    try:
        positions = await polymarket_client.get_positions()
        
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
                market_data = await polymarket_client.get_market_by_ticker(ticker)
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
                    event_data = await polymarket_client.get_event_by_ticker(event_ticker)
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
    global polymarket_client, market_matcher_polymarket, bookiebeats_monitor, telegram_bot_token, telegram_chat_id, bookiebeats_monitors
    
    # Initialize Polymarket client
    polymarket_client = PolymarketClient()
    
    # Initialize market matcher (team mappings are now static in market_matcher_polymarket.py)
    # Run abbreviation_finder.py periodically to update mappings
    market_matcher_polymarket = MarketMatcherPolymarket(polymarket_client)
    
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
    
    # Initialize BookieBeats API monitor (MUCH FASTER than DOM scraping!)
    # Try to get auth token from environment
    auth_token = os.getenv("BOOKIEBEATS_TOKEN")
    if not auth_token:
        print("Warning: No BOOKIEBEATS_TOKEN in .env")
        print("   Extracting token automatically...")
        # Try to extract token automatically
        try:
            import subprocess
            result = subprocess.run(
                [sys.executable, "extract_bookiebeats_token.py"],
                input="\n",  # Send Enter to skip if no token provided
                capture_output=True,
                text=True,
                timeout=60
            )
            # Reload .env
            load_dotenv(override=True)
            auth_token = os.getenv("BOOKIEBEATS_TOKEN")
        except Exception as e:
            print(f"Warning: Auto-extraction failed: {e}")
            print("   Run manually: python extract_bookiebeats_token.py")
            auth_token = os.getenv("BOOKIEBEATS_TOKEN")
    
    if auth_token:
        print(f"Using BookieBeats auth token (first 20 chars: {auth_token[:20]}...)")
    
    # Initialize monitors for all selected dashboard filters
    for filter_name in selected_dashboard_filters:
        if filter_name not in bookiebeats_monitors:
            filter_payload = saved_filters.get(filter_name)
            if filter_payload:
                monitor = BookieBeatsAPIMonitorPolymarket(auth_token=auth_token)
                monitor.set_filter(filter_payload)
                monitor.poll_interval = 0.5  # 0.5 second refresh as requested
                bookiebeats_monitors[filter_name] = monitor
                print(f"Initialized monitor for filter: {filter_name}")
    
    # Set legacy monitor to first selected filter (for backward compatibility)
    if selected_dashboard_filters and selected_dashboard_filters[0] in bookiebeats_monitors:
        bookiebeats_monitor = bookiebeats_monitors[selected_dashboard_filters[0]]
    elif bookiebeats_monitors:
        # Fallback to first monitor if no dashboard filters selected
        bookiebeats_monitor = list(bookiebeats_monitors.values())[0]
    else:
        # Fallback to default monitor
        bookiebeats_monitor = BookieBeatsAPIMonitorPolymarket(auth_token=auth_token)
        bookiebeats_monitor.set_filter(DEFAULT_FILTER_PAYLOAD)
        bookiebeats_monitor.poll_interval = 0.5
    
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
                    
                    # Recalculate EV if we have fair odds from BookieBeats
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
        
        polymarket_client.ws_callback = ws_update_callback
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
            This processes YOUR ACTUAL POSITIONS (non-zero holdings) from Polymarket WebSocket
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
            # Polymarket sends ALL positions including zeros, but we only care about actual holdings
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
                    market_data = await polymarket_client.get_market_by_ticker(ticker)
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
                        event_data = await polymarket_client.get_event_by_ticker(event_ticker)
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
        
        polymarket_client.ws_positions_callback = ws_positions_callback
        print(f"[WS] ✅ Position callback registered - real-time position updates will be logged immediately")
        
        # NOW connect WebSocket (callbacks are set, so connection handler will see them)
        print(f"[WS] Connecting WebSocket...")
        await polymarket_client.connect_ws()
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
                    # Extract token from command (format: /token <token> or token <token>)
                    token_parts = text.split(' ', 1)
                    if len(token_parts) == 2:
                        new_token = token_parts[1].strip()
                        if new_token:
                            # Update token in monitor
                            global bookiebeats_monitor
                            if bookiebeats_monitor:
                                bookiebeats_monitor.update_token(new_token)
                            
                            # Update .env file
                            try:
                                env_file = os.path.join(os.path.dirname(__file__), '.env')
                                env_lines = []
                                token_updated = False
                                
                                # Read existing .env file
                                if os.path.exists(env_file):
                                    with open(env_file, 'r', encoding='utf-8') as f:
                                        env_lines = f.readlines()
                                
                                # Update or add BOOKIEBEATS_TOKEN
                                for i, line in enumerate(env_lines):
                                    if line.strip().startswith('BOOKIEBEATS_TOKEN='):
                                        env_lines[i] = f'BOOKIEBEATS_TOKEN={new_token}\n'
                                        token_updated = True
                                        break
                                
                                if not token_updated:
                                    env_lines.append(f'BOOKIEBEATS_TOKEN={new_token}\n')
                                
                                # Write back to .env
                                with open(env_file, 'w', encoding='utf-8') as f:
                                    f.writelines(env_lines)
                                
                                # Update environment variable
                                os.environ['BOOKIEBEATS_TOKEN'] = new_token
                                
                                response_text = f"✅ Token updated successfully!\n\nFirst 20 chars: {new_token[:20]}..."
                                print(f"[TELEGRAM] Token updated via Telegram (first 20 chars: {new_token[:20]}...)")
                            except Exception as e:
                                response_text = f"❌ Error updating token: {str(e)}"
                                print(f"[TELEGRAM] Error updating token: {e}")
                        else:
                            response_text = "❌ Invalid token format. Use: /token <your_token>"
                    else:
                        response_text = "❌ Invalid format. Use: /token <your_token>\n\nExample: /token eyJhbGciOiJSUzI1NiIs..."
                    
                    keyboard = {
                        'inline_keyboard': [[
                            {'text': '📊 Status', 'callback_data': 'status'}
                        ]]
                    }
                
                elif text.lower() in ['/help', 'help']:
                    response_text = """🤖 <b>Polymarket Auto-Bet Bot Commands</b>

<b>Text Commands (just type these):</b>
/start or "start" - Start auto-betting
/stop or "stop" - Stop auto-betting
/status or "status" - Check current status
/stats or "stats" - View auto-bet statistics
/token <token> - Update BookieBeats bearer token
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
    
    print("Dashboard initialized")


def start_monitor():
    """Start the BookieBeats monitor in background thread"""
    global monitor_thread
    
    if monitor_thread and monitor_thread.is_alive():
        return
    
    monitor_thread = threading.Thread(target=run_monitor_loop, daemon=True)
    monitor_thread.start()
    print("BookieBeats monitor started")


# Track if shutdown notification was sent (prevent duplicates)
_shutdown_notification_sent = False

def send_shutdown_notification():
    """Send Telegram notification when bot shuts down (only once)"""
    global _shutdown_notification_sent
    
    if _shutdown_notification_sent:
        return  # Already sent, don't send again
    
    _shutdown_notification_sent = True
    
    try:
        send_telegram_message("🛑 <b>BOT STOPPED</b>\n\nPolymarket betting bot has been shut down.")
    except Exception as e:
        print(f"[TELEGRAM] Could not send shutdown notification: {e}")


if __name__ == '__main__':
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
    
    print("Starting Polymarket dashboard server on http://localhost:5001")
    try:
        socketio.run(app, host='0.0.0.0', port=5001, debug=True, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        shutdown_handler()
    finally:
        send_shutdown_notification()

