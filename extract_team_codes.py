#!/usr/bin/env python3
"""
Extract all team codes/abbreviations from Kalshi markets for all sports.
This script queries Kalshi API to find all unique team codes used in market tickers.
"""

import asyncio
import aiohttp
import json
import re
import time
import base64
from collections import defaultdict
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding

load_dotenv()

KALSHI_BASE = "https://api.elections.kalshi.com"

# Sport series prefixes
SPORT_SERIES = {
    'NCAAB': ['KXNCAAMBGAME', 'KXNCAAMBSPREAD', 'KXNCAAMBTOTAL'],
    'NBA': ['KXNBAGAME', 'KXNBASPREAD', 'KXNBATOTAL'],
    'NHL': ['KXNHLGAME', 'KXNHLSPREAD', 'KXNHLTOTAL'],
    'NFL': ['KXNFLGAME', 'KXNFLSPREAD', 'KXNFLTOTAL'],
    'MLB': ['KXMLBGAME', 'KXMLBSPREAD', 'KXMLBTOTAL'],
    'NCAAF': ['KXNCAAFGAME', 'KXNCAAFSPREAD', 'KXNCAAFTOTAL'],
}

class KalshiAuth:
    """Kalshi authentication handler using RSA private key signing"""
    
    def __init__(self):
        self.kid = os.getenv("KALSHI_KEY_ID")
        self.kfile = os.getenv("KALSHI_KEY_FILE", "kalshi.key")
        self.priv = self.load_key()
    
    def load_key(self):
        """Load RSA private key from file"""
        try:
            if not self.kfile or not os.path.exists(self.kfile):
                print(f"Warning: Kalshi key file not found at {self.kfile}")
                return None
            
            with open(self.kfile, "rb") as f:
                return serialization.load_pem_private_key(f.read(), password=None)
        except Exception as e:
            print(f"Auth error loading key: {e}")
            return None
    
    def sign(self, method, path):
        """Sign a request using RSA-PSS"""
        path_only = path.split('?')[0]
        ts = str(int(time.time() * 1000))
        
        if not self.priv:
            return ts, "MISSING_KEY"
        
        msg = f"{ts}{method}{path_only}"
        sig = self.priv.sign(
            msg.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256()
        )
        sig_b64 = base64.b64encode(sig).decode()
        return ts, sig_b64


class KalshiTeamCodeExtractor:
    def __init__(self):
        self.auth = KalshiAuth()
        self.session = None
        self.team_codes = defaultdict(set)  # sport -> set of team codes
        self.event_tickers = defaultdict(set)  # sport -> set of event tickers
        self.market_tickers = defaultdict(set)  # sport -> set of market tickers
        self.team_code_mappings = defaultdict(dict)  # sport -> {team_code: set of team names}
        
    async def init(self):
        """Initialize Kalshi session"""
        self.session = aiohttp.ClientSession()
        
        if not self.auth.priv or not self.auth.kid:
            print("WARNING: Kalshi authentication not configured.")
            print("   Set KALSHI_KEY_ID and KALSHI_KEY_FILE environment variables")
            print("   Or place kalshi.key file in current directory")
            print("   Script will continue but API calls may fail...")
        else:
            print("Kalshi authentication initialized")
    
    async def fetch_markets_for_series(self, series_ticker, limit=1000):
        """Fetch markets for a specific series"""
        path = f"/trade-api/v2/markets"
        params = {
            'series_ticker': series_ticker,
            'limit': limit,
            'status': 'open'  # Only get open markets
        }
        
        # Build query string
        query_str = '&'.join([f"{k}={v}" for k, v in params.items()])
        full_path = f"{path}?{query_str}"
        
        # Sign request
        if not self.auth.kid:
            print(f"   ERROR: Cannot fetch {series_ticker} - authentication not configured")
            return []
        
        ts, sig = self.auth.sign("GET", full_path)
        
        headers = {
            "KALSHI-ACCESS-KEY": self.auth.kid,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }
        
        url = f"{KALSHI_BASE}{full_path}"
        
        try:
            async with self.session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get('markets', [])
                elif resp.status == 429:
                    print(f"   WARNING: Rate limited for {series_ticker}, waiting...")
                    await asyncio.sleep(2)
                    return []
                else:
                    error_text = await resp.text()
                    print(f"   ERROR: Fetching {series_ticker}: {resp.status} - {error_text[:100]}")
                    return []
        except Exception as e:
            print(f"   EXCEPTION: Fetching {series_ticker}: {e}")
            return []
    
    def extract_team_codes_from_ticker(self, ticker, sport):
        """Extract team codes from a market ticker"""
        # Format examples:
        # KXNCAAMBGAME-26JAN31OHIOBUFF-OHIO (moneyline)
        # KXNCAAMBSPREAD-26JAN31OHIOBUFF-OHIO12 (spread)
        # KXNCAAMBTOTAL-26JAN31OHIOBUFF-154 (total)
        
        ticker_upper = ticker.upper()
        
        # Split by dashes
        parts = ticker_upper.split('-')
        if len(parts) < 2:
            return None, None
        
        # First part is series (e.g., KXNCAAMBGAME)
        series = parts[0]
        
        # Second part is event suffix (e.g., 26JAN31OHIOBUFF)
        event_suffix = parts[1]
        
        # Extract date part (e.g., 26JAN31)
        date_match = re.match(r'(\d{1,2}[A-Z]{3}\d{1,2})', event_suffix)
        if date_match:
            date_part = date_match.group(1)
            team_codes_part = event_suffix[len(date_part):]  # Everything after date
            
            # Try to split team codes (they're concatenated)
            # Common patterns: 3+3, 4+3, 3+4, 4+4, 5+3, etc.
            team_codes = []
            for i in range(2, min(8, len(team_codes_part) - 1)):
                code1 = team_codes_part[:i]
                code2 = team_codes_part[i:]
                if len(code1) >= 2 and len(code2) >= 2:
                    team_codes.append((code1, code2))
            
            # Also check if there's a third part (market-specific suffix)
            market_suffix = None
            if len(parts) >= 3:
                market_suffix = parts[2]
                # Remove line number if present (e.g., OHIO12 -> OHIO, 154 -> None)
                market_suffix_clean = re.sub(r'\d+$', '', market_suffix)
                if market_suffix_clean:
                    team_codes.append((market_suffix_clean, None))
            
            return team_codes, event_suffix
        
        return None, None
    
    def extract_team_name_from_subtitle(self, subtitle):
        """Extract team name from market subtitle (e.g., 'Holy Cross wins by over 4.5 Points' -> 'Holy Cross')"""
        if not subtitle or subtitle.upper() == 'N/A':
            return None
        
        # Common patterns:
        # "Team Name wins by over X Points"
        # "Team Name wins by under X Points"
        # "Team Name" (moneyline)
        # "Over X.X" (total)
        # "Under X.X" (total)
        
        import re
        
        # Pattern 1: "Team Name wins by over/under X Points"
        match = re.match(r'^(.+?)\s+wins\s+by\s+(over|under)', subtitle, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        
        # Pattern 2: "Team Name" (for moneyline markets)
        # If it doesn't contain "wins", "over", "under", "points", "goals", it's likely just a team name
        subtitle_lower = subtitle.lower()
        if not any(word in subtitle_lower for word in ['wins', 'over', 'under', 'points', 'goals', 'runs', 'total']):
            # Likely just a team name
            return subtitle.strip()
        
        return None
    
    async def extract_from_sport(self, sport, series_list):
        """Extract team codes and map them to team names for a specific sport"""
        print(f"\nExtracting team codes for {sport}...")
        
        all_team_codes = set()
        all_event_tickers = set()
        team_code_to_names = {}  # Map: team_code -> set of team names
        
        for series in series_list:
            print(f"   Fetching {series}...")
            markets = await self.fetch_markets_for_series(series, limit=1000)
            
            if not markets:
                print(f"   WARNING: No markets found for {series}")
                continue
            
            print(f"   Found {len(markets)} markets")
            
            for market in markets:
                ticker = market.get('ticker', '')
                event_ticker = market.get('event_ticker', '')
                yes_subtitle = market.get('yes_sub_title', '')
                no_subtitle = market.get('no_sub_title', '')
                
                if ticker:
                    all_event_tickers.add(event_ticker)
                    
                    # Extract team codes from ticker
                    team_code_pairs, event_suffix = self.extract_team_codes_from_ticker(ticker, sport)
                    
                    # Extract team names from subtitles
                    yes_team = self.extract_team_name_from_subtitle(yes_subtitle)
                    no_team = self.extract_team_name_from_subtitle(no_subtitle)
                    
                    if team_code_pairs:
                        for code1, code2 in team_code_pairs:
                            # Helper function to check if code matches team name
                            def code_matches_team(code, team_name):
                                if not code or not team_name:
                                    return False
                                code_upper = code.upper()
                                team_upper = team_name.upper()
                                
                                # Method 1: First letters of words match code
                                team_words = team_upper.split()
                                if len(team_words) >= len(code_upper):
                                    first_letters = ''.join([w[0] for w in team_words[:len(code_upper)]])
                                    if first_letters == code_upper:
                                        return True
                                
                                # Method 2: Code is substring of team name (common for abbreviations)
                                if code_upper in team_upper:
                                    return True
                                
                                # Method 3: Code matches a word in team name (e.g., "ASU" in "Arizona State")
                                for word in team_words:
                                    if code_upper in word or word.startswith(code_upper):
                                        return True
                                
                                return False
                            
                            if code1:
                                all_team_codes.add(code1)
                                # Try to map code to team name
                                if yes_team and code_matches_team(code1, yes_team):
                                    if code1 not in team_code_to_names:
                                        team_code_to_names[code1] = set()
                                    team_code_to_names[code1].add(yes_team)
                                if no_team and code_matches_team(code1, no_team):
                                    if code1 not in team_code_to_names:
                                        team_code_to_names[code1] = set()
                                    team_code_to_names[code1].add(no_team)
                            
                            if code2:
                                all_team_codes.add(code2)
                                # Same mapping logic for code2
                                if yes_team and code_matches_team(code2, yes_team):
                                    if code2 not in team_code_to_names:
                                        team_code_to_names[code2] = set()
                                    team_code_to_names[code2].add(yes_team)
                                if no_team and code_matches_team(code2, no_team):
                                    if code2 not in team_code_to_names:
                                        team_code_to_names[code2] = set()
                                    team_code_to_names[code2].add(no_team)
                    
                    # Also extract from event_ticker if available
                    if event_ticker:
                        team_code_pairs, _ = self.extract_team_codes_from_ticker(event_ticker, sport)
                        if team_code_pairs:
                            # Helper function to check if code matches team name
                            def code_matches_team(code, team_name):
                                if not code or not team_name:
                                    return False
                                code_upper = code.upper()
                                team_upper = team_name.upper()
                                
                                # Method 1: First letters of words match code
                                team_words = team_upper.split()
                                if len(team_words) >= len(code_upper):
                                    first_letters = ''.join([w[0] for w in team_words[:len(code_upper)]])
                                    if first_letters == code_upper:
                                        return True
                                
                                # Method 2: Code is substring of team name
                                if code_upper in team_upper:
                                    return True
                                
                                # Method 3: Code matches a word in team name
                                for word in team_words:
                                    if code_upper in word or word.startswith(code_upper):
                                        return True
                                
                                return False
                            
                            for code1, code2 in team_code_pairs:
                                if code1:
                                    all_team_codes.add(code1)
                                    if yes_team and code_matches_team(code1, yes_team):
                                        if code1 not in team_code_to_names:
                                            team_code_to_names[code1] = set()
                                        team_code_to_names[code1].add(yes_team)
                                    if no_team and code_matches_team(code1, no_team):
                                        if code1 not in team_code_to_names:
                                            team_code_to_names[code1] = set()
                                        team_code_to_names[code1].add(no_team)
                                
                                if code2:
                                    all_team_codes.add(code2)
                                    if yes_team and code_matches_team(code2, yes_team):
                                        if code2 not in team_code_to_names:
                                            team_code_to_names[code2] = set()
                                        team_code_to_names[code2].add(yes_team)
                                    if no_team and code_matches_team(code2, no_team):
                                        if code2 not in team_code_to_names:
                                            team_code_to_names[code2] = set()
                                        team_code_to_names[code2].add(no_team)
            
            # Small delay to avoid rate limiting
            await asyncio.sleep(0.5)
        
        self.team_codes[sport] = all_team_codes
        self.event_tickers[sport] = all_event_tickers
        self.team_code_mappings[sport] = team_code_to_names
        
        print(f"   Found {len(all_team_codes)} unique team codes for {sport}")
        print(f"   Mapped {len(team_code_to_names)} codes to team names")
        return all_team_codes
    
    async def run(self):
        """Main extraction process"""
        await self.init()
        
        print("\n" + "="*60)
        print("KALSHI TEAM CODE EXTRACTOR")
        print("="*60)
        
        # Extract for each sport
        for sport, series_list in SPORT_SERIES.items():
            await self.extract_from_sport(sport, series_list)
        
        # Output results
        print("\n" + "="*60)
        print("RESULTS")
        print("="*60)
        
        # Save to JSON with mappings
        output = {
            'extracted_at': datetime.now().isoformat(),
            'team_codes_by_sport': {sport: sorted(list(codes)) for sport, codes in self.team_codes.items()},
            'team_code_mappings': {
                sport: {
                    code: sorted(list(names)) if names else []
                    for code, names in mappings.items()
                }
                for sport, mappings in self.team_code_mappings.items()
            },
            'total_unique_codes': len(set().union(*self.team_codes.values())) if self.team_codes else 0,
        }
        
        with open('kalshi_team_codes.json', 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2)
        
        print(f"\nSaved results to kalshi_team_codes.json")
        print(f"   Total unique team codes across all sports: {output['total_unique_codes']}")
        
        # Print summary by sport
        print("\nTeam codes by sport:")
        for sport, codes in sorted(output['team_codes_by_sport'].items()):
            print(f"   {sport}: {len(codes)} codes")
            if len(codes) <= 20:  # Print if small enough
                print(f"      {', '.join(sorted(codes))}")
        
        # Print mappings summary
        print("\nTeam code mappings (code -> team names):")
        for sport, mappings in sorted(output['team_code_mappings'].items()):
            mapped_count = len([m for m in mappings.values() if m])
            print(f"   {sport}: {mapped_count} codes mapped to team names (out of {len(mappings)} total)")
            # Show a few examples
            examples = [(code, names) for code, names in list(mappings.items())[:5] if names]
            if examples:
                print(f"      Examples:")
                for code, names in examples:
                    print(f"         {code} -> {', '.join(names)}")
        
        # Also save as Python dict for easy copy-paste
        python_output = "# Kalshi Team Codes Extracted from API\n"
        python_output += "# Generated: " + datetime.now().isoformat() + "\n\n"
        python_output += "kalshi_team_codes_dict = {\n"
        for sport, codes in sorted(output['team_codes_by_sport'].items()):
            python_output += f"    '{sport}': {sorted(codes)},\n"
        python_output += "}\n\n"
        
        # Also output mappings as Python dict
        python_output += "# Team Code to Team Name Mappings\n"
        python_output += "kalshi_team_code_mappings = {\n"
        for sport, mappings in sorted(output['team_code_mappings'].items()):
            python_output += f"    '{sport}': {{\n"
            for code, names in sorted(mappings.items()):
                if names:  # Only include codes that have mappings
                    names_str = ', '.join([f"'{n}'" for n in sorted(names)])
                    python_output += f"        '{code}': [{names_str}],\n"
            python_output += "    },\n"
        python_output += "}\n"
        
        with open('kalshi_team_codes_dict.py', 'w', encoding='utf-8') as f:
            f.write(python_output)
        
        print(f"\nSaved Python dict to kalshi_team_codes_dict.py")
        
        await self.session.close()

async def main():
    extractor = KalshiTeamCodeExtractor()
    await extractor.run()

if __name__ == '__main__':
    asyncio.run(main())
