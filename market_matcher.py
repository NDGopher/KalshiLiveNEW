"""
Market Matcher
Matches live EV alerts to Kalshi markets with high accuracy
"""
import re
from typing import Optional, Dict, List
from thefuzz import fuzz, process
from ev_alert import EvAlert
from kalshi_client import KalshiClient


class MarketMatcher:
    """Matches live EV alerts to Kalshi markets"""
    
    def __init__(self, kalshi_client: KalshiClient):
        self.client = kalshi_client
        self.market_cache = {}  # Cache of Kalshi markets
    
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

    @staticmethod
    def teams_from_odds_api_event(event: Dict) -> str:
        """Build teams string from Odds-API.io event dict (home/away)."""
        if not event:
            return ""
        home = str(event.get("home") or "").strip()
        away = str(event.get("away") or "").strip()
        if away and home:
            return f"{away} @ {home}"
        return home or away
    
    def parse_odds_to_price_cents(self, odds_str: str) -> Optional[int]:
        """Convert odds string (e.g., '+105', '-564') to price in cents"""
        if not odds_str:
            return None
        
        try:
            odds_str = odds_str.strip().replace('+', '').replace(',', '')
            odds = int(odds_str)
            
            # Convert American odds to probability (price)
            if odds > 0:
                # Positive odds: probability = 100 / (odds + 100)
                price = 100 / (odds + 100)
            else:
                # Negative odds: probability = abs(odds) / (abs(odds) + 100)
                price = abs(odds) / (abs(odds) + 100)
            
            return int(price * 100)  # Convert to cents
        except:
            return None
    
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
    
    async def find_market_by_ticker(self, ticker: str) -> Optional[Dict]:
        """Find market by ticker (fastest method)"""
        if not ticker:
            return None
        
        # Check cache first
        if ticker.upper() in self.market_cache:
            return self.market_cache[ticker.upper()]
        
        # Fetch from API
        market = await self.client.get_market_by_ticker(ticker)
        if market:
            self.market_cache[ticker.upper()] = market
        return market
    
    async def search_market_by_details(self, alert: EvAlert) -> Optional[Dict]:
        """Search for market using team names and market type"""
        if not alert.teams:
            return None
        
        team1, team2 = self.extract_teams_from_string(alert.teams)
        if not team1 or not team2:
            return None
        
        # Build search query
        # Try searching with team names
        query_terms = [team1, team2]
        
        # Search markets
        markets = await self.client.search_markets({
            "status": "open",
            "limit": 100
        })
        
        if not markets:
            return None
        
        # Score and rank matches
        scored_markets = []
        for market in markets:
            title = market.get('title', '').upper()
            subtitle_yes = market.get('yes_sub_title', '').upper()
            subtitle_no = market.get('no_sub_title', '').upper()
            
            # Check if both team names appear in title
            team1_in_title = team1 in title or any(word in title for word in team1.split())
            team2_in_title = team2 in title or any(word in title for word in team2.split())
            
            if not (team1_in_title and team2_in_title):
                continue
            
            # Calculate match score
            score = 0
            
            # Market type matching
            market_type_lower = alert.market_type.lower()
            if 'spread' in market_type_lower and 'spread' in title.lower():
                score += 30
            elif 'moneyline' in market_type_lower and ('moneyline' in title.lower() or 'win' in title.lower()):
                score += 30
            elif 'total' in market_type_lower and 'total' in title.lower():
                score += 30
            
            # Team name matching
            if team1 in title:
                score += 20
            if team2 in title:
                score += 20
            
            # Qualifier/line matching
            if alert.qualifier:
                qualifier_clean = alert.qualifier.replace('+', '').replace('*', '').strip()
                if qualifier_clean in title or qualifier_clean in subtitle_yes or qualifier_clean in subtitle_no:
                    score += 20
            
            if score > 0:
                scored_markets.append((score, market))
        
        # Return best match
        if scored_markets:
            scored_markets.sort(key=lambda x: x[0], reverse=True)
            best_match = scored_markets[0][1]
            ticker = best_match.get('ticker', '').upper()
            if ticker:
                self.market_cache[ticker] = best_match
            return best_match
        
        return None
    
    async def match_alert_to_kalshi(self, alert: EvAlert) -> Optional[Dict]:
        """
        Match an EV alert to a Kalshi market
        Returns matched market data with additional info
        
        CRITICAL: alert.ticker is EVENT ticker, not submarket ticker!
        Use find_submarket to get exact submarket.
        """
        # Method 1: Event ticker → Find exact submarket (FASTEST and MOST ACCURATE)
        event_ticker = alert.ticker or alert.extract_ticker_from_url()
        if event_ticker:
            # Get line from alert
            line = getattr(alert, 'line', None)
            if line is None and alert.qualifier:
                try:
                    import re
                    line_str = alert.qualifier.replace('+', '').replace('*', '').strip()
                    line = float(line_str)
                except:
                    pass
            
            # Find exact submarket within event
            submarket = await self.client.find_submarket(
                event_ticker=event_ticker,
                market_type=alert.market_type,
                line=line,
                selection=alert.pick
            )
            
            if submarket:
                submarket_ticker = submarket.get('ticker', '').upper()
                return {
                    'market': submarket,
                    'ticker': submarket_ticker,
                    'match_method': 'exact_submarket',
                    'confidence': 1.0
                }
        
        # REMOVED: Method 2 fallback search - we ONLY use direct ticker building now
        # This prevents matching wrong markets (e.g., KXMVESPORTSMULTIGAMEEXTENDED)
        # If direct ticker building fails, return None immediately (lightning fast!)
        return None
    
    def _determine_sport_from_ticker(self, ticker: str) -> str:
        """Extract sport/league from ticker (e.g., KXNHLGAME -> NHL)"""
        if not ticker:
            return "Unknown"
        ticker_upper = ticker.upper()
        if ticker_upper.startswith('KXNHL'):
            return "NHL"
        elif ticker_upper.startswith('KXNBA'):
            return "NBA"
        elif ticker_upper.startswith('KXNFL'):
            return "NFL"
        elif ticker_upper.startswith('KXNCAAM') or ticker_upper.startswith('KXNCAAB'):
            return "NCAAB"
        elif ticker_upper.startswith('KXNCAAF'):
            return "NCAAF"
        elif ticker_upper.startswith('KXMLB'):
            return "MLB"
        elif ticker_upper.startswith('KXUCL'):
            return "UCL"
        elif ticker_upper.startswith('KXEPL'):
            return "EPL"
        else:
            return "Unknown"
    
    def _get_team_code_map_by_sport(self, sport: str) -> Dict:
        """Get sport-specific team code mapping to prevent collisions"""
        # Organized by sport to prevent collisions (e.g., "BOSTON" could be Bruins, Celtics, Red Sox)
        team_code_maps = {
            "NHL": {
                'ANAHEIM DUCKS': ['ANA'], 'ANAHEIM': ['ANA'],
                'BOSTON BRUINS': ['BOS'], 'BOSTON': ['BOS'],
                'BUFFALO SABRES': ['BUF'], 'BUFFALO': ['BUF'],
                'CALGARY FLAMES': ['CGY'], 'CALGARY': ['CGY'],
                'CAROLINA HURRICANES': ['CAR'], 'CAROLINA': ['CAR'],
                'CHICAGO BLACKHAWKS': ['CHI'], 'CHICAGO': ['CHI'],
                'COLORADO AVALANCHE': ['COL'], 'COLORADO': ['COL'],
                'COLUMBUS BLUE JACKETS': ['CBJ'], 'COLUMBUS': ['CBJ'],
                'DALLAS STARS': ['DAL'], 'DALLAS': ['DAL'],
                'DETROIT RED WINGS': ['DET'], 'DETROIT': ['DET'],
                'EDMONTON OILERS': ['EDM'], 'EDMONTON': ['EDM'],
                'FLORIDA PANTHERS': ['FLA'], 'FLORIDA': ['FLA'],
                'LOS ANGELES KINGS': ['LAK'], 'LOS ANGELES': ['LAK'],
                'MINNESOTA WILD': ['MIN'], 'MINNESOTA': ['MIN'],
                'MONTREAL CANADIENS': ['MTL'], 'MONTREAL': ['MTL'],
                'NASHVILLE PREDATORS': ['NSH'], 'NASHVILLE': ['NSH', 'NASH'], 'NASH': ['NSH'],
                'NEW JERSEY DEVILS': ['NJD', 'NJ'], 'NEW JERSEY': ['NJD', 'NJ'],
                'NEW YORK ISLANDERS': ['NYI'],
                'NEW YORK RANGERS': ['NYR'],
                'OTTAWA SENATORS': ['OTT'], 'OTTAWA': ['OTT'],
                'PHILADELPHIA FLYERS': ['PHI'], 'PHILADELPHIA': ['PHI'],
                'PITTSBURGH PENGUINS': ['PIT'], 'PITTSBURGH': ['PIT'],
                'SAN JOSE SHARKS': ['SJS', 'SJ'], 'SAN JOSE': ['SJS', 'SJ'], 'SHARKS': ['SJS', 'SJ'],
                'SEATTLE KRAKEN': ['SEA'], 'SEATTLE': ['SEA'],
                'ST. LOUIS BLUES': ['STL'], 'ST LOUIS': ['STL'], 'SAINT LOUIS': ['STL'],
                'TAMPA BAY LIGHTNING': ['TBL'], 'TAMPA BAY': ['TBL'], 'TAMPA': ['TBL'],
                'TORONTO MAPLE LEAFS': ['TOR'], 'TORONTO': ['TOR'],
                'UTAH MAMMOTH': ['UTA'], 'UTAH': ['UTA'], 'UTAH HOCKEY CLUB': ['UTA'],
                'VANCOUVER CANUCKS': ['VAN'], 'VANCOUVER': ['VAN'],
                'VEGAS GOLDEN KNIGHTS': ['VGK'], 'VEGAS': ['VGK'],
                'WASHINGTON CAPITALS': ['WSH'], 'WASHINGTON': ['WSH', 'WAS'], 'WAS': ['WSH'],
                'WINNIPEG JETS': ['WPG'], 'WINNIPEG': ['WPG'],
            },
            "NBA": {
                'ATLANTA HAWKS': ['ATL'], 'ATLANTA': ['ATL'],
                'BROOKLYN NETS': ['BKN'], 'BROOKLYN': ['BKN'],
                'BOSTON CELTICS': ['BOS'], 'BOSTON': ['BOS'],
                'CHARLOTTE HORNETS': ['CHA'], 'CHARLOTTE': ['CHA'],
                'CHICAGO BULLS': ['CHI'], 'CHICAGO': ['CHI'],
                'CLEVELAND CAVALIERS': ['CLE'], 'CLEVELAND': ['CLE'],
                'DALLAS MAVERICKS': ['DAL'], 'DALLAS': ['DAL'],
                'DENVER NUGGETS': ['DEN'], 'DENVER': ['DEN'],
                'DETROIT PISTONS': ['DET'], 'DETROIT': ['DET'],
                'GOLDEN STATE WARRIORS': ['GSW'], 'GOLDEN STATE': ['GSW'], 'GS': ['GSW'],
                'HOUSTON ROCKETS': ['HOU'], 'HOUSTON': ['HOU'],
                'INDIANA PACERS': ['IND'], 'INDIANA': ['IND'],
                'LOS ANGELES CLIPPERS': ['LAC'], 'CLIPPERS': ['LAC'],
                'LOS ANGELES LAKERS': ['LAL'], 'LAKERS': ['LAL'],
                'MEMPHIS GRIZZLIES': ['MEM'], 'MEMPHIS': ['MEM'],
                'MIAMI HEAT': ['MIA'], 'MIAMI': ['MIA'],
                'MILWAUKEE BUCKS': ['MIL'], 'MILWAUKEE': ['MIL'],
                'MINNESOTA TIMBERWOLVES': ['MIN'], 'MINNESOTA': ['MIN'], 'MINN': ['MIN'],
                'NEW ORLEANS PELICANS': ['NOP'], 'NEW ORLEANS': ['NOP'], 'NO': ['NOP'],
                'NEW YORK KNICKS': ['NYK'], 'NEW YORK': ['NYK'], 'KNICKS': ['NYK'],
                'OKLAHOMA CITY THUNDER': ['OKC'], 'OKLAHOMA CITY': ['OKC'],
                'ORLANDO MAGIC': ['ORL'], 'ORLANDO': ['ORL'],
                'PHILADELPHIA 76ERS': ['PHI'], 'PHILADELPHIA': ['PHI'], '76ERS': ['PHI'],
                'PHOENIX SUNS': ['PHX'], 'PHOENIX': ['PHX'],
                'PORTLAND TRAIL BLAZERS': ['POR'], 'PORTLAND': ['POR'], 'TRAIL BLAZERS': ['POR'],
                'SACRAMENTO KINGS': ['SAC'], 'SACRAMENTO': ['SAC'],
                'SAN ANTONIO SPURS': ['SAS'], 'SAN ANTONIO': ['SAS'], 'SA': ['SAS'],
                'TORONTO RAPTORS': ['TOR'], 'TORONTO': ['TOR'],
                'UTAH JAZZ': ['UTA'], 'UTAH': ['UTA'],
                'WASHINGTON WIZARDS': ['WAS'], 'WASHINGTON': ['WAS', 'WSH'], 'WSH': ['WAS'],
            },
            "NFL": {
                'ARIZONA CARDINALS': ['ARI'], 'ARIZONA': ['ARI'],
                'ATLANTA FALCONS': ['ATL'], 'ATLANTA': ['ATL'],
                'BALTIMORE RAVENS': ['BAL'], 'BALTIMORE': ['BAL'],
                'BUFFALO BILLS': ['BUF'], 'BUFFALO': ['BUF'],
                'CAROLINA PANTHERS': ['CAR'], 'CAROLINA': ['CAR'],
                'CHICAGO BEARS': ['CHI'], 'CHICAGO': ['CHI'],
                'CINCINNATI BENGALS': ['CIN'], 'CINCINNATI': ['CIN'],
                'CLEVELAND BROWNS': ['CLE'], 'CLEVELAND': ['CLE'],
                'DALLAS COWBOYS': ['DAL'], 'DALLAS': ['DAL'],
                'DENVER BRONCOS': ['DEN'], 'DENVER': ['DEN'],
                'DETROIT LIONS': ['DET'], 'DETROIT': ['DET'],
                'GREEN BAY PACKERS': ['GB'], 'GREEN BAY': ['GB'],
                'HOUSTON TEXANS': ['HOU'], 'HOUSTON': ['HOU'],
                'INDIANAPOLIS COLTS': ['IND'], 'INDIANAPOLIS': ['IND'],
                'JACKSONVILLE JAGUARS': ['JAX'], 'JACKSONVILLE': ['JAX'],
                'KANSAS CITY CHIEFS': ['KC'], 'KANSAS CITY': ['KC'],
                'LAS VEGAS RAIDERS': ['LV'], 'LAS VEGAS': ['LV'], 'RAIDERS': ['LV'],
                'LOS ANGELES CHARGERS': ['LAC'], 'LOS ANGELES C': ['LAC'], 'CHARGERS': ['LAC'],
                'LOS ANGELES RAMS': ['LAR'], 'LOS ANGELES R': ['LAR'], 'RAMS': ['LAR'],
                'MIAMI DOLPHINS': ['MIA'], 'MIAMI': ['MIA'],
                'MINNESOTA VIKINGS': ['MIN'], 'MINNESOTA': ['MIN'],
                'NEW ENGLAND PATRIOTS': ['NE'], 'NEW ENGLAND': ['NE'], 'PATRIOTS': ['NE'],
                'NEW ORLEANS SAINTS': ['NO'], 'NEW ORLEANS': ['NO'],
                'NEW YORK GIANTS': ['NYG'], 'NEW YORK G': ['NYG'], 'GIANTS': ['NYG'],
                'NEW YORK JETS': ['NYJ'], 'NEW YORK J': ['NYJ'], 'JETS': ['NYJ'],
                'PHILADELPHIA EAGLES': ['PHI'], 'PHILADELPHIA': ['PHI'], 'EAGLES': ['PHI'],
                'PITTSBURGH STEELERS': ['PIT'], 'PITTSBURGH': ['PIT'], 'STEELERS': ['PIT'],
                'SAN FRANCISCO 49ERS': ['SF'], 'SAN FRANCISCO': ['SF'], '49ERS': ['SF'],
                'SEATTLE SEAHAWKS': ['SEA'], 'SEATTLE': ['SEA'], 'SEAHAWKS': ['SEA'],
                'TAMPA BAY BUCCANEERS': ['TB'], 'TAMPA BAY': ['TB'], 'BUCCANEERS': ['TB'],
                'TENNESSEE TITANS': ['TEN'], 'TENNESSEE': ['TEN'], 'TITANS': ['TEN'],
                'WASHINGTON COMMANDERS': ['WAS'], 'WASHINGTON': ['WAS', 'WSH'], 'WSH': ['WAS'], 'COMMANDERS': ['WAS'],
            },
            "MLB": {
                'ATLANTA BRAVES': ['ATL'], 'ATLANTA': ['ATL'],
                'BALTIMORE ORIOLES': ['BAL'], 'BALTIMORE': ['BAL'],
                'BOSTON RED SOX': ['BOS'], 'BOSTON': ['BOS'], 'RED SOX': ['BOS'],
                'CHICAGO CUBS': ['CHC'], 'CHICAGO C': ['CHC'], 'CUBS': ['CHC'],
                'CHICAGO WHITE SOX': ['CWS', 'CHW'], 'CHICAGO W': ['CWS', 'CHW'], 'WHITE SOX': ['CWS', 'CHW'],
                'CINCINNATI REDS': ['CIN'], 'CINCINNATI': ['CIN'],
                'CLEVELAND GUARDIANS': ['CLE'], 'CLEVELAND': ['CLE'],
                'COLORADO ROCKIES': ['COL'], 'COLORADO': ['COL'],
                'DETROIT TIGERS': ['DET'], 'DETROIT': ['DET'],
                'HOUSTON ASTROS': ['HOU'], 'HOUSTON': ['HOU'],
                'KANSAS CITY ROYALS': ['KCR', 'KC'], 'KANSAS CITY': ['KCR', 'KC'], 'ROYALS': ['KCR', 'KC'],
                'LOS ANGELES ANGELS': ['LAA', 'ANA'], 'LOS ANGELES A': ['LAA', 'ANA'], 'ANGELS': ['LAA', 'ANA'],
                'LOS ANGELES DODGERS': ['LAD'], 'LOS ANGELES D': ['LAD'], 'DODGERS': ['LAD'],
                'MIAMI MARLINS': ['MIA'], 'MIAMI': ['MIA'],
                'MILWAUKEE BREWERS': ['MIL'], 'MILWAUKEE': ['MIL'],
                'MINNESOTA TWINS': ['MIN'], 'MINNESOTA': ['MIN'],
                'NEW YORK METS': ['NYM'], 'NEW YORK M': ['NYM'], 'METS': ['NYM'],
                'NEW YORK YANKEES': ['NYY'], 'NEW YORK Y': ['NYY'], 'YANKEES': ['NYY'],
                'OAKLAND ATHLETICS': ['OAK'], 'OAKLAND': ['OAK'], 'ATHLETICS': ['OAK'],
                'PHILADELPHIA PHILLIES': ['PHI'], 'PHILADELPHIA': ['PHI'], 'PHILLIES': ['PHI'],
                'PITTSBURGH PIRATES': ['PIT'], 'PITTSBURGH': ['PIT'], 'PIRATES': ['PIT'],
                'SAN DIEGO PADRES': ['SDP', 'SD'], 'SAN DIEGO': ['SDP', 'SD'], 'PADRES': ['SDP', 'SD'],
                'SAN FRANCISCO GIANTS': ['SFG', 'SF'], 'SAN FRANCISCO': ['SFG', 'SF'], 'GIANTS': ['SFG', 'SF'],
                'SEATTLE MARINERS': ['SEA'], 'SEATTLE': ['SEA'], 'MARINERS': ['SEA'],
                'ST. LOUIS CARDINALS': ['STL'], 'ST LOUIS': ['STL'], 'SAINT LOUIS': ['STL'], 'CARDINALS': ['STL'],
                'TAMPA BAY RAYS': ['TBR', 'TB'], 'TAMPA BAY': ['TBR', 'TB'], 'RAYS': ['TBR', 'TB'],
                'TEXAS RANGERS': ['TEX'], 'TEXAS': ['TEX'], 'RANGERS': ['TEX'],
                'TORONTO BLUE JAYS': ['TOR'], 'TORONTO': ['TOR'], 'BLUE JAYS': ['TOR'],
                'WASHINGTON NATIONALS': ['WSH'], 'WASHINGTON': ['WSH', 'WAS'], 'NATIONALS': ['WSH'],
            },
            "UCL": {
                'BARCELONA': ['BAR'], 'COPENHAGEN': ['FCC'],
                'BENFICA': ['BEN'], 'SL BENFICA': ['BEN'], 'REAL MADRID': ['RMA'],
                'ARSENAL': ['ARS'], 'KAIRAT': ['ALM'],
                'EINDHOVEN': ['PSV'], 'PSV': ['PSV'], 'BAYERN MUNICH': ['BMU'], 'BAYERN': ['BMU'],
                'NAPOLI': ['NAP'], 'CHELSEA': ['CFC'],
                'MONACO': ['ASM'], 'JUVENTUS': ['JUV'],
                'FRANKFURT': ['SGE'], 'EINTRACHT FRANKFURT': ['SGE'], 'TOTTENHAM': ['TOT'],
                'AJAX': ['AJA'], 'OLYMPIACOS': ['OLY'],
                'BILBAO': ['ATH'], 'ATHLETIC BILBAO': ['ATH'], 'SPORTING CP': ['SPO'], 'SPORTING LISBON': ['SPO'],
                'ATLETICO': ['ATM'], 'ATLETICO MADRID': ['ATM'], 'BODOE': ['BOG'], 'BODOE/GLIMT': ['BOG'], 'GLIMT': ['BOG'],
                'CLUB BRUGGE': ['BRU'], 'BRUGGE': ['BRU'], 'MARSEILLE': ['OM'],
                'DORTMUND': ['BVB'], 'BORUSSIA DORTMUND': ['BVB'], 'INTER': ['INT'], 'INTER MILAN': ['INT'],
                'LEVERKUSEN': ['LEV'], 'BAYER LEVERKUSEN': ['LEV'], 'VILLARREAL': ['VIL'],
                'LIVERPOOL': ['LFC'], 'QARABAG': ['QAR'],
                'MANCHESTER CITY': ['MCI'], 'GALATASARAY': ['GAL'],
                'PAFOS': ['PAF'], 'SLAVIA PRAGUE': ['SLA'],
                'PSG': ['PSG'], 'PARIS SAINT-GERMAIN': ['PSG'], 'NEWCASTLE': ['NEW'],
                'UNION GILLOISE': ['USG'], 'ATALANTA': ['ATA'],
            },
            "EPL": {
                'LEEDS': ['LEE'], 'ARSENAL': ['ARS'], 'WOLVERHAMPTON': ['WOL'], 'WOLVES': ['WOL'],
                'BOURNEMOUTH': ['BOU'], 'CHELSEA': ['CFC'], 'WEST HAM': ['WHU'],
                'LIVERPOOL': ['LFC'], 'NEWCASTLE': ['NEW'], 'ASTON VILLA': ['AVL'], 'VILLA': ['AVL'],
                'BRENTFORD': ['BRE'], 'BRIGHTON': ['BRI'], 'MANCHESTER UNITED': ['MUN'], 'MAN UNITED': ['MUN'],
                'FULHAM': ['FUL'], 'NOTTINGHAM': ['NFO'], 'NOTTINGHAM FOREST': ['NFO'],
                'CRYSTAL PALACE': ['CRY'], 'PALACE': ['CRY'], 'TOTTENHAM': ['TOT'], 'SPURS': ['TOT'],
                'MANCHESTER CITY': ['MCI'], 'SUNDERLAND': ['SUN'], 'BURNLEY': ['BUR'],
            },
            "NCAAB": {},  # College teams use the large inline map in determine_side
            "NCAAF": {},  # College teams are dynamically extracted
        }
        
        # Return sport-specific map, or empty dict if sport not found
        return team_code_maps.get(sport, {})
    
    def determine_side(self, alert: EvAlert, market: Dict) -> Optional[str]:
        """
        Determine which side (yes/no) to bet based on the alert
        Returns 'yes' or 'no'
        
        CRITICAL: Use ticker structure to determine side - it's more reliable than parsing team names
        Now uses sport-specific team code mappings to prevent collisions
        """
        if not alert.pick:
            return None
        
        pick_upper = alert.pick.upper()
        market_type_lower = alert.market_type.lower()
        
        # Get ticker from market - this tells us which team is in the subtitle
        ticker = market.get('ticker', '').upper()
        
        # Extract sport from ticker for sport-specific team code mapping
        sport = self._determine_sport_from_ticker(ticker)
        
        # Check market subtitles
        yes_subtitle = market.get('yes_sub_title', '').upper()
        no_subtitle = market.get('no_sub_title', '').upper()
        market_title = market.get('title', '').upper()
        
        # CRITICAL: For Total Points/Goals (Over/Under) - SIMPLE LOGIC
        # YES = Over, NO = Under (always, regardless of subtitle bugs)
        if 'total' in market_type_lower:
            if pick_upper == "OVER":
                # Over → bet YES (always)
                print(f"   ✅ [TOTAL] Pick is Over → bet YES (YES = Over, NO = Under)")
                return 'yes'
            elif pick_upper == "UNDER":
                # Under → bet NO (always)
                print(f"   ✅ [TOTAL] Pick is Under → bet NO (YES = Over, NO = Under)")
                return 'no'
            # If pick doesn't explicitly say Over/Under, try to infer
            if "OVER" in pick_upper:
                print(f"   ✅ [TOTAL] Pick contains 'Over' → bet YES")
                return 'yes'
            if "UNDER" in pick_upper:
                print(f"   ✅ [TOTAL] Pick contains 'Under' → bet NO")
                return 'no'
        
        # For Point Spread / Puck Line
        if market_type_lower in ['point spread', 'spread', 'puck line']:
            # CRITICAL: Use ticker structure to determine side - much more reliable!
            # Ticker format: KXNCAAMBSPREAD-26JAN10DEPCONN-CONN19
            # The suffix (CONN19) tells us which team is in the subtitle (UConn with line 19.5)
            # If ticker ends with team code + line, that team is favored (in subtitle)
            
            if ticker and alert.qualifier:
                # Extract team code from ticker suffix (e.g., "CONN" from "CONN19" or "CONN19.5")
                # Ticker format: ...-TEAMCODE[LINE] where LINE is the spread
                import re
                qualifier_clean = alert.qualifier.replace('+', '').replace('-', '').replace('*', '').strip()
                
                # Try to extract team code from ticker suffix
                # Pattern: Last segment after final dash, remove line number
                ticker_parts = ticker.split('-')
                if len(ticker_parts) >= 2:
                    suffix = ticker_parts[-1]  # Last part (e.g., "CONN19" or "CONN19.5" or "FLA2")
                    # Remove line number from suffix to get team code
                    # Try to match line in suffix (could be integer or decimal)
                    line_match = re.search(r'(\d+\.?\d*)', suffix)
                    if line_match:
                        team_code_in_ticker = suffix[:line_match.start()].upper()  # Everything before the line number
                    else:
                        # No line number in suffix, use entire suffix as team code
                        team_code_in_ticker = suffix.upper()
                    
                    print(f"   Debug: Ticker-based logic - suffix: {suffix}, extracted team code: {team_code_in_ticker}")
                    
                    # CRITICAL: Check if pick maps to ticker code
                    # Example: Ticker "LAC3" means "LAC wins by over 3.5"
                    # If pick is "Los Angeles Chargers" and we know "Los Angeles Chargers" -> "LAC", then pick matches ticker
                    # We'll use heuristics first, then comprehensive map lookup after it's defined
                    
                    pick_matches_ticker_team = False
                    
                    # Heuristic 1: Check if ticker code is abbreviation of pick (first letters)
                    # "LAC" from "Los Angeles Chargers" = L + A + C
                    pick_words = [w for w in pick_upper.split() if len(w) > 0]
                    if len(pick_words) >= len(team_code_in_ticker):
                        first_letters = ''.join([w[0] for w in pick_words[:len(team_code_in_ticker)]])
                        if first_letters == team_code_in_ticker:
                            pick_matches_ticker_team = True
                            print(f"   Debug: Ticker code {team_code_in_ticker} matches first letters: {first_letters}")
                    
                    # Heuristic 2: Check if ticker code is substring of pick
                    if not pick_matches_ticker_team and team_code_in_ticker in pick_upper:
                        pick_matches_ticker_team = True
                        print(f"   Debug: Ticker code {team_code_in_ticker} found as substring in pick")
                    
                    # Heuristic 3: Check if any word in pick matches ticker code
                    if not pick_matches_ticker_team:
                        for word in pick_words:
                            if word == team_code_in_ticker or team_code_in_ticker in word or word in team_code_in_ticker:
                                pick_matches_ticker_team = True
                                print(f"   Debug: Ticker code {team_code_in_ticker} matches word '{word}' in pick")
                                break
                    
                    # Note: Comprehensive team_code_map reverse lookup will be done after map is defined (around line 1080)
                    # For now, heuristics should catch most cases like "LAC" from "Los Angeles Chargers"
                    
                    # Determine if pick is underdog or favorite
                    # CRITICAL: If qualifier doesn't have + or -, and pick matches ticker, assume it's favored (negative spread)
                    # For "Chargers 3.5" without sign, if ticker is "LAC3", it's "Chargers -3.5" (favored)
                    has_plus = alert.qualifier.startswith('+')
                    has_minus = alert.qualifier.startswith('-')
                    
                    if has_plus:
                        is_underdog = True
                    elif has_minus:
                        is_underdog = False
                    else:
                        # No sign: if pick matches ticker, assume favored (negative spread)
                        # If pick doesn't match ticker, assume underdog (positive spread)
                        is_underdog = not pick_matches_ticker_team
                    
                    print(f"   Debug: Ticker team code: {team_code_in_ticker}, pick: {pick_upper}, matches: {pick_matches_ticker_team}, is_underdog: {is_underdog}, qualifier: {alert.qualifier}")
                    
                    if pick_matches_ticker_team:
                        # Pick is the team in the ticker (favored team in subtitle)
                        if is_underdog:
                            # This shouldn't happen (favored team can't be underdog), but handle it
                            # If subtitle says "Team wins by over X" and pick is that team with +X, bet NO
                            print(f"   ⚠️  Warning: Pick ({pick_upper}) matches ticker team but is underdog (+{qualifier_clean}) → bet NO")
                            return 'no'
                        else:
                            # Pick is favored team, subtitle says "Team wins by over X" → bet YES
                            print(f"   ✅ Logic: Pick ({pick_upper}) matches ticker team code ({team_code_in_ticker}), favored → bet YES")
                            return 'yes'
                    else:
                        # Pick is the OTHER team (not in ticker suffix)
                        if is_underdog:
                            # Subtitle says "TickerTeam wins by over X", pick is other team +X → bet NO
                            # (If ticker team wins by over X, other team +X loses)
                            print(f"   ✅ Logic: Pick ({pick_upper}) is OTHER team, ticker team ({team_code_in_ticker}) in subtitle, pick is underdog (+{qualifier_clean}) → bet NO")
                            return 'no'
                        else:
                            # Pick is other team but marked as favorite - check subtitle
                            # If subtitle says "TickerTeam wins by over X" and pick is other team -X, bet YES
                            print(f"   ✅ Logic: Pick ({pick_upper}) is OTHER team but favorite, ticker team ({team_code_in_ticker}) in subtitle → bet YES")
                            return 'yes'
            
            # Fallback to old method if ticker parsing fails
            pick_words = [w for w in pick_upper.split() if len(w) > 3]  # Filter short words like "LA", "NY"
            if not pick_words:
                pick_words = [pick_upper]  # Fallback to full pick if all words are short
            
            # Extract both teams from alert.teams (format: "Team A @ Team B")
            teams_str = alert.teams.upper() if alert.teams else ""
            other_team = None
            if teams_str:
                # Split by @ or vs (use alternation, not character class)
                import re
                # Match @ or VS (case-insensitive), with optional whitespace
                # Pattern: optional whitespace, then @ OR optional whitespace + VS + optional whitespace
                parts = re.split(r'\s*[@]\s*|\s*VS\s*', teams_str, maxsplit=1, flags=re.IGNORECASE)
                if len(parts) == 2:
                    team1 = parts[0].strip()
                    team2 = parts[1].strip()
                    print(f"   Debug: teams_str='{teams_str}', team1='{team1}', team2='{team2}', pick='{pick_upper}'")
                    # Find which team is NOT the pick
                    # Check if pick matches team1
                    pick_matches_team1 = (pick_upper in team1 or 
                                         any(word in team1 for word in pick_words) or
                                         any(word in pick_upper for word in team1.split() if len(word) > 2))
                    # Check if pick matches team2
                    pick_matches_team2 = (pick_upper in team2 or 
                                         any(word in team2 for word in pick_words) or
                                         any(word in pick_upper for word in team2.split() if len(word) > 2))
                    
                    if pick_matches_team1:
                        other_team = team2
                        print(f"   Debug: Pick matches team1, other_team='{other_team}'")
                    elif pick_matches_team2:
                        other_team = team1
                        print(f"   Debug: Pick matches team2, other_team='{other_team}'")
                    else:
                        # Fallback: if we can't determine, try to find team name in subtitle
                        print(f"   Debug: Could not match pick to teams, trying subtitle matching...")
                        # Check which team name appears in the subtitle
                        if any(word in yes_subtitle for word in team1.split() if len(word) > 2):
                            if not any(word in yes_subtitle for word in team2.split() if len(word) > 2):
                                other_team = team1  # team1 is in subtitle, so other_team is team2
                        elif any(word in yes_subtitle for word in team2.split() if len(word) > 2):
                            if not any(word in yes_subtitle for word in team1.split() if len(word) > 2):
                                other_team = team2  # team2 is in subtitle, so other_team is team1
            
            if alert.qualifier:
                qualifier_clean = alert.qualifier.replace('+', '').replace('*', '').strip()
                # Check if qualifier is positive (underdog) or negative (favorite)
                is_underdog = alert.qualifier.startswith('+') or (not alert.qualifier.startswith('-') and float(qualifier_clean) > 0)
                is_favorite = alert.qualifier.startswith('-')
                
                # CRITICAL: Handle negative spreads (e.g., -10.5 means team is favored by 10.5)
                # Kalshi shows "Team wins by over X Points" for favored teams
                # Feed shows "-X" for favored teams
                try:
                    qualifier_float = float(qualifier_clean)
                    # For negative spreads, convert to positive for matching
                    line_to_match = abs(qualifier_float) if qualifier_float < 0 else qualifier_float
                    qualifier_clean = str(line_to_match)
                except:
                    pass
                
                # Check if subtitle mentions the pick team winning
                pick_in_yes = any(word in yes_subtitle for word in pick_words) or pick_upper in yes_subtitle
                pick_in_no = any(word in no_subtitle for word in pick_words) or pick_upper in no_subtitle
                
                # Check if subtitle mentions the OTHER team winning
                # Use partial matching for team names (e.g., "Toronto Raptors" vs "Toronto")
                other_in_yes = False
                other_in_no = False
                if other_team:
                    # Extract meaningful words (allow 3+ char words, but also include common team name parts)
                    other_words = [w for w in other_team.split() if len(w) >= 3]  # Changed from > 3 to >= 3
                    if not other_words:
                        other_words = [other_team]
                    
                    # Common team name abbreviations/nicknames mapping
                    team_abbrev_map = {
                        'UCONN': ['CONNECTICUT', 'CONN'],
                        'CONNECTICUT': ['UCONN', 'CONN'],
                        'CONN': ['UCONN', 'CONNECTICUT'],
                        'NORTH CAROLINA': ['UNC', 'NORTH CAROLINA ST', 'NC STATE', 'NC ST'],
                        'UNC': ['NORTH CAROLINA', 'NORTH CAROLINA ST', 'NC STATE', 'NC ST'],
                        'NC STATE': ['NORTH CAROLINA ST', 'NORTH CAROLINA', 'UNC'],
                        'NORTH CAROLINA ST': ['NC STATE', 'NORTH CAROLINA', 'UNC'],
                        'NC ST': ['NC STATE', 'NORTH CAROLINA ST', 'NORTH CAROLINA', 'UNC'],
                    }
                    
                    # Get all possible variations of the team name
                    other_variations = [other_team]
                    other_upper = other_team.upper()
                    if other_upper in team_abbrev_map:
                        other_variations.extend(team_abbrev_map[other_upper])
                    # Also check if any word matches
                    for word in other_words:
                        word_upper = word.upper()
                        if word_upper in team_abbrev_map:
                            other_variations.extend(team_abbrev_map[word_upper])
                    
                    # Also try matching without filtering (for cases like "San Antonio" where "San" is important)
                    # Make sure all variations are uppercase for matching (subtitles are already uppercase)
                    other_variations_upper = [v.upper() for v in other_variations]
                    other_in_yes = (
                        any(word in yes_subtitle for word in other_words) or 
                        other_team in yes_subtitle or
                        any(word in yes_subtitle for word in other_team.split()) or  # Check all words, not just filtered
                        any(variation in yes_subtitle for variation in other_variations_upper)  # Check abbreviations (case-insensitive)
                    )
                    other_in_no = (
                        any(word in no_subtitle for word in other_words) or 
                        other_team in no_subtitle or
                        any(word in no_subtitle for word in other_team.split()) or  # Check all words, not just filtered
                        any(variation in no_subtitle for variation in other_variations_upper)  # Check abbreviations (case-insensitive)
                    )
                    print(f"   Debug: other_team='{other_team}', other_words={other_words}, other_variations={other_variations}, other_in_yes={other_in_yes}, other_in_no={other_in_no}")
                
                line_match_yes = qualifier_clean in yes_subtitle
                line_match_no = qualifier_clean in no_subtitle
                
                # Handle case where both subtitles are identical (Kalshi data issue)
                both_same = yes_subtitle == no_subtitle
                
                if 'WINS BY' in yes_subtitle or 'WINS BY' in no_subtitle:
                    # Case 1: Pick team is in subtitle winning by X
                    # Example: Pick="Atlanta", Market="Atlanta wins by over 6.5" → bet YES
                    if (pick_in_yes and line_match_yes) or (both_same and pick_in_yes and line_match_yes):
                        return 'yes'
                    if (pick_in_no and line_match_no) or (both_same and pick_in_no and line_match_no):
                        return 'no'
                    
                    # Case 2: OTHER team is in subtitle winning by X
                    # Example: Pick="Atlanta +6.5" (underdog), Market="Toronto wins by over 6.5"
                    # - YES = Toronto wins by over 6.5 (Toronto -6.5) → bet NO (Atlanta +6.5 wins)
                    # - NO = Toronto does NOT win by over 6.5 (Atlanta +6.5) → bet NO (Atlanta +6.5 wins)
                    # CRITICAL: If other team is winning by over X, and pick is underdog (+X), bet NO
                    print(f"   Debug: other_in_yes={other_in_yes}, line_match_yes={line_match_yes}, both_same={both_same}, is_underdog={is_underdog}")
                    if (other_in_yes and line_match_yes) or (both_same and other_in_yes and line_match_yes):
                        if is_underdog:
                            # Other team winning by over X means pick (underdog) loses → bet NO
                            print(f"   ✅ Logic: Other team ({other_team}) in YES subtitle, pick is underdog (+{qualifier_clean}) → bet NO")
                            return 'no'
                        elif is_favorite:
                            # This shouldn't happen (pick is favorite but other team is in subtitle)
                            # But if it does, pick winning by over X → bet YES
                            return 'yes'
                    
                    # Case 3: Both subtitles same (Kalshi data issue) - use logic based on pick
                    if both_same and line_match_yes:
                        print(f"   Debug: Both subtitles same, checking underdog logic...")
                        print(f"   Debug: is_underdog={is_underdog}, other_in_yes={other_in_yes}, other_team='{other_team}'")
                        
                        # CRITICAL FIX: If both subtitles say "Other Team wins by over X" and pick is underdog (+X)
                        # Then we bet NO (because if other team wins by over X, pick +X loses)
                        # Check if subtitle contains any team name from the teams string
                        if teams_str:
                            parts = re.split(r'\s*[@|VS]\s*', teams_str, maxsplit=1)
                            if len(parts) == 2:
                                team1_words = [w for w in parts[0].strip().split() if len(w) > 2]
                                team2_words = [w for w in parts[1].strip().split() if len(w) > 2]
                                
                                # Check which team is mentioned in subtitle
                                team1_in_subtitle = any(word in yes_subtitle for word in team1_words)
                                team2_in_subtitle = any(word in yes_subtitle for word in team2_words)
                                
                                # Check which team is the pick
                                pick_is_team1 = any(word in pick_upper for word in team1_words) or pick_upper in parts[0].strip()
                                pick_is_team2 = any(word in pick_upper for word in team2_words) or pick_upper in parts[1].strip()
                                
                                print(f"   Debug: team1_in_subtitle={team1_in_subtitle}, team2_in_subtitle={team2_in_subtitle}")
                                print(f"   Debug: pick_is_team1={pick_is_team1}, pick_is_team2={pick_is_team2}")
                                
                                # If OTHER team (not pick) is in subtitle and pick is underdog, bet NO
                                if is_underdog:
                                    if (team1_in_subtitle and pick_is_team2) or (team2_in_subtitle and pick_is_team1):
                                        print(f"   ✅ Logic: Both subtitles same, OTHER team in subtitle, pick is underdog (+{qualifier_clean}) → bet NO")
                                        return 'no'
                                    elif (team1_in_subtitle and pick_is_team1) or (team2_in_subtitle and pick_is_team2):
                                        # Pick team is in subtitle, but this shouldn't happen for underdog spreads
                                        # If it does, we'd bet YES (pick wins by over X)
                                        print(f"   ⚠️  Logic: Both subtitles same, PICK team in subtitle, pick is underdog (+{qualifier_clean}) → bet YES (unusual)")
                                        return 'yes'
                        
                        # Fallback to original logic
                        if is_underdog and other_in_yes:
                            print(f"   ✅ Logic: Both subtitles same, other team ({other_team}) in subtitle, pick is underdog (+{qualifier_clean}) → bet NO")
                            return 'no'
                        # If pick is favorite and pick is in subtitle, bet YES
                        elif is_favorite and pick_in_yes:
                            return 'yes'
                        # Final fallback: if other team in subtitle and pick is underdog, bet NO
                        elif other_in_yes and is_underdog:
                            print(f"   ✅ Logic: Fallback - other team ({other_team}) in subtitle, pick is underdog (+{qualifier_clean}) → bet NO")
                            return 'no'
        
        # For Moneyline - SIMPLE LOGIC: Use ticker suffix to determine side
        if market_type_lower == 'moneyline':
            # CRITICAL: Kalshi creates TWO separate markets for moneyline:
            # - KXNHLGAME-26JAN11CBJUTA-CBJ = Columbus's market (YES=Columbus wins, NO=Utah wins)
            # - KXNHLGAME-26JAN11CBJUTA-UTA = Utah's market (YES=Utah wins, NO=Columbus wins)
            # The ticker suffix tells us which team's market we're in
            # If suffix matches pick → bet YES (this is pick team's market)
            # If suffix doesn't match pick → bet NO (this is opponent's market, NO on opponent = YES on pick)
            
            print(f"   [MONEYLINE] pick='{pick_upper}', ticker='{ticker}'")
            print(f"   [MONEYLINE] YES subtitle='{yes_subtitle}', NO subtitle='{no_subtitle}'")
            
            # Extract ticker suffix (e.g., "CBJ" from "KXNHLGAME-26JAN11CBJUTA-CBJ")
            ticker_suffix = None
            if ticker and '-' in ticker:
                ticker_parts = ticker.split('-')
                if len(ticker_parts) >= 2:
                    ticker_suffix = ticker_parts[-1].upper()  # Last part (e.g., "CBJ" or "UTA")
            
            if not ticker_suffix:
                print(f"   ❌ [MONEYLINE] Could not extract ticker suffix from '{ticker}'")
                return None
            
            # Map team names to possible ticker suffixes
            # Start with static comprehensive mappings for all NBA, NHL, NFL, MLB teams
            team_code_map = {
                # NHL - All 32 teams
                'ANAHEIM DUCKS': ['ANA'], 'ANAHEIM': ['ANA'],
                'BOSTON BRUINS': ['BOS'], 'BOSTON': ['BOS'],
                'BUFFALO SABRES': ['BUF'], 'BUFFALO': ['BUF'],
                'CALGARY FLAMES': ['CGY'], 'CALGARY': ['CGY'],
                'CAROLINA HURRICANES': ['CAR'], 'CAROLINA': ['CAR'],
                'CHICAGO BLACKHAWKS': ['CHI'], 'CHICAGO': ['CHI'],
                'COLORADO AVALANCHE': ['COL'], 'COLORADO': ['COL'],
                'COLUMBUS BLUE JACKETS': ['CBJ'], 'COLUMBUS': ['CBJ'],
                'DALLAS STARS': ['DAL'], 'DALLAS': ['DAL'],
                'DETROIT RED WINGS': ['DET'], 'DETROIT': ['DET'],
                'EDMONTON OILERS': ['EDM'], 'EDMONTON': ['EDM'],
                'FLORIDA PANTHERS': ['FLA'], 'FLORIDA': ['FLA'],
                'LOS ANGELES KINGS': ['LAK'], 'LOS ANGELES': ['LAK'],
                'MINNESOTA WILD': ['MIN'], 'MINNESOTA': ['MIN'],
                'MONTREAL CANADIENS': ['MTL'], 'MONTREAL': ['MTL'],
                'NASHVILLE PREDATORS': ['NSH'], 'NASHVILLE': ['NSH', 'NASH'], 'NASH': ['NSH'],
                'NEW JERSEY DEVILS': ['NJD', 'NJ'], 'NEW JERSEY': ['NJD', 'NJ'],
                'NEW YORK ISLANDERS': ['NYI'],
                'NEW YORK RANGERS': ['NYR'],
                'OTTAWA SENATORS': ['OTT'], 'OTTAWA': ['OTT'],
                'PHILADELPHIA FLYERS': ['PHI'], 'PHILADELPHIA': ['PHI'],
                'PITTSBURGH PENGUINS': ['PIT'], 'PITTSBURGH': ['PIT'],
                'SAN JOSE SHARKS': ['SJS', 'SJ'], 'SAN JOSE': ['SJS', 'SJ'], 'SHARKS': ['SJS', 'SJ'],
                'SEATTLE KRAKEN': ['SEA'], 'SEATTLE': ['SEA'],
                'ST. LOUIS BLUES': ['STL'], 'ST LOUIS': ['STL'], 'SAINT LOUIS': ['STL'],
                'TAMPA BAY LIGHTNING': ['TBL'], 'TAMPA BAY': ['TBL'], 'TAMPA': ['TBL'],
                'TORONTO MAPLE LEAFS': ['TOR'], 'TORONTO': ['TOR'],
                'UTAH MAMMOTH': ['UTA'], 'UTAH': ['UTA'], 'UTAH HOCKEY CLUB': ['UTA'],
                'VANCOUVER CANUCKS': ['VAN'], 'VANCOUVER': ['VAN'],
                'VEGAS GOLDEN KNIGHTS': ['VGK'], 'VEGAS': ['VGK'],
                'WASHINGTON CAPITALS': ['WSH'], 'WASHINGTON': ['WSH', 'WAS'], 'WAS': ['WSH'],
                'WINNIPEG JETS': ['WPG'], 'WINNIPEG': ['WPG'],
                # NBA - All 30 teams
                'ATLANTA HAWKS': ['ATL'], 'ATLANTA': ['ATL'],
                'BROOKLYN NETS': ['BKN'], 'BROOKLYN': ['BKN'],
                'BOSTON CELTICS': ['BOS'], 'BOSTON': ['BOS'],
                'CHARLOTTE HORNETS': ['CHA'], 'CHARLOTTE': ['CHA'],
                'CHICAGO BULLS': ['CHI'], 'CHICAGO': ['CHI'],
                'CLEVELAND CAVALIERS': ['CLE'], 'CLEVELAND': ['CLE'],
                'DALLAS MAVERICKS': ['DAL'], 'DALLAS': ['DAL'],
                'DENVER NUGGETS': ['DEN'], 'DENVER': ['DEN'],
                'DETROIT PISTONS': ['DET'], 'DETROIT': ['DET'],
                'GOLDEN STATE WARRIORS': ['GSW'], 'GOLDEN STATE': ['GSW'], 'GS': ['GSW'],
                'HOUSTON ROCKETS': ['HOU'], 'HOUSTON': ['HOU'],
                'INDIANA PACERS': ['IND'], 'INDIANA': ['IND'],
                'LOS ANGELES CLIPPERS': ['LAC'], 'CLIPPERS': ['LAC'],
                'LOS ANGELES LAKERS': ['LAL'], 'LAKERS': ['LAL'],
                'MEMPHIS GRIZZLIES': ['MEM'], 'MEMPHIS': ['MEM'],
                'MIAMI HEAT': ['MIA'], 'MIAMI': ['MIA'],
                'MILWAUKEE BUCKS': ['MIL'], 'MILWAUKEE': ['MIL'],
                'MINNESOTA TIMBERWOLVES': ['MIN'], 'MINNESOTA': ['MIN'], 'MINN': ['MIN'],
                'NEW ORLEANS PELICANS': ['NOP'], 'NEW ORLEANS': ['NOP'], 'NO': ['NOP'],
                'NEW YORK KNICKS': ['NYK'], 'NEW YORK': ['NYK'], 'KNICKS': ['NYK'],
                'OKLAHOMA CITY THUNDER': ['OKC'], 'OKLAHOMA CITY': ['OKC'],
                'ORLANDO MAGIC': ['ORL'], 'ORLANDO': ['ORL'],
                'PHILADELPHIA 76ERS': ['PHI'], 'PHILADELPHIA': ['PHI'], '76ERS': ['PHI'],
                'PHOENIX SUNS': ['PHX'], 'PHOENIX': ['PHX'],
                'PORTLAND TRAIL BLAZERS': ['POR'], 'PORTLAND': ['POR'], 'TRAIL BLAZERS': ['POR'],
                'SACRAMENTO KINGS': ['SAC'], 'SACRAMENTO': ['SAC'],
                'SAN ANTONIO SPURS': ['SAS'], 'SAN ANTONIO': ['SAS'], 'SA': ['SAS'],
                'TORONTO RAPTORS': ['TOR'], 'TORONTO': ['TOR'],
                'UTAH JAZZ': ['UTA'], 'UTAH': ['UTA'],
                'WASHINGTON WIZARDS': ['WAS'], 'WASHINGTON': ['WAS', 'WSH'], 'WSH': ['WAS'],
                # NFL - All 32 teams
                'ARIZONA CARDINALS': ['ARI'], 'ARIZONA': ['ARI'],
                'ATLANTA FALCONS': ['ATL'], 'ATLANTA': ['ATL'],
                'BALTIMORE RAVENS': ['BAL'], 'BALTIMORE': ['BAL'],
                'BUFFALO BILLS': ['BUF'], 'BUFFALO': ['BUF'],
                'CAROLINA PANTHERS': ['CAR'], 'CAROLINA': ['CAR'],
                'CHICAGO BEARS': ['CHI'], 'CHICAGO': ['CHI'],
                'CINCINNATI BENGALS': ['CIN'], 'CINCINNATI': ['CIN'],
                'CLEVELAND BROWNS': ['CLE'], 'CLEVELAND': ['CLE'],
                'DALLAS COWBOYS': ['DAL'], 'DALLAS': ['DAL'],
                'DENVER BRONCOS': ['DEN'], 'DENVER': ['DEN'],
                'DETROIT LIONS': ['DET'], 'DETROIT': ['DET'],
                'GREEN BAY PACKERS': ['GB'], 'GREEN BAY': ['GB'],
                'HOUSTON TEXANS': ['HOU'], 'HOUSTON': ['HOU'],
                'INDIANAPOLIS COLTS': ['IND'], 'INDIANAPOLIS': ['IND'],
                'JACKSONVILLE JAGUARS': ['JAX'], 'JACKSONVILLE': ['JAX'],
                'KANSAS CITY CHIEFS': ['KC'], 'KANSAS CITY': ['KC'],
                'LAS VEGAS RAIDERS': ['LV'], 'LAS VEGAS': ['LV'], 'RAIDERS': ['LV'],
                'LOS ANGELES CHARGERS': ['LAC'], 'LOS ANGELES C': ['LAC'], 'CHARGERS': ['LAC'],
                'LOS ANGELES RAMS': ['LAR'], 'LOS ANGELES R': ['LAR'], 'RAMS': ['LAR'],
                'MIAMI DOLPHINS': ['MIA'], 'MIAMI': ['MIA'],
                'MINNESOTA VIKINGS': ['MIN'], 'MINNESOTA': ['MIN'],
                'NEW ENGLAND PATRIOTS': ['NE'], 'NEW ENGLAND': ['NE'], 'PATRIOTS': ['NE'],
                'NEW ORLEANS SAINTS': ['NO'], 'NEW ORLEANS': ['NO'],
                'NEW YORK GIANTS': ['NYG'], 'NEW YORK G': ['NYG'], 'GIANTS': ['NYG'],
                'NEW YORK JETS': ['NYJ'], 'NEW YORK J': ['NYJ'], 'JETS': ['NYJ'],
                'PHILADELPHIA EAGLES': ['PHI'], 'PHILADELPHIA': ['PHI'], 'EAGLES': ['PHI'],
                'PITTSBURGH STEELERS': ['PIT'], 'PITTSBURGH': ['PIT'], 'STEELERS': ['PIT'],
                'SAN FRANCISCO 49ERS': ['SF'], 'SAN FRANCISCO': ['SF'], '49ERS': ['SF'],
                'SEATTLE SEAHAWKS': ['SEA'], 'SEATTLE': ['SEA'], 'SEAHAWKS': ['SEA'],
                'TAMPA BAY BUCCANEERS': ['TB'], 'TAMPA BAY': ['TB'], 'BUCCANEERS': ['TB'],
                'TENNESSEE TITANS': ['TEN'], 'TENNESSEE': ['TEN'], 'TITANS': ['TEN'],
                'WASHINGTON COMMANDERS': ['WAS'], 'WASHINGTON': ['WAS', 'WSH'], 'WSH': ['WAS'], 'COMMANDERS': ['WAS'],
                # MLB - All 30 teams
                'ATLANTA BRAVES': ['ATL'], 'ATLANTA': ['ATL'],
                'BALTIMORE ORIOLES': ['BAL'], 'BALTIMORE': ['BAL'],
                'BOSTON RED SOX': ['BOS'], 'BOSTON': ['BOS'], 'RED SOX': ['BOS'],
                'CHICAGO CUBS': ['CHC'], 'CHICAGO C': ['CHC'], 'CUBS': ['CHC'],
                'CHICAGO WHITE SOX': ['CWS', 'CHW'], 'CHICAGO W': ['CWS', 'CHW'], 'WHITE SOX': ['CWS', 'CHW'],
                'CINCINNATI REDS': ['CIN'], 'CINCINNATI': ['CIN'],
                'CLEVELAND GUARDIANS': ['CLE'], 'CLEVELAND': ['CLE'],
                'COLORADO ROCKIES': ['COL'], 'COLORADO': ['COL'],
                'DETROIT TIGERS': ['DET'], 'DETROIT': ['DET'],
                'HOUSTON ASTROS': ['HOU'], 'HOUSTON': ['HOU'],
                'KANSAS CITY ROYALS': ['KCR', 'KC'], 'KANSAS CITY': ['KCR', 'KC'], 'ROYALS': ['KCR', 'KC'],
                'LOS ANGELES ANGELS': ['LAA', 'ANA'], 'LOS ANGELES A': ['LAA', 'ANA'], 'ANGELS': ['LAA', 'ANA'],
                'LOS ANGELES DODGERS': ['LAD'], 'LOS ANGELES D': ['LAD'], 'DODGERS': ['LAD'],
                'MIAMI MARLINS': ['MIA'], 'MIAMI': ['MIA'],
                'MILWAUKEE BREWERS': ['MIL'], 'MILWAUKEE': ['MIL'],
                'MINNESOTA TWINS': ['MIN'], 'MINNESOTA': ['MIN'],
                'NEW YORK METS': ['NYM'], 'NEW YORK M': ['NYM'], 'METS': ['NYM'],
                'NEW YORK YANKEES': ['NYY'], 'NEW YORK Y': ['NYY'], 'YANKEES': ['NYY'],
                'OAKLAND ATHLETICS': ['OAK'], 'OAKLAND': ['OAK'], 'ATHLETICS': ['OAK'],
                'PHILADELPHIA PHILLIES': ['PHI'], 'PHILADELPHIA': ['PHI'], 'PHILLIES': ['PHI'],
                'PITTSBURGH PIRATES': ['PIT'], 'PITTSBURGH': ['PIT'], 'PIRATES': ['PIT'],
                'SAN DIEGO PADRES': ['SDP', 'SD'], 'SAN DIEGO': ['SDP', 'SD'], 'PADRES': ['SDP', 'SD'],
                'SAN FRANCISCO GIANTS': ['SFG', 'SF'], 'SAN FRANCISCO': ['SFG', 'SF'], 'GIANTS': ['SFG', 'SF'],
                'SEATTLE MARINERS': ['SEA'], 'SEATTLE': ['SEA'], 'MARINERS': ['SEA'],
                'ST. LOUIS CARDINALS': ['STL'], 'ST LOUIS': ['STL'], 'SAINT LOUIS': ['STL'], 'CARDINALS': ['STL'],
                'TAMPA BAY RAYS': ['TBR', 'TB'], 'TAMPA BAY': ['TBR', 'TB'], 'RAYS': ['TBR', 'TB'],
                'TEXAS RANGERS': ['TEX'], 'TEXAS': ['TEX'], 'RANGERS': ['TEX'],
                'TORONTO BLUE JAYS': ['TOR'], 'TORONTO': ['TOR'], 'BLUE JAYS': ['TOR'],
                'WASHINGTON NATIONALS': ['WSH'], 'WASHINGTON': ['WSH', 'WAS'], 'NATIONALS': ['WSH'],
# College Basketball (NCAAB) & College Football (NCAAF) - Dynamically Extracted
                # College Basketball (NCAAB) & College Football (NCAAF) - Dynamically Extracted
                'ABILENE CHRISTIAN': ['AC'],
                'AIR FORCE': ['AFA'],
                'AKRON': ['AKR'],
                'ALABAMA': ['ALA'],
                'ALABAMA A&M': ['AAMU'],
                'ALABAMA ST': ['ALST'],
                'ALCORN ST': ['ALCN'],
                'AMERICAN': ['AMER'],
                'APPALACHIAN ST': ['APP'],
                'ARIZONA': ['ARIZ'],
                'ARIZONA ST': ['ASU'],
                'ARKANSAS': ['ARK'],
                'ARKANSAS ST': ['ARST'],
                'ARKANSAS-PINE BLUFF': ['ARPB'],
                'AUBURN': ['AUB'],
                'AUSTIN PEAY': ['PEAY'],
                'BALL ST': ['BALL'],
                'BAYLOR': ['BAY'],
                'BELLARMINE': ['BELL'],
                'BELMONT': ['BEL'],
                'BETHUNE-COOKMAN': ['COOK'],
                'BINGHAMTON': ['BING'],
                'BOISE ST': ['BSU'],
                'BOSTON COLLEGE': ['BC'],
                'BOSTON UNIVERSITY': ['BU'],
                'BOWLING GREEN': ['BGSU'],
                'BRADLEY': ['BRAD'],
                'BROWN': ['BRWN'],
                'BRYANT': ['BRY'],
                'BRYANT & STRATTON': ['BRST'],
                'BUCKNELL': ['BUCK'],
                'BUTLER': ['BUT'],
                'BUFFALO': ['BUFF'],
                'CAL POLY': ['CP'],
                'CAL STATE BAKERSFIELD': ['CSB'],
                'CAL STATE FULLERTON': ['CSF'],
                'CAL STATE NORTHRIDGE': ['CSN'],
                'CALIFORNIA': ['CAL'],
                'CALIFORNIA BAPTIST': ['CBU'],
                'CAMPBELL': ['CAMP'],
                'CANISIUS': ['CAN'],
                'IONA': ['IONA'],
                'CENTRAL ARKANSAS': ['CARK'],
                'CENTRAL CONNECTICUT ST': ['CCSU'],
                'CENTRAL MICHIGAN': ['CMU'],
                'CHARLESTON': ['COFC'],
                'CHARLESTON SOUTHERN': ['CHSO'],
                'CHARLOTTE': ['CHAR'],
                'CHATTANOOGA': ['CHAT'],
                'CHICAGO ST': ['CHS'],
                'CINCINNATI': ['CIN'],
                'CLEMSON': ['CLEM'],
                'CLEVELAND ST': ['CLEV'],
                'COASTAL CAROLINA': ['CCAR'],
                'COLGATE': ['COLG'],
                'COLORADO': ['COLO'],
                'COLORADO ST': ['CSU'],
                'COLUMBIA': ['CLMB'],
                'COPPIN ST': ['COPP'],
                'CORNELL': ['COR'],
                'CREIGHTON': ['CREI'],
                'DARTMOUTH': ['DART'],
                'DAVIDSON': ['DAV'],
                'DAYTON': ['DAY'],
                'DELAWARE': ['DEL'],
                'DELAWARE ST': ['DSU'],
                'DENVER': ['DEN'],
                'DEPAUL': ['DEP'],
                'DETROIT MERCY': ['DET'],
                'DRAKE': ['DRKE'],
                'DREXEL': ['DREX'],
                'DUQUESNE': ['DUQ'],
                'EAST CAROLINA': ['ECU'],
                'EAST TENNESSEE ST': ['ETSU'],
                'EAST TEXAS A&M': ['ETAM'],
                'EASTERN ILLINOIS': ['EIU'],
                'EASTERN KENTUCKY': ['EKY', 'EKU'],
                'EASTERN MICHIGAN': ['EMU'],
                'EASTERN WASHINGTON': ['EWU'],
                'EVANSVILLE': ['EVAN'],
                'FAIRFIELD': ['FAIR'],
                'FLORIDA': ['FLA'],
                'FLORIDA A&M': ['FAMU'],
                'FLORIDA ATLANTIC': ['FAU'],
                'FLORIDA GULF COAST': ['FGCU'],
                'FLORIDA INTERNATIONAL': ['FIU'],
                'FLORIDA ST': ['FSU'],
                'FORDHAM': ['FOR'],
                'FRESNO ST': ['FRES'],
                'FURMAN': ['FUR'],
                'GEORGE MASON': ['GMU'],
                'GEORGE WASHINGTON': ['GW'],
                'GEORGETOWN': ['GTWN'],
                'GEORGIA': ['UGA'],
                'GEORGIA SOUTHERN': ['GASO'],
                'GEORGIA ST': ['GAST'],
                'GEORGIA TECH': ['GT'],
                'GONZAGA': ['GONZ'],
                'GRAMBLING ST': ['GRAM'],
                'GRAND CANYON': ['GC'],
                'GREEN BAY': ['GB'],
                'HAMPTON': ['HAMP'],
                'HARVARD': ['HARV'],
                'HAWAI\'I': ['HAW'],
                'HIGH POINT': ['HP'],
                'HOFSTRA': ['HOF'],
                'HOLY CROSS': ['HC'],
                'HOUSTON': ['HOU'],
                'HOUSTON CHRISTIAN': ['HCU'],
                'HOWARD': ['HOW'],
                'IDAHO': ['IDHO'],
                'IDAHO ST': ['IDST'],
                'ILLINOIS': ['ILL'],
                'ILLINOIS ST': ['ILST'],
                'INCARNATE WORD': ['IW'],
                'INDIANA': ['IND'],
                'INDIANA ST': ['INST'],
                'IOWA': ['IOWA'],
                'IOWA ST': ['ISU'],
                'IU INDY': ['IUIN'],
                'JACKSON ST': ['JKST'],
                'JACKSONVILLE': ['JAC'],
                'JACKSONVILLE ST': ['JVST'],
                'JAMES MADISON': ['JMU'],
                'KANSAS': ['KU'], 'KU': ['KU'],
                'KANSAS CITY': ['UMKC'],
                'KANSAS ST': ['KSU'],
                'KENNESAW ST': ['KENN'],
                'KENT ST': ['KENT'],
                'KENTUCKY': ['UK'],
                'LA SALLE': ['LAS'],
                'LAFAYETTE': ['LAF'],
                'LAMAR': ['LAM'],
                'LE MOYNE': ['LMC'],
                'LEHIGH': ['LEH'],
                'LIBERTY': ['LIB'],
                'LINDENWOOD': ['LINW'],
                'LIPSCOMB': ['LIP'],
                'LITTLE ROCK': ['UALR'],
                'LONG BEACH ST': ['LBSU'],
                'LONGWOOD': ['LONG'],
                'LOUISIANA': ['ULL'],
                'LOUISIANA ST': ['LSU'],
                'LOUISIANA TECH': ['LT'],
                'LSU': ['LSU'],
                'LOUISIANA-MONROE': ['ULM'],
                'LOUISVILLE': ['LOU'],
                'LOYOLA CHICAGO': ['LCHI'],
                'LOYOLA MARYLAND': ['LMD'],
                'LOYOLA MARYMOUNT': ['LMU'],
                'MAINE': ['ME'],
                'MANHATTAN': ['MAN'],
                'MARIST': ['MRST'],
                'MARQUETTE': ['MARQ'],
                'MARSHALL': ['MRSH'],
                'MARYLAND': ['MD'],
                'MARYLAND-EASTERN SHORE': ['UMES'],
                'MCNEESE': ['MCNS'], 'MCNEESE STATE': ['MCNS'],
                'MEMPHIS': ['MEM'],
                'MERCER': ['MER'],
                'MERCYHURST': ['MHU'],
                'MERRIMACK': ['MRMK'],
                'MIAMI (FL)': ['MIA'],
                'MIAMI (OH)': ['MOH'],
                'MICHIGAN': ['MICH'],
                'MICHIGAN ST': ['MSU'],
                'MIDDLE TENNESSEE': ['MTU'],
                'MILWAUKEE': ['MILW'],
                'MINNESOTA': ['MINN'],
                'MISSISSIPPI ST': ['MSST'],
                'MISSISSIPPI VALLEY ST': ['MVSU'],
                'MISSOURI': ['MIZZ'],
                'MISSOURI ST': ['MOSU'],
                'MONMOUTH': ['MONM'],
                'MONTANA': ['MONT'],
                'MONTANA ST': ['MTST'],
                'MOREHEAD ST': ['MORE'],
                'MORGAN ST': ['MORG'],
                'MOUNT ST. MARY\'S': ['MSM'],
                'MURRAY ST': ['MURR'],
                'NEBRASKA': ['NEB'],
                'NEVADA': ['NEV'],
                'NEW HAMPSHIRE': ['UNH'],
                'NEW HAVEN': ['NHC'],
                'NEW MEXICO': ['UNM'],
                'NEW MEXICO ST': ['NMSU'],
                'NEW ORLEANS': ['UNO'],
                'NIAGARA': ['NIAG'],
                'NICHOLLS ST': ['NICH'], 'NICHOLLS': ['NICH'], 'NICHOL': ['NICH'],
                'NORFOLK ST': ['NORF'],
                'NORTH ALABAMA': ['UNA'],
                'NORTH CAROLINA': ['UNC'],
                'NORTH CAROLINA CENTRAL': ['NCCU'],
                'NORTH CAROLINA ST': ['NCST'],
                'NORTH DAKOTA': ['UND'],
                'NORTH DAKOTA ST': ['NDSU'],
                'NORTH FLORIDA': ['UNF'],
                'NORTH TEXAS': ['UNT'],
                'NORTHEASTERN': ['NE'],
                'NORTHERN ARIZONA': ['NAU'],
                'NORTHERN COLORADO': ['UNCO'],
                'NORTHERN ILLINOIS': ['NIU'],
                'NORTHERN IOWA': ['UNI'],
                'NORTHERN KENTUCKY': ['NKU'],
                'NORTHWESTERN': ['NW'],
                'NORTHWESTERN ST': ['NWST'], 'NORTHWESTERN STATE': ['NWST'],
                'NOTRE DAME': ['ND'],
                'OAKLAND': ['OAK'],
                'OHIO ST': ['OSU'],
                'OKLAHOMA': ['OKLA'],
                'OKLAHOMA ST': ['OKST'],
                'OLD DOMINION': ['ODU'],
                'OLE MISS': ['MISS'],
                'OMAHA': ['NEOM'],
                'ORAL ROBERTS': ['ORU'],
                'OREGON': ['ORE'],
                'OREGON ST': ['ORST'],
                'PACIFIC': ['PAC'],
                'PENN ST': ['PSU'],
                'PEPPERDINE': ['PEPP'],
                'PITTSBURGH': ['PITT'],
                'PORTLAND': ['PORT'],
                'PORTLAND ST': ['PRST'],
                'PRAIRIE VIEW A&M': ['PV'],
                'PRESBYTERIAN': ['PRE'],
                'PRINCETON': ['PRIN'],
                'PROVIDENCE': ['PROV'],
                'PURDUE': ['PUR'],
                'PURDUE FORT WAYNE': ['PFW'],
                'QUEENS UNIVERSITY': ['QUC'],
                'QUINNIPIAC': ['QUIN'],
                'RADFORD': ['RAD'],
                'RHODE ISLAND': ['URI'],
                'RICE': ['RICE'],
                'RICHMOND': ['RICH'],
                'RIDER': ['RID'],
                'ROBERT MORRIS': ['RMU'],
                'RUTGERS': ['RUTG'],
                'SACRAMENTO ST': ['SAC'],
                'SACRED HEART': ['SHU'],
                'SAINT JOSEPH\'S': ['JOES'],
                'SAINT LOUIS': ['SLU'],
                'SAINT MARY\'S': ['SMC'],
                'SAINT PETER\'S': ['SPC'],
                'SAM HOUSTON': ['SHSU'],
                'SAMFORD': ['SAM'],
                'SAN DIEGO': ['USD'],
                'SAN DIEGO ST': ['SDSU'],
                'SAN FRANCISCO': ['SF'],
                'SAN JOSE ST': ['SJSU'],
                'SANTA CLARA': ['SCU'],
                'SEATTLE': ['SEA'],
                'SETON HALL': ['HALL'],
                'SIENA': ['SIE'],
                'SOUTH ALABAMA': ['USA'],
                'SOUTH CAROLINA': ['SCAR'],
                'SOUTH CAROLINA ST': ['SCST'],
                'SOUTH DAKOTA': ['SDAK'],
                'SOUTH DAKOTA ST': ['SDST'],
                'SOUTH FLORIDA': ['USF'],
                'SOUTHEAST MISSOURI ST': ['SEMO'],
                'SOUTHEASTERN LOUISIANA': ['SELA'],
                'SOUTHERN CALIFORNIA': ['USC'],
                'SOUTHERN ILLINOIS': ['SIU'],
                'SOUTHERN INDIANA': ['USI'],
                'SOUTHERN MISS': ['USM'],
                'SOUTHERN UNIVERSITY': ['SOU'],
                'SOUTHERN UTAH': ['SUU'],
                'ST. BONAVENTURE': ['SBON'],
                'ST. FRANCIS (PA)': ['SFPA'],
                'ST. JOHN\'S': ['SJU'],
                'ST. THOMAS': ['UST'],
                'STANFORD': ['STAN'],
                'STEPHEN F. AUSTIN': ['SFA'],
                'STETSON': ['STET'],
                'STONEHILL': ['STNH'],
                'STONY BROOK': ['STON'],
                'SYRACUSE': ['SYR'],
                'TENNESSEE': ['TENN'],
                'TENNESSEE ST': ['TNST'],
                'TENNESSEE TECH': ['TNTC'],
                'TENNESSEE-MARTIN': ['UTM'],
                'TEMPLE': ['TEM'],
                'TEXAS': ['TEX'],
                'TEXAS CHRISTIAN': ['TCU'],
                'TCU': ['TCU'],
                'TEXAS A&M': ['TXAM'],
                'TEXAS A&M-CORPUS CHRISTI': ['AMCC'],
                'TEXAS SOUTHERN': ['TXSO'],
                'TEXAS ST': ['TXST'],
                'TEXAS TECH': ['TTU'], 'TTU': ['TTU'],
                'THE CITADEL': ['CIT'],
                'TOLEDO': ['TOL'],
                'TOWSON': ['TOWS'],
                'TULANE': ['TULN'],
                'TULSA': ['TLSA'],
                'UC DAVIS': ['UCD'],
                'UC IRVINE': ['UCI'],
                'UC RIVERSIDE': ['UCRV'],
                'UC SAN DIEGO': ['UCSD'],
                'UC SANTA BARBARA': ['UCSB'],
                'UAB': ['UAB'],
                'UCONN': ['CONN'],
                'USC': ['USC'],
                'UMASS': ['MASS'],
                'UMASS LOWELL': ['MASSL'],
                'UNC ASHEVILLE': ['UNCA'],
                'UNC GREENSBORO': ['UNCG'],
                'UNC WILMINGTON': ['UNCW'],
                'UNIVERSITY AT ALBANY': ['ALBY'],
                'USC UPSTATE': ['SCUS'],
                'UT ARLINGTON': ['UTA'],
                'UT RIO GRANDE VALLEY': ['UTRGV'],
                'UTAH ST': ['USU'],
                'UTAH TECH': ['UTU'],
                'UTAH VALLEY': ['UVU'],
                'UTEP': ['UTEP'],
                'UTSA': ['UTSA'],
                'VALPARAISO': ['VALP'],
                'VANDERBILT': ['VAN'],
                'VILLANOVA': ['VILL'],
                'VIRGINIA': ['UVA'],
                'VIRGINIA TECH': ['VT'],
                'WAGNER': ['WAG'],
                'WAKE FOREST': ['WAKE'],
                'WASHINGTON': ['WASH'],
                'WASHINGTON ST': ['WSU'],
                'WEBER ST': ['WEB'],
                'WEST GEORGIA': ['UWGA'],
                'WEST VIRGINIA': ['WVU'],
                'WESTERN CAROLINA': ['WCU'],
                'WESTERN ILLINOIS': ['WIU'],
                'WESTERN KENTUCKY': ['WKU'],
                'WESTERN MICHIGAN': ['WMU'],
                'WICHITA ST': ['WICH'],
                'WILLIAM & MARY': ['WM'],
                'WINTHROP': ['WIN'],
                'WISCONSIN': ['WIS'],
                'WOFFORD': ['WOF'],
                'WRIGHT ST': ['WRST'],
                'WYOMING': ['WYO'],
                'XAVIER': ['XAV'],
                'YOUNGSTOWN ST': ['YSU'],



            }
            
            # CRITICAL: For spreads, check if pick maps to ticker code using sport-specific team_code_map
            # This is a REVERSE lookup: does pick_upper map to team_code_in_ticker?
            if market_type_lower in ['point spread', 'spread', 'puck line'] and ticker and alert.qualifier:
                # Get sport-specific team code map
                team_code_map = self._get_team_code_map_by_sport(sport)
                # Re-check using sport-specific map (prevents collisions between sports)
                if not pick_matches_ticker_team:
                    # Reverse lookup: check if pick maps to ticker code
                    pick_codes = team_code_map.get(pick_upper, [])
                    if team_code_in_ticker in pick_codes:
                        pick_matches_ticker_team = True
                        print(f"   Debug: REVERSE LOOKUP: Pick '{pick_upper}' maps to '{team_code_in_ticker}' via sport-specific team_code_map ({sport})")
                    
                    # Also check variations of pick (e.g., "CHARGERS" -> "LAC")
                    if not pick_matches_ticker_team:
                        for team_name, codes in team_code_map.items():
                            if team_code_in_ticker in codes:
                                # This team_name maps to our ticker code
                                # Check if pick matches this team_name
                                if team_name in pick_upper or pick_upper in team_name:
                                    pick_matches_ticker_team = True
                                    print(f"   Debug: REVERSE LOOKUP: Pick '{pick_upper}' matches team '{team_name}' which maps to '{team_code_in_ticker}' ({sport})")
                                    break
                                # Also check word-by-word
                                team_words = [w for w in team_name.split() if len(w) > 2]
                                pick_words_check = [w for w in pick_upper.split() if len(w) > 2]
                                if any(tw in pick_upper for tw in team_words) or any(pw in team_name for pw in pick_words_check):
                                    pick_matches_ticker_team = True
                                    print(f"   Debug: REVERSE LOOKUP (word match): Pick '{pick_upper}' matches team '{team_name}' which maps to '{team_code_in_ticker}' ({sport})")
                                    break
            
            # Get sport-specific team code map
            team_code_map = self._get_team_code_map_by_sport(sport)
            
            # For NCAAB, add comprehensive team mappings (sport-specific map is empty for NCAAB)
            if sport == "NCAAB":
                ncaab_mappings = {
                    'NORTHWESTERN STATE': ['NWST'], 'NORTHWESTERN ST': ['NWST'], 'NWST': ['NWST'],
                    'STEPHEN F. AUSTIN': ['SFA'], 'STEPHEN F AUSTIN': ['SFA'], 'SFA': ['SFA'],
                    'NICHOL': ['NICH'], 'NICHOLLS': ['NICH'], 'NICH': ['NICH'],
                    'INCARNATE WORD': ['IW'], 'IW': ['IW'],
                    'TEXAS A&M CORPUS CHRISTI': ['AMCC'], 'TEXAS A & M CORPUS CHRISTI': ['AMCC'], 'AMCC': ['AMCC'],
                    'BETHUNE COOKMAN': ['COOK'], 'BETHUNE-COOKMAN': ['COOK'], 'COOK': ['COOK'],
                    'ALABAMA A&M': ['AAMU'], 'ALABAMA A & M': ['AAMU'], 'AAMU': ['AAMU'],
                    'NEW ORLEANS': ['UNO'], 'UNO': ['UNO'],
                    'TEXAS A&M COMMERCE': ['TAMC'], 'TEXAS A & M COMMERCE': ['TAMC'], 'TAMC': ['TAMC'],
                    'MCNEESE': ['MCNS'], 'MCNEESE STATE': ['MCNS'], 'MCNS': ['MCNS'],
                    'GRAMBLING': ['GRAM'], 'GRAM': ['GRAM'],
                    'ALCORN STATE': ['ALCN'], 'ALCORN ST': ['ALCN'], 'ALCN': ['ALCN'],
                    'SOUTHERN': ['SOU'], 'SOU': ['SOU'],  # Southern University (not Southern Miss)
                    'JACKSON STATE': ['JKST'], 'JACKSON ST': ['JKST'], 'JKST': ['JKST'],
                    'WEBER STATE': ['WEB'], 'WEBER ST': ['WEB'], 'WEB': ['WEB'],
                    'SACRAMENTO STATE': ['SAC'], 'SACRAMENTO ST': ['SAC'], 'SAC': ['SAC'],
                    'MARYLAND-EASTERN SHORE': ['UMES'], 'MARYLAND EASTERN SHORE': ['UMES'], 'UMES': ['UMES'],
                    'FLORIDA ATLANTIC': ['FAU'], 'FAU': ['FAU'],
                    'EAST CAROLINA': ['ECU'], 'ECU': ['ECU'],
                    'QUINNIPIAC': ['QUIN'], 'FAIRFIELD': ['FAIR'],
                    'WICHITA STATE': ['WICH'], 'WICHITA ST': ['WICH'],
                    'TULSA': ['TLSA'], 'NORTH TEXAS': ['UNT'],
                    'SOUTHEASTERN LOUISIANA': ['SELA'], 'EAST TEXAS A&M': ['ETAM'],
                    
                    # Add more as needed
                }
                # Merge NCAAB mappings into team_code_map
                for team_name, codes in ncaab_mappings.items():
                    if team_name not in team_code_map:
                        team_code_map[team_name] = codes
                    else:
                        team_code_map[team_name].extend(codes)
                        team_code_map[team_name] = list(set(team_code_map[team_name]))
            
            # Check if pick matches ticker suffix
            possible_suffixes = team_code_map.get(pick_upper, [])
            
            # Also check if any team name in the map contains the pick (or vice versa)
            for team_name, codes in team_code_map.items():
                if team_name in pick_upper or pick_upper in team_name:
                    possible_suffixes.extend(codes)
            
            # Remove duplicates
            possible_suffixes = list(set(possible_suffixes))
            
            # Check if ticker suffix matches pick
            # CRITICAL: For college teams, be more lenient - check if ticker suffix is abbreviation of pick
            ticker_matches_pick = ticker_suffix in possible_suffixes or ticker_suffix in pick_upper.replace(' ', '')
            
            # ADDITIONAL CHECK: For college teams, use hardcoded team mapping from kalshi_client
            # This handles cases like "UNF" for "NORTH FLORIDA", "INST" for "INDIANA STATE"
            if not ticker_matches_pick and sport == 'NCAAB':
                # Import the team code mapping logic (same as in kalshi_client.build_market_ticker)
                # Check common college team codes
                college_team_codes = {
                    'NORTH FLORIDA': 'UNF',
                    'INDIANA STATE': 'INST',
                    'SOUTH CAROLINA': 'SCAR',
                    'TEXAS TECH': 'TTU',
                    'TEXAS A&M': 'TXAM',
                    'NORTHERN IOWA': 'UNI',
                    'DENVER': 'DEN',
                    'GEORGETOWN': 'GTWN',
                    'HOLY CROSS': 'HC',
                    'STONEHILL': 'STNH',
                    'LE MOYNE': 'LMC',
                    'FAIRLEIGH DICKINSON': 'FDU',
                    'WAGNER': 'WAG',
                    'ARIZONA STATE': 'ASU',
                    'ARIZONA': 'ARIZ',
                    'LSU': 'LSU',
                    'LOUISIANA STATE': 'LSU',
                    'UMASS LOWELL': 'MASSL',
                    'MAINE': 'ME',
                    'CHARLESTON SOUTHERN': 'COFC',
                    'NORTHEASTERN': 'NE',
                'DEPAUL': 'DEP',
                'XAVIER': 'XAV',
                'PRESBYTERIAN': 'PRE',
                'LONG ISLAND': 'LIU',
                'CENTRAL CONNECTICUT STATE': 'CCSU',
                'SAINT JOSEPH\'S': 'JOES',
                'ST JOSEPH\'S': 'JOES',
                'LA SALLE': 'LAS',
                'HOFSTRA': 'HOF',
                'MONMOUTH': 'MON',
                'OHIO': 'OHIO',
                'BUFFALO': 'BUFF',
                'OLD DOMINION': 'ODU',
                'TEXAS STATE': 'TXST',
                'QUEENS': 'QU',
                'BELLARMINE': 'BEL',
                'SOUTHERN INDIANA': 'USI',  # Southern Indiana
                'TENNESSEE STATE': 'TSU',  # Tennessee State
                'TENNESSEE ST': 'TSU',  # Tennessee State (abbreviation)
            }
                
                # Check if pick matches any team in the mapping
                for team_name, code in college_team_codes.items():
                    if team_name in pick_upper or pick_upper in team_name:
                        if ticker_suffix == code:
                            ticker_matches_pick = True
                            print(f"   [MONEYLINE] College team code match: '{ticker_suffix}' = '{code}' for '{team_name}' (pick: '{pick_upper}')")
                            break
                
                # CRITICAL: Also do reverse lookup - if ticker suffix matches a code, check if that code maps to the pick
                # This handles cases where the ticker was built using the mapping (e.g., 'DRAKE' -> 'DRKE')
                if not ticker_matches_pick:
                    for team_name, code in college_team_codes.items():
                        if code == ticker_suffix:
                            # Found the code - check if this team name matches the pick
                            if team_name in pick_upper or pick_upper in team_name or pick_upper == team_name:
                                ticker_matches_pick = True
                                print(f"   [MONEYLINE] Reverse lookup match: Ticker suffix '{ticker_suffix}' maps to '{team_name}' which matches pick '{pick_upper}'")
                                break
                
                # Also check if ticker suffix matches first letters of words
                # e.g., "HC" matches "HOLY CROSS" (H + C from first letters)
                if not ticker_matches_pick:
                    pick_words = pick_upper.split()
                    if len(pick_words) >= 2:
                        # Check if ticker suffix matches first letters (e.g., "HC" from "HOLY CROSS")
                        first_letters = ''.join([w[0] for w in pick_words if w])
                        if ticker_suffix == first_letters or first_letters.startswith(ticker_suffix) or ticker_suffix.startswith(first_letters):
                            ticker_matches_pick = True
                            print(f"   [MONEYLINE] College team abbreviation match: '{ticker_suffix}' matches first letters '{first_letters}' of '{pick_upper}'")
            
            print(f"   [MONEYLINE] Ticker suffix: '{ticker_suffix}', Pick: '{pick_upper}', Possible suffixes: {possible_suffixes}")
            print(f"   [MONEYLINE] Ticker matches pick: {ticker_matches_pick}")
            
            # CRITICAL: Check for Kalshi subtitle bug (both YES and NO say same thing OR both are N/A)
            subtitles_buggy = (yes_subtitle.upper() == no_subtitle.upper() and yes_subtitle) or (yes_subtitle.upper() == 'N/A' and no_subtitle.upper() == 'N/A')
            if subtitles_buggy:
                if yes_subtitle.upper() == 'N/A':
                    print(f"   🚨 KALSHI BUG DETECTED: Both subtitles are N/A - IGNORING subtitles, using ticker '{ticker_suffix}'")
                else:
                    print(f"   🚨 KALSHI BUG DETECTED: Both subtitles identical ('{yes_subtitle}') - IGNORING subtitles, using ticker '{ticker_suffix}'")
                    print(f"   ⚠️  Expected: YES='{yes_subtitle}', NO='[opponent]' but both say '{yes_subtitle}'")
                
                # CRITICAL: Perform reverse lookup - check if ticker suffix matches any team code that maps to the pick name
                # This handles cases like "USI" (ticker) -> "SOUTHERN INDIANA" (pick) where the mapping is correct
                if not ticker_matches_pick:
                    # Get sport-specific team code map
                    team_code_map = self._get_team_code_map_by_sport(sport)
                    
                    # Reverse lookup: Find all team names that map to codes containing the ticker suffix
                    matching_team_names = []
                    for team_name, codes in team_code_map.items():
                        if ticker_suffix in codes:
                            matching_team_names.append(team_name)
                    
                    # Check if pick matches any of the team names that map to this ticker suffix
                    for team_name in matching_team_names:
                        if team_name in pick_upper or pick_upper in team_name:
                            ticker_matches_pick = True
                            print(f"   ✅ [MONEYLINE] Reverse lookup: Ticker suffix '{ticker_suffix}' maps to team '{team_name}' which matches pick '{pick_upper}' → bet YES")
                            break
                
                # CRITICAL: If subtitles are buggy AND ticker suffix is found in pick (even if not exact match), trust the ticker
                # For college teams, ticker suffixes are often abbreviations (HC, FDU, STNH, etc.)
                if not ticker_matches_pick and sport == 'NCAAB':
                    # Check if ticker suffix appears anywhere in pick (e.g., "HC" in "HOLY CROSS", "STNH" in "STONEHILL")
                    pick_no_spaces = pick_upper.replace(' ', '')
                    # Check if ticker suffix matches start of words (e.g., "STNH" = "ST" + "NH" from "STONEHILL")
                    if ticker_suffix in pick_no_spaces or any(ticker_suffix in word for word in pick_words):
                        ticker_matches_pick = True
                        print(f"   [MONEYLINE] College team: Ticker suffix '{ticker_suffix}' found in pick '{pick_upper}' → trusting ticker despite subtitle bug")
                    # Also check if ticker is abbreviation (first letters of words)
                    elif len(pick_words) >= 2:
                        first_letters = ''.join([w[0] for w in pick_words if w])
                        if ticker_suffix.startswith(first_letters) or first_letters.startswith(ticker_suffix):
                            ticker_matches_pick = True
                            print(f"   [MONEYLINE] College team: Ticker suffix '{ticker_suffix}' matches abbreviation of '{pick_upper}' → trusting ticker")
                    # For single-word teams, check if ticker is contained in the word
                    elif len(pick_words) == 1 and ticker_suffix in pick_words[0]:
                        ticker_matches_pick = True
                        print(f"   [MONEYLINE] College team: Ticker suffix '{ticker_suffix}' found in single-word pick '{pick_upper}' → trusting ticker")
            
            if ticker_matches_pick:
                # Ticker suffix matches pick → this is the pick team's market → bet YES
                # CRITICAL: Even if subtitles are wrong (both say same thing), ticker is reliable
                if yes_subtitle.upper() == no_subtitle.upper() and yes_subtitle:
                    print(f"   ✅ [MONEYLINE] Ticker suffix '{ticker_suffix}' matches pick '{pick_upper}' → bet YES (IGNORING buggy subtitles, using ticker)")
                else:
                    print(f"   ✅ [MONEYLINE] Ticker suffix '{ticker_suffix}' matches pick '{pick_upper}' → bet YES (this is pick team's market)")
                return 'yes'
            else:
                # Ticker suffix doesn't match pick → this is the WRONG market
                # We should have matched the other team's market instead
                # Don't bet on this market - return None to reject it
                print(f"   ❌ [MONEYLINE] Ticker suffix '{ticker_suffix}' doesn't match pick '{pick_upper}' → REJECTING (this is the wrong market, should match the other team's market)")
                return None
            
            # OLD LOGIC BELOW - KEPT AS FALLBACK BUT SHOULD NOT BE REACHED
            both_same = yes_subtitle == no_subtitle
            if both_same:
                print(f"   [MONEYLINE] ⚠️  Both subtitles identical (Kalshi bug): '{yes_subtitle}' - using ticker suffix matching")
                
                # Extract ticker suffix (e.g., "NE" from "KXNFLGAME-26JAN11LACNE-NE")
                ticker_parts = ticker.split('-')
                if len(ticker_parts) >= 2:
                    ticker_suffix = ticker_parts[-1].upper()  # Last part (e.g., "NE" or "LAC")
                    
                    # Get sport-specific team code map (prevents collisions between sports)
                    team_code_map = self._get_team_code_map_by_sport(sport)
                    
                    # Check if pick matches ticker suffix
                    pick_upper_clean = pick_upper.replace(' ', '').replace('.', '')
                    possible_suffixes = team_code_map.get(pick_upper, [])
                    
                    # Also try matching pick words to suffixes (check each word in pick)
                    for word in pick_words:
                        if word in team_code_map:
                            possible_suffixes.extend(team_code_map[word])
                    
                    # Also check if any team name in the map contains the pick
                    for team_name, codes in team_code_map.items():
                        if team_name in pick_upper or pick_upper in team_name:
                            possible_suffixes.extend(codes)
                    
                    # Remove duplicates
                    possible_suffixes = list(set(possible_suffixes))
                    
                    # Check if ticker suffix matches pick
                    ticker_matches_pick = ticker_suffix in possible_suffixes or ticker_suffix in pick_upper_clean
                    
                    print(f"   [MONEYLINE] Ticker suffix: '{ticker_suffix}', Pick: '{pick_upper}', Possible suffixes: {possible_suffixes} (sport: {sport})")
                    print(f"   [MONEYLINE] Ticker matches pick: {ticker_matches_pick}")
                    
                    if ticker_matches_pick:
                        # Ticker suffix matches pick → this is the pick team's market → bet YES
                        print(f"   ✅ [MONEYLINE] Ticker suffix '{ticker_suffix}' matches pick '{pick_upper}' → bet YES (this is pick team's market, sport: {sport})")
                        return 'yes'
                    else:
                        # Ticker suffix doesn't match pick → this is the opponent's market → bet NO
                        # (NO on opponent = YES on pick for non-tie sports)
                        print(f"   ✅ [MONEYLINE] Ticker suffix '{ticker_suffix}' doesn't match pick '{pick_upper}' → bet NO (this is opponent's market, NO on opponent = YES on pick, sport: {sport})")
                        return 'no'
                
                # If we can't extract ticker suffix, fall through to team name matching
                print(f"   ⚠️  [MONEYLINE] Could not extract ticker suffix, falling back to team name matching")
            
            # Check YES side - try partial matching first
            yes_match = any(word in yes_subtitle for word in pick_words) or pick_upper in yes_subtitle or yes_subtitle in pick_upper
            # Check NO side - try partial matching first
            no_match = any(word in no_subtitle for word in pick_words) or pick_upper in no_subtitle or no_subtitle in pick_upper
            
            print(f"   [MONEYLINE] yes_match={yes_match}, no_match={no_match}, both_same={both_same}")
            
            # If both subtitles are the same (and we didn't handle it above), use team name matching
            if both_same:
                # Both subtitles are identical - use ticker to determine
                # Ticker format: KXNHLGAME-26JAN11WSHNSH-NSH
                # The suffix (NSH) might indicate which team is YES
                # But we can't rely on this - need to check which team name appears in subtitle
                if team1 and team2:
                    # Check which team is in the subtitle
                    team1_words = [w for w in team1.split() if len(w) > 3]
                    team2_words = [w for w in team2.split() if len(w) > 3]
                    team1_in_subtitle = any(word in yes_subtitle for word in team1_words) or team1 in yes_subtitle
                    team2_in_subtitle = any(word in yes_subtitle for word in team2_words) or team2 in yes_subtitle
                    
                    # Check which team is the pick
                    pick_is_team1 = any(word in pick_upper for word in team1_words) or pick_upper in team1 or team1 in pick_upper
                    pick_is_team2 = any(word in pick_upper for word in team2_words) or pick_upper in team2 or team2 in pick_upper
                    
                    print(f"   [MONEYLINE] team1_in_subtitle={team1_in_subtitle}, team2_in_subtitle={team2_in_subtitle}")
                    print(f"   [MONEYLINE] pick_is_team1={pick_is_team1}, pick_is_team2={pick_is_team2}")
                    
                    # If pick team is in subtitle, bet YES
                    if pick_is_team1 and team1_in_subtitle:
                        print(f"   ✅ [MONEYLINE] Pick ({pick_upper}) is team1, team1 in subtitle → bet YES")
                        return 'yes'
                    if pick_is_team2 and team2_in_subtitle:
                        print(f"   ✅ [MONEYLINE] Pick ({pick_upper}) is team2, team2 in subtitle → bet YES")
                        return 'yes'
                    # If OTHER team is in subtitle, bet NO
                    if pick_is_team1 and team2_in_subtitle:
                        print(f"   ✅ [MONEYLINE] Pick ({pick_upper}) is team1, but team2 in subtitle → bet NO")
                        return 'no'
                    if pick_is_team2 and team1_in_subtitle:
                        print(f"   ✅ [MONEYLINE] Pick ({pick_upper}) is team2, but team1 in subtitle → bet NO")
                        return 'no'
            
            # Normal case: subtitles are different
            if yes_match and not no_match:
                print(f"   ✅ [MONEYLINE] Pick matches YES subtitle only → bet YES")
                return 'yes'
            if no_match and not yes_match:
                print(f"   ✅ [MONEYLINE] Pick matches NO subtitle only → bet NO")
                return 'no'
            
            # If both match (ambiguous), check which team is in which subtitle
            if yes_match and no_match:
                print(f"   ⚠️  [MONEYLINE] Pick matches both subtitles - checking team names...")
                if team1 and team2:
                    team1_words = [w for w in team1.split() if len(w) > 3]
                    team2_words = [w for w in team2.split() if len(w) > 3]
                    pick_is_team1 = any(word in pick_upper for word in team1_words) or pick_upper in team1 or team1 in pick_upper
                    pick_is_team2 = any(word in pick_upper for word in team2_words) or pick_upper in team2 or team2 in pick_upper
                    
                    # Check which team appears more strongly in each subtitle
                    team1_in_yes = any(word in yes_subtitle for word in team1_words) or team1 in yes_subtitle
                    team2_in_yes = any(word in yes_subtitle for word in team2_words) or team2 in yes_subtitle
                    team1_in_no = any(word in no_subtitle for word in team1_words) or team1 in no_subtitle
                    team2_in_no = any(word in no_subtitle for word in team2_words) or team2 in no_subtitle
                    
                    print(f"   [MONEYLINE] pick_is_team1={pick_is_team1}, pick_is_team2={pick_is_team2}")
                    print(f"   [MONEYLINE] team1_in_yes={team1_in_yes}, team2_in_yes={team2_in_yes}")
                    print(f"   [MONEYLINE] team1_in_no={team1_in_no}, team2_in_no={team2_in_no}")
                    
                    # If pick is team1 and team1 is in YES subtitle, bet YES
                    if pick_is_team1 and team1_in_yes and not team1_in_no:
                        print(f"   ✅ [MONEYLINE] Pick is team1, team1 in YES → bet YES")
                        return 'yes'
                    # If pick is team1 and team1 is in NO subtitle, bet NO
                    if pick_is_team1 and team1_in_no and not team1_in_yes:
                        print(f"   ✅ [MONEYLINE] Pick is team1, team1 in NO → bet NO")
                        return 'no'
                    # If pick is team2 and team2 is in YES subtitle, bet YES
                    if pick_is_team2 and team2_in_yes and not team2_in_no:
                        print(f"   ✅ [MONEYLINE] Pick is team2, team2 in YES → bet YES")
                        return 'yes'
                    # If pick is team2 and team2 is in NO subtitle, bet NO
                    if pick_is_team2 and team2_in_no and not team2_in_yes:
                        print(f"   ✅ [MONEYLINE] Pick is team2, team2 in NO → bet NO")
                        return 'no'
            
            # CRITICAL: Don't use ambiguous fallback - if we can't determine clearly, return None
            # The validation in dashboard.py will catch this and reject the bet
            # This is safer than betting on the wrong side
            if yes_match and not no_match:
                print(f"   ✅ [MONEYLINE] Clear match: YES only → bet YES")
                return 'yes'
            if no_match and not yes_match:
                print(f"   ✅ [MONEYLINE] Clear match: NO only → bet NO")
                return 'no'
            
            # If we get here, matching is ambiguous - return None to let validation handle it
            print(f"   ❌ [MONEYLINE] Ambiguous matching - yes_match={yes_match}, no_match={no_match}, both_same={both_same}")
            print(f"   ❌ [MONEYLINE] Cannot safely determine side - returning None for validation")
            return None
        
        # Fallback: Check if pick matches yes or no subtitle (general matching)
        if pick_upper in yes_subtitle or yes_subtitle in pick_upper:
            return 'yes'
        if pick_upper in no_subtitle or no_subtitle in pick_upper:
            return 'no'
        
        return None
    
    def calculate_contracts_from_dollars(self, dollars: float, price_cents: int) -> int:
        """Calculate number of contracts from dollar amount"""
        if price_cents <= 0:
            return 0
        
        price = price_cents / 100.0
        contracts = int(dollars / price)
        return max(1, contracts)  # At least 1 contract
    
    def calculate_max_contracts(self, liquidity_dollars: float, price_cents: int) -> int:
        """Calculate maximum contracts available at current price"""
        return self.calculate_contracts_from_dollars(liquidity_dollars, price_cents)
    
    def check_reverse_middle(self, new_alert: 'EvAlert', new_line: Optional[float], 
                            existing_positions: List[Dict]) -> tuple[bool, Optional[str]]:
        """
        Check if a new alert would create a reverse middle with existing positions.
        
        Reverse Middle (BAD - Block): Both bets can lose
        - Team A +4.5 and Team B -7.5: If score lands in 4.5-7.5 gap, both lose
        - Over 220.5 and Under 216.5: If score lands in 216.5-220.5 gap, both lose
        
        True Middle (GOOD - Allow): Both bets can win
        - Team A -4.5 and Team B +7.5: If score lands in 4.5-7.5 gap, both win
        - Over 216.5 and Under 220.5: If score lands in 216.5-220.5 gap, both win
        
        Args:
            new_alert: The new alert being evaluated
            new_line: The line value from the new alert (e.g., 4.5, -7.5, 220.5)
            existing_positions: List of existing position dicts with keys:
                - 'line': float (the line value)
                - 'pick': str (team name or Over/Under)
                - 'market_type': str (Point Spread, Total Points, etc.)
                - 'teams': str (game identifier)
        
        Returns:
            (is_reverse_middle: bool, reason: Optional[str])
            - If is_reverse_middle is True, the bet should be BLOCKED
            - reason explains why it's a reverse middle
        """
        if not existing_positions or new_line is None:
            return False, None
        
        new_pick = (new_alert.pick or '').upper()
        new_market_type = (new_alert.market_type or '').lower()
        new_teams = new_alert.teams or ''
        is_new_over = 'OVER' in new_pick
        is_new_under = 'UNDER' in new_pick
        is_new_spread = 'spread' in new_market_type or 'puck line' in new_market_type
        is_new_total = 'total' in new_market_type
        
        # Check each existing position
        for pos in existing_positions:
            existing_line = pos.get('line')
            existing_pick = (pos.get('pick', '') or '').upper()
            existing_market_type = (pos.get('market_type', '') or '').lower()
            existing_teams = pos.get('teams', '')
            
            # Must be same game
            if existing_teams != new_teams:
                continue
            
            # Skip if same market type and same pick (duplicate, handled elsewhere)
            if existing_market_type == new_market_type and existing_pick == new_pick:
                continue
            
            existing_is_over = 'OVER' in existing_pick
            existing_is_under = 'UNDER' in existing_pick
            existing_is_spread = 'spread' in existing_market_type or 'puck line' in existing_market_type
            existing_is_total = 'total' in existing_market_type
            existing_is_moneyline = 'moneyline' in existing_market_type or 'game' in existing_market_type.lower()
            
            is_new_moneyline = 'moneyline' in new_market_type or 'game' in new_market_type.lower()
            
            # Skip if line is required but missing (totals and spreads need lines, moneylines don't)
            if existing_line is None and (existing_is_total or existing_is_spread):
                continue
            
            # 1. TOTALS REVERSE MIDDLE CHECK
            if is_new_total and existing_is_total:
                # Must be opposite directions (one Over, one Under)
                if (is_new_over and existing_is_under) or (is_new_under and existing_is_over):
                    # Reverse middle: Over X and Under Y where X > Y (gap where both lose)
                    if (is_new_over and existing_is_under and new_line > existing_line) or \
                       (is_new_under and existing_is_over and new_line < existing_line):
                        gap = abs(new_line - existing_line)
                        return True, f"TOTALS REVERSE MIDDLE: {new_pick} {new_line} vs {existing_pick} {existing_line} (gap: {gap:.1f} - both can lose)"
                    # True middle: Over X and Under Y where X < Y (gap where both win) - ALLOW
                    # This case doesn't return, so it's allowed
            
            # 2. SPREAD REVERSE MIDDLE CHECK
            if is_new_spread and existing_is_spread:
                # Extract team names (non-Over/Under picks are team names)
                new_team = new_pick if not is_new_over and not is_new_under else None
                existing_team = existing_pick if not existing_is_over and not existing_is_under else None
                
                # Get original sides and picks for same-team detection
                new_side = getattr(new_alert, 'side', '').lower() if hasattr(new_alert, 'side') and new_alert.side else ''
                existing_side = pos.get('side', '').lower() if pos.get('side') else ''
                existing_raw_pick = (pos.get('raw_pick', '') or existing_pick).upper()
                
                # Check for SAME TEAM reverse middle (NO on lower line + YES on higher line)
                # Example: NO on "wins by over 1.5" + YES on "wins by over 4.5" = gap 2-4 where all lose
                if new_team and existing_team and new_team == existing_team:
                    # Same team - need to check original sides and lines
                    # Get original lines (before NO bet transformation)
                    new_line_original = getattr(new_alert, 'original_line', new_line)  # Use original_line if available, else use new_line
                    existing_line_original = existing_line
                    
                    # If existing was a NO bet, reverse the transformation to get original line
                    # NO bets are stored with flipped line (e.g., NO on 1.5 stored as -1.5)
                    if existing_side == 'no' and existing_line_original is not None:
                        existing_line_original = abs(existing_line_original)  # Reverse the flip: -1.5 -> 1.5
                    
                    # If new is a NO bet, get original line
                    if new_side == 'no' and new_line_original is not None:
                        # new_line_original should already be the original (from alert.original_line)
                        # But if it's negative, it might be transformed - use abs
                        if new_line_original < 0:
                            new_line_original = abs(new_line_original)
                    
                    if new_line_original is not None and existing_line_original is not None:
                        # Check for NO on lower line + YES on higher line pattern
                        # NO on "wins by over X" = wins by ≤X
                        # YES on "wins by over Y" = wins by >Y
                        # Gap exists if Y > X (e.g., NO on 1.5 + YES on 4.5 = gap 2-4)
                        if new_side == 'no' and existing_side == 'yes':
                            # New: NO on X, Existing: YES on Y
                            if existing_line_original > new_line_original:
                                gap_start = new_line_original + 0.5
                                gap_end = existing_line_original - 0.5
                                return True, f"SAME TEAM SPREAD REVERSE MIDDLE: NO on {new_team} {new_line_original} + YES on {existing_team} {existing_line_original} (gap: {gap_start:.1f}-{gap_end:.1f} where all lose)"
                        elif new_side == 'yes' and existing_side == 'no':
                            # New: YES on Y, Existing: NO on X
                            if new_line_original > existing_line_original:
                                gap_start = existing_line_original + 0.5
                                gap_end = new_line_original - 0.5
                                return True, f"SAME TEAM SPREAD REVERSE MIDDLE: YES on {new_team} {new_line_original} + NO on {existing_team} {existing_line_original} (gap: {gap_start:.1f}-{gap_end:.1f} where all lose)"
                
                # Check for DIFFERENT TEAM reverse middle (existing logic)
                if new_team and existing_team and new_team != existing_team:
                    # Different teams - check for reverse middle
                    # Reverse middle: Team A +X and Team B -Y where X < Y (gap where both lose)
                    # Example: Team A +4.5 and Team B -7.5: If margin is 5-7, both lose
                    if new_line > 0 and existing_line < 0:
                        # New is underdog (+X), existing is favorite (-Y)
                        if new_line < abs(existing_line):
                            gap = abs(existing_line) - new_line
                            return True, f"SPREAD REVERSE MIDDLE: {new_team} +{new_line} vs {existing_team} {existing_line} (gap: {gap:.1f} - both can lose)"
                    elif new_line < 0 and existing_line > 0:
                        # New is favorite (-X), existing is underdog (+Y)
                        if existing_line < abs(new_line):
                            gap = abs(new_line) - existing_line
                            return True, f"SPREAD REVERSE MIDDLE: {new_team} {new_line} vs {existing_team} +{existing_line} (gap: {gap:.1f} - both can lose)"
                    # True middle: Team A -X and Team B +Y where X < Y (gap where both win) - ALLOW
                    # This case doesn't return, so it's allowed
            
            # 3. MONEYLINE + MONEYLINE REVERSE MIDDLE CHECK
            # CRITICAL: Betting both teams' moneylines on the same event is ALWAYS a reverse middle
            # Only one team can win, so both bets can't win (reverse middle)
            # Example: Lakers ML + Sacramento ML = reverse middle (only one can win)
            if is_new_moneyline and existing_is_moneyline:
                # Extract team names
                new_team = new_pick if not is_new_over and not is_new_under else None
                existing_team = existing_pick if not existing_is_over and not existing_is_under else None
                
                if new_team and existing_team and new_team != existing_team:
                    # Different teams on same event - ALWAYS a reverse middle for moneylines
                    return True, f"MONEYLINE + MONEYLINE REVERSE MIDDLE: {new_team} ML vs {existing_team} ML (only one team can win - both bets can't win)"
            
            # 4. MONEYLINE + SPREAD REVERSE MIDDLE CHECK
            # Reverse middle: Team A -X (spread, favorite) + Team B ML (opponent moneyline)
            # If Team A wins by 1 to X-1, both bets lose
            # Example: Lakers -3.5 + Sacramento ML
            # If Lakers win by 1-3 points: Lakers -3.5 loses, Sacramento ML loses (reverse middle)
            if (is_new_moneyline and existing_is_spread) or (is_new_spread and existing_is_moneyline):
                # Extract team names
                new_team = new_pick if not is_new_over and not is_new_under else None
                existing_team = existing_pick if not existing_is_over and not existing_is_under else None
                
                if new_team and existing_team and new_team != existing_team:
                    # Different teams - check for reverse middle
                    if is_new_moneyline and existing_is_spread:
                        # New: Team A ML, Existing: Team B -X (favorite spread)
                        # This is a reverse middle if Team B is the favorite (negative line)
                        if existing_line is not None and existing_line < 0:
                            # Existing spread is favorite (Team B -X)
                            # New moneyline is on Team A (opponent)
                            # If Team B wins by 1 to |X|-1, both lose
                            return True, f"ML+SPREAD REVERSE MIDDLE: {new_team} ML vs {existing_team} {existing_line} (if {existing_team} wins by 1-{abs(existing_line)-1}, both lose)"
                    elif is_new_spread and existing_is_moneyline:
                        # New: Team A -X (favorite spread), Existing: Team B ML
                        # This is a reverse middle if Team A is the favorite (negative line)
                        if new_line is not None and new_line < 0:
                            # New spread is favorite (Team A -X)
                            # Existing moneyline is on Team B (opponent)
                            # If Team A wins by 1 to |X|-1, both lose
                            return True, f"ML+SPREAD REVERSE MIDDLE: {new_team} {new_line} vs {existing_team} ML (if {new_team} wins by 1-{abs(new_line)-1}, both lose)"
        
        return False, None

