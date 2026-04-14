"""
BookieBeats Browser Reader - SAFEST METHOD
Connects to existing browser windows (no automation flags!)
Drop-in replacement for BookieBeatsAPIMonitor

This is a complete implementation that matches the API monitor interface
so it can be used as a direct replacement in dashboard.py
"""
import asyncio
import re
from datetime import datetime
from typing import Dict, List, Callable, Optional
from playwright.async_api import async_playwright, Browser, Page
import json
import sys
import os

# Import BookieBeatsAlert from parent directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bookiebeats_monitor import BookieBeatsAlert


class BookieBeatsBrowserReader:
    """
    SAFEST METHOD: Connects to existing browser instances
    No automation flags - looks like normal browser usage!
    
    Drop-in replacement for BookieBeatsAPIMonitor - same interface
    """
    
    def __init__(self, cdp_endpoints: List[str] = None, poll_interval: float = 0.5):
        """
        Args:
            cdp_endpoints: List of Chrome DevTools Protocol endpoints
                          e.g., ['http://localhost:9222', 'http://localhost:9223']
                          Or None to auto-detect
            poll_interval: Polling interval in seconds (default 0.5 to match BookieBeats)
        """
        self.cdp_endpoints = cdp_endpoints or ['http://localhost:9222']
        self.browsers: List[Browser] = []
        self.pages: List[Page] = []
        self.running = False
        self._seen_alerts = set()  # Track by hash for deduplication
        self.alert_callbacks: List[Callable] = []
        self.removed_alert_callbacks: List[Callable] = []
        self.updated_alert_callbacks: List[Callable] = []
        self.poll_interval = poll_interval  # Match BookieBeats exactly
        self.last_check_time = None
        self.last_poll_time = None
        self.playwright = None
        self._empty_poll_count = 0
        
        # Filter payload (for compatibility with API monitor interface)
        self.filter_payload = None
    
    def set_filter(self, filter_payload: Dict):
        """Set filter payload (for compatibility with API monitor interface)"""
        self.filter_payload = filter_payload
        print(f"   📋 Filter set (browser reader doesn't use payload - reads what's displayed)")
    
    def add_alert_callback(self, callback: Callable):
        """Add callback for new alerts"""
        self.alert_callbacks.append(callback)
    
    def add_removed_alert_callback(self, callback: Callable):
        """Add callback for removed alerts"""
        self.removed_alert_callbacks.append(callback)
    
    def add_updated_alert_callback(self, callback: Callable):
        """Add callback for updated alerts"""
        self.updated_alert_callbacks.append(callback)
    
    async def connect_to_browsers(self):
        """Connect to existing Chrome instances via CDP"""
        self.playwright = await async_playwright().start()
        
        for endpoint in self.cdp_endpoints:
            try:
                print(f"🔌 Connecting to browser at {endpoint}...")
                # Connect to existing browser via CDP
                browser = await self.playwright.chromium.connect_over_cdp(endpoint)
                self.browsers.append(browser)
                
                # Get all pages from this browser
                contexts = browser.contexts
                for context in contexts:
                    pages = context.pages
                    for page in pages:
                        # Only monitor BookieBeats pages
                        url = page.url
                        if 'bookiebeats.com' in url.lower() or 'tools/live' in url.lower():
                            self.pages.append(page)
                            print(f"   ✅ Connected to page: {url[:80]}...")
                
                # If no pages found, create one and navigate
                if not self.pages:
                    print(f"   ⚠️  No BookieBeats pages found, creating new page...")
                    context = await browser.new_context()
                    page = await context.new_page()
                    await page.goto('https://www.bookiebeats.com/tools/live', wait_until='domcontentloaded')
                    self.pages.append(page)
                    print(f"   ✅ Created and monitoring new page")
                    
            except Exception as e:
                print(f"   ❌ Failed to connect to {endpoint}: {e}")
                print(f"   💡 Make sure Chrome is running with: chrome.exe --remote-debugging-port=9222")
        
        if not self.pages:
            raise Exception("No browser pages connected! Make sure Chrome is running with remote debugging enabled.")
        
        print(f"✅ Connected to {len(self.pages)} browser page(s)")
        return True
    
    async def parse_market_row(self, row_element) -> Optional[BookieBeatsAlert]:
        """Parse a market row from DOM element (matches bookiebeats_monitor.py logic)"""
        try:
            # Get the outer HTML for this row
            row_html = await row_element.get_attribute('outerHTML') or ''
            
            # Extract market type (Point Spread, Moneyline, Total Points)
            market_type_elem = await row_element.query_selector('.marketHeader_2AwCj div:first-child > div:first-child')
            market_type = (await market_type_elem.inner_text()).strip() if market_type_elem else ''
            
            # Extract teams
            teams_elem = await row_element.query_selector('.teams_BTQpJ')
            teams = (await teams_elem.inner_text()).strip() if teams_elem else ''
            
            # Extract EV percentage and expected profit
            profit_elem = await row_element.query_selector('.profit_2v_99')
            ev_percent = 0.0
            expected_profit = 0.0
            if profit_elem:
                profit_text = (await profit_elem.inner_text()).strip()
                # Parse "2.91%\n$2.76" format
                lines = profit_text.split('\n')
                if lines:
                    ev_str = lines[0].replace('%', '').strip()
                    try:
                        ev_percent = float(ev_str)
                    except:
                        pass
                if len(lines) > 1:
                    profit_str = lines[1].replace('$', '').strip()
                    try:
                        expected_profit = float(profit_str)
                    except:
                        pass
            
            # Extract pick and qualifier from outcome container
            outcome_container = await row_element.query_selector('.outcomeContainer_1dTd_')
            pick = ''
            qualifier = ''
            if outcome_container:
                # Get competitor name
                competitor_elem = await outcome_container.query_selector('.competitorName_17Adq .competitorNameText_3HWqV')
                if competitor_elem:
                    pick = (await competitor_elem.inner_text()).strip()
                
                # Get qualifier (e.g., "+17.5")
                qualifier_elem = await outcome_container.query_selector('.qualifier_Mc6wy')
                if qualifier_elem:
                    qualifier = (await qualifier_elem.inner_text()).strip()
            
            # Extract odds
            odds_elem = await row_element.query_selector('.info_1BZOX')
            odds = (await odds_elem.inner_text()).strip() if odds_elem else ''
            
            # Extract liquidity, book price, fair odds
            liquidity_elem = await row_element.query_selector('.extraInfo_1kqn- > div:first-child')
            liquidity = 0.0
            if liquidity_elem:
                liq_text = (await liquidity_elem.inner_text()).strip().replace('$', '').replace(',', '')
                try:
                    liquidity = float(liq_text)
                except:
                    pass
            
            book_price_elem = await row_element.query_selector('.bookDisplayPrice_3ugg5')
            book_price = (await book_price_elem.inner_text()).strip() if book_price_elem else ''
            
            fair_odds_elem = await row_element.query_selector('.fairOdds_3pMH-')
            fair_odds = (await fair_odds_elem.inner_text()).strip() if fair_odds_elem else ''
            
            # Extract market URL
            link_elem = await row_element.query_selector('a[href*="kalshi.com"]')
            market_url = ''
            if link_elem:
                market_url = await link_elem.get_attribute('href') or ''
            
            # Create alert
            alert_data = {
                'market_type': market_type,
                'teams': teams,
                'ev_percent': ev_percent,
                'expected_profit': expected_profit,
                'pick': pick,
                'qualifier': qualifier,
                'odds': odds,
                'liquidity': liquidity,
                'book_price': book_price,
                'fair_odds': fair_odds,
                'market_url': market_url,
                'raw_html': row_html
            }
            
            alert = BookieBeatsAlert(alert_data)
            alert.ticker = alert.extract_ticker_from_url()
            
            return alert
            
        except Exception as e:
            print(f"   ⚠️  Error parsing market row: {e}")
            return None
    
    async def check_for_new_alerts(self):
        """Check all connected pages for new alerts"""
        all_alerts = []
        current_hashes = set()
        
        for page in self.pages:
            try:
                # Wait for page to be ready (with timeout)
                try:
                    await page.wait_for_load_state('networkidle', timeout=2000)
                except:
                    pass  # Continue even if networkidle times out
                
                # Find alert rows (same selector as bookiebeats_monitor.py)
                # Look for table rows that contain market data
                rows = await page.query_selector_all('table tbody tr, [class*="marketRow"], [class*="alertRow"]')
                
                for row in rows:
                    alert = await self.parse_market_row(row)
                    if alert and alert.teams:  # Only add if we have teams (valid alert)
                        all_alerts.append(alert)
                        alert_hash = hash(f"{alert.teams}_{alert.pick}_{alert.qualifier}")
                        current_hashes.add(alert_hash)
                        
            except Exception as e:
                print(f"   ⚠️  Error checking page {page.url}: {e}")
                continue
        
        # Check for new alerts
        new_alerts = []
        for alert in all_alerts:
            alert_hash = hash(f"{alert.teams}_{alert.pick}_{alert.qualifier}")
            if alert_hash not in self._seen_alerts:
                self._seen_alerts.add(alert_hash)
                new_alerts.append(alert)
        
        # Check for removed alerts
        removed_hashes = self._seen_alerts - current_hashes
        if removed_hashes:
            self._seen_alerts -= removed_hashes
            for callback in self.removed_alert_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(list(removed_hashes))
                    else:
                        callback(list(removed_hashes))
                except Exception as e:
                    print(f"   ⚠️  Error in removed alert callback: {e}")
        
        # Call callbacks for new alerts
        for alert in new_alerts:
            for callback in self.alert_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(alert)
                    else:
                        callback(alert)
                except Exception as e:
                    print(f"   ⚠️  Error in alert callback: {e}")
        
        if new_alerts:
            print(f"🔔 Found {len(new_alerts)} new alert(s)")
        
        self.last_check_time = datetime.now()
        self.last_poll_time = datetime.now()
        
        # Track empty polls
        if not all_alerts:
            self._empty_poll_count += 1
        else:
            self._empty_poll_count = 0
    
    async def monitor_loop(self):
        """Main monitoring loop"""
        print("👀 Starting BookieBeats browser reader...")
        print(f"   Reading from {len(self.pages)} browser page(s)")
        print(f"   Polling every {self.poll_interval} seconds (matching BookieBeats rate)")
        
        while self.running:
            try:
                await self.check_for_new_alerts()
                await asyncio.sleep(self.poll_interval)
            except Exception as e:
                print(f"❌ Error in monitor loop: {e}")
                await asyncio.sleep(1)
    
    async def start(self):
        """Start monitoring (matches API monitor interface)"""
        await self.connect_to_browsers()
        self.running = True
        return True
    
    async def stop(self):
        """Stop monitoring"""
        self.running = False
        # Don't close browsers - they're user's browsers!
        if self.playwright:
            await self.playwright.stop()
        print("🛑 Stopped monitoring (browsers remain open)")


# For compatibility - can be used as drop-in replacement
BookieBeatsBrowserMonitor = BookieBeatsBrowserReader
