"""
Polymarket API Client
Handles authentication, market fetching, orderbook access, and ORDER PLACEMENT
Supports VPN/Proxy for accessing non-US Polymarket site
"""
import asyncio
import aiohttp
import json
import os
import time
import csv
from datetime import datetime
from typing import Dict, List, Optional
from dotenv import load_dotenv
import websockets

load_dotenv()

# Polymarket API endpoints
POLYMARKET_BASE = "https://clob.polymarket.com"
POLYMARKET_WSS = "wss://clob.polymarket.com/ws"

# VPN/Proxy configuration
POLYMARKET_PROXY = os.getenv('POLYMARKET_PROXY')  # HTTP proxy: http://proxy:port
POLYMARKET_SOCKS5 = os.getenv('POLYMARKET_SOCKS5')  # SOCKS5 proxy: socks5://proxy:port


class PolymarketAuth:
    """Polymarket authentication handler using API keys"""
    
    def __init__(self):
        self.api_key = os.getenv("POLYMARKET_API_KEY")
        self.private_key = os.getenv("POLYMARKET_PRIVATE_KEY")  # For signing orders if required
        
        if not self.api_key:
            print("WARNING: POLYMARKET_API_KEY not set in environment")
        else:
            print(f"Polymarket auth initialized: API Key = {self.api_key[:8]}...")


class PolymarketClient:
    """Client for Polymarket API with order placement"""
    
    def __init__(self):
        self.auth = PolymarketAuth()
        self.session = None
        self.markets = {}  # market_id -> market_data
        self.orderbooks = {}  # market_id -> orderbook_data with timestamp
        self.event_cache = {}  # event_id -> (event_data, timestamp)
        
        # WebSocket state
        self.ws = None
        self.ws_connected = False
        
        # Position tracking (CSV)
        self.positions_file = "Polymarket/positions.csv"
        self.positions = {}  # market_id -> position_data
        
        # Duplicate bet prevention
        self.recent_bets = set()
        self.bet_lock = None
        self.bet_cooldown_seconds = 60
        
        # Proxy configuration
        self.proxy = None
        if POLYMARKET_PROXY:
            self.proxy = POLYMARKET_PROXY
            print(f"Using HTTP proxy: {self.proxy}")
        elif POLYMARKET_SOCKS5:
            self.proxy = POLYMARKET_SOCKS5
            print(f"Using SOCKS5 proxy: {self.proxy}")
    
    async def init(self):
        """Initialize HTTP session with proxy support"""
        current_loop = asyncio.get_event_loop()
        
        if self.bet_lock is None:
            self.bet_lock = asyncio.Lock()
        
        # Create connector with proxy support
        connector = None
        if self.proxy:
            if self.proxy.startswith('socks5://'):
                # SOCKS5 proxy requires aiohttp-socks
                try:
                    from aiohttp_socks import ProxyConnector
                    connector = ProxyConnector.from_url(self.proxy)
                    print(f"[POLYMARKET] Using SOCKS5 proxy: {self.proxy}")
                except ImportError:
                    print("⚠️  aiohttp-socks not installed. Install with: pip install aiohttp-socks")
                    print("   Falling back to HTTP proxy or no proxy")
                    connector = aiohttp.TCPConnector()
            else:
                # HTTP proxy
                connector = aiohttp.TCPConnector()
                print(f"[POLYMARKET] Using HTTP proxy: {self.proxy}")
        else:
            connector = aiohttp.TCPConnector()
            print("[POLYMARKET] No proxy configured - direct connection")
        
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=10)
        )
        
        # Load positions from CSV
        await self.load_positions()
        
        print("✅ Polymarket client initialized")
    
    async def load_positions(self):
        """Load positions from CSV file"""
        if not os.path.exists(self.positions_file):
            # Create CSV with headers
            with open(self.positions_file, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'market_id', 'side', 'shares', 'price', 
                    'cost', 'status', 'order_id', 'teams', 'pick', 'line'
                ])
            print(f"Created positions file: {self.positions_file}")
            return
        
        try:
            with open(self.positions_file, 'r', newline='') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    market_id = row.get('market_id')
                    if market_id:
                        self.positions[market_id] = row
            print(f"Loaded {len(self.positions)} positions from CSV")
        except Exception as e:
            print(f"Error loading positions: {e}")
    
    async def save_position(self, market_id: str, side: str, shares: float, 
                           price: float, cost: float, status: str, 
                           order_id: str, teams: str = "", pick: str = "", line: str = ""):
        """Save position to CSV"""
        try:
            with open(self.positions_file, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.now().isoformat(),
                    market_id,
                    side,
                    shares,
                    price,
                    cost,
                    status,
                    order_id,
                    teams,
                    pick,
                    line
                ])
            
            # Update in-memory cache
            self.positions[market_id] = {
                'market_id': market_id,
                'side': side,
                'shares': shares,
                'price': price,
                'cost': cost,
                'status': status,
                'order_id': order_id,
                'teams': teams,
                'pick': pick,
                'line': line
            }
        except Exception as e:
            print(f"Error saving position: {e}")
    
    async def get_markets(self, event_id: Optional[str] = None) -> List[Dict]:
        """Fetch markets from Polymarket API"""
        if not self.session:
            await self.init()
        
        try:
            url = f"{POLYMARKET_BASE}/markets"
            if event_id:
                url += f"?event_id={event_id}"
            
            headers = {}
            if self.auth.api_key:
                headers['Authorization'] = f'Bearer {self.auth.api_key}'
            
            async with self.session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get('markets', [])
                else:
                    error_text = await resp.text()
                    print(f"Error fetching markets: HTTP {resp.status} - {error_text[:200]}")
                    return []
        except Exception as e:
            print(f"Error fetching markets: {e}")
            return []
    
    async def find_submarket(self, event_id: str, market_type: str, line: float, selection: str) -> Optional[Dict]:
        """Find matching submarket for a BookieBeats alert"""
        # TODO: Implement market matching logic for Polymarket
        # This will need to be adapted from the Kalshi market matcher
        markets = await self.get_markets(event_id)
        
        # Placeholder - will need full implementation
        for market in markets:
            # Match logic here
            pass
        
        return None
    
    async def place_order(self, market_id: str, side: str, shares: float, 
                         price: Optional[float] = None) -> Dict:
        """Place an order on Polymarket"""
        if not self.session:
            await self.init()
        
        # TODO: Implement Polymarket order placement
        # Polymarket uses different order format than Kalshi
        # This is a placeholder structure
        
        try:
            url = f"{POLYMARKET_BASE}/orders"
            
            headers = {
                'Content-Type': 'application/json'
            }
            if self.auth.api_key:
                headers['Authorization'] = f'Bearer {self.auth.api_key}'
            
            # Polymarket order format (needs to be verified with actual API docs)
            order_data = {
                'market_id': market_id,
                'side': side,  # 'yes' or 'no'
                'shares': shares,
                'price': price,  # Price per share (0-1 range)
                'type': 'limit'  # or 'market'
            }
            
            async with self.session.post(url, json=order_data, headers=headers) as resp:
                if resp.status in [200, 201]:
                    result = await resp.json()
                    order_id = result.get('order_id') or result.get('id', 'N/A')
                    
                    # Save to positions CSV
                    await self.save_position(
                        market_id=market_id,
                        side=side,
                        shares=shares,
                        price=price or 0.0,
                        cost=shares * (price or 0.0),
                        status='pending',
                        order_id=order_id
                    )
                    
                    return {
                        "success": True,
                        "order_id": order_id,
                        "status": "pending",
                        "shares": shares,
                        "price": price
                    }
                else:
                    error_text = await resp.text()
                    return {
                        "success": False,
                        "error": f"HTTP {resp.status}: {error_text[:200]}"
                    }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
    
    async def get_portfolio(self) -> Dict:
        """Get portfolio balance"""
        # TODO: Implement portfolio fetching
        # For now, calculate from positions CSV
        total_cost = sum(float(p.get('cost', 0)) for p in self.positions.values())
        return {
            'balance': 0.0,  # Would need to fetch from API
            'portfolio_value': total_cost,
            'updated_ts': int(time.time())
        }
    
    async def get_positions(self) -> List[Dict]:
        """Get current positions from CSV"""
        return list(self.positions.values())
    
    async def close(self):
        """Close session"""
        if self.session:
            await self.session.close()
            self.session = None
