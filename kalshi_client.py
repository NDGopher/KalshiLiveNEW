"""
Enhanced Kalshi API Client for Live Betting
Handles authentication, market fetching, orderbook access, and ORDER PLACEMENT
"""
import asyncio
import aiohttp
import json
import os
import time
import base64
import re
from datetime import datetime
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
import websockets
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent / ".env", override=True, encoding="utf-8-sig")
load_dotenv(Path.cwd() / ".env", override=True, encoding="utf-8-sig")

KALSHI_BASE = "https://api.elections.kalshi.com"
KALSHI_V1_BASE = "https://api.elections.kalshi.com/v1"  # For current_value endpoint
KALSHI_DEMO_BASE = "https://demo-api.kalshi.com"
KALSHI_WSS = "wss://api.elections.kalshi.com/trade-api/ws/v2"
KALSHI_DEMO_WSS = "wss://demo-api.kalshi.com/trade-api/ws/v2"


class KalshiAuth:
    """Kalshi authentication handler using RSA private key signing"""
    
    def __init__(self):
        self.kid = os.getenv("KALSHI_KEY_ID")
        self.kfile = os.getenv("KALSHI_KEY_FILE", "kalshi.key")
        self.priv = self.load_key()
        
        # Debug: Check if auth is properly loaded
        if not self.priv:
            print(f"WARNING: Kalshi private key not loaded from {self.kfile}")
        if not self.kid:
            print("WARNING: KALSHI_KEY_ID not set in environment")
        if self.priv and self.kid:
            print(f"Kalshi auth initialized: Key ID = {self.kid[:8]}..., Key loaded = Yes")
    
    def load_key(self):
        """Load RSA private key from file"""
        try:
            if not self.kfile or not os.path.exists(self.kfile):
                print(f"Warning: Kalshi key file not found at {self.kfile}")
                print("   Set KALSHI_KEY_FILE environment variable or place key at kalshi.key")
                return None
            
            with open(self.kfile, "rb") as f:
                return serialization.load_pem_private_key(f.read(), password=None)
        except Exception as e:
            print(f"Auth error loading key: {e}")
            return None
    
    def sign(self, method, path, body=None):
        """Sign a request using RSA-PSS
        
        CRITICAL: Kalshi API signature is ALWAYS just timestamp + method + path (NO body)
        This is different from AWS/Coinbase APIs that include body in signature.
        For v2 API: path should be relative (e.g., /portfolio/orders, /events/{ticker})
        """
        path_only = path.split('?')[0]
        ts = str(int(time.time() * 1000))
        
        if not self.priv:
            return ts, "MISSING_KEY"
        
        # CRITICAL FIX: Kalshi signatures NEVER include the body, even for POST/PUT
        # Signature is ALWAYS: timestamp + method + path (body is sent but not signed)
        msg = f"{ts}{method}{path_only}"
        
        # EXTENSIVE LOGGING for POST/PUT requests only (GET requests are too noisy)
        if method in ['POST', 'PUT']:
            print(f"[AUTH] ========== SIGNATURE GENERATION ({method}) ==========")
            print(f"[AUTH] Method: {method}")
            print(f"[AUTH] Path: {path_only}")
            print(f"[AUTH] Timestamp: {ts}")
            if body:
                # Log body for reference, but note it's NOT included in signature
                if isinstance(body, dict):
                    body_str = json.dumps(body, separators=(',', ':'), sort_keys=True)
                    print(f"[AUTH] Body (dict): {body}")
                    print(f"[AUTH] Body (serialized): {body_str}")
                else:
                    body_str = body
                    print(f"[AUTH] Body (string): {body_str}")
                print(f"[AUTH] Body length: {len(body_str)} chars")
                print(f"[AUTH] ⚠️  Body is NOT included in signature (Kalshi requirement)")
            print(f"[AUTH] Message to sign: {msg}")
            print(f"[AUTH] Message length: {len(msg)} chars")
            print(f"[AUTH] Message (bytes): {msg.encode('utf-8')}")
            print(f"[AUTH] Message (hex, first 200): {msg.encode('utf-8').hex()[:200]}")
        
        sig = self.priv.sign(
            msg.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256()
        )
        sig_b64 = base64.b64encode(sig).decode()
        
        if method in ['POST', 'PUT']:
            print(f"[AUTH] Signature (base64, first 100): {sig_b64[:100]}")
            print(f"[AUTH] Signature length: {len(sig_b64)} chars")
            print(f"[AUTH] ==========================================")
        
        return ts, sig_b64


class KalshiClient:
    """Enhanced client for Kalshi API with order placement"""
    
    def __init__(self, demo_mode=False):
        self.auth = KalshiAuth()
        self.session = None
        self.markets = {}  # ticker -> market_data
        self.orderbooks = {}  # ticker -> orderbook_data with timestamp
        # CACHE REMOVED: Always fetch fresh data to avoid stale markets
        self.demo_mode = demo_mode
        self.base_url = KALSHI_DEMO_BASE if demo_mode else KALSHI_BASE
        self.wss_url = KALSHI_DEMO_WSS if demo_mode else KALSHI_WSS
        # WebSocket state
        self.ws = None
        self.ws_callback = None  # Set in dashboard for emits
        self.ws_positions_callback = None  # Callback for position updates
        self.ws_subscriptions = {}  # Track subscription IDs per ticker
        self.ws_connected = False
        self.positions_subscribed = False

        # Warm Cache state - pre-subscribe to orderbooks for fast order execution
        self.warm_cache_enabled = False
        self.warm_cache_tickers = set()  # Tickers we've pre-subscribed to
        self.warm_cache_last_refresh = 0  # Unix timestamp of last refresh
        self.warm_cache_refresh_interval = 300  # Refresh every 5 minutes (new games appear)
        
        # Duplicate bet prevention - track recent bets to prevent double-betting
        self.recent_bets = set()  # Set of (ticker.upper(), side.lower()) tuples
        self.bet_lock = None  # Will be initialized in init() when event loop is available
        
        # Learned team codes - dynamically learned from tickers when one team is known
        self.learned_team_codes = {}  # Dict mapping team_name -> team_code (learned from successful matches)
        self.bet_cooldown_seconds = 60  # Prevent duplicate bets within 60 seconds
    
    async def init(self):
        """Initialize HTTP session with current event loop.
        Always replaces any existing session so the session is bound to the calling loop.
        Required when the monitor thread is restarted (e.g. after token update) so we
        don't reuse a session from a dead loop (would cause 'attached to a different loop').
        """
        # Initialize bet lock if not already initialized
        if self.bet_lock is None:
            self.bet_lock = asyncio.Lock()
        
        # Replace any existing session so we have one for THIS event loop.
        # Do not await session.close() from a different loop (e.g. new monitor thread
        # closing old thread's session) - it raises RuntimeError "attached to a different loop".
        if self.session is not None:
            if not self.session.closed:
                try:
                    await self.session.close()
                except RuntimeError as e:
                    if "different loop" in str(e):
                        pass  # Old session belonged to another loop; abandon it
                    else:
                        raise
            self.session = None
        self.session = aiohttp.ClientSession()
    
    async def close(self):
        """Close HTTP session"""
        if self.session:
            await self.session.close()
            self.session = None
    
    async def search_markets(self, query_params):
        """
        Search for markets with flexible query parameters
        Returns list of matching markets
        """
        if not self.session:
            await self.init()
        
        try:
            path = "/trade-api/v2/markets"
            params = "&".join([f"{k}={v}" for k, v in query_params.items()])
            if params:
                path += f"?{params}"
            
            ts, sig = self.auth.sign("GET", path)
            headers = {
                "KALSHI-ACCESS-KEY": self.auth.kid,
                "KALSHI-ACCESS-SIGNATURE": sig,
                "KALSHI-ACCESS-TIMESTAMP": ts
            }
            
            # Use asyncio.wait_for instead of ClientTimeout to avoid "Timeout context manager should be used inside a task" error
            # when called from run_coroutine_threadsafe
            async def _make_request():
                async with self.session.get(
                    f"{self.base_url}{path}",
                    headers=headers
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get('markets', [])
                    elif resp.status == 429:
                        await asyncio.sleep(2)
                        # Retry once
                        ts, sig = self.auth.sign("GET", path)
                        headers_retry = {
                            "KALSHI-ACCESS-KEY": self.auth.kid,
                            "KALSHI-ACCESS-SIGNATURE": sig,
                            "KALSHI-ACCESS-TIMESTAMP": ts
                        }
                        async with self.session.get(
                            f"{self.base_url}{path}",
                            headers=headers_retry
                        ) as retry_resp:
                            if retry_resp.status == 200:
                                retry_data = await retry_resp.json()
                                return retry_data.get('markets', [])
                    return []
            
            try:
                return await asyncio.wait_for(_make_request(), timeout=30.0)
            except asyncio.TimeoutError:
                print(f"Error searching markets: Timeout")
                return []
        except Exception as e:
            error_msg = str(e)
            # Check if it's the timeout context manager error
            if "Timeout context manager should be used inside a task" in error_msg:
                print(f"Error searching markets: Async context issue - retrying without timeout wrapper")
                # Retry without timeout wrapper (let aiohttp handle it)
                try:
                    path = "/trade-api/v2/markets"
                    params = "&".join([f"{k}={v}" for k, v in query_params.items()])
                    if params:
                        path += f"?{params}"
                    
                    ts, sig = self.auth.sign("GET", path)
                    headers = {
                        "KALSHI-ACCESS-KEY": self.auth.kid,
                        "KALSHI-ACCESS-SIGNATURE": sig,
                        "KALSHI-ACCESS-TIMESTAMP": ts
                    }
                    
                    # Use asyncio.wait_for instead of timeout context manager
                    async def _make_request():
                        async with self.session.get(
                            f"{self.base_url}{path}",
                            headers=headers
                        ) as resp:
                            if resp.status == 200:
                                return await resp.json()
                            return {}
                    
                    data = await asyncio.wait_for(_make_request(), timeout=5.0)
                    return data.get('markets', [])
                except Exception as retry_e:
                    print(f"Error searching markets (retry failed): {retry_e}")
                    return []
            else:
                print(f"Error searching markets: {e}")
                return []
    
    async def get_event_by_ticker(self, event_ticker):
        """
        Get event details by event_ticker (includes all submarkets)
        CRITICAL: Event ticker is different from market ticker!
        
        ALWAYS FETCHES FRESH DATA - No caching to avoid stale markets
        
        Uses the same approach as main project:
        1. Try /events/{event_ticker} first (fast, but may only return moneylines)
        2. Always supplement with /markets endpoint using series_ticker (gets spreads/totals)
        
        Args:
            event_ticker: Event ticker to fetch
        """
        if not self.session:
            await self.init()
        
        # ALWAYS FETCH FRESH - No cache to avoid stale/incomplete market data
        event_ticker_upper = event_ticker.upper()
        
        event_data = None
        markets = []
        seen_tickers = set()
        
        # METHOD 1: Try /events/{event_ticker} first (may only have moneylines)
        try:
            path = f"/trade-api/v2/events/{event_ticker}"
            ts, sig = self.auth.sign("GET", path)
            headers = {
                "KALSHI-ACCESS-KEY": self.auth.kid,
                "KALSHI-ACCESS-SIGNATURE": sig,
                "KALSHI-ACCESS-TIMESTAMP": ts
            }
            
            # Wrap in asyncio.wait_for to avoid timeout context manager issues when called from run_coroutine_threadsafe
            async def _fetch_event():
                async with self.session.get(
                    f"{self.base_url}{path}",
                    headers=headers
                ) as resp:
                    if resp.status == 200:
                        event_data = await resp.json()
                        nested_markets = event_data.get('markets', [])
                        if nested_markets:
                            print(f"   Fetched {len(nested_markets)} markets from /events endpoint")
                            for m in nested_markets:
                                ticker = m.get('ticker', '')
                                if ticker and ticker not in seen_tickers:
                                    markets.append(m)
                                    seen_tickers.add(ticker)
                        return event_data
                    return None
            
            await asyncio.wait_for(_fetch_event(), timeout=35.0)
        except Exception as e:
            error_msg = str(e)
            # Suppress "Timeout context manager" errors - these are handled by asyncio.wait_for
            if "Timeout context manager should be used inside a task" not in error_msg:
                print(f"   Warning: Error fetching /events: {e}")
        
        # METHOD 2: ALWAYS fetch from /markets endpoint (gets spreads/totals)
        # This is critical because /events often misses standalone spread/total markets
        print(f"   Fetching all markets via /markets endpoint...")
        additional_markets = await self.search_markets_by_event(event_ticker)
        
        if additional_markets:
            # Merge markets, avoiding duplicates
            for m in additional_markets:
                ticker = m.get('ticker', '')
                if ticker and ticker not in seen_tickers:
                    markets.append(m)
                    seen_tickers.add(ticker)
        
        # CRITICAL DEBUG: Log what markets we found
        print(f"   🔍 DEBUG: Total markets found: {len(markets)}")
        total_count = sum(1 for m in markets if 'TOTAL' in m.get('ticker', '').upper())
        spread_count = sum(1 for m in markets if 'SPREAD' in m.get('ticker', '').upper())
        game_count = sum(1 for m in markets if 'GAME' in m.get('ticker', '').upper())
        print(f"   🔍 DEBUG: Breakdown - TOTAL: {total_count}, SPREAD: {spread_count}, GAME: {game_count}")
        if total_count == 0:
            print(f"   🚨 CRITICAL: No TOTAL markets found! All market tickers:")
            for m in markets[:10]:  # Show first 10
                print(f"      - {m.get('ticker', 'N/A')}")
        if spread_count == 0:
            print(f"   🚨 CRITICAL: No SPREAD markets found! All market tickers:")
            for m in markets[:10]:  # Show first 10
                print(f"      - {m.get('ticker', 'N/A')}")
        
        # Update event_data with all markets
        if event_data:
            event_data['markets'] = markets
            print(f"   Total markets found: {len(markets)}")
        else:
            # If /events failed, create minimal event_data structure
            event_data = {
                'event_ticker': event_ticker,
                'markets': markets
            }
            print(f"   Created event data with {len(markets)} markets from /markets endpoint")
        
        # NO CACHING - Always return fresh data
        return event_data
    
    async def search_markets_by_event(self, event_ticker):
        """
        Search for all markets related to an event (spreads, totals, etc.)
        Uses the same approach as main project: /markets endpoint with series_ticker
        """
        if not self.session:
            await self.init()
        
        try:
            # Extract series prefix from event ticker
            # Event ticker format: KXNBAGAME-26JAN04MILSAC
            # Series prefixes: KXNBAGAME, KXNBASPREAD, KXNBATOTAL
            # Extract sport prefix (first 5 chars): KXNBA, KXNFL, KXNHL, KXEPL, etc.
            event_ticker_upper = event_ticker.upper()
            
            # Determine sport prefix (first 5 chars before the series type)
            # Examples: KXNBAGAME -> KXNBA, KXNFLGAME -> KXNFL
            if len(event_ticker_upper) >= 5:
                sport_prefix = event_ticker_upper[:5]  # e.g., "KXNBA"
            else:
                sport_prefix = event_ticker_upper.split('-')[0][:5] if '-' in event_ticker_upper else event_ticker_upper[:5]
            
            # Determine the base series from event ticker
            # Event ticker like "KXNBAGAME-26JAN04MILSAC" -> series is "KXNBAGAME"
            base_series = event_ticker_upper.split('-')[0] if '-' in event_ticker_upper else event_ticker_upper
            
            # Determine possible series tickers for this sport
            # GAME series (moneylines) - already have this
            # SPREAD series (spreads)
            # TOTAL series (totals)
            possible_series = [base_series]  # Start with the base series
            
            # Try to determine other series types
            if 'GAME' in base_series:
                # Replace GAME with SPREAD and TOTAL
                spread_series = base_series.replace('GAME', 'SPREAD')
                total_series = base_series.replace('GAME', 'TOTAL')
                possible_series.extend([spread_series, total_series])
            elif 'SPREAD' in base_series:
                # If we have SPREAD, also get GAME and TOTAL
                game_series = base_series.replace('SPREAD', 'GAME')
                total_series = base_series.replace('SPREAD', 'TOTAL')
                possible_series.extend([game_series, total_series])
            elif 'TOTAL' in base_series:
                # If we have TOTAL, also get GAME and SPREAD
                game_series = base_series.replace('TOTAL', 'GAME')
                spread_series = base_series.replace('TOTAL', 'SPREAD')
                possible_series.extend([game_series, spread_series])
            
            all_markets = []
            seen_tickers = set()
            
            # Fetch markets from each series (OLD FILE APPROACH - this works!)
            for series_ticker in possible_series:
                try:
                    path = "/trade-api/v2/markets"
                    params = {
                        "series_ticker": series_ticker,
                        "status": "open",
                        "limit": 1000
                    }
                    
                    ts, sig = self.auth.sign("GET", path)
                    headers = {
                        "KALSHI-ACCESS-KEY": self.auth.kid,
                        "KALSHI-ACCESS-SIGNATURE": sig,
                        "KALSHI-ACCESS-TIMESTAMP": ts
                    }
                    
                    # Wrap in asyncio.wait_for to avoid timeout context manager issues when called from run_coroutine_threadsafe
                    async def _fetch_series():
                        async with self.session.get(
                            f"{self.base_url}{path}",
                            headers=headers,
                            params=params
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                return data.get('markets', []), resp.status
                            return [], resp.status
                    
                    markets, status = await asyncio.wait_for(_fetch_series(), timeout=35.0)
                    if status == 200:
                        print(f"   🔍 DEBUG: API returned {len(markets)} markets for series {series_ticker}")
                        if len(markets) > 0 and len(markets) < 10:
                            print(f"   🔍 DEBUG: All {len(markets)} market tickers from {series_ticker}:")
                            for m in markets:
                                ticker = m.get('ticker', 'N/A')
                                event_tick = m.get('event_ticker', 'N/A')
                                print(f"      - {ticker} (event_ticker: {event_tick})")
                        
                        # Filter markets by event_ticker (OLD FILE METHOD - this works!)
                        matched_count = 0
                        for m in markets:
                                ticker = m.get('ticker', '')
                                market_event_ticker = m.get('event_ticker', '')
                                
                                if not ticker or ticker in seen_tickers:
                                    continue
                                
                                ticker_upper = ticker.upper()
                                # Filter out non-standard markets
                                excluded_types = ['MULTIGAMEEXTENDED', 'EXTENDED', 'MULTIGAME', 'PARLAY', 'COMBO']
                                if any(excluded_type in ticker_upper for excluded_type in excluded_types):
                                    continue
                                
                                # Method 1: Market ticker starts with event ticker
                                if ticker_upper.startswith(event_ticker_upper + '-'):
                                    all_markets.append(m)
                                    seen_tickers.add(ticker)
                                    matched_count += 1
                                    continue
                                
                                # Method 2: event_ticker field matches exactly
                                if market_event_ticker and market_event_ticker.upper() == event_ticker_upper:
                                    all_markets.append(m)
                                    seen_tickers.add(ticker)
                                    matched_count += 1
                                    continue
                                
                                # Method 3: For TOTAL/SPREAD series, event ticker might be in different format
                                # TOTAL series markets: KXNCAAMBTOTAL-26JAN31PITTCLEM-119
                                # Event ticker: KXNCAAMBGAME-26JAN31PITTCLEM
                                # Extract the date/event part: "26JAN31PITTCLEM"
                                event_parts = event_ticker_upper.split('-')
                                if len(event_parts) >= 2:
                                    event_suffix = '-'.join(event_parts[1:])  # e.g., "26JAN31PITTCLEM"
                                    # Check if market ticker contains this suffix
                                    if event_suffix in ticker_upper:
                                        # Also check if it's a TOTAL or SPREAD market
                                        if 'TOTAL' in ticker_upper or 'SPREAD' in ticker_upper:
                                            all_markets.append(m)
                                            seen_tickers.add(ticker)
                                            matched_count += 1
                                            continue
                                
                                # Method 4: Extract event portion from market ticker and compare
                                ticker_parts = ticker_upper.split('-')
                                if len(ticker_parts) >= 3:
                                    # Reconstruct event ticker from first parts
                                    potential_event = '-'.join(ticker_parts[:-2])  # Remove last 2 parts
                                    if potential_event == event_ticker_upper:
                                        all_markets.append(m)
                                        seen_tickers.add(ticker)
                                        matched_count += 1
                        
                        if matched_count > 0:
                            print(f"   Series {series_ticker}: Matched {matched_count} markets for event")
                            if matched_count <= 5:
                                print(f"   🔍 DEBUG: Matched {matched_count} market(s) from {series_ticker}:")
                                for m in all_markets[-matched_count:]:
                                    ticker = m.get('ticker', 'N/A')
                                    print(f"      - {ticker}")
                        elif len(markets) > 0:
                            print(f"   ⚠️  Series {series_ticker}: Got {len(markets)} markets from API but NONE matched event {event_ticker_upper}")
                    
                    elif status == 429:
                            print(f"   Warning: Rate limited for series {series_ticker}, waiting...")
                            await asyncio.sleep(1.0)
                    elif status != 404:  # 404 is OK if series doesn't exist
                        print(f"   Warning: Error fetching {series_ticker}: status {status}")
                    
                    # Small delay between series requests
                    await asyncio.sleep(0.2)
                    
                except Exception as e:
                    error_msg = str(e)
                    # Suppress "Timeout context manager" errors - these are handled by asyncio.wait_for
                    if "Timeout context manager should be used inside a task" not in error_msg:
                        print(f"   Warning: Error fetching series {series_ticker}: {e}")
                    continue
            
            if all_markets:
                print(f"   Found {len(all_markets)} markets via /markets endpoint (series: {', '.join(possible_series)})")
            return all_markets
            
        except Exception as e:
            print(f"   ⚠️  Error searching markets: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    async def build_all_sports_team_mapping(self):
        """
        Dynamically build team name to ticker code mappings for ALL sports by fetching
        active markets from Kalshi API.
        
        Returns:
            dict: Mapping of team name variations to ticker codes for all sports
            Format: {'ILLINOIS': ['ILL'], 'IOWA': ['IOWA'], 'MEMPHIS': ['MEM'], ...}
        """
        if not self.session:
            await self.init()
        
        all_sport_mappings = {}
        
        # Define all sport series we want to check
        sport_series = {
            'NCAAB': ['KXNCAAMBGAME', 'KXNCAAMBSPREAD', 'KXNCAAMBTOTAL'],
            'NCAAF': ['KXNCAAMBGAME', 'KXNCAAMBSPREAD', 'KXNCAAMBTOTAL'],  # College Football (same series)
        }
        
        print(f"[DYNAMIC MAPPING] Building team code mappings for all sports...")
        
        for sport_name, series_list in sport_series.items():
            sport_mapping = {}
            seen_codes = set()
            
            for series in series_list:
                try:
                    query_params = {
                        'series_ticker': series,
                        'limit': 500,  # Get as many as possible
                    }
                    
                    markets = await self.search_markets(query_params)
                    
                    for market in markets:
                        ticker = market.get('ticker', '').upper()
                        if not ticker:
                            continue
                        
                        # Extract team codes from ticker
                        # Format: KXNCAAMBGAME-26JAN10ILLIOWA-ILL
                        # Or: KXNCAAMBSPREAD-26JAN10ILLIOWA-ILL13
                        parts = ticker.split('-')
                        if len(parts) >= 3:
                            # Last part contains the team code (e.g., ILL, ILL13, IOWA)
                            team_code_part = parts[-1]
                            
                            # Extract base code (remove numbers, e.g., "ILL13" -> "ILL")
                            # re imported at top of file
                            base_code = re.sub(r'\d+$', '', team_code_part)
                            
                            if base_code and len(base_code) >= 2 and base_code not in seen_codes:
                                seen_codes.add(base_code)
                                
                                # Try to get team name from market data
                                yes_subtitle = market.get('yes_subtitle', '').upper()
                                no_subtitle = market.get('no_subtitle', '').upper()
                                
                                # Extract team names from subtitles
                                team_names = set()
                                if yes_subtitle:
                                    # Remove common suffixes like "wins by over", "wins", etc.
                                    clean_yes = re.sub(r'\s+(WINS|WINS BY|BY OVER|BY UNDER).*$', '', yes_subtitle, flags=re.IGNORECASE)
                                    if clean_yes and len(clean_yes) > 2:
                                        team_names.add(clean_yes)
                                if no_subtitle:
                                    clean_no = re.sub(r'\s+(WINS|WINS BY|BY OVER|BY UNDER).*$', '', no_subtitle, flags=re.IGNORECASE)
                                    if clean_no and len(clean_no) > 2:
                                        team_names.add(clean_no)
                                
                                # Add mappings
                                for team_name in team_names:
                                    if team_name not in sport_mapping:
                                        sport_mapping[team_name] = []
                                    if base_code not in sport_mapping[team_name]:
                                        sport_mapping[team_name].append(base_code)
                                
                                # Also add the code itself as a key (e.g., "ILL" -> ["ILL"])
                                if base_code not in sport_mapping:
                                    sport_mapping[base_code] = [base_code]
                    
                    await asyncio.sleep(0.2)
                except Exception as e:
                    print(f"  [DYNAMIC MAPPING] Error with {series}: {e}")
                    continue
            
            all_sport_mappings[sport_name] = sport_mapping
            print(f"  [{sport_name}] Built {len(sport_mapping)} team entries from {len(seen_codes)} unique codes")
        
        # Merge all sports into one mapping (college sports share codes)
        merged_mapping = {}
        for sport_name, mapping in all_sport_mappings.items():
            for team_name, codes in mapping.items():
                if team_name not in merged_mapping:
                    merged_mapping[team_name] = []
                for code in codes:
                    if code not in merged_mapping[team_name]:
                        merged_mapping[team_name].append(code)
        
        print(f"[DYNAMIC MAPPING] Total: {len(merged_mapping)} team entries across all sports")
        return merged_mapping
    
    async def build_ncaab_team_mapping(self, days_ahead=7):
        """
        Legacy function - now calls build_all_sports_team_mapping()
        """
        return await self.build_all_sports_team_mapping()
    
    async def get_market_by_ticker(self, ticker):
        """Get market details by ticker (submarket ticker, not event ticker)"""
        if not self.session:
            await self.init()
        
        try:
            # For v2 API, path must include /trade-api/v2 prefix
            path = f"/trade-api/v2/markets/{ticker}"
            ts, sig = self.auth.sign("GET", path)
            headers = {
                "KALSHI-ACCESS-KEY": self.auth.kid,
                "KALSHI-ACCESS-SIGNATURE": sig,
                "KALSHI-ACCESS-TIMESTAMP": ts
            }
            
            # Wrap in asyncio.wait_for to avoid timeout context manager issues when called from run_coroutine_threadsafe
            async def _fetch_market():
                async with self.session.get(
                    f"{self.base_url}{path}",
                    headers=headers
                ) as resp:
                    if resp.status == 200:
                        market = await resp.json()
                        # CRITICAL: Ensure ticker field is set (API might return it nested or missing)
                        if market and 'ticker' not in market:
                            market['ticker'] = ticker
                        return market
                    return None
            
            return await asyncio.wait_for(_fetch_market(), timeout=35.0)
        except Exception as e:
            error_msg = str(e)
            # Suppress "Timeout context manager" errors - these are handled by asyncio.wait_for
            if "Timeout context manager should be used inside a task" not in error_msg:
                print(f"Error fetching market {ticker}: {e}")
            return None
    
    async def get_market_by_id(self, market_id):
        """
        Get market details by market ID (MOST RELIABLE METHOD)
        Market ID uniquely identifies each submarket (e.g., '85c57010-79bb-4672-8dfa-c3d4c03fb8bd')
        This is more reliable than ticker matching when Kalshi has data bugs
        """
        if not self.session:
            await self.init()
        
        try:
            # Try v1 API first (as shown in user's example: /v1/series/KXNBAGAME/markets/{market_id})
            # But we need the series ticker... let's try v2 API with market_id
            # Actually, v2 API might use ticker, not market_id
            # Let's check if we can use market_id directly
            
            # Try v1 API format (if we have series ticker)
            # For now, try v2 API with market_id as parameter
            path = f"/trade-api/v2/markets"
            params = {"market_id": market_id}
            
            ts, sig = self.auth.sign("GET", path)
            headers = {
                "KALSHI-ACCESS-KEY": self.auth.kid,
                "KALSHI-ACCESS-SIGNATURE": sig,
                "KALSHI-ACCESS-TIMESTAMP": ts
            }
            
            # Use asyncio.wait_for instead of ClientTimeout to avoid context manager issues
            async def _make_request():
                async with self.session.get(
                    f"{self.base_url}{path}",
                    headers=headers,
                    params=params
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        markets = data.get('markets', [])
                        if markets:
                            return markets[0]  # Return first match
                        return None
                    return None
            
            try:
                return await asyncio.wait_for(_make_request(), timeout=5.0)
            except asyncio.TimeoutError:
                print(f"Error fetching market by ID {market_id}: Timeout")
                return None
            except Exception as e:
                print(f"Error fetching market by ID {market_id}: {e}")
                return None
                if resp.status == 200:
                    data = await resp.json()
                    markets = data.get('markets', [])
                    if markets:
                        return markets[0]  # Return first match
                    return None
                return None
        except Exception as e:
            print(f"Error fetching market by ID {market_id}: {e}")
            return None
    
    def _get_team_code_from_selection(self, selection, team_code_map):
        """Get team code from selection using team code map"""
        if not selection:
            return None
        
        selection_upper = selection.upper()
        
        # Check team code map first (most reliable)
        for team_name, codes in team_code_map.items():
            if team_name in selection_upper or selection_upper in team_name:
                # Return first code (most common)
                if codes:
                    return codes[0]
        
        # Fallback: Try first 3-4 letters (for teams not in map)
        words = selection_upper.split()
        if words:
            first_word = words[0]
            if len(first_word) >= 3:
                return first_word[:3].upper()
            else:
                return first_word.upper()
        
        return None
    
    def _extract_team_codes_from_event_ticker(self, event_ticker, teams_str=None, selection=None):
        """
        Extract team codes from event ticker suffix.
        Format: KXNCAAMBGAME-26JAN31SDSUUSU -> ["SDSU", "USU"]
        The suffix is [DATE][TEAM1][TEAM2] where team codes are concatenated.
        """
        event_ticker_upper = event_ticker.upper()
        event_parts = event_ticker_upper.split('-')
        if len(event_parts) < 2:
            return None, None
        
        event_suffix = event_parts[1]  # e.g., "26JAN31SDSUUSU"
        
        # Find where date ends (date format: DDMMMYY or similar, ends with numbers/letters before team codes)
        # Date typically ends with 2 digits (year) followed by team codes
        # Pattern: numbers + letters (month) + numbers (day/year) + team codes
        import re
        
        # Try to find date pattern and extract team codes part
        # Common patterns: 26JAN31, 31JAN26, etc.
        # After date, we have team codes concatenated
        date_match = re.match(r'(\d{1,2}[A-Z]{3}\d{1,2})', event_suffix)
        if date_match:
            date_part = date_match.group(1)
            team_codes_part = event_suffix[len(date_part):]  # Everything after date
            
            # Now we need to split team_codes_part into two team codes
            # Team codes are typically 2-5 characters each
            # Try common lengths: 3+3, 4+3, 3+4, 4+4, 5+3, etc.
            possible_splits = []
            for i in range(2, min(6, len(team_codes_part) - 1)):
                code1 = team_codes_part[:i]
                code2 = team_codes_part[i:]
                if len(code1) >= 2 and len(code2) >= 2:
                    possible_splits.append((code1, code2))
            
            # CRITICAL: Try to validate splits using known team codes
            # Determine sport from event ticker
            base_series = event_ticker_upper.split('-')[0] if '-' in event_ticker_upper else event_ticker_upper
            validated_splits = []
            for code1, code2 in possible_splits:
                # Check if these codes match known team codes for this sport
                if 'KXNCAAMBGAME' in base_series or 'KXNCAAMB' in base_series:
                    # NCAAB - validate using known team codes
                    # Common NCAAB codes: WEB, SAC, IW, AMCC, KU, TTU, etc.
                    known_ncaab_codes = ['WEB', 'SAC', 'IW', 'AMCC', 'KU', 'TTU', 'UNT', 'COOK', 'AAMU', 'UNO', 'TAMC', 'MCNS', 'GRAM', 'ALCN', 'SOU', 'JKST', 'UMES', 'FAU', 'ECU', 'WICH', 'TLSA', 'MINN', 'PSU', 'MSM', 'QUIN', 'FAIR', 'MAN', 'GB', 'WRST', 'COLO', 'TCU', 'HC', 'NCST', 'UNC', 'DUKE', 'VT', 'UGA', 'TENN', 'AUB', 'ALA', 'ARK', 'CLEM', 'MIA', 'ND', 'PUR', 'WIS', 'MD', 'RUTG', 'NEB', 'IOWA', 'CREI', 'VILL', 'PROV', 'GONZ', 'SMC', 'BYU', 'HOU', 'MEM', 'CIN', 'TEM', 'UAB']
                    # Prefer splits where at least one code is in known list, OR both are 3-4 chars
                    if (code1 in known_ncaab_codes or code2 in known_ncaab_codes) or (3 <= len(code1) <= 4 and 3 <= len(code2) <= 4):
                        validated_splits.append((code1, code2))
                elif 'KXNHLGAME' in base_series or 'KXNHL' in base_series:
                    # NHL - known codes: WPG, DAL, DET, COL, PHI, etc.
                    known_nhl_codes = ['WPG', 'DAL', 'DET', 'COL', 'PHI', 'TOR', 'MTL', 'BOS', 'NYR', 'NYI', 'BUF', 'OTT', 'FLA', 'TBL', 'CAR', 'WSH', 'PIT', 'CBJ', 'NJ', 'CHI', 'MIN', 'NSH', 'STL', 'VGK', 'EDM', 'CGY', 'VAN', 'SEA', 'ANA', 'SJ', 'LA', 'ARI']
                    if code1 in known_nhl_codes or code2 in known_nhl_codes:
                        validated_splits.append((code1, code2))
                elif 'KXNBAGAME' in base_series or 'KXNBA' in base_series:
                    # NBA - known codes: PHI, LAC, MIN, MEM, etc.
                    known_nba_codes = ['PHI', 'LAC', 'MIN', 'MEM', 'BOS', 'BKN', 'NYK', 'TOR', 'CHI', 'CLE', 'DET', 'IND', 'MIL', 'ATL', 'CHA', 'MIA', 'ORL', 'WAS', 'DEN', 'OKC', 'POR', 'UTA', 'GS', 'LAL', 'PHX', 'SAC', 'DAL', 'HOU', 'NO', 'SA']
                    if code1 in known_nba_codes or code2 in known_nba_codes:
                        validated_splits.append((code1, code2))
                elif 'KXNFLGAME' in base_series or 'KXNFL' in base_series:
                    # NFL - known codes: KC, PHI, DAL, BAL, PIT, etc.
                    known_nfl_codes = ['KC', 'PHI', 'DAL', 'BAL', 'PIT', 'BUF', 'CAR', 'CHI', 'CIN', 'CLE', 'DEN', 'DET', 'GB', 'HOU', 'IND', 'JAX', 'LV', 'LAC', 'LAR', 'MIA', 'MIN', 'NE', 'NO', 'NYG', 'NYJ', 'SF', 'SEA', 'TB', 'TEN', 'WAS', 'ARI', 'ATL']
                    if code1 in known_nfl_codes or code2 in known_nfl_codes:
                        validated_splits.append((code1, code2))
                elif 'KXNCAAFGAME' in base_series or 'KXNCAAF' in base_series:
                    # NCAAF - similar to NCAAB, use NCAAB validation
                    known_ncaab_codes = ['WEB', 'SAC', 'IW', 'AMCC', 'KU', 'TTU', 'UNT', 'COOK', 'AAMU', 'UNO', 'TAMC', 'MCNS', 'GRAM', 'ALCN', 'SOU', 'JKST', 'UMES', 'FAU', 'ECU', 'WICH', 'TLSA', 'MINN', 'PSU', 'MSM', 'QUIN', 'FAIR', 'MAN', 'GB', 'WRST', 'COLO', 'TCU', 'HC', 'NCST', 'UNC', 'DUKE', 'VT', 'UGA', 'TENN', 'AUB', 'ALA', 'ARK', 'CLEM', 'MIA', 'ND', 'PUR', 'WIS', 'MD', 'RUTG', 'NEB', 'IOWA', 'CREI', 'VILL', 'PROV', 'GONZ', 'SMC', 'BYU', 'HOU', 'MEM', 'CIN', 'TEM', 'UAB']
                    if (code1 in known_ncaab_codes or code2 in known_ncaab_codes) or (3 <= len(code1) <= 4 and 3 <= len(code2) <= 4):
                        validated_splits.append((code1, code2))
            
            # If we found validated splits, prefer those (they're more likely correct)
            if validated_splits:
                possible_splits = validated_splits
            
            # If we have teams_str or selection, try to match
            if teams_str or selection:
                teams_upper = (teams_str or "").upper()
                selection_upper = (selection or "").upper()
                
                # CRITICAL: Use teams_str to determine which team is which
                # Format: "Team1 @ Team2" or "Team1 VS Team2"
                if teams_upper and '@' in teams_upper:
                    team_parts = teams_upper.split('@')
                    if len(team_parts) == 2:
                        away_team = team_parts[0].strip()
                        home_team = team_parts[1].strip()
                        
                        # Match selection to away or home team
                        selection_matches_away = (selection_upper in away_team or away_team in selection_upper or
                                                  any(word in selection_upper for word in away_team.split() if len(word) > 2) or
                                                  any(word in away_team for word in selection_upper.split() if len(word) > 2))
                        selection_matches_home = (selection_upper in home_team or home_team in selection_upper or
                                                  any(word in selection_upper for word in home_team.split() if len(word) > 2) or
                                                  any(word in home_team for word in selection_upper.split() if len(word) > 2))
                        
                        # CRITICAL: Event ticker format is [DATE][AWAY][HOME]
                        # If selection matches away team, code1 is the away team code
                        # If selection matches home team, code2 is the home team code
                        # We can trust the order from the event ticker!
                        if selection_matches_away:
                            # Selection is away team - code1 should be away team code
                            # Try to find a split where code1 could match away team
                            for code1, code2 in possible_splits:
                                # Check if code1 could be abbreviation of away team
                                # GTWN could be from Georgetown (G-T-W-N from different parts)
                                # Check if first letter matches and code length is reasonable
                                if away_team and code1:
                                    if (away_team[0] == code1[0] or  # First letter matches
                                        code1 in away_team.replace(' ', '') or  # Code appears in team name (no spaces)
                                        any(code1 in word for word in away_team.split() if len(word) >= len(code1))):
                                        return code1, code2
                            # If no perfect match, trust the order: first code = away team
                            if possible_splits:
                                # Prefer 4-char codes for away team (common pattern)
                                for code1, code2 in possible_splits:
                                    if len(code1) == 4:
                                        return code1, code2
                                return possible_splits[0]
                        elif selection_matches_home:
                            # Selection is home team - code2 should be home team code
                            for code1, code2 in possible_splits:
                                if home_team and code2:
                                    if (home_team[0] == code2[0] or
                                        code2 in home_team.replace(' ', '') or
                                        any(code2 in word for word in home_team.split() if len(word) >= len(code2))):
                                        return code1, code2
                            # If no perfect match, trust the order: second code = home team
                            if possible_splits:
                                for code1, code2 in possible_splits:
                                    if len(code2) == 4:
                                        return code1, code2
                                return possible_splits[0]
                
                # Fallback: Try to match selection to one of the team codes directly
                for code1, code2 in possible_splits:
                    # Check if code1 or code2 appears in selection or teams
                    if (code1 in selection_upper or selection_upper.startswith(code1) or 
                        code2 in selection_upper or selection_upper.startswith(code2) or
                        code1 in teams_upper or code2 in teams_upper):
                        return code1, code2
            
            # If no match, return the most likely split (longer first code is usually away team)
            if possible_splits:
                # Prefer splits where first code is 4 chars (common for college teams)
                for code1, code2 in possible_splits:
                    if len(code1) == 4:
                        return code1, code2
                # Otherwise return first split
                return possible_splits[0]
        
        return None, None
    
    def _learn_team_codes_from_ticker(self, ticker, teams_str):
        """
        Learn team codes from a successfully matched ticker.
        If we know one team's code, we can infer the other from the ticker.
        
        Example: If ticker is "KXNCAAMBSPREAD-26FEB13IONACAN-IONA8" and teams_str is "Iona @ Canisius",
        and we know CAN = Canisius, we can learn IONA = Iona.
        """
        if not ticker or not teams_str:
            return
        
        try:
            # Extract team codes from ticker
            team_code1, team_code2 = self._extract_team_codes_from_event_ticker(ticker, teams_str)
            if not team_code1 or not team_code2:
                return
            
            # Parse teams string to get team names
            teams_upper = teams_str.upper()
            if '@' in teams_upper:
                team_parts = teams_upper.split('@')
                if len(team_parts) == 2:
                    away_team = team_parts[0].strip()
                    home_team = team_parts[1].strip()
                    
                    # CRITICAL: Check ONLY hardcoded mappings (not learned) to determine if team is "known"
                    # We only want to learn if one team is in our hardcoded mappings
                    known_away = self._get_ncaab_team_code(away_team, use_learned=False)
                    known_home = self._get_ncaab_team_code(home_team, use_learned=False)
                    
                    # If we know away team code (hardcoded) matches code1, learn home team code = code2
                    if known_away and known_away == team_code1 and not known_home:
                        self.learned_team_codes[home_team] = team_code2
                        print(f"   🎓 LEARNED: {home_team} -> {team_code2} (from ticker {ticker}, hardcoded: {away_team} -> {team_code1})")
                    
                    # If we know home team code (hardcoded) matches code2, learn away team code = code1
                    elif known_home and known_home == team_code2 and not known_away:
                        self.learned_team_codes[away_team] = team_code1
                        print(f"   🎓 LEARNED: {away_team} -> {team_code1} (from ticker {ticker}, hardcoded: {home_team} -> {team_code2})")
                    
                    # If we know both (hardcoded), no learning needed - both are already correct
                    # If we know neither (hardcoded), we can't learn (need at least one hardcoded anchor)
        except Exception as e:
            print(f"   ⚠️  Error learning team codes from ticker: {e}")
    
    def _get_ncaab_team_code(self, selection_upper, use_learned=True):
        """
        Comprehensive NCAAB team code mapping.
        Returns team code for given selection, or None if not found.
        More specific matches should be checked first.
        
        Args:
            selection_upper: Team name in uppercase
            use_learned: If True, check learned codes after hardcoded mappings (default True)
        """
        if not selection_upper:
            return None
        
        # Remove common suffixes that might interfere with matching
        # e.g., "Mount St. Mary's (MD)" -> "Mount St. Mary's"
        selection_clean = selection_upper
        for suffix in [' (MD)', ' (NY)', ' (PA)', ' (FL)', ' (OH)', ' (CA)', ' (TX)', ' (NC)', ' (VA)', ' (MA)']:
            if suffix in selection_clean:
                selection_clean = selection_clean.replace(suffix, '')
        
        # CRITICAL: Check hardcoded mappings FIRST (they're known to be correct)
        # Comprehensive NCAAB mappings (most specific first)
        # Format: (team_name_pattern, team_code)
        mappings = [
            # States with multiple teams - most specific first
            ('NORTH CAROLINA STATE', 'NCST'), ('NORTH CAROLINA ST', 'NCST'), ('NC STATE', 'NCST'),
            ('NORTH CAROLINA', 'UNC'),
            ('SOUTH CAROLINA STATE', 'SCST'), ('SOUTH CAROLINA ST', 'SCST'),
            ('SOUTH CAROLINA', 'SCAR'),
            ('NORTH DAKOTA STATE', 'NDSU'), ('NORTH DAKOTA ST', 'NDSU'),
            ('NORTH DAKOTA', 'UND'),
            ('SOUTH DAKOTA STATE', 'SDST'), ('SOUTH DAKOTA ST', 'SDST'),
            ('SOUTH DAKOTA', 'SDAK'),
            ('TEXAS A&M', 'TXAM'), ('TEXAS A & M', 'TXAM'),
            ('TEXAS TECH', 'TTU'), ('TTU', 'TTU'),  # Texas Tech
            ('TEXAS STATE', 'TXST'), ('TEXAS ST', 'TXST'),
            ('TEXAS', 'TEX'),
            ('ARIZONA STATE', 'ASU'), ('ARIZONA ST', 'ASU'),
            ('ARIZONA', 'ARIZ'),
            ('MICHIGAN STATE', 'MSU'), ('MICHIGAN ST', 'MSU'),
            ('MICHIGAN', 'MICH'),
            ('OHIO STATE', 'OSU'), ('OHIO ST', 'OSU'),
            ('OHIO', 'OHIO'),
            ('OKLAHOMA STATE', 'OKST'), ('OKLAHOMA ST', 'OKST'),
            ('OKLAHOMA', 'OKLA'),
            ('IOWA STATE', 'ISU'), ('IOWA ST', 'ISU'),
            ('IOWA', 'IOWA'),
            # CRITICAL: Check ARKANSAS LITTLE ROCK before KANSAS (KANSAS is substring of ARKANSAS)
            ('ARKANSAS LITTLE ROCK', 'UALR'), ('ARKANSAS-LITTLE ROCK', 'UALR'), ('LITTLE ROCK', 'UALR'), ('UALR', 'UALR'),
            ('KANSAS STATE', 'KSU'), ('KANSAS ST', 'KSU'),
            ('KANSAS', 'KU'), ('KU', 'KU'),  # Kansas
            ('INDIANA STATE', 'INST'), ('INDIANA ST', 'INST'),
            ('INDIANA', 'IND'),
            ('ILLINOIS STATE', 'ILST'), ('ILLINOIS ST', 'ILST'),
            ('ILLINOIS', 'ILL'),
            ('MISSISSIPPI STATE', 'MSST'), ('MISSISSIPPI ST', 'MSST'),
            ('MISSISSIPPI VALLEY STATE', 'MVSU'), ('MISSISSIPPI VALLEY ST', 'MVSU'), ('MVSU', 'MVSU'),
            ('MISSOURI STATE', 'MOSU'), ('MISSOURI ST', 'MOSU'),
            ('MISSOURI', 'MIZZ'),
            ('FLORIDA STATE', 'FSU'), ('FLORIDA ST', 'FSU'),
            # Note: 'FLORIDA' -> 'FLA' is too generic - check FLORIDA ATLANTIC first (line 1015)
            # ('FLORIDA', 'FLA'),  # Disabled - too generic, matches FLORIDA ATLANTIC incorrectly
            ('PENN STATE', 'PSU'), ('PENN ST', 'PSU'),
            ('OREGON STATE', 'ORST'), ('OREGON ST', 'ORST'),
            ('OREGON', 'ORE'),
            ('WASHINGTON STATE', 'WSU'), ('WASHINGTON ST', 'WSU'),
            ('WASHINGTON', 'WASH'),
            ('COLORADO STATE', 'CSU'), ('COLORADO ST', 'CSU'),
            ('COLORADO', 'COLO'),
            ('UTAH STATE', 'USU'), ('UTAH ST', 'USU'),
            ('UTAH VALLEY', 'UVU'), ('UTAH VAL', 'UVU'), ('UVU', 'UVU'),  # Utah Valley
            ('UTAH TECH', 'UTU'), ('UTAH TECH', 'DIXIE'), ('DIXIE', 'UTU'), ('UTTECH', 'UTU'),  # Utah Tech (formerly Dixie State)
            ('UTAH', 'UTA'),
            ('IDAHO STATE', 'IDST'), ('IDAHO ST', 'IDST'),
            ('IDAHO', 'IDHO'),
            ('PORTLAND STATE', 'PRST'), ('PORTLAND ST', 'PRST'), ('PRST', 'PRST'),  # Portland State
            ('PORTLAND', 'PORT'),  # Portland (University of Portland, not Portland State)
            ('MONTANA STATE', 'MTST'), ('MONTANA ST', 'MTST'),
            ('MONTANA', 'MONT'),
            ('NORTHERN ARIZONA', 'NAU'),
            ('NORTHERN COLORADO', 'UNCO'),
            ('NORTHERN ILLINOIS', 'NIU'),
            ('NORTHERN IOWA', 'UNI'),
            ('NORTHERN KENTUCKY', 'NKU'),
            ('DENVER', 'DEN'),  # Denver (University of Denver)
            ('ABILENE CHRISTIAN', 'ACU'), ('ABILENE CHRIS', 'ACU'), ('ACU', 'ACU'),  # Abilene Christian
            ('UT ARLINGTON', 'UTA'), ('UT ARL', 'UTA'), ('ARLINGTON', 'UTA'), ('UTAR', 'UTA'),  # UT Arlington
            ('NORTHWESTERN STATE', 'NWST'), ('NORTHWESTERN ST', 'NWST'), ('NWST', 'NWST'),  # Northwestern State
            ('NORTHWESTERN', 'NW'),
            ('STEPHEN F. AUSTIN', 'SFA'), ('STEPHEN F AUSTIN', 'SFA'), ('SFA', 'SFA'),  # Stephen F. Austin
            ('NICHOL', 'NICH'), ('NICHOLLS', 'NICH'), ('NICH', 'NICH'),  # Nicholls State
            ('SOUTHERN CALIFORNIA', 'USC'),
            ('SOUTHERN ILLINOIS', 'SIU'),
            ('SOUTHERN INDIANA', 'USI'),
            ('SOUTHERN MISS', 'USM'),
            ('SOUTHERN UTAH', 'SUU'),
            ('SOUTHEAST MISSOURI STATE', 'SEMO'), ('SOUTHEAST MISSOURI ST', 'SEMO'),
            ('SOUTHEASTERN LOUISIANA', 'SELA'),
            ('EASTERN KENTUCKY', 'EKU'),
            ('EASTERN MICHIGAN', 'EMU'),
            ('EASTERN ILLINOIS', 'EIU'),
            ('EASTERN WASHINGTON', 'EWU'),
            ('WESTERN KENTUCKY', 'WKU'),
            ('WESTERN MICHIGAN', 'WMU'),
            ('WESTERN ILLINOIS', 'WIU'),
            ('WESTERN CAROLINA', 'WCU'),
            ('CENTRAL MICHIGAN', 'CMU'),
            ('CENTRAL CONNECTICUT STATE', 'CCSU'), ('CENTRAL CONNECTICUT ST', 'CCSU'),
            ('CENTRAL ARKANSAS', 'CARK'),
            ('LOUISIANA STATE', 'LSU'),
            ('LOUISIANA TECH', 'LT'),
            ('LOUISIANA-MONROE', 'ULM'),
            ('LOUISIANA', 'ULL'),
            ('LSU', 'LSU'),
            # Common abbreviations
            ('GEORGETOWN', 'GTWN'),
            ('HOLY CROSS', 'HC'),
            ('STONEHILL', 'STNH'),
            ('LE MOYNE', 'LMC'),
            ('SETON HALL', 'HALL'),
            ('MARQUETTE', 'MARQ'),
            ('WAGNER', 'WAG'),
            ('MERCYHURST', 'MHU'),
            ('STONY BROOK', 'STON'),
            ('TOWSON', 'TOWS'),
            ('UNC ASHEVILLE', 'UNCA'),
            ('LONGWOOD', 'LONG'),
            ('IONA', 'IONA'), ('IONA COLLEGE', 'IONA'),  # Iona College
            ('GEORGE MASON', 'GMU'), ('GMU', 'GMU'),  # George Mason
            ('GEORGE WASHINGTON', 'GW'), ('GW', 'GW'), ('GWU', 'GW'),  # George Washington
            ('WINTHROP', 'WIN'),
            ('GARDNER-WEBB', 'WEBB'), ('GARDNER WEBB', 'WEBB'),
            ('YOUNGSTOWN STATE', 'YSU'), ('YOUNGSTOWN ST', 'YSU'),
            ('OAKLAND', 'OAK'),
            ('COASTAL CAROLINA', 'CCAR'),
            ('ST. THOMAS', 'UST'), ('SAINT THOMAS', 'UST'),
            ('OMAHA', 'NEOM'),
            ('CALIFORNIA BAPTIST', 'CBU'),
            ('CAL POLY SLO', 'CP'), ('CAL POLY', 'CP'), ('POLY SLO', 'CP'), ('CP', 'CP'),  # Cal Poly San Luis Obispo
            ('UC IRVINE', 'UCI'), ('CAL IRVINE', 'UCI'), ('IRVINE', 'UCI'), ('UCI', 'UCI'),  # UC Irvine
            ('CLEVELAND STATE', 'CLEV'), ('CLEVELAND ST', 'CLEV'),
            ('FAIRLEIGH DICKINSON', 'FDU'), ('FAIRLEIGH', 'FDU'), ('FDU', 'FDU'),
            ('DUKE', 'DUKE'),
            ('VIRGINIA TECH', 'VT'),
            ('GEORGIA', 'UGA'),
            ('GEORGIA STATE', 'GAST'), ('GEORGIA ST', 'GAST'),
            ('GEORGIA SOUTHERN', 'GASO'),
            ('GEORGIA TECH', 'GT'),
            ('ARMY', 'ARMY'),
            ('ROBERT MORRIS', 'RMU'),
            ('PURDUE FORT WAYNE', 'PFW'), ('IPFW', 'PFW'),
            ('WILLIAM & MARY', 'WM'), ('WILLIAM AND MARY', 'WM'),
            ('CAMPBELL', 'CAMP'),
            ('WAKE FOREST', 'WAKE'),
            ('BALL STATE', 'BSU'), ('BALL ST', 'BSU'),
            ('TOLEDO', 'TOL'),
            ('UNC GREENSBORO', 'UNCG'),
            ('THE CITADEL', 'CIT'), ('CITADEL', 'CIT'),
            ('SAN DIEGO STATE', 'SDSU'), ('SAN DIEGO ST', 'SDSU'),
            ('SMU', 'SMU'), ('SOUTHERN METHODIST', 'SMU'),
            ('TCU', 'TCU'), ('TEXAS CHRISTIAN', 'TCU'), ('TEXAS CHRISTIAN UNIVERSITY', 'TCU'),
            ('LOUISVILLE', 'LOU'),
            ('NORTH ALABAMA', 'UNA'),
            ('STETSON', 'STET'),
            ('RADFORD', 'RAD'),
            ('PRESBYTERIAN', 'PRE'),
            ('LIPSCOMB', 'LIP'),
            ('NORTH FLORIDA', 'UNF'),
            ('UMASS LOWELL', 'MASSL'),
            ('MAINE', 'ME'),
            ('CHARLESTON SOUTHERN', 'CHSO'),
            ('CHARLESTON', 'COFC'),  # College of Charleston
            ('NORTHEASTERN', 'NE'),
            ('ELON', 'ELON'),
            ('UNC WILMINGTON', 'UNCW'),
            ('HIGH POINT', 'HPS'),
            ('USC UPSTATE', 'CUS'), ('SOUTH CAROLINA UPSTATE', 'CUS'),
            ('DREXEL', 'DREX'),
            ('WRIGHT STATE', 'WRST'), ('WRIGHT ST', 'WRST'),
            ('NJIT', 'NJIT'),
            ('NEW HAMPSHIRE', 'UNH'),
            ('UMBC', 'UMBC'),
            ('MIDDLE TENNESSEE', 'MTU'), ('MIDDLE TENN', 'MTU'),
            ('KENNESAW STATE', 'KENN'), ('KENNESAW ST', 'KENN'),
            ('IU INDY', 'IUIN'), ('IUPUI', 'IUIN'), ('INDIANA UNIVERSITY-PURDUE UNIVERSITY INDIANAPOLIS', 'IUIN'),
            ('BINGHAMTON', 'BING'),
            ('NEW HAVEN', 'NHC'),
            ('DEPAUL', 'DEP'),
            ('XAVIER', 'XAV'),
            ('LONG ISLAND', 'LIU'),
            ('SAINT JOSEPH', 'JOES'), ('ST JOSEPH', 'JOES'), ("ST. JOSEPH'S", 'JOES'),
            ('LA SALLE', 'LAS'),
            ('HOFSTRA', 'HOF'),
            ('MONMOUTH', 'MONM'),
            ('BUFFALO', 'BUFF'),
            ('OLD DOMINION', 'ODU'),
            ('QUEENS', 'QU'),
            ('BELLARMINE', 'BELL'),
            ("MOUNT ST. MARY'S", 'MSM'), ("MOUNT ST MARY'S", 'MSM'), ("MOUNT ST MARY", 'MSM'), ('MOUNT SAINT MARY', 'MSM'),
            ('MANHATTAN', 'MAN'),
            ('FAIRFIELD', 'FAIR'),
            ('QUINNIPIAC', 'QUIN'),
            ('EAST CAROLINA', 'ECU'),
            ('FLORIDA ATLANTIC', 'FAU'), ('FAU', 'FAU'),  # Must come before generic 'FLORIDA'
            ('WICHITA STATE', 'WICH'), ('WICHITA ST', 'WICH'),
            ('TULSA', 'TLSA'),
            ('NORTH TEXAS', 'UNT'),
            ('INCARNATE WORD', 'IW'), ('IW', 'IW'),
            ('TEXAS A&M CORPUS CHRISTI', 'AMCC'), ('TEXAS A & M CORPUS CHRISTI', 'AMCC'), ('AMCC', 'AMCC'),
            ('BETHUNE COOKMAN', 'COOK'), ('BETHUNE-COOKMAN', 'COOK'), ('COOK', 'COOK'),
            ('ALABAMA A&M', 'AAMU'), ('ALABAMA A & M', 'AAMU'), ('AAMU', 'AAMU'),
            ('NEW ORLEANS', 'UNO'), ('UNO', 'UNO'),
            ('TEXAS A&M COMMERCE', 'TAMC'), ('TEXAS A & M COMMERCE', 'TAMC'), ('TAMC', 'TAMC'),
            ('MCNEESE', 'MCNS'), ('MCNEESE STATE', 'MCNS'), ('MCNS', 'MCNS'),
            ('GRAMBLING', 'GRAM'), ('GRAM', 'GRAM'),
            ('ALCORN STATE', 'ALCN'), ('ALCORN ST', 'ALCN'), ('ALCN', 'ALCN'),
            ('SOUTHERN', 'SOU'), ('SOU', 'SOU'),  # Southern University (not Southern Miss)
            ('JACKSON STATE', 'JKST'), ('JACKSON ST', 'JKST'), ('JKST', 'JKST'),
            ('WEBER STATE', 'WEB'), ('WEBER ST', 'WEB'), ('WEB', 'WEB'),
            ('SACRAMENTO STATE', 'SAC'), ('SACRAMENTO ST', 'SAC'), ('SAC', 'SAC'),
            ('SOUTH ALABAMA', 'USA'),
            ('SANTA CLARA', 'SCU'),
            ('LOYOLA MARYMOUNT', 'LMU'),
            ('UCF', 'UCF'), ('CENTRAL FLORIDA', 'UCF'),
            ('BUTLER', 'BUT'),
            ('VALPARAISO', 'VALP'),
            ('BRADLEY', 'BRAD'),
            ('EVANSVILLE', 'EVAN'),
            ('LOYOLA CHICAGO', 'LCHI'), ('LOYOLA', 'LCHI'),
            ('ST. JOHN\'S', 'SJU'), ("ST. JOHN'S", 'SJU'), ('ST JOHN\'S', 'SJU'), ("ST JOHN'S", 'SJU'), ('ST. JOHNS', 'SJU'), ('ST JOHNS', 'SJU'),
            ('UIC', 'UIC'), ('ILLINOIS-CHICAGO', 'UIC'), ('ILLINOIS CHICAGO', 'UIC'),
            ('MURRAY STATE', 'MURR'), ('MURRAY ST', 'MURR'), ('MURR', 'MURR'),
            ('SIU EDWARDSVILLE', 'SIUE'), ('SIUE', 'SIUE'), ('EDWARDSVILLE', 'SIUE'),
            ('LINDENWOOD', 'LINW'),
            ('PITTSBURGH', 'PITT'), ('PITT', 'PITT'),
            ('VIRGINIA', 'UVA'), ('UVA', 'UVA'),
            ('AIR FORCE', 'AFA'), ('AFA', 'AFA'),
            ('GRAND CANYON', 'GC'), ('GC', 'GC'),
            ('NEW MEXICO', 'UNM'), ('UNM', 'UNM'), ('NEW MEXICO STATE', 'NMSU'), ('NMSU', 'NMSU'),
            # Note: ARKANSAS LITTLE ROCK moved earlier (before KANSAS) to prevent false matches
            ('TENNESSEE-MARTIN', 'UTM'), ('TENNESSEE MARTIN', 'UTM'), ('UTM', 'UTM'), ('UT MARTIN', 'UTM'),
            ('TENNESSEE STATE', 'TSU'), ('TENNESSEE ST', 'TSU'), ('TSU', 'TSU'), ('TNST', 'TSU'),  # Tennessee State
            ('SAINT LOUIS', 'SLU'), ('ST. LOUIS', 'SLU'), ('ST LOUIS', 'SLU'), ('SLU', 'SLU'),
            ('DAVIDSON', 'DAV'),
            ('UCLA', 'UCLA'),
            ('UNLV', 'UNLV'), ('NEVADA-LAS VEGAS', 'UNLV'), ('NEVADA LAS VEGAS', 'UNLV'),
            ('FRESNO STATE', 'FRES'), ('FRESNO ST', 'FRES'), ('FRES', 'FRES'),
            ('NEVADA', 'NEV'), ('NEV', 'NEV'),
            ('BOISE STATE', 'BSU'), ('BOISE ST', 'BSU'), ('BSU', 'BSU'),
            ('DRAKE', 'DRKE'),
            ('BELMONT', 'BEL'),
            # Add more common teams
            ('KENTUCKY', 'UK'),
            ('CONNECTICUT', 'CONN'), ('UCONN', 'CONN'),
            ('TENNESSEE', 'TENN'),
            ('AUBURN', 'AUB'),
            ('ALABAMA', 'ALA'),
            ('ARKANSAS', 'ARK'),
            ('CLEMSON', 'CLEM'),
            ('MIAMI', 'MIA'),
            ('NOTRE DAME', 'ND'),
            ('PURDUE', 'PUR'),
            ('WISCONSIN', 'WIS'),
            ('MARYLAND-EASTERN SHORE', 'UMES'), ('MARYLAND EASTERN SHORE', 'UMES'), ('UMES', 'UMES'),  # Must come before generic 'MARYLAND'
            ('MARYLAND', 'MD'),
            ('RUTGERS', 'RUTG'),
            ('NEBRASKA', 'NEB'),
            ('MINNESOTA', 'MINN'),
            ('IOWA', 'IOWA'),
            ('CREIGHTON', 'CREI'),
            ('VILLANOVA', 'VILL'),
            ('PROVIDENCE', 'PROV'),
            ('GONZAGA', 'GONZ'),
            ('SAINT MARY\'S', 'SMC'), ("ST. MARY'S", 'SMC'),
            ('BYU', 'BYU'),
            ('HOUSTON', 'HOU'),
            ('MEMPHIS', 'MEM'),
            ('CINCINNATI', 'CIN'),
            ('TEMPLE', 'TEM'),
            ('UAB', 'UAB'),
        ]
        
        # Check mappings (most specific first)
        # Use cleaned selection for matching
        # CRITICAL: Sort by length (longest first) to prevent substring false matches
        # e.g., "ARKANSAS LITTLE ROCK" should match before "KANSAS" or "ARKANSAS"
        sorted_mappings = sorted(mappings, key=lambda x: len(x[0]), reverse=True)
        
        for pattern, code in sorted_mappings:
            if pattern in selection_clean or pattern in selection_upper:
                print(f"   🔍 DEBUG: [MAPPING] Matched '{pattern}' -> '{code}' for selection '{selection_upper}'")
                return code
        
        # Only check learned codes if no hardcoded mapping found AND use_learned is True
        if use_learned:
            if selection_upper in self.learned_team_codes:
                print(f"   🔍 DEBUG: [LEARNED] Matched '{selection_upper}' -> '{self.learned_team_codes[selection_upper]}' (from previous successful match)")
                return self.learned_team_codes[selection_upper]
            if selection_clean in self.learned_team_codes:
                print(f"   🔍 DEBUG: [LEARNED] Matched '{selection_clean}' -> '{self.learned_team_codes[selection_clean]}' (from previous successful match)")
                return self.learned_team_codes[selection_clean]
        
        print(f"   🔍 DEBUG: [MAPPING] No match found for '{selection_upper}' (cleaned: '{selection_clean}')")
        return None
    
    def _get_nhl_team_code(self, selection_upper):
        """Get NHL team code from selection"""
        if not selection_upper:
            return None
        
        # Sort by length (longest first) to prevent substring false matches
        nhl_mappings = [
            # Verified from actual Kalshi tickers
            ('ANAHEIM DUCKS', 'ANA'), ('ANAHEIM', 'ANA'), ('DUCKS', 'ANA'),
            ('BOSTON BRUINS', 'BOS'), ('BOSTON', 'BOS'), ('BRUINS', 'BOS'),
            ('BUFFALO SABRES', 'BUF'), ('BUFFALO', 'BUF'), ('SABRES', 'BUF'),
            ('CALGARY FLAMES', 'CGY'), ('CALGARY', 'CGY'), ('FLAMES', 'CGY'),
            ('CAROLINA HURRICANES', 'CAR'), ('CAROLINA', 'CAR'), ('HURRICANES', 'CAR'),
            ('CHICAGO BLACKHAWKS', 'CHI'), ('CHICAGO', 'CHI'), ('BLACKHAWKS', 'CHI'),
            ('COLORADO AVALANCHE', 'COL'), ('COLORADO', 'COL'), ('AVALANCHE', 'COL'),
            ('COLUMBUS BLUE JACKETS', 'CBJ'), ('COLUMBUS', 'CBJ'), ('BLUE JACKETS', 'CBJ'),
            ('DALLAS STARS', 'DAL'), ('DALLAS', 'DAL'), ('STARS', 'DAL'),
            ('DETROIT RED WINGS', 'DET'), ('DETROIT', 'DET'), ('RED WINGS', 'DET'),
            ('EDMONTON OILERS', 'EDM'), ('EDMONTON', 'EDM'), ('OILERS', 'EDM'),
            ('FLORIDA PANTHERS', 'FLA'), ('FLORIDA', 'FLA'), ('PANTHERS', 'FLA'),
            ('LOS ANGELES KINGS', 'LA'), ('LA KINGS', 'LA'), ('KINGS', 'LA'),
            ('MINNESOTA WILD', 'MIN'), ('MINNESOTA', 'MIN'), ('WILD', 'MIN'),
            ('MONTREAL CANADIENS', 'MTL'), ('MONTREAL', 'MTL'), ('CANADIENS', 'MTL'),
            ('NASHVILLE PREDATORS', 'NSH'), ('NASHVILLE', 'NSH'), ('PREDATORS', 'NSH'),
            ('NEW JERSEY DEVILS', 'NJ'), ('NEW JERSEY', 'NJ'), ('DEVILS', 'NJ'),
            ('NEW YORK ISLANDERS', 'NYI'), ('NY ISLANDERS', 'NYI'), ('ISLANDERS', 'NYI'),
            ('NEW YORK RANGERS', 'NYR'), ('NY RANGERS', 'NYR'), ('RANGERS', 'NYR'),
            ('OTTAWA SENATORS', 'OTT'), ('OTTAWA', 'OTT'), ('SENATORS', 'OTT'),
            ('PHILADELPHIA FLYERS', 'PHI'), ('PHILADELPHIA', 'PHI'), ('FLYERS', 'PHI'),
            ('PITTSBURGH PENGUINS', 'PIT'), ('PITTSBURGH', 'PIT'), ('PENGUINS', 'PIT'),
            ('SAN JOSE SHARKS', 'SJ'), ('SAN JOSE', 'SJ'), ('SHARKS', 'SJ'),
            ('SEATTLE KRAKEN', 'SEA'), ('SEATTLE', 'SEA'), ('KRAKEN', 'SEA'),
            ('ST. LOUIS BLUES', 'STL'), ('ST LOUIS BLUES', 'STL'), ('ST. LOUIS', 'STL'), ('ST LOUIS', 'STL'), ('BLUES', 'STL'),
            ('TAMPA BAY LIGHTNING', 'TB'), ('TAMPA BAY', 'TB'), ('LIGHTNING', 'TB'),
            ('TORONTO MAPLE LEAFS', 'TOR'), ('TORONTO', 'TOR'), ('MAPLE LEAFS', 'TOR'),
            ('UTAH HOCKEY CLUB', 'UTA'), ('UTAH', 'UTA'),
            ('VANCOUVER CANUCKS', 'VAN'), ('VANCOUVER', 'VAN'), ('CANUCKS', 'VAN'),
            ('VEGAS GOLDEN KNIGHTS', 'VGK'), ('VEGAS', 'VGK'), ('GOLDEN KNIGHTS', 'VGK'),
            ('WASHINGTON CAPITALS', 'WSH'), ('WASHINGTON', 'WSH'), ('CAPITALS', 'WSH'),
            ('WINNIPEG JETS', 'WPG'), ('WINNIPEG', 'WPG'), ('JETS', 'WPG'),
        ]
        # Sort by length (longest first) to check most specific patterns first
        sorted_mappings = sorted(nhl_mappings, key=lambda x: len(x[0]), reverse=True)
        
        for pattern, code in sorted_mappings:
            if pattern in selection_upper:
                print(f"   🔍 DEBUG: [NHL MAPPING] Matched '{pattern}' -> '{code}' for selection '{selection_upper}'")
                return code
        
        print(f"   🔍 DEBUG: [NHL MAPPING] No match found for '{selection_upper}'")
        return None
    
    def _get_nba_team_code(self, selection_upper):
        """Get NBA team code from selection"""
        nba_mappings = [
            ('ATLANTA HAWKS', 'ATL'), ('ATLANTA', 'ATL'), ('HAWKS', 'ATL'),
            ('BOSTON CELTICS', 'BOS'), ('BOSTON', 'BOS'), ('CELTICS', 'BOS'),
            ('BROOKLYN NETS', 'BKN'), ('BROOKLYN', 'BKN'), ('NETS', 'BKN'),
            ('CHARLOTTE HORNETS', 'CHA'), ('CHARLOTTE', 'CHA'), ('HORNETS', 'CHA'),
            ('CHICAGO BULLS', 'CHI'), ('CHICAGO', 'CHI'), ('BULLS', 'CHI'),
            ('CLEVELAND CAVALIERS', 'CLE'), ('CLEVELAND', 'CLE'), ('CAVALIERS', 'CLE'),
            ('DALLAS MAVERICKS', 'DAL'), ('DALLAS', 'DAL'), ('MAVERICKS', 'DAL'),
            ('DENVER NUGGETS', 'DEN'), ('DENVER', 'DEN'), ('NUGGETS', 'DEN'),
            ('DETROIT PISTONS', 'DET'), ('DETROIT', 'DET'), ('PISTONS', 'DET'),
            ('GOLDEN STATE WARRIORS', 'GSW'), ('GOLDEN STATE', 'GSW'), ('WARRIORS', 'GSW'),
            ('HOUSTON ROCKETS', 'HOU'), ('HOUSTON', 'HOU'), ('ROCKETS', 'HOU'),
            ('INDIANA PACERS', 'IND'), ('INDIANA', 'IND'), ('PACERS', 'IND'),
            ('LOS ANGELES CLIPPERS', 'LAC'), ('CLIPPERS', 'LAC'),
            ('LOS ANGELES LAKERS', 'LAL'), ('LAKERS', 'LAL'),
            ('MEMPHIS GRIZZLIES', 'MEM'), ('MEMPHIS', 'MEM'), ('GRIZZLIES', 'MEM'),
            ('MIAMI HEAT', 'MIA'), ('MIAMI', 'MIA'), ('HEAT', 'MIA'),
            ('MILWAUKEE BUCKS', 'MIL'), ('MILWAUKEE', 'MIL'), ('BUCKS', 'MIL'),
            ('MINNESOTA TIMBERWOLVES', 'MIN'), ('MINNESOTA', 'MIN'), ('TIMBERWOLVES', 'MIN'),
            ('NEW ORLEANS PELICANS', 'NOP'), ('NEW ORLEANS', 'NOP'), ('PELICANS', 'NOP'),
            ('NEW YORK KNICKS', 'NYK'), ('NY KNICKS', 'NYK'), ('KNICKS', 'NYK'),
            ('OKLAHOMA CITY THUNDER', 'OKC'), ('OKLAHOMA CITY', 'OKC'), ('THUNDER', 'OKC'),
            ('ORLANDO MAGIC', 'ORL'), ('ORLANDO', 'ORL'), ('MAGIC', 'ORL'),
            ('PHILADELPHIA 76ERS', 'PHI'), ('PHILADELPHIA', 'PHI'), ('76ERS', 'PHI'),
            ('PHOENIX SUNS', 'PHX'), ('PHOENIX', 'PHX'), ('SUNS', 'PHX'),
            ('PORTLAND TRAIL BLAZERS', 'POR'), ('PORTLAND', 'POR'), ('TRAIL BLAZERS', 'POR'),
            ('SACRAMENTO KINGS', 'SAC'), ('SACRAMENTO', 'SAC'), ('KINGS', 'SAC'),
            ('SAN ANTONIO SPURS', 'SAS'), ('SAN ANTONIO', 'SAS'), ('SPURS', 'SAS'),
            ('TORONTO RAPTORS', 'TOR'), ('TORONTO', 'TOR'), ('RAPTORS', 'TOR'),
            ('UTAH JAZZ', 'UTA'), ('UTAH', 'UTA'), ('JAZZ', 'UTA'),
            ('WASHINGTON WIZARDS', 'WAS'), ('WASHINGTON', 'WAS'), ('WIZARDS', 'WAS'),
        ]
        # Sort by length (longest first) to check most specific patterns first
        sorted_mappings = sorted(nba_mappings, key=lambda x: len(x[0]), reverse=True)
        
        for pattern, code in sorted_mappings:
            if pattern in selection_upper:
                print(f"   🔍 DEBUG: [NBA MAPPING] Matched '{pattern}' -> '{code}' for selection '{selection_upper}'")
                return code
        
        print(f"   🔍 DEBUG: [NBA MAPPING] No match found for '{selection_upper}'")
        return None
    
    def _get_nfl_team_code(self, selection_upper):
        """Get NFL team code from selection"""
        nfl_mappings = [
            ('ARIZONA CARDINALS', 'ARI'), ('ARIZONA', 'ARI'), ('CARDINALS', 'ARI'),
            ('ATLANTA FALCONS', 'ATL'), ('ATLANTA', 'ATL'), ('FALCONS', 'ATL'),
            ('BALTIMORE RAVENS', 'BAL'), ('BALTIMORE', 'BAL'), ('RAVENS', 'BAL'),
            ('BUFFALO BILLS', 'BUF'), ('BUFFALO', 'BUF'), ('BILLS', 'BUF'),
            ('CAROLINA PANTHERS', 'CAR'), ('CAROLINA', 'CAR'), ('PANTHERS', 'CAR'),
            ('CHICAGO BEARS', 'CHI'), ('CHICAGO', 'CHI'), ('BEARS', 'CHI'),
            ('CINCINNATI BENGALS', 'CIN'), ('CINCINNATI', 'CIN'), ('BENGALS', 'CIN'),
            ('CLEVELAND BROWNS', 'CLE'), ('CLEVELAND', 'CLE'), ('BROWNS', 'CLE'),
            ('DALLAS COWBOYS', 'DAL'), ('DALLAS', 'DAL'), ('COWBOYS', 'DAL'),
            ('DENVER BRONCOS', 'DEN'), ('DENVER', 'DEN'), ('BRONCOS', 'DEN'),
            ('DETROIT LIONS', 'DET'), ('DETROIT', 'DET'), ('LIONS', 'DET'),
            ('GREEN BAY PACKERS', 'GB'), ('GREEN BAY', 'GB'), ('PACKERS', 'GB'),
            ('HOUSTON TEXANS', 'HOU'), ('HOUSTON', 'HOU'), ('TEXANS', 'HOU'),
            ('INDIANAPOLIS COLTS', 'IND'), ('INDIANAPOLIS', 'IND'), ('COLTS', 'IND'),
            ('JACKSONVILLE JAGUARS', 'JAX'), ('JACKSONVILLE', 'JAX'), ('JAGUARS', 'JAX'),
            ('KANSAS CITY CHIEFS', 'KC'), ('KANSAS CITY', 'KC'), ('CHIEFS', 'KC'),
            ('LAS VEGAS RAIDERS', 'LV'), ('LAS VEGAS', 'LV'), ('RAIDERS', 'LV'),
            ('LOS ANGELES CHARGERS', 'LAC'), ('CHARGERS', 'LAC'),
            ('LOS ANGELES RAMS', 'LAR'), ('RAMS', 'LAR'),
            ('MIAMI DOLPHINS', 'MIA'), ('MIAMI', 'MIA'), ('DOLPHINS', 'MIA'),
            ('MINNESOTA VIKINGS', 'MIN'), ('MINNESOTA', 'MIN'), ('VIKINGS', 'MIN'),
            ('NEW ENGLAND PATRIOTS', 'NE'), ('NEW ENGLAND', 'NE'), ('PATRIOTS', 'NE'),
            ('NEW ORLEANS SAINTS', 'NO'), ('NEW ORLEANS', 'NO'), ('SAINTS', 'NO'),
            ('NEW YORK GIANTS', 'NYG'), ('NY GIANTS', 'NYG'), ('GIANTS', 'NYG'),
            ('NEW YORK JETS', 'NYJ'), ('NY JETS', 'NYJ'), ('JETS', 'NYJ'),
            ('PHILADELPHIA EAGLES', 'PHI'), ('PHILADELPHIA', 'PHI'), ('EAGLES', 'PHI'),
            ('PITTSBURGH STEELERS', 'PIT'), ('PITTSBURGH', 'PIT'), ('STEELERS', 'PIT'),
            ('SAN FRANCISCO 49ERS', 'SF'), ('SAN FRANCISCO', 'SF'), ('49ERS', 'SF'),
            ('SEATTLE SEAHAWKS', 'SEA'), ('SEATTLE', 'SEA'), ('SEAHAWKS', 'SEA'),
            ('TAMPA BAY BUCCANEERS', 'TB'), ('TAMPA BAY', 'TB'), ('BUCCANEERS', 'TB'),
            ('TENNESSEE TITANS', 'TEN'), ('TENNESSEE', 'TEN'), ('TITANS', 'TEN'),
            ('WASHINGTON COMMANDERS', 'WAS'), ('WASHINGTON', 'WAS'), ('COMMANDERS', 'WAS'),
        ]
        for pattern, code in nfl_mappings:
            if pattern in selection_upper:
                return code
        return None
    
    def _get_ncaaf_team_code(self, selection_upper):
        """Get NCAAF team code from selection - many overlap with NCAAB"""
        # NCAAF uses similar codes to NCAAB, but we'll add sport-specific ones here
        # Most teams can use the NCAAB mapping, but some may differ
        # For now, return None to fall back to NCAAB logic
        # This can be expanded with NCAAF-specific mappings if needed
        return None
    
    def _log_mapping_mismatch(self, event_ticker, market_type, line, selection, teams_str, built_ticker, reason):
        """Log team mapping mismatches to help identify incorrect mappings"""
        import json
        import os
        from datetime import datetime
        
        log_file = "team_mapping_mismatches.jsonl"
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "event_ticker": event_ticker,
            "market_type": market_type,
            "line": line,
            "selection": selection,
            "teams_str": teams_str,
            "built_ticker": built_ticker,
            "reason": reason
        }
        
        try:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry) + '\n')
            print(f"   📝 [MISMATCH LOG] Logged mapping issue to {log_file}")
        except Exception as e:
            print(f"   ⚠️  [MISMATCH LOG] Failed to log mismatch: {e}")
    
    def build_market_ticker(self, event_ticker, market_type, line, selection, teams_str=None):
        """
        Build the exact market ticker from event ticker, market type, line, and selection.
        This is MUCH faster than querying 1000 markets and filtering!
        
        Format examples:
        - Total 143.5: KXNCAAMBTOTAL-26JAN31DUKEVT-143
        - Spread -5.5 Marquette: KXNCAAMBSPREAD-26JAN31MARQHALL-MARQ5
        - Moneyline Wagner: KXNCAAMBGAME-26JAN31FDUWAG-WAG
        """
        event_ticker_upper = event_ticker.upper()
        market_type_lower = market_type.lower()
        selection_upper = selection.upper() if selection else ""
        
        # Extract event suffix (e.g., "26JAN31DUKEVT" from "KXNCAAMBGAME-26JAN31DUKEVT")
        event_parts = event_ticker_upper.split('-')
        if len(event_parts) < 2:
            return None
        event_suffix = '-'.join(event_parts[1:])  # Everything after first dash
        
        # Determine series prefix (KXNCAAMBTOTAL, KXNCAAMBSPREAD, KXNCAAMBGAME)
        base_series = event_parts[0]  # e.g., "KXNCAAMBGAME"
        
        # CRITICAL: Extract team codes from event ticker (FASTEST and MOST ACCURATE!)
        team_code1, team_code2 = self._extract_team_codes_from_event_ticker(event_ticker, teams_str, selection)
        if team_code1 or team_code2:
            print(f"   🔍 DEBUG: Extracted team codes from event ticker: code1={team_code1}, code2={team_code2}")
            if teams_str:
                print(f"   🔍 DEBUG: Teams string: {teams_str}, Selection: {selection}")
        
        # Build ticker based on market type
        if 'total' in market_type_lower or 'over' in market_type_lower or 'under' in market_type_lower:
            if line is None:
                return None
            # Total: KXNCAAMBTOTAL-26JAN31DUKEVT-143 (143 = integer part of 143.5)
            line_int = int(float(line))
            total_series = base_series.replace('GAME', 'TOTAL').replace('SPREAD', 'TOTAL')
            return f"{total_series}-{event_suffix}-{line_int}"
        
        elif 'spread' in market_type_lower or 'puck line' in market_type_lower:
            if line is None or not selection:
                return None
            # Spread: KXNCAAMBSPREAD-26JAN31MARQHALL-MARQ5 (MARQ = team code, 5 = integer part of 5.5)
            # CRITICAL: For underdog spreads (+line), Kalshi uses the FAVORITE's team code!
            # Example: "Arizona State +11.5" = KXNCAAMBSPREAD-26JAN31ARIZASU-ARIZ11 (uses ARIZ, not ASU)
            line_float = float(line)
            line_int = int(abs(line_float))
            spread_series = base_series.replace('GAME', 'SPREAD').replace('TOTAL', 'SPREAD')
            
            # Check if this is an underdog spread (positive line)
            is_underdog = line_float > 0
            
            # Get team code - ALWAYS try explicit mappings FIRST (MOST RELIABLE!), extraction is LAST RESORT
            team_code = None
            selection_upper = selection.upper() if selection else ""
            
            # STEP 1: Check explicit mappings FIRST (most reliable)
            # CRITICAL: For underdog spreads (+line), we need the OTHER team's code (the favorite)
            # For favorite spreads (-line), we need the selection's team code
            if selection_upper and teams_str:
                teams_upper = teams_str.upper()
                team_parts = teams_upper.split('@')
                if len(team_parts) == 2:
                    away_team = team_parts[0].strip()
                    home_team = team_parts[1].strip()
                    
                    # Check which team is the selection
                    selection_matches_away = (selection_upper in away_team or away_team in selection_upper or
                                              any(word in selection_upper for word in away_team.split() if len(word) > 2) or
                                              any(word in away_team for word in selection_upper.split() if len(word) > 2))
                    selection_matches_home = (selection_upper in home_team or home_team in selection_upper or
                                              any(word in selection_upper for word in home_team.split() if len(word) > 2) or
                                              any(word in home_team for word in selection_upper.split() if len(word) > 2))
                    
                    if is_underdog:
                        # Underdog spread: need the OTHER team's code (favorite)
                        if selection_matches_away:
                            # Selection is away team, favorite is home team
                            favorite_team = home_team
                        elif selection_matches_home:
                            # Selection is home team, favorite is away team
                            favorite_team = away_team
                        else:
                            favorite_team = None
                        
                        if favorite_team:
                            # Use explicit mapping for the favorite team
                            if 'KXNCAAMBGAME' in base_series or 'KXNCAAMB' in base_series:
                                team_code = self._get_ncaab_team_code(favorite_team)
                            elif 'KXNHLGAME' in base_series or 'KXNHL' in base_series:
                                team_code = self._get_nhl_team_code(favorite_team)
                            elif 'KXNBAGAME' in base_series or 'KXNBA' in base_series:
                                team_code = self._get_nba_team_code(favorite_team)
                            elif 'KXNFLGAME' in base_series or 'KXNFL' in base_series:
                                team_code = self._get_nfl_team_code(favorite_team)
                            elif 'KXNCAAFGAME' in base_series or 'KXNCAAF' in base_series:
                                team_code = self._get_ncaaf_team_code(favorite_team) or self._get_ncaab_team_code(favorite_team)
                            else:
                                team_code = self._get_ncaab_team_code(favorite_team)
                            
                            if team_code:
                                print(f"   🔍 DEBUG: Underdog spread - using explicit mapping for favorite {favorite_team} -> {team_code}")
                    else:
                        # Favorite spread: use the selection's team code
                        if 'KXNCAAMBGAME' in base_series or 'KXNCAAMB' in base_series:
                            team_code = self._get_ncaab_team_code(selection_upper)
                        elif 'KXNHLGAME' in base_series or 'KXNHL' in base_series:
                            team_code = self._get_nhl_team_code(selection_upper)
                        elif 'KXNBAGAME' in base_series or 'KXNBA' in base_series:
                            team_code = self._get_nba_team_code(selection_upper)
                        elif 'KXNFLGAME' in base_series or 'KXNFL' in base_series:
                            team_code = self._get_nfl_team_code(selection_upper)
                        elif 'KXNCAAFGAME' in base_series or 'KXNCAAF' in base_series:
                            team_code = self._get_ncaaf_team_code(selection_upper) or self._get_ncaab_team_code(selection_upper)
                        else:
                            team_code = self._get_ncaab_team_code(selection_upper)
                        
                        if team_code:
                            print(f"   🔍 DEBUG: Favorite spread - using explicit mapping for selection {selection_upper} -> {team_code}")
            
            # STEP 2: If explicit mapping failed, try extraction as LAST RESORT (unreliable!)
            if not team_code:
                print(f"   ⚠️  WARNING: Explicit mapping failed, falling back to extraction (unreliable)")
                # Try to use extracted codes as last resort
                if is_underdog and teams_str and team_code1 and team_code2:
                    # For underdog spreads, use the OTHER team's code
                    teams_upper = teams_str.upper()
                    team_parts = teams_upper.split('@')
                    if len(team_parts) == 2:
                        away_team = team_parts[0].strip()
                        home_team = team_parts[1].strip()
                        
                        # Check if selection matches away team
                        selection_matches_away = (selection_upper in away_team or away_team in selection_upper or
                                                  any(word in selection_upper for word in away_team.split() if len(word) > 2) or
                                                  any(word in away_team for word in selection_upper.split() if len(word) > 2))
                        # Check if selection matches home team
                        selection_matches_home = (selection_upper in home_team or home_team in selection_upper or
                                                  any(word in selection_upper for word in home_team.split() if len(word) > 2) or
                                                  any(word in home_team for word in selection_upper.split() if len(word) > 2))
                        
                        # If selection is underdog, use the OTHER team's code (favorite)
                        if selection_matches_away:
                            team_code = team_code2  # Use home team code (favorite)
                            print(f"   🔍 DEBUG: [EXTRACTION FALLBACK] Underdog spread - selection {selection_upper} matches away team, using extracted home code {team_code2}")
                        elif selection_matches_home:
                            team_code = team_code1  # Use away team code (favorite)
                            print(f"   🔍 DEBUG: [EXTRACTION FALLBACK] Underdog spread - selection {selection_upper} matches home team, using extracted away code {team_code1}")
                        else:
                            print(f"   ⚠️  WARNING: Could not match selection {selection_upper} to either team in {teams_str}")
                elif not is_underdog and teams_str and team_code1 and team_code2:
                    # For favorite spreads, match selection to team name, then use corresponding code
                    teams_upper = teams_str.upper()
                    team_parts = teams_upper.split('@')
                    if len(team_parts) == 2:
                        away_team = team_parts[0].strip()
                        home_team = team_parts[1].strip()
                        
                        # Check if selection matches away team
                        selection_matches_away = (selection_upper in away_team or away_team in selection_upper or
                                                  any(word in selection_upper for word in away_team.split() if len(word) > 2) or
                                                  any(word in away_team for word in selection_upper.split() if len(word) > 2))
                        # Check if selection matches home team
                        selection_matches_home = (selection_upper in home_team or home_team in selection_upper or
                                                  any(word in selection_upper for word in home_team.split() if len(word) > 2) or
                                                  any(word in home_team for word in selection_upper.split() if len(word) > 2))
                        
                        # If selection matches away team, use code1; if matches home team, use code2
                        if selection_matches_away:
                            team_code = team_code1
                            print(f"   🔍 DEBUG: [EXTRACTION FALLBACK] Favorite spread - selection {selection_upper} matches away team, using extracted code1 {team_code1}")
                        elif selection_matches_home:
                            team_code = team_code2
                            print(f"   🔍 DEBUG: [EXTRACTION FALLBACK] Favorite spread - selection {selection_upper} matches home team, using extracted code2 {team_code2}")
                        else:
                            print(f"   ⚠️  WARNING: Could not match selection {selection_upper} to either team in {teams_str}")
            
            # STEP 3: Final fallback - old hardcoded mappings (should rarely be needed, most should be in _get_ncaab_team_code)
            # These are legacy mappings kept as absolute last resort
            if not team_code and selection_upper:
                # Only keep critical ones that might not be in _get_ncaab_team_code
                if 'TEXAS A&M CORPUS CHRISTI' in selection_upper or 'TEXAS A & M CORPUS CHRISTI' in selection_upper:
                    team_code = 'AMCC'
                elif 'TEXAS A&M COMMERCE' in selection_upper or 'TEXAS A & M COMMERCE' in selection_upper:
                    team_code = 'TAMC'
                elif ('TEXAS A&M' in selection_upper or 'TEXAS A & M' in selection_upper) and 'CORPUS' not in selection_upper and 'COMMERCE' not in selection_upper:
                    team_code = 'TXAM'
            
            # If still no team_code, we failed - extraction is too unreliable to use
            if not team_code:
                print(f"   ❌ ERROR: Could not determine team code for spread - selection={selection}, teams={teams_str}, is_underdog={is_underdog}")
                return None
            
            # Build the spread ticker
            return f"{spread_series}-{event_suffix}-{team_code}{line_int}"
        
        elif 'moneyline' in market_type_lower or 'game' in market_type_lower:
            if not selection:
                return None
            # Moneyline: KXNCAAMBGAME-26JAN31FDUWAG-WAG (WAG = team code)
            # Get team code - FIRST try hardcoded mapping (MOST RELIABLE!), then extraction
            team_code = None
            selection_upper = selection.upper() if selection else ""
            
            # STEP 1: Check sport-specific team code map FIRST (most reliable)
            if selection_upper:
                # Determine sport from event ticker
                if 'KXNCAAMBGAME' in base_series or 'KXNCAAMB' in base_series:
                    # NCAAB
                    team_code = self._get_ncaab_team_code(selection_upper)
                    # CRITICAL: If mapping found team code, build ticker immediately (no fallbacks needed)
                    if team_code and base_series and event_suffix:
                        built_ticker = f"{base_series}-{event_suffix}-{team_code}"
                        print(f"   🔨 [MONEYLINE] Built ticker immediately after NCAAB mapping: {built_ticker}")
                        return built_ticker
                elif 'KXNHLGAME' in base_series or 'KXNHL' in base_series:
                    # NHL
                    team_code = self._get_nhl_team_code(selection_upper)
                    # CRITICAL: If mapping found team code, build ticker immediately (no fallbacks needed)
                    if team_code and base_series and event_suffix:
                        built_ticker = f"{base_series}-{event_suffix}-{team_code}"
                        print(f"   🔨 [MONEYLINE] Built ticker immediately after NHL mapping: {built_ticker}")
                        return built_ticker
                elif 'KXNBAGAME' in base_series or 'KXNBA' in base_series:
                    # NBA
                    team_code = self._get_nba_team_code(selection_upper)
                    # CRITICAL: If mapping found team code, build ticker immediately (no fallbacks needed)
                    if team_code and base_series and event_suffix:
                        built_ticker = f"{base_series}-{event_suffix}-{team_code}"
                        print(f"   🔨 [MONEYLINE] Built ticker immediately after NBA mapping: {built_ticker}")
                        return built_ticker
                elif 'KXNFLGAME' in base_series or 'KXNFL' in base_series:
                    # NFL
                    team_code = self._get_nfl_team_code(selection_upper)
                    # CRITICAL: If mapping found team code, build ticker immediately (no fallbacks needed)
                    if team_code and base_series and event_suffix:
                        built_ticker = f"{base_series}-{event_suffix}-{team_code}"
                        print(f"   🔨 [MONEYLINE] Built ticker immediately after NFL mapping: {built_ticker}")
                        return built_ticker
                elif 'KXNCAAFGAME' in base_series or 'KXNCAAF' in base_series:
                    # NCAAF - fallback to NCAAB since many teams overlap
                    team_code = self._get_ncaaf_team_code(selection_upper) or self._get_ncaab_team_code(selection_upper)
                    # CRITICAL: If mapping found team code, build ticker immediately (no fallbacks needed)
                    if team_code and base_series and event_suffix:
                        built_ticker = f"{base_series}-{event_suffix}-{team_code}"
                        print(f"   🔨 [MONEYLINE] Built ticker immediately after NCAAF mapping: {built_ticker}")
                        return built_ticker
                else:
                    # Default to NCAAB for backwards compatibility
                    team_code = self._get_ncaab_team_code(selection_upper)
                    # CRITICAL: If mapping found team code, build ticker immediately (no fallbacks needed)
                    if team_code and base_series and event_suffix:
                        built_ticker = f"{base_series}-{event_suffix}-{team_code}"
                        print(f"   🔨 [MONEYLINE] Built ticker immediately after default NCAAB mapping: {built_ticker}")
                        return built_ticker
                
                # Fallback to old hardcoded mappings if helper didn't find it
                if not team_code:
                    # College basketball team codes (from actual market tickers)
                    # CRITICAL: Check most specific first to avoid false matches
                    if 'TEXAS A&M CORPUS CHRISTI' in selection_upper or 'TEXAS A & M CORPUS CHRISTI' in selection_upper:
                        team_code = 'AMCC'
                    elif 'TEXAS A&M COMMERCE' in selection_upper or 'TEXAS A & M COMMERCE' in selection_upper:
                        team_code = 'TAMC'
                    elif 'TEXAS TECH' in selection_upper or 'TEXAS TECH RED RAIDERS' in selection_upper:
                        team_code = 'TTU'
                    elif 'UCF' in selection_upper or 'CENTRAL FLORIDA' in selection_upper:
                        team_code = 'UCF'
                    elif 'GEORGETOWN' in selection_upper:
                        team_code = 'GTWN'
                elif 'BUTLER' in selection_upper:
                    team_code = 'BUT'
                elif 'HOLY CROSS' in selection_upper:
                    team_code = 'HC'
                elif 'STONEHILL' in selection_upper:
                    team_code = 'STNH'
                elif 'LE MOYNE' in selection_upper:
                    team_code = 'LMC'
                elif 'SETON HALL' in selection_upper or 'HALL' in selection_upper:
                    team_code = 'HALL'
                elif 'MARQUETTE' in selection_upper:
                    team_code = 'MARQ'
                elif 'WAGNER' in selection_upper:
                    team_code = 'WAG'
                elif 'FAIRLEIGH' in selection_upper or 'FDU' in selection_upper:
                    team_code = 'FDU'
                elif 'DUKE' in selection_upper:
                    team_code = 'DUKE'
                elif 'VIRGINIA TECH' in selection_upper or 'VT' in selection_upper:
                    team_code = 'VT'
                elif 'TEXAS A&M' in selection_upper or 'TEXAS A & M' in selection_upper:
                    team_code = 'TXAM'
                elif 'GEORGIA' in selection_upper:
                    team_code = 'UGA'
                elif 'ARMY' in selection_upper:
                    team_code = 'ARMY'
                elif 'ROBERT MORRIS' in selection_upper:
                    team_code = 'RMU'
                elif 'PURDUE FORT WAYNE' in selection_upper or 'IPFW' in selection_upper:
                    team_code = 'PFW'
                elif 'WILLIAM' in selection_upper and 'MARY' in selection_upper:
                    team_code = 'WM'
                elif 'CAMPBELL' in selection_upper:
                    team_code = 'CAMP'
                elif 'NC STATE' in selection_upper or 'NORTH CAROLINA STATE' in selection_upper:
                    team_code = 'NCST'
                elif 'WAKE FOREST' in selection_upper:
                    team_code = 'WAKE'
                elif 'BALL STATE' in selection_upper:
                    team_code = 'BSU'
                elif 'TOLEDO' in selection_upper:
                    team_code = 'TOL'
                elif 'UNC GREENSBORO' in selection_upper:
                    team_code = 'UNCG'
                elif 'THE CITADEL' in selection_upper or 'CITADEL' in selection_upper:
                    team_code = 'CIT'
                elif 'SAN DIEGO STATE' in selection_upper:
                    team_code = 'SDSU'
                elif 'UTAH STATE' in selection_upper:
                    team_code = 'USU'
                elif 'SOUTH CAROLINA' in selection_upper:
                    team_code = 'SCAR'
                elif 'LSU' in selection_upper or 'LOUISIANA STATE' in selection_upper:
                    team_code = 'LSU'
                elif 'ARIZONA STATE' in selection_upper or 'ASU' in selection_upper:
                    team_code = 'ASU'
                elif 'ARIZONA' in selection_upper:
                    team_code = 'ARIZ'
                elif 'INDIANA STATE' in selection_upper:
                    team_code = 'INST'
                elif 'VALPARAISO' in selection_upper:
                    team_code = 'VALP'
                elif 'TEXAS' in selection_upper and 'A&M' not in selection_upper and 'TECH' not in selection_upper:
                    team_code = 'TEX'
                elif 'OKLAHOMA' in selection_upper:
                    team_code = 'OKLA'
                elif 'SMU' in selection_upper or 'SOUTHERN METHODIST' in selection_upper:
                    team_code = 'SMU'
                elif 'LOUISVILLE' in selection_upper:
                    team_code = 'LOU'
                elif 'NORTH ALABAMA' in selection_upper:
                    team_code = 'UNA'
                elif 'STETSON' in selection_upper:
                    team_code = 'STET'
                elif 'RADFORD' in selection_upper:
                    team_code = 'RAD'
                elif 'PRESBYTERIAN' in selection_upper:
                    team_code = 'PRES'
                elif 'LIPSCOMB' in selection_upper:
                    team_code = 'LIP'
                elif 'NORTH FLORIDA' in selection_upper:
                    team_code = 'UNF'
                elif 'UMASS LOWELL' in selection_upper or 'UMASS' in selection_upper and 'LOWELL' in selection_upper:
                    team_code = 'MASSL'
                elif 'MAINE' in selection_upper:
                    team_code = 'ME'
                elif 'CHARLESTON SOUTHERN' in selection_upper:
                    team_code = 'COFC'
                elif 'NORTHEASTERN' in selection_upper:
                    team_code = 'NE'
                elif 'DEPAUL' in selection_upper:
                    team_code = 'DEP'
                elif 'XAVIER' in selection_upper:
                    team_code = 'XAV'
                elif 'PRESBYTERIAN' in selection_upper:
                    team_code = 'PRE'  # Not PRES - event ticker shows PRE
                elif 'LONG ISLAND' in selection_upper or 'LIU' in selection_upper:
                    team_code = 'LIU'  # Not LI - event ticker shows LIU
                elif 'CENTRAL CONNECTICUT' in selection_upper or 'CENTRAL CONNECTICUT STATE' in selection_upper:
                    team_code = 'CCSU'
                elif 'SAINT JOSEPH' in selection_upper or 'ST JOSEPH' in selection_upper:
                    team_code = 'JOES'  # Not JO - event ticker shows JOES
                elif 'LA SALLE' in selection_upper:
                    team_code = 'LAS'
                elif 'HOFSTRA' in selection_upper:
                    team_code = 'HOF'
                elif 'MONMOUTH' in selection_upper:
                    team_code = 'MON'
                elif 'OHIO' in selection_upper and 'STATE' not in selection_upper:
                    team_code = 'OHIO'  # Not OH - event ticker shows OHIO
                elif 'BUFFALO' in selection_upper:
                    team_code = 'BUFF'
                elif 'OLD DOMINION' in selection_upper:
                    team_code = 'ODU'
                elif 'TEXAS STATE' in selection_upper:
                    team_code = 'TXST'
                elif 'QUEENS' in selection_upper:
                    team_code = 'QU'
                elif 'BELLARMINE' in selection_upper:
                    team_code = 'BEL'
                elif 'IDAHO' in selection_upper:
                    team_code = 'IDHO'  # Not ID - event ticker shows IDHO
                elif 'NORTH TEXAS' in selection_upper or 'UNT' in selection_upper:
                    team_code = 'UNT'  # Not TEX - North Texas is UNT
                elif 'GEORGIA STATE' in selection_upper:
                    team_code = 'GAST'
                elif 'SOUTH ALABAMA' in selection_upper:
                    team_code = 'USA'
                elif 'SANTA CLARA' in selection_upper:
                    team_code = 'SCU'
                elif 'LOYOLA MARYMOUNT' in selection_upper or 'LMU' in selection_upper:
                    team_code = 'LMU'
                # NHL team codes
                elif 'ST. LOUIS' in selection_upper or 'ST LOUIS' in selection_upper or 'STLOUIS' in selection_upper:
                    if 'BLUES' in selection_upper:
                        team_code = 'STL'  # Not JSTL - St. Louis Blues is STL
                elif 'COLUMBUS' in selection_upper:
                    if 'BLUE JACKETS' in selection_upper or 'JACKETS' in selection_upper:
                        team_code = 'CBJ'
            
            # STEP 2: If hardcoded map didn't work, try extraction from event ticker
            if not team_code and team_code1 and team_code2:
                # Match selection to one of the extracted team codes
                selection_upper = selection.upper() if selection else ""
                if selection_upper:
                    # CRITICAL: Use teams_str to determine which code is which
                    # Event ticker format is [DATE][AWAY][HOME], so code1=away, code2=home
                    if teams_str:
                        teams_upper = teams_str.upper()
                        team_parts = teams_upper.split('@')
                        if len(team_parts) == 2:
                            away_team = team_parts[0].strip()
                            home_team = team_parts[1].strip()
                            
                            # Check if selection matches away team
                            selection_matches_away = (selection_upper in away_team or away_team in selection_upper or
                                                      any(word in selection_upper for word in away_team.split() if len(word) > 2) or
                                                      any(word in away_team for word in selection_upper.split() if len(word) > 2))
                            # Check if selection matches home team
                            selection_matches_home = (selection_upper in home_team or home_team in selection_upper or
                                                      any(word in selection_upper for word in home_team.split() if len(word) > 2) or
                                                      any(word in home_team for word in selection_upper.split() if len(word) > 2))
                            
                            # For moneyline, just match selection to the correct team code
                            if selection_matches_away:
                                team_code = team_code1  # Away team = first code
                            elif selection_matches_home:
                                team_code = team_code2  # Home team = second code
                    
                    # Fallback: Try direct substring matching if teams_str didn't work
                    if not team_code:
                        # For moneyline, match selection to whichever code it matches
                        if (team_code1 in selection_upper or selection_upper.startswith(team_code1) or
                            any(word.startswith(team_code1) for word in selection_upper.split())):
                            team_code = team_code1
                        elif (team_code2 in selection_upper or selection_upper.startswith(team_code2) or
                              any(word.startswith(team_code2) for word in selection_upper.split())):
                            team_code = team_code2
            
        
        elif 'moneyline' in market_type_lower or 'game' in market_type_lower:
            if not selection:
                return None
            # Moneyline: KXNCAAMBGAME-26JAN31FDUWAG-WAG (WAG = team code)
            # Get team code - FIRST try hardcoded mapping (MOST RELIABLE!), then extraction
            team_code = None
            selection_upper = selection.upper() if selection else ""
            
            # STEP 1: Check sport-specific team code map FIRST (most reliable)
            if selection_upper:
                # Determine sport from event ticker
                if 'KXNCAAMBGAME' in base_series or 'KXNCAAMB' in base_series:
                    # NCAAB
                    print(f"   🔍 DEBUG: [MONEYLINE NCAAB] Attempting to map selection '{selection_upper}'")
                    team_code = self._get_ncaab_team_code(selection_upper)
                    print(f"   🔍 DEBUG: [MONEYLINE NCAAB] After mapping: team_code={repr(team_code)}, base_series={repr(base_series)}, event_suffix={repr(event_suffix)}")
                    # CRITICAL: If mapping found team code, build ticker immediately (no fallbacks needed)
                    if team_code and base_series and event_suffix:
                        built_ticker = f"{base_series}-{event_suffix}-{team_code}"
                        print(f"   🔨 [MONEYLINE] ✅ Built ticker immediately after NCAAB mapping: {built_ticker}")
                        return built_ticker
                    else:
                        print(f"   ⚠️  [MONEYLINE NCAAB] ❌ Early return skipped: team_code={repr(team_code)}, base_series={repr(base_series)}, event_suffix={repr(event_suffix)}")
                        if not team_code:
                            print(f"   ⚠️  [MONEYLINE NCAAB] ❌ FAILED: No team code found for selection '{selection_upper}'")
                        if not base_series:
                            print(f"   ⚠️  [MONEYLINE NCAAB] ❌ FAILED: No base_series (event_ticker={repr(event_ticker)})")
                        if not event_suffix:
                            print(f"   ⚠️  [MONEYLINE NCAAB] ❌ FAILED: No event_suffix (event_ticker={repr(event_ticker)})")
                elif 'KXNHLGAME' in base_series or 'KXNHL' in base_series:
                    # NHL
                    team_code = self._get_nhl_team_code(selection_upper)
                    # CRITICAL: If mapping found team code, build ticker immediately (no fallbacks needed)
                    if team_code and base_series and event_suffix:
                        built_ticker = f"{base_series}-{event_suffix}-{team_code}"
                        print(f"   🔨 [MONEYLINE] Built ticker immediately after NHL mapping: {built_ticker}")
                        return built_ticker
                elif 'KXNBAGAME' in base_series or 'KXNBA' in base_series:
                    # NBA
                    team_code = self._get_nba_team_code(selection_upper)
                    print(f"   🔍 DEBUG: [MONEYLINE NBA] After mapping: team_code={repr(team_code)}, base_series={repr(base_series)}, event_suffix={repr(event_suffix)}")
                    # CRITICAL: If mapping found team code, build ticker immediately (no fallbacks needed)
                    if team_code and base_series and event_suffix:
                        built_ticker = f"{base_series}-{event_suffix}-{team_code}"
                        print(f"   🔨 [MONEYLINE] Built ticker immediately after NBA mapping: {built_ticker}")
                        return built_ticker
                    else:
                        print(f"   ⚠️  [MONEYLINE NBA] Early return skipped: team_code={repr(team_code)}, base_series={repr(base_series)}, event_suffix={repr(event_suffix)}")
                elif 'KXNFLGAME' in base_series or 'KXNFL' in base_series:
                    # NFL
                    team_code = self._get_nfl_team_code(selection_upper)
                    # CRITICAL: If mapping found team code, build ticker immediately (no fallbacks needed)
                    if team_code and base_series and event_suffix:
                        built_ticker = f"{base_series}-{event_suffix}-{team_code}"
                        print(f"   🔨 [MONEYLINE] Built ticker immediately after NFL mapping: {built_ticker}")
                        return built_ticker
                elif 'KXNCAAFGAME' in base_series or 'KXNCAAF' in base_series:
                    # NCAAF - fallback to NCAAB since many teams overlap
                    team_code = self._get_ncaaf_team_code(selection_upper) or self._get_ncaab_team_code(selection_upper)
                    # CRITICAL: If mapping found team code, build ticker immediately (no fallbacks needed)
                    if team_code and base_series and event_suffix:
                        built_ticker = f"{base_series}-{event_suffix}-{team_code}"
                        print(f"   🔨 [MONEYLINE] Built ticker immediately after NCAAF mapping: {built_ticker}")
                        return built_ticker
                else:
                    # Default to NCAAB for backwards compatibility
                    team_code = self._get_ncaab_team_code(selection_upper)
                    # CRITICAL: If mapping found team code, build ticker immediately (no fallbacks needed)
                    if team_code and base_series and event_suffix:
                        built_ticker = f"{base_series}-{event_suffix}-{team_code}"
                        print(f"   🔨 [MONEYLINE] Built ticker immediately after default NCAAB mapping: {built_ticker}")
                        return built_ticker
                
                # Fallback to old hardcoded mappings if helper didn't find it
                if not team_code:
                    # College basketball team codes (from actual market tickers)
                    # CRITICAL: Check most specific first to avoid false matches
                    if 'TEXAS A&M CORPUS CHRISTI' in selection_upper or 'TEXAS A & M CORPUS CHRISTI' in selection_upper:
                        team_code = 'AMCC'
                    elif 'TEXAS A&M COMMERCE' in selection_upper or 'TEXAS A & M COMMERCE' in selection_upper:
                        team_code = 'TAMC'
                    elif 'TEXAS TECH' in selection_upper or 'TEXAS TECH RED RAIDERS' in selection_upper:
                        team_code = 'TTU'
                    elif 'UCF' in selection_upper or 'CENTRAL FLORIDA' in selection_upper:
                        team_code = 'UCF'
                    elif 'GEORGETOWN' in selection_upper:
                        team_code = 'GTWN'
                    elif 'BUTLER' in selection_upper:
                        team_code = 'BUT'
                    elif 'HOLY CROSS' in selection_upper:
                        team_code = 'HC'
                    elif 'STONEHILL' in selection_upper:
                        team_code = 'STNH'
                    elif 'LE MOYNE' in selection_upper:
                        team_code = 'LMC'
                    elif 'SETON HALL' in selection_upper or 'HALL' in selection_upper:
                        team_code = 'HALL'
                    elif 'MARQUETTE' in selection_upper:
                        team_code = 'MARQ'
                    elif 'WAGNER' in selection_upper:
                        team_code = 'WAG'
                    elif 'FAIRLEIGH' in selection_upper or 'FDU' in selection_upper:
                        team_code = 'FDU'
                    elif 'DUKE' in selection_upper:
                        team_code = 'DUKE'
                    elif 'VIRGINIA TECH' in selection_upper or 'VT' in selection_upper:
                        team_code = 'VT'
                    elif ('TEXAS A&M' in selection_upper or 'TEXAS A & M' in selection_upper) and 'CORPUS' not in selection_upper and 'COMMERCE' not in selection_upper:
                        # Only match generic "Texas A&M" if it's NOT Corpus Christi or Commerce
                        team_code = 'TXAM'
                elif 'GEORGIA' in selection_upper:
                    team_code = 'UGA'
                elif 'ARMY' in selection_upper:
                    team_code = 'ARMY'
                elif 'ROBERT MORRIS' in selection_upper:
                    team_code = 'RMU'
                elif 'PURDUE FORT WAYNE' in selection_upper or 'IPFW' in selection_upper:
                    team_code = 'PFW'
                elif 'WILLIAM' in selection_upper and 'MARY' in selection_upper:
                    team_code = 'WM'
                elif 'CAMPBELL' in selection_upper:
                    team_code = 'CAMP'
                elif 'NC STATE' in selection_upper or 'NORTH CAROLINA STATE' in selection_upper:
                    team_code = 'NCST'
                elif 'WAKE FOREST' in selection_upper:
                    team_code = 'WAKE'
                elif 'BALL STATE' in selection_upper:
                    team_code = 'BSU'
                elif 'TOLEDO' in selection_upper:
                    team_code = 'TOL'
                elif 'UNC GREENSBORO' in selection_upper:
                    team_code = 'UNCG'
                elif 'THE CITADEL' in selection_upper or 'CITADEL' in selection_upper:
                    team_code = 'CIT'
                elif 'SAN DIEGO STATE' in selection_upper:
                    team_code = 'SDSU'
                elif 'UTAH STATE' in selection_upper:
                    team_code = 'USU'
                elif 'SOUTH CAROLINA' in selection_upper:
                    team_code = 'SCAR'
                elif 'LSU' in selection_upper or 'LOUISIANA STATE' in selection_upper:
                    team_code = 'LSU'
                elif 'ARIZONA STATE' in selection_upper or 'ASU' in selection_upper:
                    team_code = 'ASU'
                elif 'ARIZONA' in selection_upper:
                    team_code = 'ARIZ'
                elif 'INDIANA STATE' in selection_upper:
                    team_code = 'INST'
                elif 'VALPARAISO' in selection_upper:
                    team_code = 'VALP'
                elif 'TEXAS' in selection_upper and 'A&M' not in selection_upper and 'TECH' not in selection_upper:
                    team_code = 'TEX'
                elif 'OKLAHOMA' in selection_upper:
                    team_code = 'OKLA'
                elif 'SMU' in selection_upper or 'SOUTHERN METHODIST' in selection_upper:
                    team_code = 'SMU'
                elif 'LOUISVILLE' in selection_upper:
                    team_code = 'LOU'
                elif 'NORTH ALABAMA' in selection_upper:
                    team_code = 'UNA'
                elif 'STETSON' in selection_upper:
                    team_code = 'STET'
                elif 'RADFORD' in selection_upper:
                    team_code = 'RAD'
                elif 'PRESBYTERIAN' in selection_upper:
                    team_code = 'PRES'
                elif 'LIPSCOMB' in selection_upper:
                    team_code = 'LIP'
                elif 'NORTH FLORIDA' in selection_upper:
                    team_code = 'UNF'
                elif 'UMASS LOWELL' in selection_upper or 'UMASS' in selection_upper and 'LOWELL' in selection_upper:
                    team_code = 'MASSL'
                elif 'MAINE' in selection_upper:
                    team_code = 'ME'
                elif 'CHARLESTON SOUTHERN' in selection_upper:
                    team_code = 'COFC'
                elif 'NORTHEASTERN' in selection_upper:
                    team_code = 'NE'
                elif 'DEPAUL' in selection_upper:
                    team_code = 'DEP'
                elif 'XAVIER' in selection_upper:
                    team_code = 'XAV'
                elif 'PRESBYTERIAN' in selection_upper:
                    team_code = 'PRE'  # Not PRES - event ticker shows PRE
                elif 'LONG ISLAND' in selection_upper or 'LIU' in selection_upper:
                    team_code = 'LIU'  # Not LI - event ticker shows LIU
                elif 'CENTRAL CONNECTICUT' in selection_upper or 'CENTRAL CONNECTICUT STATE' in selection_upper:
                    team_code = 'CCSU'
                elif 'SAINT JOSEPH' in selection_upper or 'ST JOSEPH' in selection_upper:
                    team_code = 'JOES'  # Not JO - event ticker shows JOES
                elif 'LA SALLE' in selection_upper:
                    team_code = 'LAS'
                elif 'HOFSTRA' in selection_upper:
                    team_code = 'HOF'
                elif 'MONMOUTH' in selection_upper:
                    team_code = 'MON'
                elif 'OHIO' in selection_upper and 'STATE' not in selection_upper:
                    team_code = 'OHIO'  # Not OH - event ticker shows OHIO
                elif 'BUFFALO' in selection_upper:
                    team_code = 'BUFF'
                elif 'OLD DOMINION' in selection_upper:
                    team_code = 'ODU'
                elif 'TEXAS STATE' in selection_upper:
                    team_code = 'TXST'
                elif 'QUEENS' in selection_upper:
                    team_code = 'QU'
                elif 'BELLARMINE' in selection_upper:
                    team_code = 'BEL'
                elif 'IDAHO' in selection_upper:
                    team_code = 'IDHO'  # Not ID - event ticker shows IDHO
                elif 'NORTH TEXAS' in selection_upper or 'UNT' in selection_upper:
                    team_code = 'UNT'  # Not TEX - North Texas is UNT
                elif 'GEORGIA STATE' in selection_upper:
                    team_code = 'GAST'
                elif 'SOUTH ALABAMA' in selection_upper:
                    team_code = 'USA'
                elif 'SANTA CLARA' in selection_upper:
                    team_code = 'SCU'
                elif 'LOYOLA MARYMOUNT' in selection_upper or 'LMU' in selection_upper:
                    team_code = 'LMU'
                # NHL team codes
                elif 'ST. LOUIS' in selection_upper or 'ST LOUIS' in selection_upper or 'STLOUIS' in selection_upper:
                    if 'BLUES' in selection_upper:
                        team_code = 'STL'  # Not JSTL - St. Louis Blues is STL
                elif 'COLUMBUS' in selection_upper:
                    if 'BLUE JACKETS' in selection_upper or 'JACKETS' in selection_upper:
                        team_code = 'CBJ'
            
            # STEP 2: If hardcoded map didn't work, try extraction from event ticker
            if not team_code:
                print(f"   🔍 DEBUG: [MONEYLINE] Explicit mapping failed for '{selection_upper}', trying extraction fallback...")
                print(f"   🔍 DEBUG: [MONEYLINE] Extracted codes: code1={team_code1}, code2={team_code2}, teams_str={teams_str}")
            
            if not team_code and team_code1 and team_code2:
                # Match selection to one of the extracted team codes
                if selection_upper:
                    # CRITICAL: Use teams_str to determine which code is which
                    # Event ticker format is [DATE][AWAY][HOME], so code1=away, code2=home
                    if teams_str:
                        teams_upper = teams_str.upper()
                        team_parts = teams_upper.split('@')
                        if len(team_parts) == 2:
                            away_team = team_parts[0].strip()
                            home_team = team_parts[1].strip()
                            
                            print(f"   🔍 DEBUG: [MONEYLINE] Teams: away='{away_team}', home='{home_team}', selection='{selection_upper}'")
                            
                            # Check if selection matches away team
                            selection_matches_away = (selection_upper in away_team or away_team in selection_upper or
                                                      any(word in selection_upper for word in away_team.split() if len(word) > 2) or
                                                      any(word in away_team for word in selection_upper.split() if len(word) > 2))
                            # Check if selection matches home team
                            selection_matches_home = (selection_upper in home_team or home_team in selection_upper or
                                                      any(word in selection_upper for word in home_team.split() if len(word) > 2) or
                                                      any(word in home_team for word in selection_upper.split() if len(word) > 2))
                            
                            print(f"   🔍 DEBUG: [MONEYLINE] Matches: away={selection_matches_away}, home={selection_matches_home}")
                            
                            # Trust the event ticker order: code1 = away, code2 = home
                            if selection_matches_away:
                                team_code = team_code1  # Away team = first code
                                print(f"   🔍 DEBUG: [MONEYLINE] Matched to away team, using code1={team_code1}")
                            elif selection_matches_home:
                                team_code = team_code2  # Home team = second code
                                print(f"   🔍 DEBUG: [MONEYLINE] Matched to home team, using code2={team_code2}")
                    
                    # Fallback: Try direct substring matching if teams_str didn't work
                    if not team_code:
                        print(f"   🔍 DEBUG: [MONEYLINE] Teams_str matching failed, trying direct code matching...")
                        if (team_code1 in selection_upper or selection_upper.startswith(team_code1) or
                            any(word.startswith(team_code1) for word in selection_upper.split())):
                            team_code = team_code1
                            print(f"   🔍 DEBUG: [MONEYLINE] Matched via direct code1={team_code1}")
                        elif (team_code2 in selection_upper or selection_upper.startswith(team_code2) or
                              any(word.startswith(team_code2) for word in selection_upper.split())):
                            team_code = team_code2
                            print(f"   🔍 DEBUG: [MONEYLINE] Matched via direct code2={team_code2}")
            elif not team_code:
                print(f"   ⚠️  [MONEYLINE] Extraction fallback unavailable: team_code1={team_code1}, team_code2={team_code2}")
            
            # STEP 3: Final fallback - use first letters (rarely needed if map is complete)
            if not team_code and selection_upper:
                # Fallback: first 3 letters (but this often fails for college teams)
                words = selection_upper.split()
                if words:
                    first_word = words[0]
                    if len(first_word) >= 3:
                        team_code = first_word[:3].upper()
                    else:
                        team_code = first_word.upper()
            
            print(f"   🔍 DEBUG: [MONEYLINE FINAL CHECK] team_code={repr(team_code)}, base_series={repr(base_series)}, event_suffix={repr(event_suffix)}")
            
            if not team_code:
                print(f"   ⚠️  [MONEYLINE] Could not determine team code for: selection={selection}, teams={teams_str}")
                print(f"   ⚠️  [MONEYLINE] Extracted codes were: code1={team_code1}, code2={team_code2}")
                print(f"   ⚠️  [MONEYLINE] base_series={base_series}, event_suffix={event_suffix}")
                return None
            
            if not base_series or not event_suffix:
                print(f"   ⚠️  [MONEYLINE] Missing base_series or event_suffix: base_series={repr(base_series)}, event_suffix={repr(event_suffix)}")
                return None
            
            print(f"   🔨 [MONEYLINE] Built ticker: {base_series}-{event_suffix}-{team_code}")
            built_ticker = f"{base_series}-{event_suffix}-{team_code}"
            print(f"   ✅ [MONEYLINE] Returning built ticker: {built_ticker}")
            return built_ticker
        
        return None
    
    async def find_submarket(self, event_ticker, market_type, line, selection, teams_str=None):
        # CRITICAL: Log the line value received at the start of find_submarket
        print(f"   [FIND_SUBMARKET] ========== STARTING MATCHING ==========")
        print(f"   [FIND_SUBMARKET] event_ticker={event_ticker}, market_type='{market_type}', line={line}, selection='{selection}', teams_str='{teams_str}'")
        if line is not None:
            print(f"   [FIND_SUBMARKET] Received line={line} for market_type='{market_type}', selection='{selection}'")
        """
        Find the exact submarket within an event
        
        Args:
            event_ticker: Event ticker (e.g., "KXNFLGAME-26JAN04BALPIT")
            market_type: Market type ("Total Points", "Point Spread", "Moneyline")
            line: Line value (e.g., 40.5 for totals, -11.5 for spreads)
            selection: Selection ("Over", "Under", team name, etc.)
        
        Returns:
            Submarket dict with ticker, or None if not found
        """
        # ONLY METHOD: Build ticker directly and fetch it (LIGHTNING FAST - NO FALLBACK SEARCH!)
        print(f"   [FIND_SUBMARKET] Calling build_market_ticker...")
        built_ticker = self.build_market_ticker(event_ticker, market_type, line, selection, teams_str)
        if built_ticker:
            print(f"   [FIND_SUBMARKET] ✅ Built ticker: {built_ticker}")
            print(f"   [FIND_SUBMARKET] Fetching market from Kalshi API...")
            
            # RETRY LOGIC: Markets may not be immediately available when first created
            # Retry up to 2 times with short delays to handle timing issues
            max_retries = 2
            retry_delay = 0.5  # 500ms delay between retries
            
            for attempt in range(max_retries + 1):
                if attempt > 0:
                    print(f"   [FIND_SUBMARKET] Retry {attempt}/{max_retries} for {built_ticker} (market may not be available yet)...")
                    await asyncio.sleep(retry_delay)
                
                market = await self.get_market_by_ticker(built_ticker)
                if market:
                    # CRITICAL: Ensure ticker field is set (API might return it nested)
                    if 'ticker' not in market or not market.get('ticker'):
                        market['ticker'] = built_ticker
                    print(f"   [FIND_SUBMARKET] ✅ Found market by built ticker: {built_ticker} (attempt {attempt + 1})")
                    print(f"   [FIND_SUBMARKET] ========== MATCHING SUCCESS ==========")
                    
                    # LEARN TEAM CODES: If we successfully matched, learn team codes from the ticker
                    # This allows us to infer unknown team codes when we know one team
                    if teams_str:
                        self._learn_team_codes_from_ticker(built_ticker, teams_str)
                    
                    # VALIDATION: Verify the market line matches expected line (for totals/spreads)
                    # Line validation removed - Kalshi uses positive numbers in tickers for favorite spreads
                    # e.g., -7.5 favorite spread shows as "7" in ticker (KXNCAAMBSPREAD-...-TEAM7)
                    # This is correct behavior, not a mismatch
                    
                    return market
            
            # All retries failed
            print(f"   [FIND_SUBMARKET] ❌ Built ticker {built_ticker} not found on Kalshi after {max_retries + 1} attempts (market may not exist)")
            print(f"   [FIND_SUBMARKET] ========== MATCHING FAILED: Market not found ==========")
            # Log mismatch for debugging
            self._log_mapping_mismatch(event_ticker, market_type, line, selection, teams_str, built_ticker, "Market not found")
            return None
        else:
            print(f"   [FIND_SUBMARKET] ❌ Could not build ticker from event_ticker={event_ticker}, market_type={market_type}, line={line}, selection={selection}")
            print(f"   [FIND_SUBMARKET] ========== MATCHING FAILED: Ticker building failed ==========")
            return None
        
        # REMOVED: All fallback search logic that queries 1000 markets - too slow!
        # We ONLY use direct ticker building now - if it doesn't exist, return None immediately
        # This ensures lightning-fast performance with no unnecessary API calls
        return None
    
    async def fetch_orderbook(self, ticker):
        """
        Fetch orderbook for a specific market ticker - WITH TIMING DIAGNOSTICS
        Returns YES and NO side prices with liquidity
        """
        fetch_start = time.time()
        print(f"[ORDERBOOK] [TIMING] Starting orderbook fetch for {ticker} at {time.strftime('%H:%M:%S.%f', time.localtime(fetch_start))}")
        
        if not self.session:
            await self.init()
        
        try:
            # For v2 API, path must include /trade-api/v2 prefix
            path = f"/trade-api/v2/markets/{ticker}/orderbook"
            ts, sig = self.auth.sign("GET", path)
            headers = {
                "KALSHI-ACCESS-KEY": self.auth.kid,
                "KALSHI-ACCESS-SIGNATURE": sig,
                "KALSHI-ACCESS-TIMESTAMP": ts
            }
            
            # Wrap in asyncio.wait_for to avoid timeout context manager issues when called from run_coroutine_threadsafe
            async def _request():
                try:
                    async with self.session.get(f"{self.base_url}{path}", headers=headers) as resp:
                        if resp.status == 200:
                            return await resp.json()
                        elif resp.status == 429:
                            # Rate limited - wait and retry once
                            await asyncio.sleep(1)
                            ts, sig = self.auth.sign("GET", path)
                            headers_retry = {
                                "KALSHI-ACCESS-KEY": self.auth.kid,
                                "KALSHI-ACCESS-SIGNATURE": sig,
                                "KALSHI-ACCESS-TIMESTAMP": ts
                            }
                            async with self.session.get(f"{self.base_url}{path}", headers=headers_retry) as retry_resp:
                                if retry_resp.status == 200:
                                    return await retry_resp.json()
                                else:
                                    error_text = await retry_resp.text()
                                    print(f"Orderbook retry error {retry_resp.status} for {ticker}: {error_text[:200]}")
                        else:
                            error_text = await resp.text()
                            print(f"Orderbook API error {resp.status} for {ticker}: {error_text[:200]}")
                except asyncio.TimeoutError:
                    print(f"Orderbook request timeout for {ticker}")
                except Exception as e:
                    error_msg = str(e)
                    # Suppress "Timeout context manager" errors - these are handled by asyncio.wait_for
                    if "Timeout context manager should be used inside a task" not in error_msg:
                        print(f"Orderbook request exception for {ticker}: {e}")
                        import traceback
                        traceback.print_exc()
                return None
            
            try:
                data = await asyncio.wait_for(_request(), timeout=5.0)
                if not data:
                    print(f"   Orderbook fetch returned None for {ticker}")
                    return None
            except asyncio.TimeoutError:
                print(f"   Timeout getting orderbook for {ticker}")
                return None
            except Exception as e:
                error_msg = str(e)
                # Suppress "Timeout context manager" and SSL errors - these are non-critical
                if "Timeout context manager should be used inside a task" not in error_msg and "SSL" not in error_msg and "APPLICATION_DATA_AFTER_CLOSE_NOTIFY" not in error_msg:
                    print(f"   Exception in orderbook fetch for {ticker}: {e}")
                    import traceback
                    traceback.print_exc()
                return None
            
            # Parse orderbook
            try:
                orderbook = data.get('orderbook', {})
                if not orderbook:
                    print(f"   No 'orderbook' key in response for {ticker}, keys: {list(data.keys())[:10]}")
                    return None
                
                # Check for alternative key names (yes_bids/no_bids vs yes/no)
                yes_bids_raw = orderbook.get('yes', []) or orderbook.get('yes_bids', []) or []
                no_bids_raw = orderbook.get('no', []) or orderbook.get('no_bids', []) or []
                
                # Debug: Log orderbook structure (only on first fetch to reduce noise)
                if not hasattr(self, '_ob_structure_logged'):
                    print(f"[ORDERBOOK] Raw orderbook keys: {list(orderbook.keys())}")
                    if yes_bids_raw:
                        print(f"[ORDERBOOK] YES sample (first 2): {yes_bids_raw[:2]}")
                    if no_bids_raw:
                        print(f"[ORDERBOOK] NO sample (first 2): {no_bids_raw[:2]}")
                    self._ob_structure_logged = True
                
                # Parse YES side
                yes_bids = []
                yes_total_liq = 0
                for bid in yes_bids_raw:
                    if isinstance(bid, list) and len(bid) >= 2:
                        price_cents = bid[0]
                        quantity = bid[1]
                        price = price_cents / 100.0
                        yes_bids.append({'price': price, 'quantity': quantity})
                        yes_total_liq += quantity
                
                # Parse NO side
                no_bids = []
                no_total_liq = 0
                for bid in no_bids_raw:
                    if isinstance(bid, list) and len(bid) >= 2:
                        price_cents = bid[0]
                        quantity = bid[1]
                        price = price_cents / 100.0
                        no_bids.append({'price': price, 'quantity': quantity})
                        no_total_liq += quantity
                
                # Get best bid/ask
                # Bids are sorted by price (ascending), so last element is highest (best bid)
                # For YES: best_ask = 1 - NO best_bid (complementary pricing)
                yes_best_bid = yes_bids[-1]['price'] if yes_bids else None
                yes_best_ask = (1.0 - no_bids[-1]['price']) if no_bids else None
                
                no_best_bid = no_bids[-1]['price'] if no_bids else None
                no_best_ask = (1.0 - yes_bids[-1]['price']) if yes_bids else None
                
                # Debug: Verify orderbook structure
                if yes_bids and no_bids:
                    print(f"[ORDERBOOK] YES best_bid={yes_best_bid:.4f}, YES best_ask={yes_best_ask:.4f}, NO best_bid={no_best_bid:.4f}, NO best_ask={no_best_ask:.4f}")
                    print(f"[ORDERBOOK] Verification: YES ask + NO bid = {yes_best_ask + no_best_bid:.4f} (should be ~1.0)")
                
                # CRITICAL: Calculate asks from bids (complementary pricing)
                # YES asks = implied from NO bids (YES ask = 1 - NO bid)
                # NO asks = implied from YES bids (NO ask = 1 - YES bid)
                yes_asks = []
                for no_bid in no_bids:
                    yes_ask_price = 1.0 - no_bid['price']
                    yes_asks.append({'price': yes_ask_price, 'quantity': no_bid['quantity']})
                
                # CRITICAL: Sort YES asks ascending by price (lowest ask first) for liquidity calculation
                yes_asks.sort(key=lambda x: x['price'])
                
                no_asks = []
                for yes_bid in yes_bids:
                    no_ask_price = 1.0 - yes_bid['price']
                    no_asks.append({'price': no_ask_price, 'quantity': yes_bid['quantity']})
                
                # CRITICAL: Sort NO asks ascending by price (lowest ask first) for liquidity calculation
                no_asks.sort(key=lambda x: x['price'])
                
                # Get best_ask_size from asks
                yes_best_ask_size = yes_asks[0]['quantity'] if yes_asks else 0
                no_best_ask_size = no_asks[0]['quantity'] if no_asks else 0
                
                orderbook_data = {
                    'yes': {
                        'best_bid': yes_best_bid,
                        'best_ask': yes_best_ask,
                        'best_ask_size': yes_best_ask_size,
                        'bids': yes_bids,
                        'asks': yes_asks,  # CRITICAL: Include asks for liquidity checks
                        'total_liquidity': yes_total_liq
                    },
                    'no': {
                        'best_bid': no_best_bid,
                        'best_ask': no_best_ask,
                        'best_ask_size': no_best_ask_size,
                        'bids': no_bids,
                        'asks': no_asks,  # CRITICAL: Include asks for liquidity checks
                        'total_liquidity': no_total_liq
                    },
                    'timestamp': datetime.now().isoformat(),
                    'fetched_at': time.time()  # Unix timestamp for age checking
                }
                
                self.orderbooks[ticker.upper()] = orderbook_data
                fetch_duration = (time.time() - fetch_start) * 1000
                print(f"[ORDERBOOK] [TIMING] Orderbook fetch completed in {fetch_duration:.1f}ms")
                print(f"[ORDERBOOK] [STRUCTURE] YES bids: {len(yes_bids)}, YES asks: {len(yes_asks)}")
                print(f"[ORDERBOOK] [STRUCTURE] NO bids: {len(no_bids)}, NO asks: {len(no_asks)}")
                return orderbook_data
            except Exception as e:
                print(f"   Error parsing orderbook for {ticker}: {e}")
                import traceback
                traceback.print_exc()
                return None
        
        except Exception as e:
            print(f"Error fetching orderbook for {ticker}: {e}")
            return None
    
    async def place_order(self, ticker, side, count, price_cents=None, validate_odds=True, expected_price_cents=None, max_liquidity_dollars=None, post_only=False, expiration_ts=None, _retry_count=0, skip_duplicate_check=False):
        """
        Place an order on Kalshi - OPTIMIZED FOR SPEED WITH EXTENSIVE LOGGING
        
        Args:
            ticker: Market ticker (e.g., "KXNCAAMB-26JAN04ORSTWSU")
            side: "yes" or "no"
            count: Number of contracts
            price_cents: Price in cents (optional, will use market price if not provided)
            validate_odds: If True, verify odds match before placing
            expected_price_cents: Expected price in cents for validation
            max_liquidity_dollars: Maximum bet amount in dollars
            post_only: If True, order will only post as maker (rejected if would cross)
            expiration_ts: Unix timestamp in milliseconds when order expires (None = no expiration)
            _retry_count: Internal counter for recursive retries (max 1)
            skip_duplicate_check: If True, do NOT check/add recent_bets (for manual dashboard bets only; auto-bettor uses dashboard-level duplicate tracking)
        
        Returns:
            Order response with success/failure and fee_type ('maker' or 'taker')
        """
        order_start_time = time.time()
        order_type_str = "POST-ONLY (maker)" if post_only else "LIMIT (taker)"
        exp_str = f", expires={expiration_ts}" if expiration_ts else ""
        manual_str = " [MANUAL - skip_duplicate_check=True]" if skip_duplicate_check else ""
        print(f"[ORDER] place_order() called: ticker={ticker}, side={side}, count={count}, expected_price={expected_price_cents}, type={order_type_str}{exp_str}{manual_str}")
        
        # Duplicate bet prevention - ONLY for auto-bettor. Manual dashboard bets pass skip_duplicate_check=True so user can click multiple times.
        bet_key = (ticker.upper(), side.lower())
        if self.bet_lock is None:
            await self.init()
        
        if not skip_duplicate_check:
            async with self.bet_lock:
                if bet_key in self.recent_bets:
                    print(f"[ORDER] BLOCKED: Duplicate bet (auto-bettor) for {ticker} {side} - already bet recently (within {self.bet_cooldown_seconds}s cooldown) [AUTO-BET DUPLICATE]")
                    return {
                        "error": "Duplicate bet",
                        "message": f"Already bet {ticker} {side} successfully recently",
                        "ticker": ticker,
                        "side": side
                    }
        
        if not self.session:
            await self.init()
        
        # Retry logic for order placement
        for attempt in range(3):
            attempt_start = time.time()
            print(f"[ORDER] Attempt {attempt + 1}/3")
            try:
                orderbook = None
                price_cents = None
                
                # SPEED OPTIMIZATION: Use WebSocket orderbook if available (real-time, no HTTP delay!)
                # WebSocket orderbook is updated in real-time via subscribe_orderbook, so it's always fresh
                cached_orderbook = self.orderbooks.get(ticker.upper())
                use_cached = False
                
                # CRITICAL: Use WebSocket orderbook if available and fresh (< 0.5s old)
                # WebSocket updates are real-time, so this is MORE accurate than HTTP polling
                if validate_odds and expected_price_cents is not None:
                    ob_start = time.time()
                    
                    # Check WebSocket orderbook first (real-time, no HTTP delay)
                    if cached_orderbook:
                        orderbook_age = time.time() - cached_orderbook.get('fetched_at', 0)
                        if orderbook_age < 0.5:  # WebSocket orderbook is fresh (< 0.5s old)
                            # CRITICAL: Verify WebSocket orderbook has required data (best_ask for YES side)
                            # If buying YES, we need YES best_ask or NO bids to calculate it
                            if side.lower() == "yes":
                                yes_data = cached_orderbook.get('yes', {})
                                no_data = cached_orderbook.get('no', {})
                                yes_best_ask = yes_data.get('best_ask')
                                no_bids = no_data.get('bids', [])
                                
                                # If no best_ask and no NO bids, WebSocket orderbook is incomplete - fall back to HTTP
                                if yes_best_ask is None and not no_bids:
                                    print(f"[ORDER] WebSocket orderbook missing YES ask/NO bids - falling back to HTTP")
                                else:
                                    orderbook = cached_orderbook
                                    ob_time = (time.time() - ob_start) * 1000
                                    print(f"[ORDER] Using WebSocket orderbook (fresh, {orderbook_age*1000:.1f}ms old) - {ob_time:.1f}ms lookup")
                                    use_cached = True
                            elif side.lower() == "no":
                                no_data = cached_orderbook.get('no', {})
                                yes_data = cached_orderbook.get('yes', {})
                                no_best_ask = no_data.get('best_ask')
                                yes_bids = yes_data.get('bids', [])
                                
                                # If no best_ask and no YES bids, WebSocket orderbook is incomplete - fall back to HTTP
                                if no_best_ask is None and not yes_bids:
                                    print(f"[ORDER] WebSocket orderbook missing NO ask/YES bids - falling back to HTTP")
                                else:
                                    orderbook = cached_orderbook
                                    ob_time = (time.time() - ob_start) * 1000
                                    print(f"[ORDER] Using WebSocket orderbook (fresh, {orderbook_age*1000:.1f}ms old) - {ob_time:.1f}ms lookup")
                                    use_cached = True
                            else:
                                # Unknown side, use WebSocket orderbook as-is
                                orderbook = cached_orderbook
                                ob_time = (time.time() - ob_start) * 1000
                                print(f"[ORDER] Using WebSocket orderbook (fresh, {orderbook_age*1000:.1f}ms old) - {ob_time:.1f}ms lookup")
                                use_cached = True
                    
                    # Fallback to HTTP only if WebSocket not available or stale
                    if not use_cached:
                        print(f"[ORDER] Fetching FRESH orderbook via HTTP (WebSocket not available or stale)...")
                        orderbook = await self.fetch_orderbook(ticker)
                        ob_time = (time.time() - ob_start) * 1000
                        if orderbook:
                            print(f"[ORDER] Fresh orderbook fetched in {ob_time:.1f}ms")
                        else:
                            print(f"[ORDER] Orderbook fetch FAILED after {ob_time:.1f}ms")
                    
                    if not orderbook:
                        if attempt < 2:
                            retry_delay = 0.05 * (2 ** attempt)
                            print(f"[ORDER] Orderbook fetch failed, retrying in {retry_delay*1000:.0f}ms...")
                            # Faster retry - only 50ms delay for first retry
                            await asyncio.sleep(retry_delay)
                            continue
                        print(f"[ORDER] ERROR: Could not fetch orderbook after {attempt + 1} attempts")
                        return {"error": "Could not fetch orderbook for validation"}
                
                # CRITICAL: Validate that current Kalshi price matches BookieBeats price
                # Also check available liquidity at that price
                # Only validate if validate_odds is True AND orderbook was fetched
                if validate_odds and orderbook is not None:
                    validation_start = time.time()
                    print(f"[ORDER] Validating price: expected={expected_price_cents} cents")
                    
                    if side.lower() == "yes":
                        best_ask = orderbook['yes'].get('best_ask')
                        # CRITICAL: If best_ask is None, try to compute it from NO bids (YES ask = 1 - NO bid)
                        if best_ask is None:
                            no_bids = orderbook['no'].get('bids', [])
                            if no_bids:
                                # NO bids are sorted by price (ascending), so last element is highest (best NO bid)
                                # YES ask = 1 - NO best_bid
                                if isinstance(no_bids, list) and len(no_bids) > 0:
                                    # Handle both dict format and list format
                                    if isinstance(no_bids[0], dict):
                                        no_best_bid_price = no_bids[-1].get('price', 0)
                                    elif isinstance(no_bids[0], list) and len(no_bids[0]) >= 2:
                                        no_best_bid_price = no_bids[-1][0] / 100.0  # Convert cents to probability
                                    else:
                                        no_best_bid_price = no_bids[-1] if isinstance(no_bids[-1], (int, float)) else 0
                                    best_ask = 1.0 - no_best_bid_price
                                    print(f"[ORDER] Computed YES best_ask from NO bids: {best_ask:.4f}")
                        
                        if best_ask is not None:
                            # For YES side: price in cents = best_ask * 100
                            # best_ask is already the probability (0.0 to 1.0), so multiply by 100 to get cents
                            current_price_cents = int(best_ask * 100)
                            price_delta = abs(current_price_cents - expected_price_cents)
                            
                            # DIAGNOSTIC: Log full orderbook structure to verify we're reading correct side
                            yes_best_bid = orderbook['yes'].get('best_bid')
                            no_best_bid = orderbook['no'].get('best_bid')
                            no_best_ask = orderbook['no'].get('best_ask')
                            print(f"[ORDER] ========== PRICE VALIDATION DIAGNOSTIC ==========")
                            print(f"[ORDER] Side: YES (buying YES contracts)")
                            print(f"[ORDER] Orderbook structure:")
                            print(f"[ORDER]   YES best_bid={yes_best_bid}, YES best_ask={best_ask}")
                            print(f"[ORDER]   NO best_bid={no_best_bid}, NO best_ask={no_best_ask}")
                            print(f"[ORDER]   Verification: YES ask + NO bid = {best_ask + (no_best_bid or 0):.4f} (should be ~1.0)")
                            print(f"[ORDER] Price comparison:")
                            print(f"[ORDER]   BookieBeats expected price: {expected_price_cents}¢")
                            print(f"[ORDER]   Kalshi current YES ask: {current_price_cents}¢ (from best_ask={best_ask})")
                            print(f"[ORDER]   Price delta: {price_delta}¢")
                            if price_delta > 10:
                                print(f"[ORDER]   ⚠️  WARNING: Large price discrepancy! This suggests:")
                                print(f"[ORDER]      - Wrong market/submarket matched")
                                print(f"[ORDER]      - Stale orderbook data")
                                print(f"[ORDER]      - Side determination error")
                            print(f"[ORDER] =================================================")
                            
                            # CRITICAL: For taker orders, we need to:
                            # 1. Validate that best_ask is within acceptable range (1 cent worse or 2 cents better)
                            # 2. Use full liquidity at the execution price (best_ask if valid, or expected_price for limit)
                            best_ask = orderbook['yes'].get('best_ask', 0)
                            best_ask_cents = int(best_ask * 100) if best_ask > 0 else expected_price_cents
                            
                            # Determine execution price based on order type and price validation
                            # For taker orders: will fill at best_ask (if within acceptable range)
                            # For limit orders: will fill at limit price (expected_price_cents)
                            if not post_only:
                                # Taker order: validate best_ask is within range, then use it for liquidity
                                # Price validation happens later, but we need execution price for liquidity calculation
                                if best_ask > 0:
                                    execution_price_cents = best_ask_cents  # Taker fills at best_ask
                                else:
                                    execution_price_cents = expected_price_cents  # Fallback
                            else:
                                # Limit order: uses limit price
                                execution_price_cents = expected_price_cents
                            
                            # Calculate available contracts at execution price or better
                            # CRITICAL: Sum ALL asks at execution_price_cents or better (not just best_ask size!)
                            available_contracts = 0
                            no_bids = orderbook['no'].get('bids', [])
                            yes_asks = orderbook['yes'].get('asks', [])
                            
                            print(f"[ORDER] ========== LIQUIDITY CALCULATION ==========")
                            print(f"[ORDER] Expected price: {expected_price_cents}¢")
                            print(f"[ORDER] Best ask: {best_ask_cents}¢ ({best_ask:.4f})")
                            print(f"[ORDER] Execution price (for liquidity): {execution_price_cents}¢")
                            print(f"[ORDER] Order type: {'Taker' if not post_only else 'Limit'}")
                            print(f"[ORDER] YES asks in orderbook: {len(yes_asks)} levels")
                            
                            # CRITICAL: For both taker and limit orders, use YES asks if available (more accurate)
                            # For limit orders: check liquidity at limit price (expected_price_cents) or better
                            # For taker orders: check liquidity at best_ask (execution_price_cents) or better
                            # Initialize available_liquidity_dollars for summing actual dollar values at each price level
                            available_liquidity_dollars = 0.0
                            
                            if yes_asks:
                                order_type_str = "taker" if not post_only else "limit"
                                print(f"[ORDER] Calculating liquidity from YES asks ({order_type_str} order):")
                                # CRITICAL: Sum actual dollar values at each price level (not just contracts * execution_price)
                                # For limit orders: This correctly accounts for better prices (e.g., $7 at 42¢ + $93 at 43¢ = $100 total)
                                # Limit orders will fill at better prices first, then at limit price
                                for i, ask in enumerate(yes_asks):
                                    ask_price = ask.get('price', 0)
                                    ask_price_cents = int(ask_price * 100)
                                    ask_quantity = ask.get('quantity', 0)
                                    ask_liquidity_dollars = (ask_quantity * ask_price_cents) / 100.0
                                    
                                    if ask_price_cents <= execution_price_cents:
                                        available_contracts += ask_quantity
                                        available_liquidity_dollars += ask_liquidity_dollars  # Sum actual dollar values
                                        print(f"[ORDER]   Level {i+1}: {ask_price_cents}¢ ({ask_price:.4f}) - {ask_quantity} contracts = ${ask_liquidity_dollars:.2f} ✓")
                                    else:
                                        print(f"[ORDER]   Level {i+1}: {ask_price_cents}¢ ({ask_price:.4f}) - {ask_quantity} contracts = ${ask_liquidity_dollars:.2f} ✗ (too expensive)")
                                        break  # Asks are sorted ascending, so we can stop here
                            else:
                                # Fallback: calculate from NO bids (when asks not available)
                                print(f"[ORDER] Calculating liquidity from NO bids (fallback - asks not available):")
                                for i, bid in enumerate(no_bids):
                                    bid_price = bid.get('price', 0)
                                    bid_price_cents = int(bid_price * 100)
                                    # YES ask price = 100 - NO bid price
                                    yes_ask_price_cents = 100 - bid_price_cents
                                    quantity = bid.get('quantity', 0)
                                    liquidity_dollars = (quantity * yes_ask_price_cents) / 100.0
                                    
                                    if yes_ask_price_cents <= execution_price_cents:
                                        available_contracts += quantity
                                        available_liquidity_dollars += liquidity_dollars  # Sum actual dollar values
                                        print(f"[ORDER]   Level {i+1}: NO bid {bid_price_cents}¢ → YES ask {yes_ask_price_cents}¢ - {quantity} contracts = ${liquidity_dollars:.2f} ✓")
                                    else:
                                        print(f"[ORDER]   Level {i+1}: NO bid {bid_price_cents}¢ → YES ask {yes_ask_price_cents}¢ - {quantity} contracts = ${liquidity_dollars:.2f} ✗ (too expensive)")
                            
                            print(f"[ORDER] Total available: {available_contracts} contracts = ${available_liquidity_dollars:.2f} at {execution_price_cents}¢ or better")
                            if post_only:
                                print(f"[ORDER] NOTE: For limit orders, fills occur at better prices first, then at limit price")
                            print(f"[ORDER] ===========================================")
                            
                            # CRITICAL: Store liquidity data in orderbook for later use (contract capping)
                            if orderbook:
                                orderbook['available_contracts_yes'] = available_contracts
                                orderbook['available_liquidity_dollars_yes'] = available_liquidity_dollars
                            
                            # CRITICAL: Validate price for limit orders
                            # If price delta is > 2 cents, it's likely the WRONG SUBMARKET (not just market movement)
                            # Only allow better prices if delta is <= 2 cents (small market movement is OK)
                            # If delta > 2 cents, reject - likely matched wrong market
                            if price_delta > 2:
                                print(f"[ORDER] VALIDATION FAILED: Price delta too large ({price_delta}¢) - likely WRONG SUBMARKET! Expected {expected_price_cents}¢, got {current_price_cents}¢")
                                print(f"[ORDER]   ⚠️  Large price discrepancy suggests wrong market/submarket was matched")
                                print(f"[ORDER]   📊 DIAGNOSTIC INFO:")
                                print(f"[ORDER]      Ticker: {ticker}")
                                print(f"[ORDER]      Side: {side}")
                                print(f"[ORDER]      Expected price: {expected_price_cents}¢ ({expected_price_cents/100:.2f}¢)")
                                print(f"[ORDER]      Current price: {current_price_cents}¢ ({current_price_cents/100:.2f}¢)")
                                print(f"[ORDER]      Price delta: {price_delta}¢ ({price_delta/expected_price_cents*100:.1f}% difference)")
                                print(f"[ORDER]      Orderbook: YES ask={best_ask:.4f}, NO bid={no_best_bid:.4f}")
                                print(f"[ORDER]   🔍 POSSIBLE CAUSES:")
                                print(f"[ORDER]      1. Wrong submarket matched (different line/team)")
                                print(f"[ORDER]      2. Market moved dramatically (unlikely for {price_delta}¢ delta)")
                                print(f"[ORDER]      3. Stale BookieBeats price data")
                                print(f"[ORDER]      4. Side determination error (betting wrong side)")
                                return {
                                    "error": "Odds changed",
                                    "expected": expected_price_cents,
                                    "current": current_price_cents,
                                    "delta": price_delta,
                                    "reason": f"Price delta too large ({price_delta}¢) - likely wrong submarket"
                                }
                            elif current_price_cents > expected_price_cents:
                                # Current price is WORSE than expected - we'd pay more than BB said
                                price_delta_worse = current_price_cents - expected_price_cents
                                if price_delta_worse > 1:
                                    print(f"[ORDER] VALIDATION FAILED: Price got WORSE! Expected {expected_price_cents}¢, got {current_price_cents}¢ (delta: {price_delta_worse}¢ worse)")
                                    print(f"[ORDER]   📊 DIAGNOSTIC INFO:")
                                    print(f"[ORDER]      Ticker: {ticker}")
                                    print(f"[ORDER]      Side: {side}")
                                    print(f"[ORDER]      Expected price: {expected_price_cents}¢ ({expected_price_cents/100:.2f}¢)")
                                    print(f"[ORDER]      Current price: {current_price_cents}¢ ({current_price_cents/100:.2f}¢)")
                                    print(f"[ORDER]      Price delta: {price_delta_worse}¢ worse ({price_delta_worse/expected_price_cents*100:.1f}% worse)")
                                    print(f"[ORDER]      Orderbook: YES ask={best_ask:.4f}, NO bid={no_best_bid:.4f}")
                                    print(f"[ORDER]   🔍 POSSIBLE CAUSES:")
                                    print(f"[ORDER]      1. Market moved against us (price got worse)")
                                    print(f"[ORDER]      2. Wrong submarket matched (different line/team)")
                                    print(f"[ORDER]      3. Stale BookieBeats price data")
                                    return {
                                        "error": "Odds changed",
                                        "expected": expected_price_cents,
                                        "current": current_price_cents,
                                        "delta": price_delta_worse,
                                        "reason": f"Price got worse by {price_delta_worse}¢ (max allowed: 1¢)"
                                    }
                            elif current_price_cents < expected_price_cents:
                                # Current price is BETTER than expected - our limit order will fill immediately at better price
                                # For manual bets: Allow up to 4¢ better (user is making decision, market may have moved)
                                # For auto-bets: Allow up to 4¢ better (good fill opportunity, unlikely to be wrong market)
                                max_better_delta = 4 if skip_duplicate_check else 4
                                price_delta_better = expected_price_cents - current_price_cents
                                if price_delta_better <= max_better_delta:
                                    bet_type_str = "Manual bet" if skip_duplicate_check else "Auto-bet"
                                    print(f"[ORDER] VALIDATION PASSED: Price got BETTER! Expected {expected_price_cents}¢, got {current_price_cents}¢ (delta: {price_delta_better}¢ better - {bet_type_str} allows up to {max_better_delta}¢ better)")
                                else:
                                    # Delta too large even though better - likely wrong market
                                    print(f"[ORDER] VALIDATION FAILED: Price delta too large ({price_delta_better}¢) even though better - likely WRONG SUBMARKET! (max allowed: {max_better_delta}¢)")
                                    print(f"[ORDER]   📊 DIAGNOSTIC INFO:")
                                    print(f"[ORDER]      Ticker: {ticker}")
                                    print(f"[ORDER]      Side: {side}")
                                    print(f"[ORDER]      Expected price: {expected_price_cents}¢ ({expected_price_cents/100:.2f}¢)")
                                    print(f"[ORDER]      Current price: {current_price_cents}¢ ({current_price_cents/100:.2f}¢)")
                                    print(f"[ORDER]      Price delta: {price_delta_better}¢ better ({price_delta_better/expected_price_cents*100:.1f}% better)")
                                    print(f"[ORDER]      Orderbook: YES ask={best_ask:.4f}, NO bid={no_best_bid:.4f}")
                                    print(f"[ORDER]   🔍 POSSIBLE CAUSES:")
                                    print(f"[ORDER]      1. Wrong submarket matched (different line/team) - price too good to be true")
                                    print(f"[ORDER]      2. Market moved dramatically in our favor (unlikely for {price_delta_better}¢ delta)")
                                    print(f"[ORDER]      3. Stale BookieBeats price data")
                                    return {
                                        "error": "Odds changed",
                                        "expected": expected_price_cents,
                                        "current": current_price_cents,
                                        "delta": price_delta_better,
                                        "reason": f"Price delta too large ({price_delta_better}¢) even though better - likely wrong submarket (max allowed: {max_better_delta}¢)"
                                    }
                            else:
                                # Prices match (within 1 cent)
                                print(f"[ORDER] VALIDATION PASSED: Price matches (delta: {price_delta}¢)")
                            
                            # Also check if we have sufficient liquidity at expected price or better
                            if available_contracts < count:
                                print(f"[ORDER] WARNING: Available liquidity ({available_contracts} contracts) is less than requested ({count} contracts) at {expected_price_cents}¢ or better")
                                # Still allow the order, but it will be capped to available liquidity
                            
                            # Validation passed! Use EXACT BookieBeats price
                            price_cents = expected_price_cents
                            
                            # LIQUIDITY FILTER: Skip if available liquidity is under $40
                            # CRITICAL: We ALWAYS place limit orders at expected_price_cents (no slippage)
                            # The order will either fill immediately at expected_price_cents or better, or sit on book
                            # For manual bets, we check if there's sufficient liquidity at best_ask (which may be better than expected)
                            # This allows manual bets when market has moved slightly, but order still fills at our limit price
                            if skip_duplicate_check:
                                # Manual bet: Check if best_ask is at or better than expected price
                                # If best_ask <= expected_price_cents, our limit order will fill immediately at best_ask (better price - no slippage!)
                                # If best_ask > expected_price_cents, our limit order sits on book at expected_price_cents (no slippage, may not fill)
                                best_ask = orderbook.get('yes', {}).get('best_ask', 0)
                                best_ask_cents = int(best_ask * 100) if best_ask > 0 else 0
                                best_ask_size = orderbook.get('yes', {}).get('best_ask_size', 0)
                                
                                if best_ask_cents > 0 and best_ask_cents <= expected_price_cents:
                                    # Best ask is at or better than expected - our limit order will fill immediately at best_ask
                                    # Check liquidity at best_ask (which is better than expected, so we'll get better fill)
                                    liquidity_at_best_ask_dollars = (best_ask_size * best_ask * 100) / 100.0
                                    if liquidity_at_best_ask_dollars >= 40.0:
                                        print(f"[ORDER] Manual bet: Market moved in our favor! Best ask {best_ask_cents}¢ <= expected {expected_price_cents}¢")
                                        print(f"[ORDER]   Limit order at {expected_price_cents}¢ will fill immediately at {best_ask_cents}¢ (better price, no slippage)")
                                        print(f"[ORDER]   Available liquidity at best_ask: ${liquidity_at_best_ask_dollars:.2f}")
                                        # Allow order - it will fill at best_ask (better than expected, no slippage)
                                    elif available_liquidity_dollars < 40.0:
                                        print(f"[ORDER] BLOCKED: Insufficient liquidity at expected price ${available_liquidity_dollars:.2f} or best_ask ${liquidity_at_best_ask_dollars:.2f}")
                                        return {
                                            "error": "Insufficient liquidity",
                                            "message": f"Available liquidity ${available_liquidity_dollars:.2f} is below $40 minimum",
                                            "available_liquidity": available_liquidity_dollars,
                                            "minimum_required": 40.0
                                        }
                                elif best_ask_cents > expected_price_cents:
                                    # Best ask is worse than expected - our limit order will sit on book at expected_price_cents
                                    # Only allow if there's liquidity at expected price (order may not fill immediately)
                                    if available_liquidity_dollars < 40.0:
                                        print(f"[ORDER] BLOCKED: Best ask {best_ask_cents}¢ > expected {expected_price_cents}¢, and insufficient liquidity ${available_liquidity_dollars:.2f}")
                                        print(f"[ORDER]   Limit order would sit on book at {expected_price_cents}¢ (may not fill)")
                                        return {
                                            "error": "Insufficient liquidity",
                                            "message": f"Available liquidity ${available_liquidity_dollars:.2f} is below $40 minimum",
                                            "available_liquidity": available_liquidity_dollars,
                                            "minimum_required": 40.0
                                        }
                                    else:
                                        print(f"[ORDER] Manual bet: Best ask {best_ask_cents}¢ > expected {expected_price_cents}¢")
                                        print(f"[ORDER]   Limit order will be placed at {expected_price_cents}¢ (may sit on book, no slippage)")
                                elif available_liquidity_dollars < 40.0:
                                    print(f"[ORDER] BLOCKED: Available liquidity ${available_liquidity_dollars:.2f} is below $40 minimum threshold")
                                    return {
                                        "error": "Insufficient liquidity",
                                        "message": f"Available liquidity ${available_liquidity_dollars:.2f} is below $40 minimum",
                                        "available_liquidity": available_liquidity_dollars,
                                        "minimum_required": 40.0
                                    }
                            else:
                                # Auto-bet: Taker order - check liquidity at best_ask where order will actually fill
                                # Taker orders (post_only=False) fill immediately at best_ask, not at limit price
                                best_ask = orderbook.get('yes', {}).get('best_ask', 0)
                                best_ask_cents = int(best_ask * 100) if best_ask > 0 else 0
                                best_ask_size = orderbook.get('yes', {}).get('best_ask_size', 0)
                                
                                # COMPREHENSIVE LIQUIDITY LOGGING
                                print(f"[ORDER] ========== LIQUIDITY CHECK (TAKER ORDER) ==========")
                                print(f"[ORDER] Expected price: {expected_price_cents}¢")
                                print(f"[ORDER] Best ask: {best_ask_cents}¢ ({best_ask:.4f})")
                                print(f"[ORDER] Best ask size: {best_ask_size} contracts")
                                
                                # Log full orderbook asks
                                yes_asks = orderbook.get('yes', {}).get('asks', [])
                                print(f"[ORDER] Full YES asks orderbook: {len(yes_asks)} levels")
                                if not yes_asks:
                                    print(f"[ORDER] ⚠️  WARNING: YES asks list is EMPTY! Orderbook structure:")
                                    print(f"[ORDER]   orderbook type: {type(orderbook)}")
                                    print(f"[ORDER]   orderbook keys: {list(orderbook.keys()) if isinstance(orderbook, dict) else 'N/A'}")
                                    print(f"[ORDER]   orderbook['yes'] type: {type(orderbook.get('yes'))}")
                                    print(f"[ORDER]   orderbook['yes'] keys: {list(orderbook.get('yes', {}).keys()) if isinstance(orderbook.get('yes'), dict) else 'N/A'}")
                                    print(f"[ORDER]   orderbook['yes'].get('asks'): {orderbook.get('yes', {}).get('asks')}")
                                    print(f"[ORDER]   orderbook['yes'].get('best_ask'): {orderbook.get('yes', {}).get('best_ask')}")
                                    print(f"[ORDER]   orderbook['yes'].get('best_ask_size'): {orderbook.get('yes', {}).get('best_ask_size')}")
                                total_asks_liquidity = 0.0
                                for i, ask in enumerate(yes_asks[:10]):  # Show top 10 levels
                                    ask_price = ask.get('price', 0)
                                    ask_price_cents = int(ask_price * 100)
                                    ask_size = ask.get('quantity', 0)
                                    ask_liquidity = (ask_size * ask_price * 100) / 100.0
                                    total_asks_liquidity += ask_liquidity
                                    print(f"[ORDER]   Level {i+1}: {ask_price_cents}¢ ({ask_price:.4f}) - {ask_size} contracts = ${ask_liquidity:.2f}")
                                if len(yes_asks) > 10:
                                    print(f"[ORDER]   ... ({len(yes_asks) - 10} more levels)")
                                print(f"[ORDER] Total liquidity in top 10 ask levels: ${total_asks_liquidity:.2f}")
                                
                                if best_ask_cents > 0:
                                    # CRITICAL: For taker orders, sum liquidity across ALL ask levels up to expected price (or within tolerance)
                                    # Taker orders will walk the book, filling at multiple price levels
                                    # We need to check if there's enough total liquidity, not just at best_ask
                                    max_price_cents = expected_price_cents + 4  # Allow up to 4 cents worse (same tolerance as price validation)
                                    total_liquidity_dollars = 0.0
                                    
                                    print(f"[ORDER] Calculating total liquidity for taker order:")
                                    print(f"[ORDER]   Expected price: {expected_price_cents}¢")
                                    print(f"[ORDER]   Best ask: {best_ask_cents}¢")
                                    print(f"[ORDER]   Max price to check: {max_price_cents}¢ (expected + 4¢ tolerance)")
                                    
                                    for i, ask in enumerate(yes_asks):
                                        ask_price = ask.get('price', 0)
                                        ask_price_cents = int(ask_price * 100)
                                        ask_quantity = ask.get('quantity', 0)
                                        
                                        # Only count asks at or below max_price_cents
                                        if ask_price_cents <= max_price_cents:
                                            ask_liquidity_dollars = (ask_quantity * ask_price * 100) / 100.0
                                            total_liquidity_dollars += ask_liquidity_dollars
                                            print(f"[ORDER]   Level {i+1}: {ask_price_cents}¢ - {ask_quantity} contracts = ${ask_liquidity_dollars:.2f} ✓ (total: ${total_liquidity_dollars:.2f})")
                                        else:
                                            print(f"[ORDER]   Level {i+1}: {ask_price_cents}¢ - {ask_quantity} contracts ✗ (too expensive, > {max_price_cents}¢)")
                                            break  # Asks are sorted by price, so we can stop here
                                    
                                    print(f"[ORDER] Total liquidity across all eligible ask levels: ${total_liquidity_dollars:.2f}")
                                    print(f"[ORDER] Minimum required: $40.00")
                                    print(f"[ORDER] Check: ${total_liquidity_dollars:.2f} >= $40.00? {total_liquidity_dollars >= 40.0}")
                                    
                                    # For taker orders, we need enough total liquidity across all price levels
                                    if total_liquidity_dollars >= 40.0:
                                        if best_ask_cents <= expected_price_cents:
                                            print(f"[ORDER] ✅ PASSED: Best ask {best_ask_cents}¢ <= expected {expected_price_cents}¢ - taker order will fill at better prices")
                                        else:
                                            print(f"[ORDER] ✅ PASSED: Best ask {best_ask_cents}¢ > expected {expected_price_cents}¢ - taker order will fill (within tolerance)")
                                        print(f"[ORDER]   Total available liquidity: ${total_liquidity_dollars:.2f}")
                                    else:
                                        print(f"[ORDER] ❌ BLOCKED: Insufficient total liquidity ${total_liquidity_dollars:.2f} (below $40 minimum)")
                                        print(f"[ORDER]   Best ask: {best_ask_cents}¢, Size: {best_ask_size} contracts")
                                        print(f"[ORDER]   Total across all eligible levels: ${total_liquidity_dollars:.2f}")
                                        print(f"[ORDER] =================================================")
                                        return {
                                            "error": "Insufficient liquidity",
                                            "message": f"Available liquidity ${total_liquidity_dollars:.2f} is below $40 minimum",
                                            "available_liquidity": total_liquidity_dollars,
                                            "minimum_required": 40.0,
                                            "best_ask": best_ask_cents,
                                            "best_ask_size": best_ask_size,
                                            "orderbook_asks": yes_asks[:10]  # Include top 10 levels for debugging
                                        }
                                    print(f"[ORDER] =================================================")
                                else:
                                    # No best_ask available - cannot place taker order
                                    print(f"[ORDER] ❌ BLOCKED: No best_ask available - cannot place taker order")
                                    print(f"[ORDER]   Orderbook YES asks: {yes_asks}")
                                    print(f"[ORDER] =================================================")
                                    return {
                                        "error": "Insufficient liquidity",
                                        "message": "No best_ask available in orderbook",
                                        "available_liquidity": 0.0,
                                        "minimum_required": 40.0,
                                        "orderbook_asks": yes_asks
                                    }
                            
                            # Store available liquidity for capping
                            orderbook['available_contracts_yes'] = available_contracts
                            orderbook['available_liquidity_dollars_yes'] = available_liquidity_dollars
                        else:
                            # No ask price in orderbook - cannot validate price match, REJECT
                            print(f"[ORDER] ERROR: No ask price available for YES side - cannot validate price match")
                            return {"error": "No ask price available - cannot validate"}
                    else:  # no
                        # CRITICAL: For buying NO, we need best_ask (what we pay to buy), not best_bid (what we get to sell)
                        best_ask = orderbook['no'].get('best_ask')
                    # CRITICAL: If best_ask is None, try to compute it from YES bids (NO ask = 1 - YES bid)
                    if best_ask is None:
                        yes_bids = orderbook['yes'].get('bids', [])
                        if yes_bids:
                            # YES bids are sorted by price (ascending), so last element is highest (best YES bid)
                            # NO ask = 1 - YES best_bid
                            if isinstance(yes_bids, list) and len(yes_bids) > 0:
                                # Handle both dict format and list format
                                if isinstance(yes_bids[0], dict):
                                    yes_best_bid_price = yes_bids[-1].get('price', 0)
                                elif isinstance(yes_bids[0], list) and len(yes_bids[0]) >= 2:
                                    yes_best_bid_price = yes_bids[-1][0] / 100.0  # Convert cents to probability
                                else:
                                    yes_best_bid_price = yes_bids[-1] if isinstance(yes_bids[-1], (int, float)) else 0
                                best_ask = 1.0 - yes_best_bid_price
                                print(f"[ORDER] Computed NO best_ask from YES bids: {best_ask:.4f}")
                    
                    if best_ask is not None:
                        # For NO side: price in cents = best_ask * 100
                        # best_ask is already the probability (0.0 to 1.0), so multiply by 100 to get cents
                        current_price_cents = int(best_ask * 100)
                        price_delta = abs(current_price_cents - expected_price_cents)
                        
                        # DIAGNOSTIC: Log full orderbook structure to verify we're reading correct side
                        yes_best_bid = orderbook['yes'].get('best_bid')
                        yes_best_ask = orderbook['yes'].get('best_ask')
                        no_best_bid = orderbook['no'].get('best_bid')
                        print(f"[ORDER] ========== PRICE VALIDATION DIAGNOSTIC ==========")
                        print(f"[ORDER] Side: NO (buying NO contracts)")
                        print(f"[ORDER] Orderbook structure:")
                        print(f"[ORDER]   YES best_bid={yes_best_bid}, YES best_ask={yes_best_ask}")
                        print(f"[ORDER]   NO best_bid={no_best_bid}, NO best_ask={best_ask}")
                        print(f"[ORDER]   Verification: NO ask + YES bid = {best_ask + (yes_best_bid or 0):.4f} (should be ~1.0)")
                        print(f"[ORDER] Price comparison:")
                        print(f"[ORDER]   BookieBeats expected price: {expected_price_cents}¢")
                        print(f"[ORDER]   Kalshi current NO ask: {current_price_cents}¢ (from best_ask={best_ask})")
                        print(f"[ORDER]   Price delta: {price_delta}¢")
                        if price_delta > 10:
                            print(f"[ORDER]   ⚠️  WARNING: Large price discrepancy! This suggests:")
                            print(f"[ORDER]      - Wrong market/submarket matched")
                            print(f"[ORDER]      - Stale orderbook data")
                            print(f"[ORDER]      - Side determination error")
                        print(f"[ORDER] =================================================")
                        
                        # CRITICAL: For taker orders, check liquidity at best_ask (where order will actually fill)
                        # For limit orders, check liquidity at limit price
                        # For buying NO: we need NO asks (people selling NO to us)
                        # NO asks are implied from YES bids: NO ask = 1 - YES bid
                        best_ask = orderbook['no'].get('best_ask', 0)
                        best_ask_cents = int(best_ask * 100) if best_ask > 0 else expected_price_cents
                        
                        # For taker orders (post_only=False), order fills at best_ask, so check liquidity there
                        # For limit orders, we check at limit price (expected_price_cents)
                        execution_price_cents = min(best_ask_cents, expected_price_cents) if best_ask > 0 else expected_price_cents
                        
                        available_contracts = 0
                        yes_bids = orderbook['yes'].get('bids', [])
                        no_asks = orderbook['no'].get('asks', [])
                        
                        # For taker orders, check NO asks (where we'll actually buy)
                        if no_asks and not post_only:
                            for ask in no_asks:
                                ask_price = ask.get('price', 0)
                                ask_price_cents = int(ask_price * 100)
                                if ask_price_cents <= execution_price_cents:
                                    available_contracts += ask.get('quantity', 0)
                                else:
                                    break  # Asks are sorted, so we can stop here
                        else:
                            # Fallback: calculate from YES bids (for limit orders)
                            print(f"[ORDER] Checking NO liquidity: {len(yes_bids)} YES bids in orderbook")
                            for bid in yes_bids:
                                bid_price = bid.get('price', 0)
                                bid_price_cents = int(bid_price * 100)
                                # NO ask price = 100 - YES bid price
                                no_ask_price_cents = 100 - bid_price_cents
                                quantity = bid.get('quantity', 0)
                                if no_ask_price_cents <= execution_price_cents:
                                    available_contracts += quantity
                                    print(f"[ORDER]   YES bid {bid_price_cents}¢ → NO ask {no_ask_price_cents}¢ (qty: {quantity}) ✓")
                                else:
                                    print(f"[ORDER]   YES bid {bid_price_cents}¢ → NO ask {no_ask_price_cents}¢ (qty: {quantity}) ✗ (too expensive)")
                        
                        # Calculate available liquidity in dollars at execution price
                        available_liquidity_dollars = (available_contracts * execution_price_cents) / 100.0
                        print(f"[ORDER] Available liquidity: {available_contracts} contracts = ${available_liquidity_dollars:.2f} at {execution_price_cents}¢ (best_ask: {best_ask_cents}¢, expected: {expected_price_cents}¢)")
                        
                        # CRITICAL: Validate price for limit orders
                        # Check direction FIRST (worse vs better), then check absolute delta
                        # This ensures we catch "worse price" cases before checking absolute delta
                        if current_price_cents > expected_price_cents:
                            # Current price is WORSE than expected - we'd pay more than BB said
                            price_delta_worse = current_price_cents - expected_price_cents
                            if price_delta_worse > 1:
                                print(f"[ORDER] VALIDATION FAILED: Price got WORSE! Expected {expected_price_cents}¢, got {current_price_cents}¢ (delta: {price_delta_worse}¢ worse)")
                                print(f"[ORDER]   ⚠️  Auto-bets only allow 1¢ worse - rejecting to avoid overpaying")
                                return {
                                    "error": "Odds changed",
                                    "expected": expected_price_cents,
                                    "current": current_price_cents,
                                    "delta": price_delta_worse,
                                    "reason": f"Price delta too large ({price_delta_worse}¢) - likely wrong submarket"
                                }
                        elif current_price_cents < expected_price_cents:
                            # Current price is BETTER than expected - our limit order will fill immediately at better price
                            # For both manual and auto-bets: Allow up to 4¢ better (good fill opportunity, unlikely to be wrong market)
                            max_better_delta = 4
                            price_delta_better = expected_price_cents - current_price_cents
                            if price_delta_better <= max_better_delta:
                                bet_type_str = "Manual bet" if skip_duplicate_check else "Auto-bet"
                                print(f"[ORDER] VALIDATION PASSED: Price got BETTER! Expected {expected_price_cents}¢, got {current_price_cents}¢ (delta: {price_delta_better}¢ better - {bet_type_str} allows up to {max_better_delta}¢ better)")
                            else:
                                # Delta too large even though better - likely wrong market
                                print(f"[ORDER] VALIDATION FAILED: Price delta too large ({price_delta_better}¢) even though better - likely WRONG SUBMARKET! (max allowed: {max_better_delta}¢)")
                                return {
                                    "error": "Odds changed",
                                    "expected": expected_price_cents,
                                    "current": current_price_cents,
                                    "delta": price_delta_better,
                                    "reason": f"Price delta too large ({price_delta_better}¢) even though better - likely wrong submarket (max allowed: {max_better_delta}¢)"
                                }
                        # If we get here, prices match (within 1 cent) - validate absolute delta as final check
                        if price_delta > 2:
                            print(f"[ORDER] VALIDATION FAILED: Price delta too large ({price_delta}¢) - likely WRONG SUBMARKET! Expected {expected_price_cents}¢, got {current_price_cents}¢")
                            print(f"[ORDER]   ⚠️  Large price discrepancy suggests wrong market/submarket was matched")
                            return {
                                "error": "Odds changed",
                                "expected": expected_price_cents,
                                "current": current_price_cents,
                                "delta": price_delta,
                                "reason": f"Price delta too large ({price_delta}¢) - likely wrong submarket"
                            }
                        else:
                            # Prices match (within 1 cent)
                            print(f"[ORDER] VALIDATION PASSED: Price matches (delta: {price_delta}¢)")
                        
                        # Also check if we have sufficient liquidity at expected price or better
                        if available_contracts < count:
                            print(f"[ORDER] WARNING: Available liquidity ({available_contracts} contracts) is less than requested ({count} contracts) at {expected_price_cents}¢ or better")
                            # Still allow the order, but it will be capped to available liquidity
                        
                        # Validation passed! Use EXACT BookieBeats price
                        price_cents = expected_price_cents
                        # Store available liquidity for capping
                        orderbook['available_contracts_no'] = available_contracts
                    else:
                        # No ask price in orderbook - try to compute from YES bids before rejecting
                        yes_bids = orderbook['yes'].get('bids', [])
                        if yes_bids:
                            # Compute NO ask from YES bids (NO ask = 1 - YES best_bid)
                            if isinstance(yes_bids, list) and len(yes_bids) > 0:
                                if isinstance(yes_bids[0], dict):
                                    yes_best_bid_price = yes_bids[-1].get('price', 0)
                                elif isinstance(yes_bids[0], list) and len(yes_bids[0]) >= 2:
                                    yes_best_bid_price = yes_bids[-1][0] / 100.0
                                else:
                                    yes_best_bid_price = yes_bids[-1] if isinstance(yes_bids[-1], (int, float)) else 0
                                
                                # Compute best_ask and validate price
                                best_ask = 1.0 - yes_best_bid_price
                                print(f"[ORDER] Computed NO best_ask from YES bids: {best_ask:.4f}")
                                current_price_cents = int(best_ask * 100)
                                price_delta = abs(current_price_cents - expected_price_cents)
                                
                                # Validate price (same logic as when best_ask was available)
                                if price_delta > 2:
                                    print(f"[ORDER] VALIDATION FAILED: Price delta too large ({price_delta}¢) - likely WRONG SUBMARKET! Expected {expected_price_cents}¢, got {current_price_cents}¢")
                                    return {
                                        "error": "Odds changed",
                                        "expected": expected_price_cents,
                                        "current": current_price_cents,
                                        "delta": price_delta
                                    }
                                elif current_price_cents > expected_price_cents:
                                    price_delta_worse = current_price_cents - expected_price_cents
                                    if price_delta_worse > 1:
                                        print(f"[ORDER] VALIDATION FAILED: Price got WORSE! Expected {expected_price_cents}¢, got {current_price_cents}¢ (delta: {price_delta_worse}¢ worse)")
                                        return {
                                            "error": "Odds changed",
                                            "expected": expected_price_cents,
                                            "current": current_price_cents
                                        }
                                elif current_price_cents < expected_price_cents:
                                    # For both manual and auto-bets: Allow up to 4¢ better (good fill opportunity, unlikely to be wrong market)
                                    max_better_delta = 4
                                    price_delta_better = expected_price_cents - current_price_cents
                                    if price_delta_better > max_better_delta:
                                        print(f"[ORDER] VALIDATION FAILED: Price delta too large ({price_delta_better}¢) even though better - likely WRONG SUBMARKET! (max allowed: {max_better_delta}¢)")
                                        return {
                                            "error": "Odds changed",
                                            "expected": expected_price_cents,
                                            "current": current_price_cents,
                                            "delta": price_delta_better
                                        }
                                
                                # Validation passed - use expected price
                                price_cents = expected_price_cents
                                print(f"[ORDER] VALIDATION PASSED: Price validated using computed best_ask (delta: {price_delta}¢)")
                            else:
                                print(f"[ORDER] ERROR: No ask price available for NO side and cannot compute from YES bids - cannot validate price match")
                                return {"error": "No ask price available - cannot validate"}
                        else:
                            print(f"[ORDER] ERROR: No ask price available for NO side - cannot validate price match")
                            return {"error": "No ask price available - cannot validate"}
                    
                    # Calculate validation time (after both YES and NO side validation)
                    validation_time = (time.time() - validation_start) * 1000
                    print(f"[ORDER] Price validation completed in {validation_time:.1f}ms")
                else:
                    # Validation skipped - use expected price directly
                    if expected_price_cents is not None:
                        price_cents = expected_price_cents
                        print(f"[ORDER] Validation skipped - using expected price: {price_cents}¢")
                    else:
                        print(f"[ORDER] WARNING: No expected price and validation disabled - cannot determine price")
                        return {"error": "No price specified and validation disabled"}
                
                # If no price specified, get from orderbook
                if price_cents is None:
                        if not orderbook:
                            orderbook = await self.fetch_orderbook(ticker)
                        if not orderbook:
                            if attempt < 2:
                                # Faster retry - only 50ms delay for first retry
                                await asyncio.sleep(0.05 * (2 ** attempt))
                                continue
                            return {"error": "Could not fetch orderbook"}
                        
                        if side.lower() == "yes":
                            best_ask = orderbook['yes'].get('best_ask')
                            if best_ask:
                                # For YES side: price in cents = best_ask * 100
                                price_cents = int(best_ask * 100)
                            else:
                                return {"error": "No ask price available"}
                        else:  # no
                            # For buying NO, use best_ask (what we pay to buy)
                            best_ask = orderbook['no'].get('best_ask')
                            if best_ask:
                                price_cents = int(best_ask * 100)
                            else:
                                return {"error": "No ask price available"}
                
                # CRITICAL: Cap contracts based on available liquidity
                # Priority: 1) Available orderbook liquidity, 2) max_liquidity_dollars setting
                final_count = int(count)
                available_contracts = None  # Initialize for error messages
                available_liquidity_dollars = None
                print(f"[ORDER] Contract capping: requested={count}, price={price_cents}¢")
                
                if orderbook:
                    # Get available contracts and liquidity from orderbook
                    available_key = f'available_contracts_{side.lower()}'
                    available_liquidity_key = f'available_liquidity_dollars_{side.lower()}'
                    available_contracts = orderbook.get(available_key, 0)
                    available_liquidity_dollars = orderbook.get(available_liquidity_key, 0)
                    
                    print(f"[ORDER] Orderbook liquidity: {available_contracts} contracts = ${available_liquidity_dollars:.2f}")
                    
                    # Cap at available orderbook liquidity (most restrictive)
                    if available_contracts > 0:
                        max_contracts_from_orderbook = available_contracts
                        final_count = min(count, max_contracts_from_orderbook)
                        if final_count < count:
                            print(f"[ORDER] Capping to available orderbook liquidity: {count} -> {final_count} contracts")
                    
                    # Also cap by max_liquidity_dollars if provided
                    if max_liquidity_dollars:
                        # Use conservative price (limit price + 2 cent buffer) for contract calculation
                        conservative_price_cents = price_cents + 2  # Add 2 cent buffer for slippage protection
                        max_contracts_from_bb = int(max_liquidity_dollars / (conservative_price_cents / 100.0))
                        
                        print(f"[ORDER] Max bet setting: ${max_liquidity_dollars:.2f} = {max_contracts_from_bb} contracts (at {conservative_price_cents}¢ with 2¢ buffer)")
                        
                        # Cap at minimum of: current final_count, BB max
                        final_count = min(final_count, max_contracts_from_bb)
                        
                        if final_count < count:
                            print(f"[ORDER] Capped by max bet setting: {count} -> {final_count} contracts")
                else:
                    print(f"[ORDER] No orderbook available for liquidity capping")
                
                print(f"[ORDER] Final contract count: {final_count}")
                
                # CRITICAL: Reject orders with 0 contracts (no liquidity available or invalid request)
                if final_count <= 0:
                    if available_contracts is not None:
                        error_msg = f"No liquidity available: requested {count} contracts but only {available_contracts} available at {price_cents}¢ or better"
                    elif count <= 0:
                        error_msg = f"Invalid request: cannot place order with {count} contracts"
                    else:
                        error_msg = f"No liquidity available: requested {count} contracts but none available at {price_cents}¢"
                    print(f"[ORDER] ERROR: Cannot place order - {error_msg}")
                    return {
                        "error": "No liquidity available",
                        "details": error_msg
                    }
                
                # Prepare order payload with SIDE-SPECIFIC price field (Kalshi requirement)
                # Kalshi requires "yes_price" for side="yes" or "no_price" for side="no" (in cents)
                order_payload = {
                    "ticker": ticker.upper(),
                    "side": side.lower(),
                    "action": "buy",  # Always buying contracts
                    "count": final_count,
                    "type": "limit"  # Limit order for price control
                }
                
                # Add side-specific price field (Kalshi API requirement)
                # CRITICAL: Limit orders will ONLY fill at price_cents or better (never worse)
                # If market price is better, order fills immediately at better price (no slippage)
                # If market price is worse, order sits on book at price_cents (may not fill, but no slippage)
                if side.lower() == "yes":
                    order_payload["yes_price"] = int(price_cents)
                    print(f"[ORDER] Limit order: Will fill at {price_cents}¢ or BETTER (never worse) - no slippage protection")
                else:
                    order_payload["no_price"] = int(price_cents)
                    print(f"[ORDER] Limit order: Will fill at {price_cents}¢ or BETTER (never worse) - no slippage protection")
                
                # Add post-only flag if requested (maker order - won't cross, may rest on book)
                if post_only:
                    order_payload["post_only"] = True
                    print(f"[ORDER] Post-only mode: Order will only post as maker (rejected if would cross)")
                
                # CRITICAL: Only set expiration_ts for post-only (maker) orders
                # Taker orders (non-post-only) execute immediately or get rejected - expiration doesn't apply
                # Kalshi API may reject taker orders with expiration_ts, so only use it for post-only orders
                # NOTE: Kalshi expects expiration_ts in SECONDS (Unix timestamp), not milliseconds!
                if expiration_ts is not None:
                    # Ensure expiration_ts is in seconds (convert from milliseconds if needed)
                    if expiration_ts > 1e10:  # If it's in milliseconds (13+ digits)
                        expiration_ts = int(expiration_ts / 1000)
                    # Only add expiration_ts for post-only orders (maker orders)
                    if post_only:
                        order_payload["expiration_ts"] = int(expiration_ts)
                        exp_seconds = expiration_ts - int(time.time())
                        print(f"[ORDER] Order expires in {exp_seconds:.1f}s (at {expiration_ts})")
                    else:
                        print(f"[ORDER] ⚠️  Skipping expiration_ts for taker order (Kalshi doesn't accept it for non-post-only orders)")
                elif post_only:
                    # For post-only orders, set short expiration (2.5 seconds) to prevent late fills
                    # This ensures orders don't sit in the orderbook and get filled when price is no longer +EV
                    expiration_ts = int(time.time() + 2.5)
                    order_payload["expiration_ts"] = expiration_ts
                    print(f"[ORDER] ⏱️  Set short expiration: 2.5s (snipe strategy - immediate fill or cancel)")
                
                print(f"[ORDER] Order payload:")
                print(f"[ORDER]   Ticker: {order_payload['ticker']}")
                print(f"[ORDER]   Side: {order_payload['side']}")
                print(f"[ORDER]   Count: {order_payload['count']}")
                price_field = "yes_price" if side.lower() == "yes" else "no_price"
                print(f"[ORDER]   {price_field}: {order_payload[price_field]} cents ({order_payload[price_field]/100:.2f}¢)")
                print(f"[ORDER]   Type: {order_payload['type']}")
                
                # CRITICAL FIX: All v2 API paths must include /trade-api/v2 prefix for signing
                # This matches what Kalshi's server sees and expects in the signature
                path = "/trade-api/v2/portfolio/orders"
                
                # CRITICAL: Serialize JSON ONCE and use for both signature AND request body
                # This ensures 100% match between what we sign and what we send
                import json as json_lib
                json_body = json_lib.dumps(order_payload, separators=(',', ':'), sort_keys=True)
                
                print(f"[ORDER] ========== AUTHENTICATION DEBUG ==========")
                print(f"[ORDER] Order payload (dict): {order_payload}")
                print(f"[ORDER] JSON body (serialized): {json_body}")
                print(f"[ORDER] JSON body (bytes): {json_body.encode('utf-8')}")
                print(f"[ORDER] JSON body (hex): {json_body.encode('utf-8').hex()}")
                print(f"[ORDER] Path (for signing and request): {path}")
                print(f"[ORDER] Base URL: {self.base_url}")
                print(f"[ORDER] Full URL: {self.base_url}{path}")
                
                sign_start = time.time()
                # CRITICAL FIX: All v2 API paths must include /trade-api/v2 prefix for signing
                # This matches what Kalshi's server sees and expects in the signature
                print(f"[ORDER] Signing with full path: {path}")
                print(f"[ORDER] Full request URL: {self.base_url}{path}")
                ts, sig = self.auth.sign("POST", path, json_body)
                sign_time = (time.time() - sign_start) * 1000
                print(f"[ORDER] Request signing completed in {sign_time:.1f}ms")
                
                headers = {
                    "KALSHI-ACCESS-KEY": self.auth.kid,
                    "KALSHI-ACCESS-SIGNATURE": sig,
                    "KALSHI-ACCESS-TIMESTAMP": ts,
                    "Content-Type": "application/json"  # Required for JSON body
                }
                
                print(f"[ORDER] Request headers:")
                print(f"[ORDER]   KALSHI-ACCESS-KEY: {self.auth.kid}")
                print(f"[ORDER]   KALSHI-ACCESS-TIMESTAMP: {ts}")
                print(f"[ORDER]   KALSHI-ACCESS-SIGNATURE: {sig[:50]}...")
                print(f"[ORDER]   Content-Type: application/json")
                print(f"[ORDER] Request body (will be sent):")
                print(f"[ORDER]   As string: {json_body}")
                print(f"[ORDER]   As bytes: {json_body.encode('utf-8')}")
                print(f"[ORDER]   Bytes length: {len(json_body.encode('utf-8'))}")
                print(f"[ORDER] ===========================================")
                
                # CRITICAL: Verify key is loaded and valid
                if not self.auth.priv:
                    print(f"[ORDER] ERROR: Private key not loaded!")
                    return {"error": "Private key not loaded"}
                if not self.auth.kid:
                    print(f"[ORDER] ERROR: Key ID not set!")
                    return {"error": "Key ID not set"}
                
                # Verify we're using production API (not demo)
                if self.demo_mode:
                    print(f"[ORDER] WARNING: Using DEMO mode - orders won't be real!")
                print(f"[ORDER] Using base URL: {self.base_url}")
                print(f"[ORDER] Key ID: {self.auth.kid[:8]}...")
                
                # Place order with minimal timeout for speed (reduced from 3s to 2s)
                api_start = time.time()
                print(f"[ORDER] Sending POST request to Kalshi API...")
                print(f"[ORDER] Full URL: {self.base_url}{path}")
                
                # CRITICAL: Use data= with pre-serialized JSON bytes to ensure exact match with signature
                # aiohttp's json= parameter uses default json.dumps (with spaces), which won't match our signature
                # We must use data= with the exact JSON string we signed
                # CRITICAL: Use data= with pre-serialized JSON bytes to ensure exact match with signature
                # aiohttp's json= parameter uses default json.dumps (with spaces), which won't match our signature
                # We must use data= with the exact JSON string we signed
                # IMPORTANT: When using data= with bytes, aiohttp will NOT automatically set Content-Type
                # So we must set it manually in headers
                # Use asyncio.wait_for instead of ClientTimeout to avoid context manager issues
                async with self.session.post(
                    f"{self.base_url}{path}",
                    headers=headers,
                    data=json_body.encode('utf-8')  # Send as UTF-8 bytes - exact match with signature
                ) as resp:
                    api_time = (time.time() - api_start) * 1000
                    print(f"[ORDER] API response received in {api_time:.1f}ms")
                    print(f"[ORDER] HTTP Status: {resp.status}")
                    
                    # Log response headers for debugging
                    if resp.status != 200:
                        print(f"[ORDER] Response headers:")
                        for header_name, header_value in resp.headers.items():
                            if 'kalshi' in header_name.lower() or 'error' in header_name.lower():
                                print(f"[ORDER]   {header_name}: {header_value}")
                    
                    # Treat both 200 (success) and 201 (created) as success
                    if resp.status in [200, 201]:
                        # Initialize fee_type at the very start to avoid "local variable referenced before assignment" errors
                        fee_type = None
                        try:
                            result = await resp.json()
                            # Kalshi returns order data in 'order' key for 201, or directly for 200
                            order_data = result.get('order', result)
                            
                            # DEBUG: Log full order response to see available fields
                            print(f"[ORDER] Full order response keys: {list(order_data.keys())}")
                            if 'fills' in order_data:
                                print(f"[ORDER] Fills data: {order_data.get('fills', [])[:2]}")  # First 2 fills
                            
                            order_id = order_data.get('order_id') or order_data.get('id', 'N/A')
                            
                            # CRITICAL: Check if 'fills' array exists - this is the most accurate source
                            fills_array = order_data.get('fills', [])
                            actual_fill_count = 0
                            actual_fill_cost_cents = 0
                            
                            if fills_array:
                                # Calculate actual fill from fills array (most accurate)
                                for fill in fills_array:
                                    fill_qty = fill.get('count', 0) or fill.get('fill_count', 0)
                                    fill_price_cents = fill.get('price', 0) or fill.get('fill_price', 0)
                                    if fill_price_cents < 1:  # Decimal format (0.45)
                                        fill_price_cents = int(fill_price_cents * 100)
                                    else:
                                        fill_price_cents = int(fill_price_cents)
                                    actual_fill_count += fill_qty
                                    actual_fill_cost_cents += fill_price_cents * fill_qty
                                
                                if actual_fill_count > 0:
                                    fill_count = actual_fill_count
                                    print(f"[ORDER] Using fills array: {actual_fill_count} contracts, ${actual_fill_cost_cents/100:.2f} total")
                                else:
                                    # Fallback to fill_count field
                                    fill_count = order_data.get('fill_count') or order_data.get('filled', 0)
                                    print(f"[ORDER] WARNING: fills array empty, using fill_count field: {fill_count}")
                            else:
                                # No fills array, use fill_count field
                                fill_count = order_data.get('fill_count') or order_data.get('filled', 0)
                                print(f"[ORDER] No fills array, using fill_count field: {fill_count}")
                            
                            initial_count = order_data.get('initial_count') or order_data.get('count', final_count)
                            # Determine status: 'executed' = fully filled, 'pending'/'queued' = not filled, 'partial' = partially filled
                            is_fully_filled = (fill_count >= initial_count)
                            status = order_data.get('status', 'executed' if is_fully_filled else 'pending')
                            remaining_count = order_data.get('remaining_count', 0) or max(0, initial_count - fill_count)
                            queue_position = order_data.get('queue_position', None)
                            
                            # Log all fill-related fields for debugging
                            print(f"[ORDER] Fill details from Kalshi:")
                            print(f"[ORDER]   fill_count field: {order_data.get('fill_count', 'N/A')}")
                            print(f"[ORDER]   initial_count: {initial_count}")
                            print(f"[ORDER]   Using fill_count: {fill_count}")
                            print(f"[ORDER]   remaining_count: {remaining_count}")
                            print(f"[ORDER]   queue_position: {queue_position}")
                            print(f"[ORDER]   Status: {status}")
                            print(f"[ORDER]   Fully filled: {is_fully_filled}")
                            
                            # Initialize fee_type, executed_price_cents, and total_cost_cents early
                            # These may be needed if we return early due to cancel
                            fee_type = None
                            executed_price_cents = None
                            total_cost_cents = None
                            
                            if fill_count > 0:
                                # Calculate fee type from order data if available
                                taker_fees_cents = order_data.get('taker_fees', 0) or 0
                                fee_type = 'maker' if taker_fees_cents == 0 else 'taker'
                            
                            # CRITICAL: If order is not fully filled, we need to cancel it immediately
                            # ALL bets (both auto and manual) are "snipes" - immediate fill or cancel
                            # Unfilled limit orders can sit in the orderbook and get filled later when price is no longer +EV
                            # This is a "snipe" strategy - we want immediate fills or nothing
                            # Manual bets are just user-selected snipes, but still need to be cancelled if not fully filled
                            if not is_fully_filled and remaining_count > 0:
                                print(f"[ORDER] ⚠️  WARNING: Order not fully filled! {fill_count}/{initial_count} filled, {remaining_count} remaining")
                                print(f"[ORDER]   Status: {status}, Queue position: {queue_position}")
                                
                                # ALL orders (both auto and manual) must be cancelled if not fully filled
                                # This prevents getting filled at a price that's no longer +EV
                                # "resting" means order is on the orderbook but not filled yet - CANCEL IMMEDIATELY
                                if status in ['pending', 'queued', 'open', 'partial', 'resting'] or queue_position is not None:
                                    bet_type = "Manual bet" if skip_duplicate_check else "Auto-bet"
                                    print(f"[ORDER] 🚨 CRITICAL: {bet_type} is {status} - cancelling IMMEDIATELY to prevent late fill at non-EV price")
                                    cancel_result = await self.cancel_order(order_id)
                                    if cancel_result.get('success'):
                                        print(f"[ORDER] ✅ Successfully cancelled pending order ({remaining_count} contracts)")
                                        # Calculate total_cost_cents for partial fill before returning
                                        # We need to calculate it here since we're returning early
                                        if fill_count > 0:
                                            if executed_price_cents:
                                                # Calculate from executed price + fees
                                                taker_fees = order_data.get('taker_fees', 0) or 0
                                                maker_fees = order_data.get('maker_fees', 0) or 0
                                                total_cost_cents = (executed_price_cents * fill_count) + taker_fees + maker_fees
                                            elif total_cost_cents is None:
                                                # Fallback: use limit price if executed price not available
                                                total_cost_cents = price_cents * fill_count
                                        else:
                                            total_cost_cents = 0
                                        # Return partial fill info
                                        return {
                                            "success": True,  # Partial success - we got some fills
                                            "order_id": order_id,
                                            "fill_count": fill_count,
                                            "initial_count": initial_count,
                                            "remaining_count": remaining_count,
                                            "status": "cancelled",
                                            "cancelled": True,
                                            "warning": "Order not fully filled, cancelled remaining portion to prevent late fill",
                                            "ticker": ticker,
                                            "side": side,
                                            "price_cents": price_cents,
                                            "executed_price_cents": executed_price_cents if fill_count > 0 else None,
                                            "total_cost_cents": total_cost_cents,
                                            "fee_type": fee_type
                                        }
                                    else:
                                        print(f"[ORDER] ⚠️  Failed to cancel order: {cancel_result.get('error', 'Unknown error')}")
                                        # Calculate total_cost_cents for partial fill before returning
                                        if fill_count > 0:
                                            if executed_price_cents:
                                                taker_fees = order_data.get('taker_fees', 0) or 0
                                                maker_fees = order_data.get('maker_fees', 0) or 0
                                                total_cost_cents = (executed_price_cents * fill_count) + taker_fees + maker_fees
                                            elif total_cost_cents is None:
                                                total_cost_cents = price_cents * fill_count
                                        else:
                                            total_cost_cents = 0
                                        # Still return partial fill info, but warn about uncancelled portion
                                        return {
                                            "success": True,  # Partial success - order was created
                                            "order_id": order_id,
                                            "fill_count": fill_count,
                                            "initial_count": initial_count,
                                            "remaining_count": remaining_count,
                                            "status": status,
                                            "cancelled": False,
                                            "warning": "Order created but not fully filled, could not cancel remaining portion - may fill later",
                                            "ticker": ticker,
                                            "side": side,
                                            "price_cents": price_cents,
                                            "executed_price_cents": executed_price_cents if fill_count > 0 else None,
                                            "total_cost_cents": total_cost_cents,
                                            "fee_type": fee_type if fee_type is not None else None
                                        }
                            if fills_array:
                                print(f"[ORDER]   Fills array length: {len(fills_array)}")
                                for i, fill in enumerate(fills_array[:3]):  # First 3 fills
                                    print(f"[ORDER]   Fill {i+1}: {fill}")
                            
                            # Extract executed/average fill price from order response
                            # Kalshi may return: executed_price, avg_fill_price, fill_price, or we calculate from cost
                            executed_price_cents = None
                            total_cost_cents = None
                            
                            # Try to get executed price directly
                            if 'executed_price' in order_data:
                                executed_price_cents = int(order_data['executed_price'])
                            elif 'avg_fill_price' in order_data:
                                executed_price_cents = int(order_data['avg_fill_price'] * 100) if order_data['avg_fill_price'] < 1 else int(order_data['avg_fill_price'])
                            elif 'fill_price' in order_data:
                                executed_price_cents = int(order_data['fill_price'] * 100) if order_data['fill_price'] < 1 else int(order_data['fill_price'])
                            elif 'average_fill_price' in order_data:
                                executed_price_cents = int(order_data['average_fill_price'] * 100) if order_data['average_fill_price'] < 1 else int(order_data['average_fill_price'])
                            
                            # Try to calculate from total cost
                            if executed_price_cents is None:
                                # Kalshi returns cost in multiple fields:
                                # - taker_fill_cost (in cents) - cost for taking liquidity
                                # - maker_fill_cost (in cents) - cost for providing liquidity
                                # - taker_fill_cost_dollars (string) - cost in dollars
                                # - maker_fill_cost_dollars (string) - cost in dollars
                                # Total cost = taker_fill_cost + maker_fill_cost
                                
                                taker_cost_cents = order_data.get('taker_fill_cost', 0) or 0
                                maker_cost_cents = order_data.get('maker_fill_cost', 0) or 0
                                
                                # If not in cents, try dollars (as string)
                                if taker_cost_cents == 0:
                                    taker_cost_dollars_str = order_data.get('taker_fill_cost_dollars', '') or ''
                                    if taker_cost_dollars_str:
                                        try:
                                            taker_cost_cents = int(float(taker_cost_dollars_str) * 100)
                                        except (ValueError, TypeError):
                                            pass
                                
                                if maker_cost_cents == 0:
                                    maker_cost_dollars_str = order_data.get('maker_fill_cost_dollars', '') or ''
                                    if maker_cost_dollars_str:
                                        try:
                                            maker_cost_cents = int(float(maker_cost_dollars_str) * 100)
                                        except (ValueError, TypeError):
                                            pass
                                
                                # Total cost is sum of taker and maker costs
                                total_cost_cents = taker_cost_cents + maker_cost_cents
                                
                                # Fallback to old fields if new ones not available
                                if total_cost_cents == 0:
                                    total_cost_cents = order_data.get('total_cost', 0) or order_data.get('cost', 0)
                                    if total_cost_cents and total_cost_cents < 1000:  # Likely in dollars, convert
                                        total_cost_cents = int(total_cost_cents * 100)
                                    elif total_cost_cents:
                                        total_cost_cents = int(total_cost_cents)
                                
                                # Calculate average price from cost
                                if total_cost_cents and fill_count > 0:
                                    executed_price_cents = int(total_cost_cents / fill_count)
                                    print(f"[ORDER] Calculated executed price from cost: {total_cost_cents}¢ / {fill_count} contracts = {executed_price_cents}¢")
                            
                            # Use fills array if we calculated from it above (most accurate)
                            if fills_array and actual_fill_count > 0:
                                executed_price_cents = int(actual_fill_cost_cents / actual_fill_count)
                                fill_cost_cents = actual_fill_cost_cents
                                print(f"[ORDER] Executed price from fills array: {actual_fill_cost_cents}¢ / {actual_fill_count} contracts = {executed_price_cents}¢")
                            # Fallback: calculate from fills array if available (old method)
                            elif executed_price_cents is None and 'fills' in order_data and order_data['fills']:
                                fills = order_data['fills']
                                total_fill_cost = 0
                                total_fill_count = 0
                                for fill in fills:
                                    fill_price = fill.get('price', 0) or fill.get('fill_price', 0)
                                    fill_count_fill = fill.get('count', 0) or fill.get('fill_count', 0)
                                    if fill_price < 1:  # Likely in decimal (0.48), convert to cents
                                        fill_price_cents = int(fill_price * 100)
                                    else:
                                        fill_price_cents = int(fill_price)
                                    total_fill_cost += fill_price_cents * fill_count_fill
                                    total_fill_count += fill_count_fill
                                
                                if total_fill_count > 0:
                                    executed_price_cents = int(total_fill_cost / total_fill_count)
                                    fill_cost_cents = total_fill_cost
                            
                            # Final fallback: use limit price (but this is not the actual executed price)
                            if executed_price_cents is None:
                                executed_price_cents = price_cents
                                print(f"[ORDER] WARNING: Could not extract executed price, using limit price {price_cents}¢")
                            
                            # CRITICAL: Calculate total cost correctly
                            # Kalshi returns:
                            # - taker_fill_cost: cost of contracts (in cents)
                            # - maker_fill_cost: cost of contracts if maker (in cents)
                            # - taker_fees: fees for taking liquidity (in cents)
                            # - maker_fees: fees for providing liquidity (in cents)
                            # Total cost = fill_cost + fees
                            
                            taker_fill_cost_cents = order_data.get('taker_fill_cost', 0) or 0
                            maker_fill_cost_cents = order_data.get('maker_fill_cost', 0) or 0
                            taker_fees_cents = order_data.get('taker_fees', 0) or 0
                            maker_fees_cents = order_data.get('maker_fees', 0) or 0
                            
                            # Total fill cost (contracts only, no fees)
                            # Use actual_fill_cost_cents if we calculated from fills array, otherwise use taker/maker costs
                            if 'fill_cost_cents' not in locals() or fill_cost_cents == 0:
                                fill_cost_cents = taker_fill_cost_cents + maker_fill_cost_cents
                            
                            # Total fees
                            total_fees_cents = taker_fees_cents + maker_fees_cents
                            
                            # Total cost = fill cost + fees
                            if fill_cost_cents > 0:
                                total_cost_cents = fill_cost_cents + total_fees_cents
                                # Only recalculate executed price if we haven't already from fills array
                                if executed_price_cents is None or (fills_array and actual_fill_count > 0):
                                    if fill_count > 0:
                                        executed_price_cents = int(fill_cost_cents / fill_count)
                                        print(f"[ORDER] Executed price (from fill cost): {fill_cost_cents}¢ / {fill_count} contracts = {executed_price_cents}¢")
                            elif total_cost_cents is None:
                                # Fallback: calculate from executed price and add fees
                                total_cost_cents = (executed_price_cents * fill_count) + total_fees_cents
                            
                            # Determine fee type: maker if taker_fees == 0, otherwise taker
                            fee_type = 'maker' if taker_fees_cents == 0 else 'taker'
                            
                            if total_fees_cents > 0:
                                print(f"[ORDER] Fees: ${total_fees_cents/100:.2f} (taker: ${taker_fees_cents/100:.2f}, maker: ${maker_fees_cents/100:.2f})")
                            else:
                                print(f"[ORDER] ✅ Fee-free maker fill! (taker_fees=0, maker_fees={maker_fees_cents/100:.2f})")
                            print(f"[ORDER] Fill cost: ${fill_cost_cents/100:.2f}, Total cost (with fees): ${total_cost_cents/100:.2f}")
                            
                            # CRITICAL: Verify actual cost doesn't exceed max bet amount (if specified)
                            if max_liquidity_dollars:
                                max_cost_cents = int(max_liquidity_dollars * 100)
                                if total_cost_cents > max_cost_cents:
                                    overage_cents = total_cost_cents - max_cost_cents
                                    overage_pct = (overage_cents / max_cost_cents * 100) if max_cost_cents > 0 else 0
                                    print(f"[ORDER] ⚠️  WARNING: Actual cost ${total_cost_cents/100:.2f} exceeds max bet ${max_liquidity_dollars:.2f} by ${overage_cents/100:.2f} ({overage_pct:.2f}%)")
                                    print(f"[ORDER]   This can happen due to slippage - limit order was placed at {price_cents}¢ but executed at {executed_price_cents}¢")
                                else:
                                    print(f"[ORDER] ✓ Cost within limit: ${total_cost_cents/100:.2f} <= ${max_liquidity_dollars:.2f}")
                            
                            print(f"[ORDER] SUCCESS! Order placed (HTTP {resp.status}):")
                            print(f"[ORDER]   Order ID: {order_id}")
                            print(f"[ORDER]   Status: {status}")
                            print(f"[ORDER]   Filled: {fill_count}/{initial_count} contracts")
                            print(f"[ORDER]   Limit price: {price_cents}¢ (requested)")
                            print(f"[ORDER]   Executed price: {executed_price_cents}¢ (actual)")
                            print(f"[ORDER]   Total cost: ${total_cost_cents/100:.2f} ({total_cost_cents}¢)")
                            if executed_price_cents != price_cents:
                                slippage_cents = executed_price_cents - price_cents
                                slippage_pct = (slippage_cents / price_cents * 100) if price_cents > 0 else 0
                                print(f"[ORDER]   Slippage: {slippage_cents:+.1f}¢ ({slippage_pct:+.2f}%)")
                            if fill_count < initial_count:
                                print(f"[ORDER]   Remaining: {initial_count - fill_count} contracts")
                            
                            total_time = (time.time() - order_start_time) * 1000
                            print(f"[ORDER] Total place_order() time: {total_time:.1f}ms")
                            
                            # Only track recent_bets for auto-bettor (skip_duplicate_check=False). Manual bets skip so user can click multiple times.
                            if not skip_duplicate_check:
                                async with self.bet_lock:
                                    self.recent_bets.add(bet_key)
                                    print(f"[ORDER] Marked {ticker} {side} as bet to prevent auto-bettor duplicates (successful order)")
                                    async def cleanup_bet():
                                        await asyncio.sleep(self.bet_cooldown_seconds)
                                        async with self.bet_lock:
                                            self.recent_bets.discard(bet_key)
                                            print(f"[ORDER] Removed {ticker} {side} from recent bets after cooldown")
                                    asyncio.create_task(cleanup_bet())
                            
                            return {
                                "success": True,
                                "order_id": order_id,
                                "fill_count": fill_count,
                                "initial_count": initial_count,
                                "status": status,
                                "order": order_data,
                                "ticker": ticker,
                                "side": side,
                                "count": final_count,
                                "requested_count": count,
                                "price_cents": price_cents,  # Limit price (requested)
                                "executed_price_cents": executed_price_cents,  # Actual executed price
                                "total_cost_cents": total_cost_cents,  # Actual total cost
                                "fee_type": fee_type,  # 'maker' or 'taker'
                                "taker_fees_cents": taker_fees_cents,
                                "maker_fees_cents": maker_fees_cents,
                                "total_fees_cents": total_fees_cents
                            }
                        except Exception as e:
                            error_text = await resp.text()
                            print(f"[ORDER] Response parse error: {e}")
                            print(f"[ORDER] Response text: {error_text[:500]}")
                            return {"success": False, "error": f"Parse error: {str(e)}"}
                    elif resp.status == 429:
                        error_text = await resp.text()
                        print(f"[ORDER] Rate limited (429): {error_text[:200]}")
                        # Rate limited - retry with faster exponential backoff
                        if attempt < 2:
                            retry_delay = 0.05 * (2 ** attempt)
                            print(f"[ORDER] Retrying in {retry_delay*1000:.0f}ms...")
                            await asyncio.sleep(retry_delay)
                            continue
                        return {
                            "success": False,
                            "error": f"Rate limited after {attempt + 1} attempts: {error_text[:200]}"
                        }
                    elif resp.status == 409:
                        error_text = await resp.text()
                        print(f"[ORDER] ⚠️  Trading paused (409): {error_text[:200]}")
                        # Check if it's specifically "trading_is_paused"
                        try:
                            import json as json_lib
                            error_json = json_lib.loads(error_text) if error_text else {}
                            error_code = error_json.get('error', {}).get('code', '') if isinstance(error_json, dict) else ''
                            error_msg = error_json.get('error', {}).get('message', '') if isinstance(error_json, dict) else ''
                            
                            if error_code == 'trading_is_paused' or 'trading is paused' in error_msg.lower():
                                print(f"[ORDER] 🛑 KALSHI EXCHANGE PAUSED: Trading is currently paused on this market (or exchange-wide).")
                                print(f"[ORDER]    This is a Kalshi-side issue - trading will resume when they unpause.")
                                print(f"[ORDER]    Common reasons: Pre-game pause, market maintenance, or game-specific pause.")
                                # Don't retry - trading is paused, retrying won't help
                                return {
                                    "success": False,
                                    "error": f"Trading paused (Kalshi-side): {error_msg or 'trading is paused'}"
                                }
                        except Exception as e:
                            print(f"[ORDER] Error parsing 409 response: {e}")
                        
                        # Generic 409 error (conflict, but not trading paused)
                        return {
                            "success": False,
                            "error": f"HTTP 409 Conflict: {error_text[:200]}"
                        }
                    else:
                        error_text = await resp.text()
                        print(f"[ORDER] API ERROR ({resp.status}): {error_text[:200]}")
                        
                        # CRITICAL: If post-only was rejected (400 invalid order), retry as taker
                        if resp.status == 400 and post_only and _retry_count == 0:
                            try:
                                import json as json_lib
                                error_json = json_lib.loads(error_text) if error_text else {}
                                error_code = error_json.get('error', {}).get('code', '') if isinstance(error_json, dict) else ''
                                error_msg = error_json.get('error', {}).get('message', '') if isinstance(error_json, dict) else ''
                                
                                # Check if it's an "invalid order" rejection (post-only would cross)
                                # Also check for common rejection messages
                                is_invalid = ('invalid' in error_msg.lower() or 'invalid' in error_code.lower() or 
                                             'post_only' in error_msg.lower() or 'would cross' in error_msg.lower())
                                
                                if is_invalid:
                                    print(f"[ORDER] Post-only rejected (would cross) - retrying as taker immediately...")
                                    # Recursive retry as taker (max 1 retry)
                                    return await self.place_order(
                                        ticker=ticker,
                                        side=side,
                                        count=count,
                                        price_cents=price_cents,
                                        validate_odds=validate_odds,
                                        expected_price_cents=expected_price_cents,
                                        max_liquidity_dollars=max_liquidity_dollars,
                                        post_only=False,  # Retry as taker
                                        expiration_ts=None,  # No expiration on taker retry
                                        _retry_count=1,  # Prevent infinite recursion
                                        skip_duplicate_check=skip_duplicate_check
                                    )
                            except Exception as e:
                                print(f"[ORDER] Error parsing rejection response: {e}")
                                # If error parsing fails, continue with normal error return
                        
                        return {
                            "success": False,
                            "error": f"HTTP {resp.status}: {error_text[:200]}"
                        }
            
            except asyncio.TimeoutError:
                attempt_time = (time.time() - attempt_start) * 1000
                print(f"[ORDER] TIMEOUT after {attempt_time:.1f}ms on attempt {attempt + 1}")
                if attempt < 2:
                    retry_delay = 0.05 * (2 ** attempt)
                    print(f"[ORDER] Retrying in {retry_delay*1000:.0f}ms...")
                    await asyncio.sleep(retry_delay)
                    continue
                return {
                    "success": False,
                    "error": f"Timeout after {attempt + 1} attempts"
                }
        
        # All retries exhausted
        return {
            "success": False,
            "error": "Failed after 3 attempts"
        }
    
    async def cancel_order(self, order_id):
        """
        Cancel an open order on Kalshi
        
        Args:
            order_id: Order ID to cancel
        
        Returns:
            Success/failure dict
        """
        if not self.session:
            await self.init()
        
        # Try both possible endpoint formats
        # Format 1: /trade-api/v2/portfolio/orders/{order_id}/cancel
        # Format 2: /trade-api/v2/portfolio/orders/{order_id} (DELETE method)
        paths_to_try = [
            (f"/trade-api/v2/portfolio/orders/{order_id}/cancel", "POST"),
            (f"/trade-api/v2/portfolio/orders/{order_id}", "DELETE"),
        ]
        
        for path, method in paths_to_try:
            # Sign the request (POST/DELETE with no body for cancel)
            ts, sig = self.auth.sign(method, path, "")
            
            headers = {
                "KALSHI-ACCESS-KEY": self.auth.kid,
                "KALSHI-ACCESS-TIMESTAMP": str(ts),
                "KALSHI-ACCESS-SIGNATURE": sig,
                "Content-Type": "application/json"
            }
            
            try:
                # Use appropriate HTTP method
                if method == "DELETE":
                    request_method = self.session.delete
                else:
                    request_method = self.session.post
                
                # Use asyncio.wait_for instead of ClientTimeout to avoid context manager issues
                async with request_method(
                    f"{self.base_url}{path}",
                    headers=headers
                ) as resp:
                    if resp.status in [200, 201, 204]:  # 204 No Content is also success for DELETE
                        try:
                            result = await resp.json()
                        except:
                            result = {}  # DELETE might return empty body
                        print(f"[ORDER] ✅ Order {order_id} cancelled successfully using {method} {path}")
                        return {"success": True, "order": result}
                    elif resp.status == 404:
                        # 404 might mean order doesn't exist (already filled/cancelled) or wrong endpoint
                        # Try next endpoint format
                        error_text = await resp.text()
                        print(f"[ORDER] ⚠️  Cancel attempt failed (404) with {method} {path}: {error_text[:200]}")
                        continue  # Try next endpoint format
                    else:
                        error_text = await resp.text()
                        print(f"[ORDER] ❌ Failed to cancel order {order_id} with {method} {path}: HTTP {resp.status} - {error_text[:200]}")
                        return {"success": False, "error": f"HTTP {resp.status}: {error_text[:200]}"}
            except Exception as e:
                print(f"[ORDER] ❌ Exception cancelling order {order_id} with {method} {path}: {e}")
                continue  # Try next endpoint format
        
        # All attempts failed
        print(f"[ORDER] ❌ All cancel attempts failed for order {order_id}")
        return {"success": False, "error": "All endpoint formats failed (404 - order may already be filled/cancelled)"}
    
    async def get_user_id(self):
        """Get user ID from exchange member endpoint"""
        if not self.session:
            await self.init()
        
        try:
            # Try to get from exchange member endpoint
            # For v2 API, signature path must include /trade-api/v2 prefix
            path = "/trade-api/v2/exchange/member"
            ts, sig = self.auth.sign("GET", path)
            headers = {
                "KALSHI-ACCESS-KEY": self.auth.kid,
                "KALSHI-ACCESS-SIGNATURE": sig,
                "KALSHI-ACCESS-TIMESTAMP": ts
            }
            
            # Use asyncio.wait_for to wrap the entire request to avoid issues with run_coroutine_threadsafe
            async def _request():
                async with self.session.get(f"{self.base_url}{path}", headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # User ID might be exchange_member_id or member_id
                        user_id = data.get('exchange_member_id') or data.get('member_id') or data.get('user_id')
                        if user_id:
                            return user_id
                    return None
            
            try:
                return await asyncio.wait_for(_request(), timeout=5.0)
            except asyncio.TimeoutError:
                print(f"Timeout getting user ID from {path}")
                return None
        except Exception as e:
            print(f"Warning: Could not get user ID: {e}")
        
        return None
    
    async def get_portfolio(self):
        """Get current portfolio value (cash + positions)
        Tries current_value endpoint first, then falls back to balance + positions"""
        if not self.session:
            await self.init()
        
        # First try the current_value endpoint if we can get user ID
        try:
            user_id = await self.get_user_id()
            if user_id:
                # Try v1 API endpoint
                v1_base = KALSHI_V1_BASE if not self.demo_mode else "https://demo-api.kalshi.com/v1"
                # For v1 API, the signature path must include /v1 prefix
                path = f"/v1/users/{user_id}/portfolio/current_value"
                
                ts, sig = self.auth.sign("GET", path)
                headers = {
                    "KALSHI-ACCESS-KEY": self.auth.kid,
                    "KALSHI-ACCESS-SIGNATURE": sig,
                    "KALSHI-ACCESS-TIMESTAMP": ts
                }
                
                # Use asyncio.wait_for to wrap the entire request to avoid issues with run_coroutine_threadsafe
                async def _request():
                    async with self.session.get(f"{v1_base}{path}", headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            print(f"Portfolio current_value response: {data}")
                            return data
                        else:
                            error_text = await resp.text()
                            print(f"Current Value API error {resp.status}: {error_text}")
                            return None
                
                try:
                    return await asyncio.wait_for(_request(), timeout=5.0)
                except asyncio.TimeoutError:
                    print(f"Timeout getting current_value from {path}")
                    return None
        except Exception as e:
            print(f"Warning: current_value endpoint failed: {e}")
        
        # Fallback: Get balance and positions separately, then combine
        return await self.get_portfolio_combined()
    
    async def get_portfolio_combined(self):
        """Get portfolio by combining balance and positions endpoints"""
        try:
            # Get balance
            balance_data = await self.get_portfolio_balance_fallback()
            
            # Get positions
            positions_data = await self.get_positions()
            
            # Calculate total position value
            positions_value_cents = 0
            if positions_data:
                for pos in positions_data:
                    # Position value = count * current_price (or average_price if current not available)
                    count = pos.get('count', 0)
                    avg_price_cents = pos.get('average_price_cents', 0) or (pos.get('average_price', 0) * 100)
                    positions_value_cents += count * avg_price_cents
            
            # Get cash and positions value from balance
            cash_cents = 0
            positions_value_cents = 0
            if balance_data:
                cash_cents = balance_data.get('balance', 0) or balance_data.get('balance_cents', 0)
                if isinstance(balance_data, (int, float)):
                    cash_cents = balance_data
                # The 'portfolio_value' field in balance response is actually the positions value
                positions_value_cents = balance_data.get('portfolio_value', 0)
            
            # Return in current_value format
            # Note: positions_value_cents comes from balance_data['portfolio_value']
            return {
                'value': {
                    'a': cash_cents,  # Available cash
                    'v': positions_value_cents,  # Positions value (from portfolio_value in balance response)
                    'cumulative_deposits': 0  # Not available from these endpoints
                }
            }
        except Exception as e:
            print(f"Error getting combined portfolio: {e}")
            return None
    
    async def get_portfolio_balance_fallback(self):
        """Fallback to balance endpoint if current_value fails"""
        if not self.session:
            await self.init()
        
        try:
            # For v2 API, path must include /trade-api/v2 prefix
            path = "/trade-api/v2/portfolio/balance"
            
            # Check if auth is properly set up
            if not self.auth.priv:
                print("Error: Kalshi private key not loaded. Check kalshi.key file and KALSHI_KEY_ID.")
                return None
            if not self.auth.kid:
                print("Error: KALSHI_KEY_ID not set in environment.")
                return None
            
            ts, sig = self.auth.sign("GET", path)
            headers = {
                "KALSHI-ACCESS-KEY": self.auth.kid,
                "KALSHI-ACCESS-SIGNATURE": sig,
                "KALSHI-ACCESS-TIMESTAMP": ts
            }
            
            # Use asyncio.wait_for to wrap the entire request
            async def _request():
                async with self.session.get(f"{self.base_url}{path}", headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        print(f"Portfolio balance response: {data}")
                        return data
                    else:
                        error_text = await resp.text()
                        print(f"Portfolio balance API error {resp.status}: {error_text}")
                        return None
            
            try:
                return await asyncio.wait_for(_request(), timeout=5.0)
            except asyncio.TimeoutError:
                print(f"Timeout getting portfolio balance")
                return None
        except Exception as e:
            print(f"Error fetching portfolio balance: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    async def get_positions(self):
        """Get current positions (open contracts)"""
        if not self.session:
            await self.init()
        
        try:
            # For v2 API, path must include /trade-api/v2 prefix
            path = "/trade-api/v2/portfolio/positions"
            print("[KALSHI] get_positions: signing request...")
            ts, sig = self.auth.sign("GET", path)
            print("[KALSHI] get_positions: sending HTTP request (10s timeout)...")
            headers = {
                "KALSHI-ACCESS-KEY": self.auth.kid,
                "KALSHI-ACCESS-SIGNATURE": sig,
                "KALSHI-ACCESS-TIMESTAMP": ts
            }
            
            # Use HTTP timeout so request cannot hang (e.g. when API is slow or 0 positions)
            async def _make_request():
                async with self.session.get(f"{self.base_url}{path}", headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # Positions are in 'market_positions' array, not 'positions'
                        market_positions = data.get('market_positions', [])
                        print(f"[KALSHI] get_positions: got response, {len(market_positions)} market_positions")
                        
                        # Calculate position values
                        formatted_positions = []
                        total_value_cents = 0
                        for pos in market_positions:
                            ticker = pos.get('ticker', '')
                            position = pos.get('position', 0)  # Number of contracts
                            market_exposure_cents = pos.get('market_exposure', 0)  # Current value in cents
                            market_exposure_dollars = float(pos.get('market_exposure_dollars', '0') or 0)
                            total_traded_cents = pos.get('total_traded', 0)
                            fees_paid_cents = pos.get('fees_paid', 0)
                            realized_pnl_cents = pos.get('realized_pnl', 0)
                            
                            # Use market_exposure as the current value
                            value_cents = market_exposure_cents
                            value_dollars = market_exposure_dollars if market_exposure_dollars > 0 else (value_cents / 100.0)
                            total_value_cents += value_cents
                            
                            # Extract average price from API (matches mobile app calculation)
                            # Kalshi's API may return average_price_cents, entry_price_cents, or average_price
                            average_price_cents = (
                                pos.get('average_price_cents', 0) or 
                                pos.get('entry_price_cents', 0) or
                                (int(pos.get('average_price', 0) * 100) if pos.get('average_price') else 0)
                            )
                            
                            formatted_positions.append({
                                'ticker': ticker,
                                'position': position,  # Number of contracts
                                'value': value_dollars,
                                'value_cents': value_cents,
                                'market_exposure': market_exposure_dollars,
                                'total_traded': total_traded_cents / 100.0,
                                'fees_paid': fees_paid_cents / 100.0,
                                'realized_pnl': realized_pnl_cents / 100.0,
                                'average_price_cents': average_price_cents,  # Use Kalshi's official average price
                                'last_updated': pos.get('last_updated_ts', '')
                            })
                        
                        # Positions are tracked silently (no log spam) - still used for reverse middle detection
                        return formatted_positions
                    else:
                        error_text = await resp.text()
                        print(f"Positions API error {resp.status}: {error_text}")
                        return []
            
            return await asyncio.wait_for(_make_request(), timeout=5.0)
        except RuntimeError as e:
            # Suppress "Task got Future attached to a different loop" errors
            # These are non-critical event loop issues when called from Flask routes
            # The error is caught and handled gracefully - requests still work
            error_msg = str(e)
            if "attached to a different loop" in error_msg:
                # Silently return empty - this is expected when Flask routes call async functions
                return []
            else:
                print(f"Error in get_positions: {e}")
                import traceback
                traceback.print_exc()
                return []
        except Exception as e:
                # Only log non-event-loop errors
                error_msg = str(e)
                if "attached to a different loop" not in error_msg:
                    print(f"Error in get_positions: {e}")
                return []
    
    async def get_settlements(self, limit: int = 100, cursor: str = None):
        """Fetch historical settlements for closed positions"""
        if not self.session:
            await self.init()
        
        try:
            path = "/trade-api/v2/portfolio/settlements"
            params = f"?limit={limit}"
            if cursor:
                params += f"&cursor={cursor}"
            path += params
            
            ts, sig = self.auth.sign("GET", path)
            headers = {
                "KALSHI-ACCESS-KEY": self.auth.kid,
                "KALSHI-ACCESS-SIGNATURE": sig,
                "KALSHI-ACCESS-TIMESTAMP": ts
            }
            
            async def _make_request():
                async with self.session.get(f"{self.base_url}{path}", headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        settlements = data.get('settlements', [])
                        cursor = data.get('cursor')
                        return settlements, cursor
                    else:
                        error_text = await resp.text()
                        print(f"[SETTLEMENTS] API ERROR ({resp.status}): {error_text}")
                        return [], None
            
            # Use asyncio.wait_for to handle timeout when called from different threads
            settlements, next_cursor = await asyncio.wait_for(_make_request(), timeout=10.0)
            return settlements, next_cursor
            
        except asyncio.TimeoutError:
            print(f"[SETTLEMENTS] Timeout fetching settlements")
            return [], None
        except Exception as e:
            print(f"Error in get_settlements: {e}")
            import traceback
            traceback.print_exc()
            return [], None
    
    def _sign_pss_text(self, msg):
        """Helper for signing WebSocket messages"""
        if not self.auth.priv:
            return None
        return base64.b64encode(
            self.auth.priv.sign(
                msg.encode(),
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
                hashes.SHA256()
            )
        ).decode()
    
    async def connect_ws(self):
        """Establish authenticated WebSocket connection"""
        if self.ws and not self.ws.closed:
            return True
        
        ts = str(int(time.time() * 1000))
        msg = f"{ts}GET/trade-api/ws/v2"
        sig = self._sign_pss_text(msg)
        
        if not sig:
            print("Cannot connect WS: Missing private key")
            return False
        
        headers = {
            "KALSHI-ACCESS-KEY": self.auth.kid,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts
        }
        
        try:
            # websockets library uses additional_headers, not extra_headers
            self.ws = await websockets.connect(
                self.wss_url,
                additional_headers=headers
            )
            self.ws_connected = True
            print("WebSocket connected")
            asyncio.create_task(self._ws_listener())
            asyncio.create_task(self._ws_keepalive())
            
            # Subscribe to positions automatically if callback is set
            if self.ws_positions_callback:
                print(f"[WS] ✅ Position callback is set - will auto-subscribe to market_positions")
                # Wait a moment for connection to stabilize, then subscribe
                async def auto_subscribe_positions():
                    await asyncio.sleep(1)  # Brief delay for connection to stabilize
                    success = await self.subscribe_positions()
                    if success:
                        print(f"[WS] ✅ Successfully subscribed to market_positions - ready for real-time updates")
                    else:
                        print(f"[WS] ⚠️ Failed to subscribe to market_positions - will retry on next connection")
                asyncio.create_task(auto_subscribe_positions())
            else:
                print(f"[WS] ⚠️ Warning: ws_positions_callback is NOT set - position updates will not be processed")
            
            # Also subscribe if explicit task is set (for backward compatibility)
            if hasattr(self, '_subscribe_positions_task'):
                asyncio.create_task(self._subscribe_positions_task())
            
            return True
        except Exception as e:
            print(f"WS connection error: {e}")
            print("   WebSocket features disabled, but system will still work")
            self.ws_connected = False
            return False
    
    async def _ws_keepalive(self):
        """Send ping every 30s to keep connection alive"""
        while self.ws_connected and self.ws:
            try:
                await asyncio.sleep(30)
                if self.ws:
                    # Check if connection is still open by trying to ping
                    try:
                        await self.ws.ping()
                    except (websockets.exceptions.ConnectionClosed, AttributeError):
                        # Connection closed or invalid
                        self.ws_connected = False
                        break
            except Exception as e:
                print(f"Warning: WS keepalive error: {e}")
                self.ws_connected = False
                break
    
    async def _ws_listener(self):
        """Listen for WebSocket messages and update cache"""
        try:
            async for message in self.ws:
                try:
                    data = json.loads(message)
                    msg_type = data.get("type")
                    
                    # Debug: Track message types (first few only)
                    if not hasattr(self, '_ws_msg_count'):
                        self._ws_msg_count = 0
                        self._ws_msg_types_seen = set()
                    self._ws_msg_count += 1
                    
                    if msg_type and msg_type not in self._ws_msg_types_seen:
                        self._ws_msg_types_seen.add(msg_type)
                        if msg_type not in ["orderbook_snapshot", "orderbook_delta", "subscribed", "pong", "market_positions", "positions_update"]:
                            print(f"[WS DEBUG] New message type detected: {msg_type} (message #{self._ws_msg_count})")
                            if isinstance(data, dict):
                                print(f"[WS DEBUG] Keys: {list(data.keys())[:10]}")
                    
                    # Debug: Log all message types to help diagnose routing issues
                    if msg_type not in ["orderbook_snapshot", "orderbook_delta", "subscribed", "pong"]:
                        # Only log non-standard messages to avoid spam
                        if msg_type not in ["market_positions", "positions_update"]:
                            print(f"[WS DEBUG] Received message type: {msg_type}")
                    
                    if msg_type in ["orderbook_snapshot", "orderbook_delta"]:
                        ticker = data.get("data", {}).get("market_ticker", "").upper()
                        if not ticker:
                            continue
                        
                        # Update orderbook cache
                        updated = self._apply_orderbook_update(data)
                        if updated:
                            updated['fetched_at'] = time.time()
                            updated['timestamp'] = datetime.now().isoformat()
                            self.orderbooks[ticker] = updated
                            
                            # Call callback if set
                            if self.ws_callback:
                                try:
                                    if asyncio.iscoroutinefunction(self.ws_callback):
                                        await self.ws_callback(ticker, updated)
                                    else:
                                        self.ws_callback(ticker, updated)
                                except Exception as e:
                                    print(f"Warning: WS callback error: {e}")
                    
                    elif msg_type == "market_positions" or msg_type == "positions_update":
                        # Real-time position updates - YOUR ACTUAL POSITIONS
                        # Kalshi sends your personal positions, not all market positions
                        print(f"[WS] 🔔 Position update message received (type: {msg_type})")
                        
                        positions_data = data.get("data", {})
                        # Also check if positions are at root level (different message formats)
                        if not positions_data and "market_positions" in data:
                            positions_data = data
                        # If still empty, check if data itself is the positions array
                        if not positions_data and isinstance(data.get("data"), list):
                            positions_data = {"market_positions": data.get("data")}
                        
                        # If still empty, try checking if data itself contains positions
                        if not positions_data:
                            # Try alternative message formats
                            if "market_positions" in data:
                                positions_data = data
                            elif isinstance(data, list):
                                positions_data = {"market_positions": data}
                            else:
                                print(f"[WS] ⚠️ Warning: Could not parse position data from message: {data.keys() if isinstance(data, dict) else type(data)}")
                        
                        if self.ws_positions_callback:
                            try:
                                # Count actual positions (non-zero) for logging
                                all_positions = positions_data.get('market_positions', []) if isinstance(positions_data, dict) else []
                                if isinstance(positions_data, list):
                                    all_positions = positions_data
                                
                                actual_count = len([p for p in all_positions if p.get('position', 0) != 0])
                                
                                if actual_count > 0:
                                    print(f"[WS] 📨 Received YOUR position update: {actual_count} actual position(s) changed")
                                elif len(all_positions) > 0:
                                    print(f"[WS] 📨 Received position update: {len(all_positions)} total position(s) (including zeros)")
                                
                                if asyncio.iscoroutinefunction(self.ws_positions_callback):
                                    await self.ws_positions_callback(positions_data)
                                else:
                                    self.ws_positions_callback(positions_data)
                            except Exception as e:
                                print(f"[WS] ⚠️ Warning: Positions callback error: {e}")
                                import traceback
                                traceback.print_exc()
                        else:
                            print(f"[WS] ⚠️ Warning: Position update received but ws_positions_callback is not set!")
                    
                    # Handle any other message types that might contain position data
                    elif "market_positions" in data or "positions" in data:
                        # Fallback: Check if message contains position data even if type doesn't match
                        print(f"[WS] 🔍 Detected position data in message type '{msg_type}', attempting to process...")
                        positions_data = data.get("data", {})
                        if not positions_data and "market_positions" in data:
                            positions_data = data
                        if not positions_data and isinstance(data.get("data"), list):
                            positions_data = {"market_positions": data.get("data")}
                        
                        if self.ws_positions_callback and positions_data:
                            try:
                                all_positions = positions_data.get('market_positions', []) if isinstance(positions_data, dict) else []
                                actual_count = len([p for p in all_positions if p.get('position', 0) != 0])
                                if actual_count > 0:
                                    print(f"[WS] 📨 Processed position data from unexpected message type: {actual_count} actual position(s)")
                                    if asyncio.iscoroutinefunction(self.ws_positions_callback):
                                        await self.ws_positions_callback(positions_data)
                                    else:
                                        self.ws_positions_callback(positions_data)
                            except Exception as e:
                                print(f"[WS] ⚠️ Error processing fallback position data: {e}")
                    
                    elif msg_type == "subscribed":
                        # Check if this is a position subscription confirmation
                        channels = data.get("channels", [])
                        if "market_positions" in channels or any("position" in str(ch).lower() for ch in channels):
                            print(f"[WS] ✅ Position subscription confirmed: {channels}")
                            self.positions_subscribed = True
                            print(f"[WS] ✅ Ready to receive real-time position updates!")
                        else:
                            print(f"[WS] Subscribed to: {channels}")
                    
                    # Log ALL non-standard messages for debugging
                    elif msg_type not in ["orderbook_snapshot", "orderbook_delta", "pong", "subscribed"]:
                        # Only log if it's not a position message (we already log those)
                        if msg_type not in ["market_positions", "positions_update"]:
                            print(f"[WS DEBUG] Unhandled message type: {msg_type}")
                            if isinstance(data, dict):
                                print(f"[WS DEBUG] Data keys: {list(data.keys())}")
                                # Log first 200 chars of data for debugging
                                data_str = str(data)
                                if len(data_str) > 200:
                                    print(f"[WS DEBUG] Data preview: {data_str[:200]}...")
                                else:
                                    print(f"[WS DEBUG] Data: {data_str}")
                    
                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    print(f"Warning: WS message processing error: {e}")
        
        except websockets.exceptions.ConnectionClosed:
            print("Warning: WebSocket connection closed")
            self.ws_connected = False
        except Exception as e:
            print(f"WS listener error: {e}")
            self.ws_connected = False
        finally:
            await self.close_ws()
    
    def _apply_orderbook_update(self, data):
        """Parse and apply snapshot/delta to orderbook"""
        msg_type = data.get("type")
        ticker = data.get("data", {}).get("market_ticker", "").upper()
        
        if msg_type == "orderbook_snapshot":
            # Full replace with orderbook data
            orderbook_data = data.get("data", {}).get("orderbook", {})
            if not orderbook_data:
                return None
            
            # Parse into our format
            yes_bids_raw = orderbook_data.get("yes", []) or []
            no_bids_raw = orderbook_data.get("no", []) or []
            
            # Parse YES side
            yes_bids = []
            yes_total_liq = 0
            for bid in yes_bids_raw:
                if isinstance(bid, list) and len(bid) >= 2:
                    price_cents = bid[0]
                    quantity = bid[1]
                    price = price_cents / 100.0
                    yes_bids.append({'price': price, 'quantity': quantity})
                    yes_total_liq += quantity
            
            # Parse NO side
            no_bids = []
            no_total_liq = 0
            for bid in no_bids_raw:
                if isinstance(bid, list) and len(bid) >= 2:
                    price_cents = bid[0]
                    quantity = bid[1]
                    price = price_cents / 100.0
                    no_bids.append({'price': price, 'quantity': quantity})
                    no_total_liq += quantity
            
            # Get best bid/ask
            yes_best_bid = yes_bids[-1]['price'] if yes_bids else None
            yes_best_ask = (1.0 - no_bids[-1]['price']) if no_bids else None
            no_best_bid = no_bids[-1]['price'] if no_bids else None
            no_best_ask = (1.0 - yes_bids[-1]['price']) if yes_bids else None
            
            return {
                'yes': {
                    'best_bid': yes_best_bid,
                    'best_ask': yes_best_ask,
                    'bids': yes_bids,
                    'total_liquidity': yes_total_liq
                },
                'no': {
                    'best_bid': no_best_bid,
                    'best_ask': no_best_ask,
                    'bids': no_bids,
                    'total_liquidity': no_total_liq
                }
            }
        
        elif msg_type == "orderbook_delta":
            if ticker not in self.orderbooks:
                return None  # No snapshot yet, ignore delta
            
            current = self.orderbooks[ticker].copy()
            deltas = data.get("data", {}).get("deltas", [])
            
            # Apply deltas (simplified - Kalshi format may vary)
            for delta in deltas:
                side = delta.get("side", "").lower()  # "yes" or "no"
                action = delta.get("action", "")  # "add", "update", "remove"
                price_cents = delta.get("price")
                size = delta.get("size", 0)
                
                if side not in ["yes", "no"] or not action or price_cents is None:
                    continue
                
                price = price_cents / 100.0
                bids = current[side].get("bids", [])
                
                if action in ["add", "update"]:
                    # Remove existing at this price
                    bids = [b for b in bids if abs(b['price'] - price) > 0.001]
                    if size > 0:
                        bids.append({'price': price, 'quantity': size})
                    # Sort by price (descending for bids)
                    bids.sort(key=lambda x: x['price'], reverse=True)
                    current[side]['bids'] = bids
                    
                    # Recalculate best bid and total liquidity
                    if bids:
                        current[side]['best_bid'] = bids[0]['price']
                        current[side]['total_liquidity'] = sum(b['quantity'] for b in bids)
                    
                    # Recalculate best ask (opposite side)
                    if side == "yes":
                        no_bids = current['no'].get('bids', [])
                        if no_bids:
                            current['yes']['best_ask'] = 1.0 - no_bids[0]['price']
                    else:
                        yes_bids = current['yes'].get('bids', [])
                        if yes_bids:
                            current['no']['best_ask'] = 1.0 - yes_bids[0]['price']
                
                elif action == "remove":
                    bids = [b for b in bids if abs(b['price'] - price) > 0.001]
                    current[side]['bids'] = bids
                    
                    if bids:
                        current[side]['best_bid'] = bids[0]['price']
                        current[side]['total_liquidity'] = sum(b['quantity'] for b in bids)
                    else:
                        current[side]['best_bid'] = None
                        current[side]['total_liquidity'] = 0
            
            return current
        
        return None
    
    async def subscribe_orderbook(self, ticker):
        """Subscribe to orderbook_delta for a ticker"""
        # Check WebSocket connection status (websockets library doesn't have .closed attribute)
        try:
            if not self.ws:
                if not await self.connect_ws():
                    return False
            else:
                # Try to ping to check if connection is alive
                try:
                    await asyncio.wait_for(self.ws.ping(), timeout=0.1)
                except (asyncio.TimeoutError, Exception):
                    # Connection is dead, reconnect
                    if not await self.connect_ws():
                        return False
        except Exception as e:
            print(f"   Warning: WebSocket check error: {e}, reconnecting...")
            if not await self.connect_ws():
                return False
        
        ticker_upper = ticker.upper()
        if ticker_upper in self.ws_subscriptions:
            return True  # Already subscribed
        
        sub_id = int(time.time() * 1000)
        sub_msg = {
            "id": sub_id,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": [ticker_upper]
            }
        }
        
        try:
            await self.ws.send(json.dumps(sub_msg))
            self.ws_subscriptions[ticker_upper] = sub_id
            print(f"Subscribed to orderbook for {ticker_upper}")
            return True
        except Exception as e:
            print(f"Error subscribing to {ticker_upper}: {e}")
            return False
    
    async def subscribe_positions(self):
        """Subscribe to market_positions service for real-time position updates"""
        try:
            if not self.ws:
                if not await self.connect_ws():
                    return False
            else:
                # Try to ping to check if connection is alive
                try:
                    await asyncio.wait_for(self.ws.ping(), timeout=0.1)
                except (asyncio.TimeoutError, Exception):
                    if not await self.connect_ws():
                        return False
        except Exception as e:
            print(f"   Warning: WebSocket check error: {e}, reconnecting...")
            if not await self.connect_ws():
                return False
        
        # Check if already subscribed to positions
        if hasattr(self, 'positions_subscribed') and self.positions_subscribed:
            return True
        
        sub_id = int(time.time() * 1000)
        # Try multiple subscription formats - Kalshi API might use different formats
        sub_msg = {
            "id": sub_id,
            "cmd": "subscribe",
            "params": {
                "channels": ["market_positions"]
            }
        }
        
        try:
            sub_msg_json = json.dumps(sub_msg)
            print(f"[WS] 📤 Sending subscription message: {sub_msg_json}")
            await self.ws.send(sub_msg_json)
            # Set a flag that we've attempted subscription (will be confirmed when we get "subscribed" message)
            print(f"[WS] ⏳ Subscription message sent, waiting for confirmation...")
            print(f"[WS] 💡 Note: Kalshi may not send real-time position updates via WebSocket")
            print(f"[WS] 💡 If updates don't appear, we'll rely on the 60-second position check loop")
            # Set to True optimistically - will be confirmed by "subscribed" message
            self.positions_subscribed = True
            return True
        except Exception as e:
            print(f"[WS] ❌ Error subscribing to positions: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    async def close_ws(self):
        """Close WebSocket connection"""
        self.ws_connected = False
        if self.ws and not self.ws.closed:
            try:
                await self.ws.close()
                print("WebSocket closed")
            except:
                pass
        self.ws_subscriptions = {}

    async def refresh_warm_cache(self):
        """
        Finds ALL live sports events on Kalshi and pre-subscribes to their orderbooks.
        This ensures zero-latency when an alert eventually pings.
        """
        if not self.ws_connected:
            await self.connect_ws()

        print("[WARM CACHE] Refreshing live sports tickers...")
        
        # Define sports categories to monitor based on your CLAUDE.md
        sports_categories = ['NBA', 'NFL', 'NHL', 'MLB', 'NCAAB', 'NCAAF']
        new_tickers_count = 0

        try:
            # Fetch all open events from Kalshi trade-api v2
            path = "/trade-api/v2/events"
            params = {"status": "open", "limit": 200}
            
            ts, sig = self.auth.sign("GET", path)
            headers = {
                "KALSHI-ACCESS-KEY": self.auth.kid,
                "KALSHI-ACCESS-SIGNATURE": sig,
                "KALSHI-ACCESS-TIMESTAMP": ts
            }

            async with self.session.get(f"{self.base_url}{path}", headers=headers, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    events = data.get('events', [])
                    
                    for event in events:
                        ticker = event.get('event_ticker', '').upper()
                        # Only warm cache for relevant sports categories
                        if any(sport in ticker for sport in sports_categories):
                            # Fetch submarkets (Spread, Total, ML) for this event
                            # Fetch event to get markets for orderbook subscription
                            event_details = await self.get_event_by_ticker(ticker)
                            if event_details:
                                for market in event_details.get('markets', []):
                                    m_ticker = market.get('ticker', '').upper()
                                    # Subscribe if not already in our 'Warm' set
                                    if m_ticker not in self.warm_cache_tickers:
                                        success = await self.subscribe_orderbook(m_ticker)
                                        if success:
                                            self.warm_cache_tickers.add(m_ticker)
                                            new_tickers_count += 1

            self.warm_cache_last_refresh = time.time()
            print(f"[WARM CACHE] Success. Subscribed to {new_tickers_count} new tickers. Total warm: {len(self.warm_cache_tickers)}")
        
        except Exception as e:
            print(f"[WARM CACHE] Error during refresh: {e}")

    async def warm_cache_loop(self):
        """Background task that keeps the cache fresh every 5 minutes
        NOTE: This is now DISABLED by default - warm cache only happens when alerts arrive.
        This prevents warming random/old tickers that aren't relevant to current alerts.
        """
        self.warm_cache_enabled = True
        print("[WARM CACHE] ⚠️  Proactive warm cache loop is DISABLED - only warming tickers when alerts arrive")
        # Don't run the loop - just mark as enabled but don't actually refresh
        # Warm cache now happens on-demand in handle_new_alert()
        while self.warm_cache_enabled:
            # Sleep indefinitely - loop is disabled
            await asyncio.sleep(3600)  # Sleep 1 hour (effectively disabled)

