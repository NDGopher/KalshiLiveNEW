"""
BookieBeats OCR Reader - ULTIMATE SAFETY
Reads what's on your screen - NO browser interaction at all!

This is the SAFEST possible method:
- No API calls
- No browser automation
- No network traffic
- Just reads pixels from your screen
- Completely undetectable
"""
import asyncio
import re
from datetime import datetime
from typing import Dict, List, Callable, Optional
import json
import sys
import os

# OCR libraries
try:
    import pytesseract
    from PIL import Image
    import mss  # Fast screenshot library
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False
    print("⚠️  OCR libraries not installed. Run: pip install pytesseract pillow mss")

# Import BookieBeatsAlert from parent
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from bookiebeats_monitor import BookieBeatsAlert


class BookieBeatsOCRReader:
    """
    ULTIMATE SAFETY: Reads screen with OCR
    No browser interaction - just reads pixels!
    """
    
    def __init__(self, screen_region: Dict = None, poll_interval: float = 0.5):
        """
        Args:
            screen_region: Dict with 'left', 'top', 'width', 'height' for BookieBeats window
                          If None, will try to auto-detect or use full screen
            poll_interval: How often to take screenshots (default 0.5s)
        """
        if not OCR_AVAILABLE:
            raise ImportError("OCR libraries not installed. Run: pip install pytesseract pillow mss")
        
        self.screen_region = screen_region or {'left': 0, 'top': 0, 'width': 1920, 'height': 1080}
        self.poll_interval = poll_interval
        self.running = False
        self._seen_alerts = set()
        self.alert_callbacks: List[Callable] = []
        self.removed_alert_callbacks: List[Callable] = []
        self.updated_alert_callbacks: List[Callable] = []
        self.last_check_time = None
        self.last_poll_time = None
        self.sct = mss.mss()  # Screenshot capture tool
        
        # OCR configuration for better accuracy
        self.ocr_config = '--psm 6 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz@.+-%$(), '
    
    def add_alert_callback(self, callback: Callable):
        """Add callback for new alerts"""
        self.alert_callbacks.append(callback)
    
    def add_removed_alert_callback(self, callback: Callable):
        """Add callback for removed alerts"""
        self.removed_alert_callbacks.append(callback)
    
    def add_updated_alert_callback(self, callback: Callable):
        """Add callback for updated alerts"""
        self.updated_alert_callbacks.append(callback)
    
    def capture_screen(self) -> Optional[Image.Image]:
        """Capture screenshot of BookieBeats window"""
        try:
            # Capture screen region
            screenshot = self.sct.grab(self.screen_region)
            # Convert to PIL Image
            img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
            return img
        except Exception as e:
            print(f"   ⚠️  Error capturing screen: {e}")
            return None
    
    def extract_text_from_image(self, img: Image.Image) -> str:
        """Extract text from image using OCR"""
        try:
            # Preprocess image for better OCR
            # Convert to grayscale
            if img.mode != 'L':
                img = img.convert('L')
            
            # Enhance contrast
            from PIL import ImageEnhance
            enhancer = ImageEnhance.Contrast(img)
            img = enhancer.enhance(2.0)  # Increase contrast
            
            # Run OCR
            text = pytesseract.image_to_string(img, config=self.ocr_config)
            return text
        except Exception as e:
            print(f"   ⚠️  Error extracting text: {e}")
            return ""
    
    def parse_alerts_from_text(self, text: str) -> List[BookieBeatsAlert]:
        """Parse alerts from OCR text"""
        alerts = []
        
        try:
            # Split text into lines
            lines = text.split('\n')
            
            # Look for alert patterns
            # Format: "Team A @ Team B | Pick | EV% | $Profit | Odds"
            # Or similar patterns based on BookieBeats display
            
            current_alert = {}
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                # Look for team pattern (e.g., "Memphis @ North Texas")
                team_match = re.search(r'([A-Za-z\s]+)\s*@\s*([A-Za-z\s]+)', line)
                if team_match:
                    away_team = team_match.group(1).strip()
                    home_team = team_match.group(2).strip()
                    current_alert['teams'] = f"{away_team} @ {home_team}"
                
                # Look for EV percentage (e.g., "10.24%")
                ev_match = re.search(r'(\d+\.?\d*)\s*%', line)
                if ev_match:
                    try:
                        current_alert['ev_percent'] = float(ev_match.group(1))
                    except:
                        pass
                
                # Look for profit (e.g., "$2.76")
                profit_match = re.search(r'\$\s*(\d+\.?\d*)', line)
                if profit_match:
                    try:
                        current_alert['expected_profit'] = float(profit_match.group(1))
                    except:
                        pass
                
                # Look for odds (e.g., "+124" or "-142")
                odds_match = re.search(r'([+-]\d+)', line)
                if odds_match:
                    current_alert['odds'] = odds_match.group(1)
                
                # Look for pick (team name or Over/Under)
                # This is trickier - might need to match against known team names
                if 'Over' in line or 'Under' in line:
                    current_alert['pick'] = 'Over' if 'Over' in line else 'Under'
                    # Look for line number
                    line_match = re.search(r'(\d+\.?\d*)', line)
                    if line_match:
                        current_alert['qualifier'] = line_match.group(1)
                
                # If we have enough data, create alert
                if 'teams' in current_alert and 'ev_percent' in current_alert:
                    alert_data = {
                        'market_type': current_alert.get('market_type', ''),
                        'teams': current_alert['teams'],
                        'ev_percent': current_alert.get('ev_percent', 0.0),
                        'expected_profit': current_alert.get('expected_profit', 0.0),
                        'pick': current_alert.get('pick', ''),
                        'qualifier': current_alert.get('qualifier', ''),
                        'odds': current_alert.get('odds', ''),
                        'liquidity': current_alert.get('liquidity', 0.0),
                        'book_price': current_alert.get('book_price', ''),
                        'fair_odds': current_alert.get('fair_odds', ''),
                        'market_url': current_alert.get('market_url', ''),
                        'ticker': current_alert.get('ticker', ''),
                        'raw_html': ''
                    }
                    
                    alert = BookieBeatsAlert(alert_data)
                    alerts.append(alert)
                    current_alert = {}  # Reset for next alert
            
        except Exception as e:
            print(f"   ⚠️  Error parsing alerts from text: {e}")
        
        return alerts
    
    async def check_for_new_alerts(self):
        """Capture screen, extract text, parse alerts"""
        # Capture screenshot
        img = self.capture_screen()
        if not img:
            return
        
        # Extract text with OCR
        text = self.extract_text_from_image(img)
        if not text:
            return
        
        # Parse alerts from text
        all_alerts = self.parse_alerts_from_text(text)
        current_hashes = set()
        
        # Check for new alerts
        new_alerts = []
        for alert in all_alerts:
            if alert.teams:  # Only valid alerts
                alert_hash = hash(f"{alert.teams}_{alert.pick}_{alert.qualifier}")
                current_hashes.add(alert_hash)
                
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
            print(f"🔔 Found {len(new_alerts)} new alert(s) via OCR")
        
        self.last_check_time = datetime.now()
        self.last_poll_time = datetime.now()
    
    async def monitor_loop(self):
        """Main monitoring loop"""
        print("👀 Starting BookieBeats OCR reader...")
        print(f"   Reading screen region: {self.screen_region}")
        print(f"   Polling every {self.poll_interval} seconds")
        print(f"   💡 Make sure BookieBeats is visible on screen!")
        
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
        return True
    
    async def stop(self):
        """Stop monitoring"""
        self.running = False
        print("🛑 Stopped OCR monitoring")


# For compatibility
BookieBeatsOCRMonitor = BookieBeatsOCRReader
