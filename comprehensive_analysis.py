"""
Comprehensive in-depth analysis of all betting data from Google Sheets
Analyzes: bet types, sharp books, filters, sports, spread YES vs NO, PX+Novig performance, etc.
"""
import os
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from collections import defaultdict
from datetime import datetime

load_dotenv()

_credentials_file = os.getenv('GOOGLE_SHEETS_CREDENTIALS_FILE', 'credentials.json')
if not os.path.isabs(_credentials_file):
    _credentials_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), _credentials_file)
GOOGLE_SHEETS_CREDENTIALS_FILE = _credentials_file
GOOGLE_SHEETS_SPREADSHEET_ID = os.getenv('GOOGLE_SHEETS_SPREADSHEET_ID', '')

def parse_float(value, default=0.0):
    """Safely parse float value"""
    if not value or value == '':
        return default
    try:
        return float(str(value).replace('$', '').replace(',', '').strip())
    except (ValueError, TypeError):
        return default

def get_all_bets():
    """Load all bets from Google Sheets"""
    if not GOOGLE_SHEETS_SPREADSHEET_ID:
        print("ERROR: GOOGLE_SHEETS_SPREADSHEET_ID not set")
        return []
    
    try:
        scope = ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_file(GOOGLE_SHEETS_CREDENTIALS_FILE, scopes=scope)
        google_sheets_client = gspread.authorize(creds)
        
        spreadsheet = google_sheets_client.open_by_key(GOOGLE_SHEETS_SPREADSHEET_ID)
        worksheet = spreadsheet.worksheet('Auto-Bets')
        
        rows = worksheet.get_all_values()
        if len(rows) == 0:
            return []
        
        # Detect header
        first_row = [col.strip() if col else '' for col in rows[0]]
        first_row_lower = [col.lower() for col in first_row]
        expected_headers = ['ticker', 'side', 'result', 'pnl', 'cost', 'filter name']
        header_matches = sum(1 for h in expected_headers if any(h in col for col in first_row_lower))
        is_header = header_matches >= 3
        
        if is_header:
            header = first_row
            data_rows = rows[1:]
        else:
            # Positional mapping
            header = ['Timestamp', 'Order ID', 'Ticker', 'Side', 'Teams', 'Market Type', 'Pick', 'Qualifier',
                     'EV %', 'Expected Price (¢)', 'Executed Price (¢)', 'American Odds',
                     'Contracts', 'Cost ($)', 'Payout ($)', 'Win Amount ($)', 'Sport', 'Status', 'Result', 'PNL ($)', 'Settled', 'Filter Name', 'Devig Books']
            data_rows = rows
        
        def get_col(name):
            if is_header:
                header_lower = [h.lower() if h else '' for h in header]
                for idx, h in enumerate(header_lower):
                    if name.lower() in h:
                        return idx
            else:
                col_map = {
                    'ticker': 2, 'side': 3, 'teams': 4, 'market_type': 5, 'pick': 6, 'qualifier': 7,
                    'ev': 8, 'expected_price': 9, 'executed_price': 10, 'american_odds': 11,
                    'contracts': 12, 'cost': 13, 'payout': 14, 'win_amount': 15,
                    'sport': 16, 'status': 17, 'result': 18, 'pnl': 19, 'settled': 20,
                    'filter_name': 21, 'devig_books': 22
                }
                return col_map.get(name)
            return None
        
        bets = []
        for row_idx, row in enumerate(data_rows, start=2):
            if len(row) < 10:
                continue
            
            ticker = row[get_col('ticker')].strip().upper() if get_col('ticker') and get_col('ticker') < len(row) else ''
            side = row[get_col('side')].strip().lower() if get_col('side') and get_col('side') < len(row) else ''
            result = row[get_col('result')].strip().upper() if get_col('result') and get_col('result') < len(row) else ''
            settled = row[get_col('settled')].strip().upper() if get_col('settled') and get_col('settled') < len(row) else 'FALSE'
            
            if not ticker or not side:
                continue
            
            bet = {
                'row': row_idx,
                'ticker': ticker,
                'side': side,
                'teams': row[get_col('teams')].strip() if get_col('teams') and get_col('teams') < len(row) else '',
                'market_type': row[get_col('market_type')].strip() if get_col('market_type') and get_col('market_type') < len(row) else '',
                'pick': row[get_col('pick')].strip() if get_col('pick') and get_col('pick') < len(row) else '',
                'qualifier': row[get_col('qualifier')].strip() if get_col('qualifier') and get_col('qualifier') < len(row) else '',
                'ev': parse_float(row[get_col('ev')] if get_col('ev') and get_col('ev') < len(row) else '0'),
                'cost': parse_float(row[get_col('cost')] if get_col('cost') and get_col('cost') < len(row) else '0'),
                'pnl': parse_float(row[get_col('pnl')] if get_col('pnl') and get_col('pnl') < len(row) else '0'),
                'win_amount': parse_float(row[get_col('win_amount')] if get_col('win_amount') and get_col('win_amount') < len(row) else '0'),
                'sport': row[get_col('sport')].strip() if get_col('sport') and get_col('sport') < len(row) else '',
                'result': result,
                'settled': settled == 'TRUE',
                'filter_name': row[get_col('filter_name')].strip() if get_col('filter_name') and get_col('filter_name') < len(row) else 'Unknown',
                'devig_books': row[get_col('devig_books')].strip() if get_col('devig_books') and get_col('devig_books') < len(row) else '',
                'american_odds': row[get_col('american_odds')].strip() if get_col('american_odds') and get_col('american_odds') < len(row) else ''
            }
            
            bets.append(bet)
        
        return bets
    
    except Exception as e:
        print(f"ERROR loading bets: {e}")
        import traceback
        traceback.print_exc()
        return []

def calculate_stats(bets, name=""):
    """Calculate statistics for a set of bets"""
    if not bets:
        return None
    
    settled = [b for b in bets if b['settled']]
    if not settled:
        return None
    
    wins = [b for b in settled if b['result'] == 'WIN']
    losses = [b for b in settled if b['result'] == 'LOSS']
    open_bets = [b for b in bets if not b['settled']]
    
    total_pnl = sum(b['pnl'] for b in settled)
    total_cost = sum(b['cost'] for b in bets)
    total_settled_cost = sum(b['cost'] for b in settled)
    
    win_rate = (len(wins) / len(settled) * 100) if settled else 0
    roi = (total_pnl / total_settled_cost * 100) if total_settled_cost > 0 else 0
    
    return {
        'name': name,
        'total_bets': len(bets),
        'settled': len(settled),
        'wins': len(wins),
        'losses': len(losses),
        'open': len(open_bets),
        'win_rate': win_rate,
        'total_pnl': total_pnl,
        'total_cost': total_cost,
        'settled_cost': total_settled_cost,
        'roi': roi
    }

def print_stats(stats):
    """Print statistics in a formatted way"""
    if not stats:
        return
    print(f"  {stats['name']}:")
    print(f"    Bets: {stats['total_bets']} ({stats['settled']} settled, {stats['open']} open)")
    print(f"    Record: {stats['wins']}-{stats['losses']} ({stats['win_rate']:.1f}% win rate)")
    print(f"    PNL: ${stats['total_pnl']:,.2f} | Cost: ${stats['settled_cost']:,.2f} | ROI: {stats['roi']:.2f}%")

def analyze_spread_yes_vs_no(bets):
    """Analyze spread bets separated by YES vs NO"""
    print(f"\n{'='*80}")
    print("SPREAD YES vs SPREAD NO ANALYSIS")
    print(f"{'='*80}")
    
    spread_bets = [b for b in bets if 'spread' in b['market_type'].lower() or 'point spread' in b['market_type'].lower()]
    
    spread_yes = [b for b in spread_bets if b['side'].lower() == 'yes']
    spread_no = [b for b in spread_bets if b['side'].lower() == 'no']
    
    print(f"\nTotal Spread Bets: {len(spread_bets)}")
    print(f"  Spread YES: {len(spread_yes)}")
    print(f"  Spread NO: {len(spread_no)}")
    
    yes_stats = calculate_stats(spread_yes, "Spread YES")
    no_stats = calculate_stats(spread_no, "Spread NO")
    all_stats = calculate_stats(spread_bets, "All Spreads")
    
    if yes_stats:
        print_stats(yes_stats)
    if no_stats:
        print_stats(no_stats)
    if all_stats:
        print_stats(all_stats)
    
    # By filter
    print(f"\n  By Filter:")
    for filter_name in set(b['filter_name'] for b in spread_bets):
        filter_spreads = [b for b in spread_bets if b['filter_name'] == filter_name]
        filter_yes = [b for b in filter_spreads if b['side'].lower() == 'yes']
        filter_no = [b for b in filter_spreads if b['side'].lower() == 'no']
        
        yes_s = calculate_stats(filter_yes, f"{filter_name} - YES")
        no_s = calculate_stats(filter_no, f"{filter_name} - NO")
        
        if yes_s or no_s:
            print(f"    {filter_name}:")
            if yes_s:
                print(f"      YES: {yes_s['wins']}-{yes_s['losses']} | ${yes_s['total_pnl']:,.2f} ({yes_s['roi']:.2f}% ROI)")
            if no_s:
                print(f"      NO:  {no_s['wins']}-{no_s['losses']} | ${no_s['total_pnl']:,.2f} ({no_s['roi']:.2f}% ROI)")
    
    # By sport
    print(f"\n  By Sport:")
    for sport in set(b['sport'] for b in spread_bets if b['sport']):
        sport_spreads = [b for b in spread_bets if b['sport'] == sport]
        sport_yes = [b for b in sport_spreads if b['side'].lower() == 'yes']
        sport_no = [b for b in sport_spreads if b['side'].lower() == 'no']
        
        yes_s = calculate_stats(sport_yes, f"{sport} - YES")
        no_s = calculate_stats(sport_no, f"{sport} - NO")
        
        if yes_s or no_s:
            print(f"    {sport}:")
            if yes_s:
                print(f"      YES: {yes_s['wins']}-{yes_s['losses']} | ${yes_s['total_pnl']:,.2f} ({yes_s['roi']:.2f}% ROI)")
            if no_s:
                print(f"      NO:  {no_s['wins']}-{no_s['losses']} | ${no_s['total_pnl']:,.2f} ({no_s['roi']:.2f}% ROI)")

def analyze_px_novig_detailed(bets):
    """Detailed analysis of ProphetX + Novig bets"""
    print(f"\n{'='*80}")
    print("PROPHETX + NOVIG DETAILED ANALYSIS")
    print(f"{'='*80}")
    
    # Find PX+Novig bets
    px_novig_bets = []
    for bet in bets:
        devig = bet['devig_books'].upper()
        has_px = 'PROPHETX' in devig or 'PX' in devig
        has_novig = 'NOVIG' in devig or 'NV' in devig
        if has_px and has_novig:
            px_novig_bets.append(bet)
    
    print(f"\nTotal PX+Novig Bets: {len(px_novig_bets)}")
    
    if not px_novig_bets:
        print("  No PX+Novig bets found")
        return
    
    # Check for $150 bets (1.5x multiplier)
    cost_150 = [b for b in px_novig_bets if abs(b['cost'] - 150.0) < 1.0]
    cost_202 = [b for b in px_novig_bets if abs(b['cost'] - 202.0) < 1.0]
    cost_151 = [b for b in px_novig_bets if abs(b['cost'] - 151.0) < 1.0]
    
    print(f"  $150 bets (1.5x): {len(cost_150)}")
    print(f"  $151 bets: {len(cost_151)}")
    print(f"  $202 bets (2x): {len(cost_202)}")
    
    stats = calculate_stats(px_novig_bets, "PX+Novig All")
    if stats:
        print_stats(stats)
    
    # By filter
    print(f"\n  By Filter:")
    for filter_name in set(b['filter_name'] for b in px_novig_bets):
        filter_bets = [b for b in px_novig_bets if b['filter_name'] == filter_name]
        s = calculate_stats(filter_bets, filter_name)
        if s:
            print_stats(s)
    
    # By market type
    print(f"\n  By Market Type:")
    for market_type in set(b['market_type'] for b in px_novig_bets if b['market_type']):
        market_bets = [b for b in px_novig_bets if b['market_type'] == market_type]
        s = calculate_stats(market_bets, market_type)
        if s:
            print_stats(s)
    
    # By sport
    print(f"\n  By Sport:")
    for sport in set(b['sport'] for b in px_novig_bets if b['sport']):
        sport_bets = [b for b in px_novig_bets if b['sport'] == sport]
        s = calculate_stats(sport_bets, sport)
        if s:
            print_stats(s)
    
    # Show recent bets (rows 629-630 mentioned by user)
    print(f"\n  Recent PX+Novig Bets (checking rows 629-630):")
    for bet in sorted(px_novig_bets, key=lambda x: x['row'])[-20:]:
        if bet['row'] >= 629:
            print(f"    Row {bet['row']}: {bet['ticker']} {bet['side']} | ${bet['cost']:.2f} | {bet['result']} | ${bet['pnl']:.2f} | Books: {bet['devig_books']}")

def analyze_sharp_books(bets):
    """Detailed analysis by sharp book combinations"""
    print(f"\n{'='*80}")
    print("SHARP BOOKS ANALYSIS")
    print(f"{'='*80}")
    
    sharp_books = ['ProphetX', 'Novig', 'BookMaker', 'Pinnacle', 'Circa', 'SportTrade']
    
    # Individual books
    print(f"\n  Individual Books:")
    for book in sharp_books:
        book_bets = [b for b in bets if book in b['devig_books']]
        s = calculate_stats(book_bets, book)
        if s and s['settled'] > 0:
            print_stats(s)
    
    # PX + Novig
    px_novig = [b for b in bets if 'ProphetX' in b['devig_books'] and 'Novig' in b['devig_books']]
    s = calculate_stats(px_novig, "ProphetX + Novig")
    if s:
        print(f"\n  Book Combinations:")
        print_stats(s)
    
    # PX only (no Novig)
    px_only = [b for b in bets if 'ProphetX' in b['devig_books'] and 'Novig' not in b['devig_books']]
    s = calculate_stats(px_only, "ProphetX Only")
    if s:
        print_stats(s)
    
    # Novig only (no PX)
    novig_only = [b for b in bets if 'Novig' in b['devig_books'] and 'ProphetX' not in b['devig_books']]
    s = calculate_stats(novig_only, "Novig Only")
    if s:
        print_stats(s)

def analyze_by_ev_ranges(bets):
    """Analyze performance by EV ranges"""
    print(f"\n{'='*80}")
    print("EV RANGE ANALYSIS")
    print(f"{'='*80}")
    
    ev_ranges = [
        (0, 5, "0-5%"),
        (5, 8, "5-8%"),
        (8, 10, "8-10%"),
        (10, 12, "10-12%"),
        (12, 15, "12-15%"),
        (15, 20, "15-20%"),
        (20, 100, "20%+")
    ]
    
    for min_ev, max_ev, label in ev_ranges:
        range_bets = [b for b in bets if min_ev <= b['ev'] < max_ev]
        s = calculate_stats(range_bets, f"EV {label}")
        if s and s['settled'] > 0:
            print_stats(s)

def analyze_comprehensive(bets):
    """Comprehensive analysis of all aspects"""
    print(f"\n{'='*80}")
    print("COMPREHENSIVE BETTING ANALYSIS")
    print(f"{'='*80}")
    
    # Overall stats
    overall = calculate_stats(bets, "OVERALL")
    if overall:
        print_stats(overall)
    
    # By filter
    print(f"\n  By Filter:")
    for filter_name in set(b['filter_name'] for b in bets):
        filter_bets = [b for b in bets if b['filter_name'] == filter_name]
        s = calculate_stats(filter_bets, filter_name)
        if s:
            print_stats(s)
    
    # By sport
    print(f"\n  By Sport:")
    for sport in set(b['sport'] for b in bets if b['sport']):
        sport_bets = [b for b in bets if b['sport'] == sport]
        s = calculate_stats(sport_bets, sport)
        if s:
            print_stats(s)
    
    # By market type
    print(f"\n  By Market Type:")
    for market_type in set(b['market_type'] for b in bets if b['market_type']):
        market_bets = [b for b in bets if b['market_type'] == market_type]
        s = calculate_stats(market_bets, market_type)
        if s:
            print_stats(s)
    
    # By side
    print(f"\n  By Side:")
    for side in ['yes', 'no']:
        side_bets = [b for b in bets if b['side'].lower() == side]
        s = calculate_stats(side_bets, side.upper())
        if s:
            print_stats(s)

if __name__ == "__main__":
    print("Loading bets from Google Sheets...")
    bets = get_all_bets()
    print(f"Loaded {len(bets)} bets")
    
    if not bets:
        print("No bets found!")
        exit(1)
    
    # Run all analyses
    analyze_comprehensive(bets)
    analyze_spread_yes_vs_no(bets)
    analyze_px_novig_detailed(bets)
    analyze_sharp_books(bets)
    analyze_by_ev_ranges(bets)
    
    print(f"\n{'='*80}")
    print("ANALYSIS COMPLETE")
    print(f"{'='*80}")
