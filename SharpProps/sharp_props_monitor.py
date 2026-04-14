"""
Sharp Props Monitor
Monitors BookieBeats lowHold API for sharp player props (pregame)
"""
import asyncio
import json
from datetime import datetime
from typing import Dict, List, Callable, Optional
import aiohttp
import sys
import os

# Add parent directory to path to import bookiebeats_monitor
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bookiebeats_monitor import BookieBeatsAlert


class SharpPropsMonitor:
    """Monitors BookieBeats lowHold API for sharp player props"""
    
    def __init__(self, api_url: str = "https://live.api.bookiebeats.com/v1/tools/lowHold", auth_token: Optional[str] = None):
        self.api_url = api_url
        self.auth_token = auth_token
        self.running = False
        self._current_alerts = {}  # alert_hash -> alert_data
        self.alert_callbacks: List[Callable] = []
        self.poll_interval = 2.0  # Poll every 2 seconds (pregame, no need for speed - saves server load)
        self.last_check_time = None
        self.session: Optional[aiohttp.ClientSession] = None
        self._token_error_count = 0
        
        # Sharp Props filter payload
        self.filter_payload = {
            "state": "ND",
            "bettingBooks": ["BetOnline", "BookMaker", "Kalshi", "Novig", "ProphetX", "SportTrade"],
            "mustIncludeBooks": ["BetOnline", "BookMaker"],
            "displayBooks": ["BetOnline", "BookMaker", "Kalshi", "Novig", "ProphetX", "SportTrade", "Kalshi", "Novig", "ProphetX", "Polymarket", "SportTrade"],
            "leagues": ["SOCCER_ALL", "TENNIS_ALL", "BASKETBALL_ALL", "FOOTBALL_ALL", "HOCKEY_ALL", "BASEBALL_ALL", "UFC_ALL"],
            "betTypes": ["PLAYER_PROPS"],
            "minRoi": 0.1,
            "middleStatus": "INCLUDE",
            "middleFilters": [{"sport": "Any", "minHold": 0.1, "minMiddle": 0}],
            "sortOrder": "ROI",
            "expectedValueFilter": {
                "sharps": ["Kalshi", "Novig", "ProphetX", "Polymarket", "SportTrade"],
                "method": "WORST_CASE",
                "type": "AVERAGE",
                "minSharpBooks": 1
            },
            "oddsRanges": [{"book": "Any", "min": -9007199254740991, "max": 1000}],
            "minLimits": [
                {"book": "Kalshi", "min": 1000},
                {"book": "Novig", "min": 1000},
                {"book": "ProphetX", "min": 1000},
                {"book": "SportTrade", "min": 1000}
            ],
            "linkType": "MOBILE_BETSLIP"
        }
    
    def set_filter(self, filter_payload: Dict):
        """Update the filter payload"""
        self.filter_payload = filter_payload
    
    def update_token(self, new_token: str):
        """Update the bearer token"""
        self.auth_token = new_token
        self._token_error_count = 0
        print("[SHARP PROPS] Token updated")
    
    def add_alert_callback(self, callback: Callable):
        """Add a callback function to be called when alerts change"""
        self.alert_callbacks.append(callback)
    
    def parse_bet_to_alert(self, bet: Dict, event: Dict) -> Optional[BookieBeatsAlert]:
        """Convert API bet data to BookieBeatsAlert"""
        try:
            market_type = bet.get('market', '')
            home_team = event.get('homeTeam', '')
            away_team = event.get('awayTeam', '')
            teams = f"{away_team} @ {home_team}" if away_team and home_team else ""
            
            selection = bet.get('selection', '')
            line = bet.get('line')
            qualifier = None
            if line is not None:
                if line > 0:
                    qualifier = f"{line:.1f}"
                else:
                    qualifier = f"{line:.1f}"
            
            odds_american = bet.get('odds')
            odds_str = None
            if odds_american is not None:
                if odds_american > 0:
                    odds_str = f"+{odds_american}"
                else:
                    odds_str = str(odds_american)
            
            price_cents = bet.get('price')
            ev_percent = bet.get('ev', 0.0)
            roi_percent = bet.get('roi', 0.0)
            
            # Extract market URL (Kalshi link)
            market_url = None
            for book_data in bet.get('books', []):
                if book_data.get('book') == 'Kalshi':
                    market_url = book_data.get('link')
                    break
            
            # Create alert
            alert = BookieBeatsAlert(
                teams=teams,
                pick=selection,
                qualifier=qualifier,
                odds=odds_str,
                ev_percent=ev_percent,
                roi_percent=roi_percent,
                market_type=market_type,
                market_url=market_url,
                line=line
            )
            
            return alert
        except Exception as e:
            print(f"[SHARP PROPS] Error parsing bet: {e}")
            return None
    
    async def fetch_alerts(self) -> List[BookieBeatsAlert]:
        """Fetch alerts from the API"""
        if not self.session:
            self.session = aiohttp.ClientSession()
        
        try:
            headers = {
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            }
            
            if self.auth_token:
                headers['Authorization'] = f'Bearer {self.auth_token}'
            
            async with self.session.post(
                self.api_url,
                json=self.filter_payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10.0)
            ) as resp:
                if resp.status == 401:
                    self._token_error_count += 1
                    if self._token_error_count >= 3:
                        print("[SHARP PROPS] ERROR: Authentication failed. Please update token.")
                    return []
                
                if resp.status != 200:
                    print(f"[SHARP PROPS] API error {resp.status}")
                    return []
                
                self._token_error_count = 0
                data = await resp.json()
                
                bets = data.get('bets', [])
                events = {e.get('id'): e for e in data.get('events', [])}
                
                alerts = []
                for bet in bets:
                    event_id = bet.get('event')
                    event = events.get(event_id, {})
                    
                    alert = self.parse_bet_to_alert(bet, event)
                    if alert:
                        alerts.append(alert)
                
                return alerts
        except asyncio.TimeoutError:
            print("[SHARP PROPS] API request timeout")
            return []
        except Exception as e:
            print(f"[SHARP PROPS] Error fetching from API: {e}")
            return []
    
    def create_alert_hash(self, alert: BookieBeatsAlert) -> str:
        """Create a unique hash for an alert"""
        # Use event ticker, pick, qualifier, and odds to create hash
        ticker = alert.ticker or alert.extract_ticker_from_url() or ""
        return f"{ticker}|{alert.pick}|{alert.qualifier}|{alert.odds}"
    
    async def check_for_alerts(self):
        """Check the API for alerts and notify callbacks of changes"""
        alerts = await self.fetch_alerts()
        
        # Create hash set of current alerts
        current_hashes = set()
        current_alerts_by_hash = {}
        
        for alert in alerts:
            alert_hash = self.create_alert_hash(alert)
            current_hashes.add(alert_hash)
            current_alerts_by_hash[alert_hash] = alert
        
        # Find new alerts (in current but not in _current_alerts)
        new_alerts = []
        for alert_hash, alert in current_alerts_by_hash.items():
            if alert_hash not in self._current_alerts:
                new_alerts.append(alert)
        
        # Update _current_alerts
        self._current_alerts = current_alerts_by_hash
        
        # Notify callbacks of current alerts (sorted by EV/ROI)
        if self.alert_callbacks:
            sorted_alerts = sorted(
                list(self._current_alerts.values()),
                key=lambda a: (a.ev_percent if hasattr(a, 'ev_percent') and a.ev_percent else 0) or (a.roi_percent if hasattr(a, 'roi_percent') and a.roi_percent else 0),
                reverse=True
            )
            for callback in self.alert_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(sorted_alerts)
                    else:
                        # Run sync callback in executor
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, callback, sorted_alerts)
                except Exception as e:
                    print(f"[SHARP PROPS] Error in callback: {e}")
                    import traceback
                    traceback.print_exc()
    
    async def start(self):
        """Start monitoring"""
        if self.running:
            return
        
        self.running = True
        print("[SHARP PROPS] Starting monitor...")
        
        if not self.session:
            self.session = aiohttp.ClientSession()
        
        while self.running:
            try:
                await self.check_for_alerts()
                await asyncio.sleep(self.poll_interval)
            except Exception as e:
                print(f"[SHARP PROPS] Error in monitor loop: {e}")
                await asyncio.sleep(self.poll_interval)
    
    async def stop(self):
        """Stop monitoring"""
        self.running = False
        if self.session:
            await self.session.close()
            self.session = None
        print("[SHARP PROPS] Monitor stopped")

