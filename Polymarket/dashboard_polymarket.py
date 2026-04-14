"""
Real-Time Betting Dashboard for Polymarket
Web-based dashboard for instant betting on Polymarket alerts
"""
import asyncio
import json
import os
import sys
import io

# Fix Unicode encoding for Windows console
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from datetime import datetime
from typing import Dict, List, Optional
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
import threading
from dotenv import load_dotenv

# Add parent directory to path to import modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bookiebeats_monitor import BookieBeatsAlert
from Polymarket.bookiebeats_api_monitor_polymarket import BookieBeatsAPIMonitorPolymarket
from Polymarket.polymarket_client import PolymarketClient

# Load environment variables
load_dotenv()

app = Flask(__name__, 
            static_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static'),
            template_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'templates'))
app.config['SECRET_KEY'] = 'polymarket-live-betting-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Global state
polymarket_client: Optional[PolymarketClient] = None
bookiebeats_monitor: Optional[BookieBeatsAPIMonitorPolymarket] = None
active_alerts: Dict[str, Dict] = {}  # alert_id -> alert_data
monitor_thread: Optional[threading.Thread] = None
monitor_loop: Optional[asyncio.AbstractEventLoop] = None
monitor_running = False
user_max_bet_amount = 200.0  # Default max bet amount in dollars
dashboard_min_ev = 0.0  # Minimum EV to show on dashboard

@app.route('/')
def index():
    """Main dashboard page"""
    response = app.make_response(render_template('dashboard_polymarket.html'))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/api/alerts', methods=['GET'])
def get_alerts():
    """Get current alerts"""
    alerts_list = list(active_alerts.values())
    return jsonify({'alerts': alerts_list})

@app.route('/api/portfolio', methods=['GET'])
def get_portfolio():
    """Get portfolio balance"""
    if polymarket_client:
        # TODO: Implement portfolio fetching from Polymarket API
        return jsonify({
            'balance': 0.0,
            'portfolio_value': 0.0,
            'updated_ts': int(datetime.now().timestamp())
        })
    return jsonify({'balance': 0.0, 'portfolio_value': 0.0, 'updated_ts': 0})

@app.route('/api/set_max_bet', methods=['POST'])
def set_max_bet():
    """Set max bet amount"""
    global user_max_bet_amount
    data = request.json
    user_max_bet_amount = float(data.get('amount', 200.0))
    return jsonify({'success': True, 'max_bet': user_max_bet_amount})

@app.route('/api/get_max_bet', methods=['GET'])
def get_max_bet():
    """Get max bet amount"""
    return jsonify({'max_bet': user_max_bet_amount})

@app.route('/api/set_filters', methods=['POST'])
def set_filters():
    """Set dashboard filters"""
    global dashboard_min_ev
    data = request.json
    dashboard_min_ev = float(data.get('min_ev', 0.0))
    return jsonify({'success': True, 'min_ev': dashboard_min_ev})

# Socket.IO event handlers
@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    print(f"[SOCKET] Client connected")
    emit('alerts_update', {'alerts': list(active_alerts.values())})

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    print(f"[SOCKET] Client disconnected")

def handle_new_alert(alert: BookieBeatsAlert):
    """Handle new alert from BookieBeats"""
    alert_id = f"{alert.ticker}|{alert.pick}|{alert.qualifier}|{alert.odds}"
    
    alert_data = {
        'id': alert_id,
        'market_type': alert.market_type,
        'teams': alert.teams,
        'ev_percent': alert.ev_percent,
        'expected_profit': alert.expected_profit,
        'pick': alert.pick,
        'qualifier': alert.qualifier,
        'odds': alert.odds,
        'liquidity': alert.liquidity,
        'book_price': alert.book_price,
        'fair_odds': alert.fair_odds,
        'market_url': alert.market_url,
        'display_books': alert.display_books,
        'devig_books': alert.devig_books,
        'timestamp': datetime.now().isoformat()
    }
    
    active_alerts[alert_id] = alert_data
    
    # Emit to connected clients
    socketio.emit('alerts_update', {'alerts': list(active_alerts.values())})
    print(f"[ALERT] New alert: {alert.teams} - {alert.pick} ({alert.ev_percent:.2f}% EV)")

def handle_removed_alert(alert_hash: str):
    """Handle removed alert"""
    if alert_hash in active_alerts:
        del active_alerts[alert_hash]
        socketio.emit('alerts_update', {'alerts': list(active_alerts.values())})
        print(f"[ALERT] Removed alert: {alert_hash}")

async def monitor_loop():
    """Main monitoring loop"""
    global monitor_running, bookiebeats_monitor, polymarket_client
    
    # Initialize Polymarket client
    print("[POLYMARKET] Initializing client...")
    polymarket_client = PolymarketClient()
    await polymarket_client.init()
    
    # Initialize BookieBeats monitor
    print("[BOOKIEBEATS] Initializing monitor...")
    bookiebeats_monitor = BookieBeatsAPIMonitorPolymarket()
    bookiebeats_monitor.add_alert_callback(handle_new_alert)
    bookiebeats_monitor.add_removed_alert_callback(handle_removed_alert)
    
    await bookiebeats_monitor.start()
    monitor_running = True
    
    print("[MONITOR] Starting monitoring loop...")
    await bookiebeats_monitor.monitor_loop()

def run_monitor():
    """Run monitor in a separate thread"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    global monitor_loop
    monitor_loop = loop
    loop.run_until_complete(monitor_loop())

def start_monitor():
    """Start the monitor thread"""
    global monitor_thread
    if monitor_thread is None or not monitor_thread.is_alive():
        monitor_thread = threading.Thread(target=run_monitor, daemon=True)
        monitor_thread.start()
        print("[MONITOR] Monitor thread started")

def initialize_dashboard():
    """Initialize dashboard components"""
    print("[DASHBOARD] Initializing Polymarket dashboard...")
    print("[DASHBOARD] Dashboard will be available at http://localhost:5001")

if __name__ == '__main__':
    import signal
    
    def shutdown_handler(signum=None, frame=None):
        print("\n[SHUTDOWN] Bot shutting down...")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    
    initialize_dashboard()
    start_monitor()
    
    print("Starting Polymarket dashboard server on http://localhost:5001")
    try:
        socketio.run(app, host='0.0.0.0', port=5001, debug=True, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        shutdown_handler()
