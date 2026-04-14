"""
Main entry point for Kalshi Live Betting System
"""
import asyncio
import sys
from dashboard import initialize_dashboard, start_monitor, socketio, app

if __name__ == '__main__':
    print("=" * 60)
    print("KALSHI LIVE BETTING SYSTEM")
    print("=" * 60)
    print()
    
    # Initialize components
    print("Initializing components...")
    initialize_dashboard()
    
    # Start monitor
    print("Starting Odds-API.io monitor (OddsEVMonitor)...")
    start_monitor()
    
    # Start dashboard server
    print()
    print("Starting dashboard server...")
    print(f"   Dashboard: http://localhost:5000")
    print()
    print("System ready! Waiting for alerts...")
    print("   Press Ctrl+C to stop")
    print()
    
    try:
        # debug=False in production: reloader would restart the process and kill the monitor thread
        # (causing "Task was destroyed but it is pending!" and monitors never reaching "Polling every 0.5s")
        socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        print()
        print("Shutting down...")
        sys.exit(0)

