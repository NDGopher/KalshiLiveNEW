"""
Market Matcher for Polymarket
Matches BookieBeats alerts to Polymarket markets
"""
import re
from typing import Optional, Dict, List
from bookiebeats_monitor import BookieBeatsAlert
from Polymarket.polymarket_client import PolymarketClient


class MarketMatcherPolymarket:
    """Matches BookieBeats alerts to Polymarket markets"""
    
    def __init__(self, polymarket_client: PolymarketClient):
        self.client = polymarket_client
        self.market_cache = {}  # Cache of Polymarket markets
    
    def normalize_team_name(self, name: str) -> str:
        """Normalize team name for matching"""
        if not name:
            return ""
        
        # Remove common suffixes
        name = name.strip()
        name = re.sub(r'\s+(St|State|University|Univ|U)$', '', name, flags=re.IGNORECASE)
        name = re.sub(r'\s+(St\.|State\.)$', '', name, flags=re.IGNORECASE)
        
        # Remove special characters
        name = re.sub(r'[^\w\s]', '', name)
        
        return name.upper().strip()
    
    def extract_teams_from_string(self, teams_str: str) -> tuple:
        """Extract team names from string like 'Team A @ Team B'"""
        if not teams_str:
            return None, None
        
        # Split by @ or vs
        parts = re.split(r'\s*[@|vs|VS]\s*', teams_str, maxsplit=1)
        if len(parts) == 2:
            team1 = self.normalize_team_name(parts[0])
            team2 = self.normalize_team_name(parts[1])
            return team1, team2
        
        return None, None
    
    def parse_qualifier_to_line(self, qualifier: str, market_type: str) -> Optional[float]:
        """Parse qualifier (e.g., '+17.5', 'Over') to numeric line"""
        if not qualifier:
            return None
        
        try:
            # Remove + sign and convert to float
            line_str = qualifier.replace('+', '').replace('*', '').strip()
            return float(line_str)
        except:
            return None
    
    async def find_market_by_id(self, market_id: str) -> Optional[Dict]:
        """Find market by Polymarket market ID"""
        if not market_id:
            return None
        
        # Check cache first
        if market_id in self.market_cache:
            return self.market_cache[market_id]
        
        # TODO: Implement market fetching from Polymarket API
        # For now, return None - will be implemented when API structure is known
        return None
    
    async def find_submarket(self, event_id: str, market_type: str, line: float, selection: str) -> Optional[Dict]:
        """Find matching submarket for a BookieBeats alert"""
        # TODO: Implement market matching logic for Polymarket
        # This will need to be adapted based on Polymarket's market structure
        
        markets = await self.client.get_markets(event_id)
        
        if not markets:
            return None
        
        # Placeholder matching logic - will be expanded
        for market in markets:
            # Match logic here based on Polymarket market structure
            # Need to understand Polymarket's market format first
            pass
        
        return None
    
    async def match_alert_to_polymarket(self, alert: BookieBeatsAlert) -> Optional[Dict]:
        """
        Match a BookieBeats alert to a Polymarket market
        Returns matched market data with additional info
        """
        # Method 1: Extract market ID from BookieBeats link
        market_id = alert.ticker or self.extract_market_id_from_url(alert.market_url)
        if market_id:
            market = await self.find_market_by_id(market_id)
            if market:
                return {
                    'market': market,
                    'market_id': market_id,
                    'match_method': 'exact_id',
                    'confidence': 1.0
                }
        
        # Method 2: Search by event and market details
        # Extract event ID from URL if available
        event_id = self.extract_event_id_from_url(alert.market_url)
        if event_id:
            line = getattr(alert, 'line', None)
            if line is None and alert.qualifier:
                line = self.parse_qualifier_to_line(alert.qualifier, alert.market_type)
            
            submarket = await self.find_submarket(
                event_id=event_id,
                market_type=alert.market_type,
                line=line,
                selection=alert.pick
            )
            
            if submarket:
                return {
                    'market': submarket,
                    'market_id': submarket.get('id', ''),
                    'match_method': 'submarket_search',
                    'confidence': 0.8
                }
        
        return None
    
    def extract_market_id_from_url(self, url: str) -> Optional[str]:
        """Extract Polymarket market ID from URL"""
        if not url:
            return None
        
        # Polymarket URL format: https://polymarket.com/event/.../market-id
        # TODO: Verify actual URL format and extract market ID
        parts = url.rstrip('/').split('/')
        if parts:
            # Last part might be market ID
            market_id = parts[-1]
            return market_id
        return None
    
    def extract_event_id_from_url(self, url: str) -> Optional[str]:
        """Extract Polymarket event ID from URL"""
        if not url:
            return None
        
        # Polymarket URL format: https://polymarket.com/event/event-id/...
        # TODO: Verify actual URL format and extract event ID
        if '/event/' in url:
            parts = url.split('/event/')
            if len(parts) > 1:
                event_part = parts[1].split('/')[0]
                return event_part
        return None
    
    def determine_side(self, alert: BookieBeatsAlert, market: Dict) -> Optional[str]:
        """
        Determine which side (yes/no) to bet based on the alert
        Returns 'yes' or 'no'
        
        TODO: Implement based on Polymarket market structure
        """
        if not alert.pick:
            return None
        
        pick_upper = alert.pick.upper()
        market_type_lower = alert.market_type.lower()
        
        # For Total Points/Goals (Over/Under)
        if 'total' in market_type_lower:
            if pick_upper == "OVER" or "OVER" in pick_upper:
                return 'yes'
            elif pick_upper == "UNDER" or "UNDER" in pick_upper:
                return 'no'
        
        # TODO: Implement spread and moneyline logic based on Polymarket structure
        # This will depend on how Polymarket structures their markets
        
        return None
