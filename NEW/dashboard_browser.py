"""
Standalone Dashboard with Browser Reader
Uses Browser Reader instead of API calls - completely undetectable!

This is a complete standalone version that doesn't break the old dashboard.
Run this instead of dashboard.py when you want to use Browser Reader.
"""
import asyncio
import json
import os
import sys
import csv
import io
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import defaultdict
from flask import Flask, render_template, jsonify, request, send_from_directory, Response, session, redirect
from flask_socketio import SocketIO, emit
from functools import wraps
import threading
from dotenv import load_dotenv
import base64
import warnings

# Fix Unicode encoding for Windows console
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

warnings.filterwarnings('ignore', category=RuntimeWarning, message='.*Future exception was never retrieved.*')
warnings.filterwarnings('ignore', message='.*APPLICATION_DATA_AFTER_CLOSE_NOTIFY.*')
warnings.filterwarnings('ignore', message='.*SSL.*')

# Import from parent directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bookiebeats_monitor import BookieBeatsAlert
from market_matcher import MarketMatcher
from kalshi_client import KalshiClient

# Import Browser Reader from NEW folder (relative import)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from browser_reader.monitor import BookieBeatsBrowserReader

# Load environment variables
load_dotenv()

INITIAL_DEPOSIT_DOLLARS = float(os.getenv('INITIAL_DEPOSIT', '980.0'))

app = Flask(__name__, static_folder='../static', static_url_path='', template_folder='../templates')
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'kalshi-live-betting-secret-key-change-in-production')
app.config['SESSION_COOKIE_SECURE'] = False

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Global state
kalshi_client = None
market_matcher = None
bookiebeats_monitor = None
bookiebeats_monitors = {}  # Dict of filter_name -> monitor
active_alerts = {}  # alert_id -> alert_data
monitor_loop = None
monitor_running = False
monitor_loop_thread = None

# Browser Reader configuration
CDP_ENDPOINTS = os.getenv('CDP_ENDPOINTS', 'http://localhost:9222').split(',')
CDP_ENDPOINTS = [e.strip() for e in CDP_ENDPOINTS]

print("=" * 60)
print("🚀 STARTING DASHBOARD WITH BROWSER READER")
print("=" * 60)
print(f"📱 CDP Endpoints: {CDP_ENDPOINTS}")
print(f"💡 Make sure Chrome is running with: chrome.exe --remote-debugging-port=9222")
print("=" * 60)


def run_monitor_loop():
    """Run the monitor loop in a separate thread"""
    global monitor_running, monitor_loop
    
    print("[MONITOR THREAD] run_monitor_loop() entered")
    sys.stderr.flush()
    
    async def async_monitor():
        global monitor_loop, monitor_running, kalshi_client, market_matcher, bookiebeats_monitors
        
        # Initialize the auto-bet lock
        global auto_bet_lock
        auto_bet_lock = asyncio.Lock()
        
        monitor_running = True
        print(f"[MONITOR] monitor_running set to True")
        
        # Initialize Kalshi client
        print("[MONITOR] Initializing Kalshi client session...")
        await kalshi_client.init()
        print("[MONITOR] Kalshi client session initialized")
        
        monitor_loop = asyncio.get_running_loop()
        
        # Initialize market matcher
        market_matcher = MarketMatcher(kalshi_client)
        
        # Create Browser Reader monitor
        print(f"[MONITOR] Creating Browser Reader monitor...")
        print(f"[MONITOR] Connecting to CDP endpoints: {CDP_ENDPOINTS}")
        
        monitor = BookieBeatsBrowserReader(
            cdp_endpoints=CDP_ENDPOINTS,
            poll_interval=0.5  # Match BookieBeats rate
        )
        
        # Set up callbacks
        async def handle_new_alert(alert: BookieBeatsAlert):
            """Handle new alert from Browser Reader"""
            print(f"[ALERT] New alert: {alert.teams} - {alert.pick} ({alert.ev_percent}% EV)")
            
            # Generate alert ID
            alert_id = hash(f"{alert.teams}_{alert.pick}_{alert.qualifier}")
            
            # Store alert
            active_alerts[alert_id] = {
                'alert': alert,
                'timestamp': datetime.now(),
                'matched': False,
                'ticker': None,
                'side': None
            }
            
            # Try to match to Kalshi market
            try:
                # Extract event ticker from alert if available
                event_ticker = None
                if alert.ticker:
                    # Extract event part (e.g., KXNCAAMBGAME-26FEB12MEMUNT from full ticker)
                    parts = alert.ticker.split('-')
                    if len(parts) >= 3:
                        event_ticker = '-'.join(parts[:3])
                
                if event_ticker:
                    result = await market_matcher.find_submarket(
                        event_ticker=event_ticker,
                        market_type=alert.market_type,
                        line=float(alert.qualifier) if alert.qualifier and alert.qualifier.replace('.', '').replace('-', '').isdigit() else None,
                        selection=alert.pick,
                        teams_str=alert.teams
                    )
                    
                    if result and result.get('ticker') and result.get('side'):
                        active_alerts[alert_id]['matched'] = True
                        active_alerts[alert_id]['ticker'] = result['ticker']
                        active_alerts[alert_id]['side'] = result['side']
                        print(f"[ALERT] ✅ Matched: {result['ticker']} {result['side']}")
            
            except Exception as e:
                print(f"[ALERT] ⚠️  Error matching alert: {e}")
            
            # Emit to frontend
            socketio.emit('new_alert', {
                'alert_id': alert_id,
                'teams': alert.teams,
                'pick': alert.pick,
                'qualifier': alert.qualifier,
                'ev_percent': alert.ev_percent,
                'expected_profit': alert.expected_profit,
                'odds': alert.odds,
                'liquidity': alert.liquidity,
                'market_type': alert.market_type,
                'ticker': active_alerts[alert_id].get('ticker'),
                'side': active_alerts[alert_id].get('side'),
                'matched': active_alerts[alert_id].get('matched', False)
            })
        
        monitor.add_alert_callback(handle_new_alert)
        
        # Start monitor
        print("[MONITOR] Starting Browser Reader monitor...")
        if await monitor.start():
            print("[MONITOR] ✅ Browser Reader monitor started successfully")
            bookiebeats_monitors['Browser Reader'] = monitor
            bookiebeats_monitor = monitor
            
            # Run monitor loop
            await monitor.monitor_loop()
        else:
            print("[MONITOR] ❌ Failed to start Browser Reader monitor")
            print("[MONITOR] 💡 Make sure Chrome is running with remote debugging enabled")
    
    # Create new event loop for monitor thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(async_monitor())


@app.route('/')
def index():
    """Main dashboard page"""
    return render_template('dashboard.html')


@app.route('/api/alerts')
def get_alerts():
    """Get all active alerts"""
    alerts_list = []
    for alert_id, alert_data in active_alerts.items():
        alert = alert_data['alert']
        alerts_list.append({
            'alert_id': alert_id,
            'teams': alert.teams,
            'pick': alert.pick,
            'qualifier': alert.qualifier,
            'ev_percent': alert.ev_percent,
            'expected_profit': alert.expected_profit,
            'odds': alert.odds,
            'liquidity': alert.liquidity,
            'market_type': alert.market_type,
            'ticker': alert_data.get('ticker'),
            'side': alert_data.get('side'),
            'matched': alert_data.get('matched', False),
            'timestamp': alert_data['timestamp'].isoformat()
        })
    return jsonify(alerts_list)


@app.route('/api/status')
def get_status():
    """Get monitor status"""
    return jsonify({
        'monitor_running': monitor_running,
        'monitors_connected': len(bookiebeats_monitors),
        'active_alerts': len(active_alerts),
        'cdp_endpoints': CDP_ENDPOINTS
    })


@socketio.on('connect')
def handle_connect():
    """Handle WebSocket connection"""
    print(f"Client connected: {request.sid}")
    emit('status', {'status': 'connected', 'monitor_running': monitor_running})


@socketio.on('disconnect')
def handle_disconnect():
    """Handle WebSocket disconnection"""
    print(f"Client disconnected: {request.sid}")


def initialize_dashboard():
    """Initialize dashboard components"""
    global kalshi_client, monitor_loop_thread
    
    # Initialize Kalshi client
    kalshi_client = KalshiClient()
    
    # Start monitor loop thread
    if monitor_loop_thread is None or not monitor_loop_thread.is_alive():
        monitor_loop_thread = threading.Thread(target=run_monitor_loop, daemon=True)
        monitor_loop_thread.start()
        print("[INIT] Monitor loop thread started")
    
    print("[INIT] Dashboard initialized with Browser Reader")


if __name__ == '__main__':
    initialize_dashboard()
    
    port = int(os.getenv('PORT', 5000))
    print(f"\n🌐 Starting dashboard on http://localhost:{port}")
    print(f"📱 Browser Reader will connect to: {CDP_ENDPOINTS}")
    print(f"💡 Make sure Chrome is running with remote debugging enabled!\n")
    
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
