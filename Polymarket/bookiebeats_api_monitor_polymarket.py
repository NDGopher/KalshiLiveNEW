"""
BookieBeats API Monitor for Polymarket
Monitors BookieBeats API for new EV alerts targeting Polymarket
"""
import asyncio
import json
import re
from datetime import datetime
from typing import Dict, List, Callable, Optional
import aiohttp
import sys
import os

# Add parent directory to path to import bookiebeats_monitor
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bookiebeats_monitor import BookieBeatsAlert


class BookieBeatsAPIMonitorPolymarket:
    """Monitors BookieBeats API for new alerts targeting Polymarket"""
    
    def __init__(self, api_url: str = "https://live.api.bookiebeats.com/v1/tools/expectedValue", 
                 auth_token: Optional[str] = None, cookies: Optional[str] = None):
        self.api_url = api_url
        self.auth_token = auth_token
        self.cookies = cookies
        self.running = False
        self._seen_alerts = set()
        self.alert_callbacks: List[Callable] = []
        self.removed_alert_callbacks: List[Callable] = []
        self.updated_alert_callbacks: List[Callable] = []
        self.poll_interval = 0.3
        self.last_check_time = None
        self.session: Optional[aiohttp.ClientSession] = None
        self._empty_poll_count = 0
        self._token_error_count = 0
        
        # Filter payload for Polymarket (change bettingBooks to Polymarket)
        self.filter_payload = {
            "state": "ND",
            "bettingBooks": ["Polymarket"],  # Changed from Kalshi to Polymarket
            "displayBooks": ["Polymarket", "Circa", "SportTrade", "BookMaker", "Pinnacle", "FanDuel"],
            "leagues": ["SOCCER_ALL", "TENNIS_ALL", "BASKETBALL_ALL", "FOOTBALL_ALL", "HOCKEY_ALL", "BASEBALL_ALL", "UFC_ALL"],
            "betTypes": ["GAMELINES", "PLAYER_PROPS"],
            "minRoi": 3,
            "middleStatus": "INCLUDE",
            "middleFilters": [{"sport": "Any", "minHold": 3, "minMiddle": 0}],
            "sortOrder": "ROI",
            "devigFilter": {
                "sharps": ["Circa", "SportTrade", "BookMaker", "Pinnacle", "FanDuel"],
                "method": "WORST_CASE",
                "type": "AVERAGE",
                "minEv": 3,
                "minLimit": 0,
                "minSharpBooks": 2,
                "hold": [{"book": "Any", "max": 8}]
            },
            "oddsRanges": [{"book": "Any", "min": -9007199254740991, "max": 500}],
            "minLimits": [{"book": "Any", "min": 50}],
            "linkType": "DESKTOP_BETSLIP"
        }
    
    def set_filter(self, filter_payload: Dict):
        """Update the filter payload"""
        self.filter_payload = filter_payload
    
    def add_alert_callback(self, callback: Callable):
        """Add a callback function to be called when a new alert is detected"""
        self.alert_callbacks.append(callback)
    
    def extract_market_id_from_link(self, link: str) -> Optional[str]:
        """Extract Polymarket market ID from market link"""
        if not link:
            return None
        
        # Polymarket link format may differ from Kalshi
        # TODO: Verify actual format and extract market ID
        parts = link.rstrip('/').split('/')
        if parts:
            market_id = parts[-1]
            return market_id
        return None
    
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
            limit = bet.get('limit', 0.0)
            
            fair_odds = bet.get('fairOdds')
            fair_odds_str = None
            if fair_odds is not None:
                if fair_odds > 0:
                    fair_odds_str = f"+{int(fair_odds)}"
                else:
                    fair_odds_str = str(int(fair_odds))
            
            link = bet.get('link', '')
            expected_profit = (ev_percent / 100.0) * limit if limit > 0 else 0.0
            book_price = f"{price_cents}¢" if price_cents else ""
            
            display_books = bet.get('displayBooks', {})
            devig_books = bet.get('devigBooks', [])
            
            alert_data = {
                'market_type': market_type,
                'teams': teams,
                'ev_percent': ev_percent,
                'expected_profit': expected_profit,
                'pick': selection,
                'qualifier': qualifier,
                'odds': odds_str,
                'liquidity': limit,
                'book_price': book_price,
                'fair_odds': fair_odds_str,
                'market_url': link,
                'display_books': display_books,
                'devig_books': devig_books,
                'raw_html': json.dumps(bet)
            }
            
            alert = BookieBeatsAlert(alert_data)
            alert.ticker = self.extract_market_id_from_link(link)
            alert.price_cents = price_cents
            alert.line = line
            
            return alert
        
        except Exception as e:
            print(f"⚠️  Error parsing bet: {e}")
            return None
    
    async def fetch_alerts(self) -> List[BookieBeatsAlert]:
        """Fetch alerts from BookieBeats API"""
        if not self.session:
            self.session = aiohttp.ClientSession()
        
        try:
            headers = {
                'Content-Type': 'application/json',
                'Accept': '*/*',
                'Accept-Encoding': 'gzip, deflate, br, zstd',
                'Accept-Language': 'en-US,en;q=0.9',
                'Origin': 'https://www.bookiebeats.com',
                'Referer': 'https://www.bookiebeats.com/',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
            
            if self.auth_token:
                headers['Authorization'] = f'Bearer {self.auth_token}'
            
            cookies_dict = None
            if self.cookies:
                cookies_dict = {}
                for cookie in self.cookies.split(';'):
                    if '=' in cookie:
                        key, value = cookie.strip().split('=', 1)
                        cookies_dict[key] = value
            
            async with self.session.post(
                self.api_url,
                json=self.filter_payload,
                headers=headers,
                cookies=cookies_dict,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 401:
                    self._token_error_count += 1
                    if self._token_error_count >= 3:
                        print("⚠️  Token expired - exiting")
                        import os
                        import sys
                        os._exit(2)
                    return []
                elif resp.status != 200:
                    error_text = await resp.text()
                    print(f"⚠️  API returned status {resp.status}: {error_text[:200]}")
                    return []
                
                self._token_error_count = 0
                
                try:
                    data = await resp.json()
                except:
                    return []
                
                if not data:
                    return []
                
                events = data.get('events', {})
                bets = data.get('bets', [])
                
                if not bets:
                    return []
                
                alerts = []
                for bet in bets:
                    event_id = bet.get('event')
                    event = events.get(event_id, {})
                    
                    alert = self.parse_bet_to_alert(bet, event)
                    if alert:
                        alerts.append(alert)
                
                return alerts
        
        except asyncio.TimeoutError:
            print("⚠️  API request timeout")
            return []
        except Exception as e:
            print(f"❌ Error fetching from API: {e}")
            return []
    
    async def check_for_new_alerts(self):
        """Check the API for alerts"""
        alerts = await self.fetch_alerts()
        
        current_hashes = set()
        current_alerts_by_hash = {}
        
        for alert in alerts:
            alert_hash = f"{alert.ticker}|{alert.pick}|{alert.qualifier}|{alert.odds}"
            current_hashes.add(alert_hash)
            current_alerts_by_hash[alert_hash] = alert
        
        if not alerts:
            self._empty_poll_count += 1
            if self._empty_poll_count >= 3 and self._seen_alerts:
                all_removed = self._seen_alerts.copy()
                self._seen_alerts.clear()
                for callback in self.removed_alert_callbacks:
                    try:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(all_removed)
                        else:
                            callback(all_removed)
                    except Exception as e:
                        print(f"⚠️  Error in removed alert callback: {e}")
                print(f"🗑️  BookieBeats returned empty for {self._empty_poll_count} polls - cleared all {len(all_removed)} alerts")
                self._empty_poll_count = 0
            return
        else:
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
                    print(f"⚠️  Error in removed alert callback: {e}")
        
        new_alerts = []
        updated_alerts = []
        
        if not hasattr(self, '_previous_alert_values'):
            self._previous_alert_values = {}
        
        for alert_hash, alert in current_alerts_by_hash.items():
            if alert_hash not in self._seen_alerts:
                self._seen_alerts.add(alert_hash)
                new_alerts.append(alert)
                self._previous_alert_values[alert_hash] = {
                    'ev_percent': alert.ev_percent,
                    'liquidity': getattr(alert, 'liquidity', 0),
                    'odds': alert.odds
                }
            else:
                prev_values = self._previous_alert_values.get(alert_hash, {})
                ev_changed = abs(prev_values.get('ev_percent', 0) - alert.ev_percent) > 0.01
                liq_changed = abs(prev_values.get('liquidity', 0) - getattr(alert, 'liquidity', 0)) > 0.01
                odds_changed = prev_values.get('odds') != alert.odds
                
                if ev_changed or liq_changed or odds_changed:
                    updated_alerts.append(alert)
                    self._previous_alert_values[alert_hash] = {
                        'ev_percent': alert.ev_percent,
                        'liquidity': getattr(alert, 'liquidity', 0),
                        'odds': alert.odds
                    }
        
        if new_alerts:
            print(f"[BB API] 📨 Emitting {len(new_alerts)} new alert(s) to callbacks")
        for alert in new_alerts:
            print(f"[BB API]   → Alert: {alert.teams} - {alert.pick} ({alert.ev_percent:.2f}% EV)")
            for callback in self.alert_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(alert)
                    else:
                        callback(alert)
                except Exception as e:
                    print(f"⚠️  Error in alert callback: {e}")
        
        for alert in updated_alerts:
            for callback in self.updated_alert_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(alert)
                    else:
                        callback(alert)
                except Exception as e:
                    print(f"⚠️  Error in updated alert callback: {e}")
        
        for removed_hash in removed_hashes:
            self._previous_alert_values.pop(removed_hash, None)
        
        if new_alerts:
            print(f"🔔 Found {len(new_alerts)} new/reappeared alert(s): {[f'{a.teams} - {a.pick}' for a in new_alerts[:3]]}")
        if updated_alerts:
            print(f"🔄 Updated {len(updated_alerts)} alert(s) with new EV/liquidity")
        if removed_hashes:
            print(f"🗑️  {len(removed_hashes)} alert(s) disappeared from BookieBeats")
        
        self.last_check_time = datetime.now()
    
    async def monitor_loop(self):
        """Main monitoring loop"""
        print("👀 Starting BookieBeats API monitoring loop for Polymarket...")
        print(f"   Polling every {self.poll_interval} seconds")
        
        while self.running:
            try:
                await self.check_for_new_alerts()
                await asyncio.sleep(self.poll_interval)
            except Exception as e:
                print(f"❌ Error in monitor loop: {e}")
                await asyncio.sleep(1)
    
    async def start(self):
        """Start monitoring"""
        self.running = True
        self.session = aiohttp.ClientSession()
        print("✅ BookieBeats API monitor started (Polymarket)")
        return True
    
    async def stop(self):
        """Stop monitoring"""
        self.running = False
        if self.session:
            await self.session.close()
            self.session = None
        print("🛑 BookieBeats API monitor stopped")
