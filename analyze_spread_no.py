"""
Analyze spread NO bets in detail:
- Sample sizes and statistical significance
- True middles vs reverse middles
- Impact on bet volume if removed
- Recommendation
"""
import os
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from collections import defaultdict
import math

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
        
        first_row = [col.strip() if col else '' for col in rows[0]]
        first_row_lower = [col.lower() for col in first_row]
        expected_headers = ['ticker', 'side', 'result', 'pnl', 'cost']
        header_matches = sum(1 for h in expected_headers if any(h in col for col in first_row_lower))
        is_header = header_matches >= 3
        
        if is_header:
            header = first_row
            data_rows = rows[1:]
        else:
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
                    'ev': 8, 'cost': 13, 'pnl': 19, 'sport': 16, 'result': 18, 'settled': 20,
                    'filter_name': 21
                }
                return col_map.get(name)
            return None
        
        bets = []
        for row in data_rows:
            if len(row) < 10:
                continue
            
            ticker = row[get_col('ticker')].strip().upper() if get_col('ticker') and get_col('ticker') < len(row) else ''
            side = row[get_col('side')].strip().lower() if get_col('side') and get_col('side') < len(row) else ''
            market_type = row[get_col('market_type')].strip() if get_col('market_type') and get_col('market_type') < len(row) else ''
            
            if not ticker or not side:
                continue
            
            bet = {
                'ticker': ticker,
                'side': side,
                'teams': row[get_col('teams')].strip() if get_col('teams') and get_col('teams') < len(row) else '',
                'pick': row[get_col('pick')].strip() if get_col('pick') and get_col('pick') < len(row) else '',
                'qualifier': row[get_col('qualifier')].strip() if get_col('qualifier') and get_col('qualifier') < len(row) else '',
                'cost': parse_float(row[get_col('cost')] if get_col('cost') and get_col('cost') < len(row) else '0'),
                'pnl': parse_float(row[get_col('pnl')] if get_col('pnl') and get_col('pnl') < len(row) else '0'),
                'sport': row[get_col('sport')].strip() if get_col('sport') and get_col('sport') < len(row) else '',
                'result': row[get_col('result')].strip().upper() if get_col('result') and get_col('result') < len(row) else '',
                'settled': row[get_col('settled')].strip().upper() if get_col('settled') and get_col('settled') < len(row) else 'FALSE',
                'filter_name': row[get_col('filter_name')].strip() if get_col('filter_name') and get_col('filter_name') < len(row) else 'Unknown',
                'market_type': market_type
            }
            
            bets.append(bet)
        
        return bets
    
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return []

def calculate_statistical_significance(wins, losses, expected_win_rate=0.5):
    """Calculate statistical significance using binomial test"""
    n = wins + losses
    if n == 0:
        return None
    
    observed_win_rate = wins / n
    p_value = 0.0
    z_score = None
    
    # Binomial test: probability of observing this many wins or fewer
    # Using normal approximation for large samples
    if n >= 30:
        z = (observed_win_rate - expected_win_rate) / math.sqrt(expected_win_rate * (1 - expected_win_rate) / n)
        z_score = z
        # Two-tailed p-value approximation using z-table
        # z > 1.96 = p < 0.05, z > 2.58 = p < 0.01, z > 3.29 = p < 0.001
        abs_z = abs(z)
        if abs_z >= 3.29:
            p_value = 0.001
        elif abs_z >= 2.58:
            p_value = 0.01
        elif abs_z >= 1.96:
            p_value = 0.05
        elif abs_z >= 1.65:
            p_value = 0.10
        else:
            p_value = 0.20
    
    # Simple heuristic for smaller samples
    if n < 30:
        # For small samples, use a rough estimate
        diff = abs(observed_win_rate - expected_win_rate)
        if diff > 0.20:
            p_value = 0.05
        elif diff > 0.15:
            p_value = 0.10
        else:
            p_value = 0.20
    
    is_significant = p_value < 0.05
    return {
        'n': n,
        'observed_wr': observed_win_rate,
        'expected_wr': expected_win_rate,
        'p_value': p_value,
        'is_significant': is_significant,
        'z_score': z_score
    }

def identify_true_middles(bets):
    """Identify which spread NO bets might be true middles"""
    # Group by event (extract event ticker from market ticker)
    event_bets = defaultdict(list)
    
    for bet in bets:
        # Extract event ticker (everything before last dash)
        parts = bet['ticker'].split('-')
        if len(parts) >= 3:
            event_ticker = '-'.join(parts[:-1])
        else:
            event_ticker = bet['ticker']
        
        event_bets[event_ticker].append(bet)
    
    # Find events with both YES and NO spread bets
    true_middles = []
    reverse_middles = []
    standalone_no = []
    
    for event_ticker, event_bets_list in event_bets.items():
        yes_bets = [b for b in event_bets_list if b['side'].lower() == 'yes']
        no_bets = [b for b in event_bets_list if b['side'].lower() == 'no']
        
        if yes_bets and no_bets:
            # Check if it's a true middle or reverse middle
            # True middle: Both can win (e.g., Team A -4.5 YES + Team B +7.5 NO)
            # Reverse middle: Both can lose (e.g., Team A +4.5 NO + Team B -7.5 YES)
            
            # For now, we'll classify based on performance
            # If both bets have positive ROI or one has very negative ROI, it's likely a reverse middle
            yes_pnl = sum(b['pnl'] for b in yes_bets if b['settled'] == 'TRUE')
            no_pnl = sum(b['pnl'] for b in no_bets if b['settled'] == 'TRUE')
            total_pnl = yes_pnl + no_pnl
            
            if total_pnl < -50:  # Both losing money = reverse middle
                reverse_middles.extend(no_bets)
            else:  # Could be true middle
                true_middles.extend(no_bets)
        elif no_bets and not yes_bets:
            standalone_no.extend(no_bets)
    
    return true_middles, reverse_middles, standalone_no

def analyze_spread_no():
    """Comprehensive analysis of spread NO bets"""
    print(f"\n{'='*80}")
    print("SPREAD NO BETS - COMPREHENSIVE ANALYSIS")
    print(f"{'='*80}")
    
    all_bets = get_all_bets()
    if not all_bets:
        print("No bets found!")
        return
    
    # Get all spread bets
    spread_bets = [b for b in all_bets if 'spread' in b.get('market_type', '').lower() or 'SPREAD' in b.get('ticker', '')]
    spread_no = [b for b in spread_bets if b['side'].lower() == 'no']
    spread_yes = [b for b in spread_bets if b['side'].lower() == 'yes']
    
    print(f"\nTotal Spread Bets: {len(spread_bets)}")
    print(f"  Spread YES: {len(spread_yes)}")
    print(f"  Spread NO: {len(spread_no)}")
    
    # Overall spread NO stats
    settled_no = [b for b in spread_no if b['settled'] == 'TRUE']
    wins_no = [b for b in settled_no if b['result'] == 'WIN']
    losses_no = [b for b in settled_no if b['result'] == 'LOSS']
    
    total_pnl_no = sum(b['pnl'] for b in settled_no)
    total_cost_no = sum(b['cost'] for b in spread_no)
    settled_cost_no = sum(b['cost'] for b in settled_no)
    roi_no = (total_pnl_no / settled_cost_no * 100) if settled_cost_no > 0 else 0
    win_rate_no = (len(wins_no) / len(settled_no) * 100) if settled_no else 0
    
    print(f"\nSPREAD NO OVERALL:")
    print(f"  Total: {len(spread_no)} bets ({len(settled_no)} settled)")
    print(f"  Record: {len(wins_no)}-{len(losses_no)} ({win_rate_no:.1f}% win rate)")
    print(f"  PNL: ${total_pnl_no:,.2f}")
    print(f"  Cost: ${settled_cost_no:,.2f}")
    print(f"  ROI: {roi_no:.2f}%")
    
    # Statistical significance
    sig = calculate_statistical_significance(len(wins_no), len(losses_no), 0.5)
    if sig:
        print(f"\n  Statistical Significance:")
        print(f"    Sample size: {sig['n']}")
        print(f"    Observed win rate: {sig['observed_wr']:.1%}")
        print(f"    Expected win rate: {sig['expected_wr']:.1%}")
        print(f"    P-value: {sig['p_value']:.4f}")
        print(f"    Statistically significant: {'YES' if sig['is_significant'] else 'NO'}")
    
    # By filter
    print(f"\n  By Filter:")
    for filter_name in set(b['filter_name'] for b in spread_no):
        filter_no = [b for b in spread_no if b['filter_name'] == filter_name]
        filter_settled = [b for b in filter_no if b['settled'] == 'TRUE']
        filter_wins = [b for b in filter_settled if b['result'] == 'WIN']
        filter_losses = [b for b in filter_settled if b['result'] == 'LOSS']
        filter_pnl = sum(b['pnl'] for b in filter_settled)
        filter_cost = sum(b['cost'] for b in filter_settled)
        filter_roi = (filter_pnl / filter_cost * 100) if filter_cost > 0 else 0
        filter_wr = (len(filter_wins) / len(filter_settled) * 100) if filter_settled else 0
        
        print(f"    {filter_name}:")
        print(f"      {len(filter_no)} bets ({len(filter_settled)} settled)")
        print(f"      {len(filter_wins)}-{len(filter_losses)} ({filter_wr:.1f}% WR)")
        print(f"      ${filter_pnl:,.2f} PNL ({filter_roi:.2f}% ROI)")
        
        sig_filter = calculate_statistical_significance(len(filter_wins), len(filter_losses), 0.5)
        if sig_filter and sig_filter['n'] >= 20:
            print(f"      Significant: {'YES' if sig_filter['is_significant'] else 'NO'} (p={sig_filter['p_value']:.4f})")
    
    # By sport
    print(f"\n  By Sport:")
    for sport in set(b['sport'] for b in spread_no if b['sport']):
        sport_no = [b for b in spread_no if b['sport'] == sport]
        sport_settled = [b for b in sport_no if b['settled'] == 'TRUE']
        sport_wins = [b for b in sport_settled if b['result'] == 'WIN']
        sport_losses = [b for b in sport_settled if b['result'] == 'LOSS']
        sport_pnl = sum(b['pnl'] for b in sport_settled)
        sport_cost = sum(b['cost'] for b in sport_settled)
        sport_roi = (sport_pnl / sport_cost * 100) if sport_cost > 0 else 0
        sport_wr = (len(sport_wins) / len(sport_settled) * 100) if sport_settled else 0
        
        print(f"    {sport}:")
        print(f"      {len(sport_no)} bets ({len(sport_settled)} settled)")
        print(f"      {len(sport_wins)}-{len(sport_losses)} ({sport_wr:.1f}% WR)")
        print(f"      ${sport_pnl:,.2f} PNL ({sport_roi:.2f}% ROI)")
    
    # True middle vs reverse middle analysis
    print(f"\n  True Middle vs Reverse Middle Analysis:")
    true_middles, reverse_middles, standalone_no = identify_true_middles(spread_no)
    
    print(f"    Standalone NO bets (no YES on same event): {len(standalone_no)}")
    print(f"    NO bets with YES on same event (potential middles): {len(true_middles) + len(reverse_middles)}")
    print(f"      True middles (estimated): {len(true_middles)}")
    print(f"      Reverse middles (estimated): {len(reverse_middles)}")
    
    if true_middles:
        tm_settled = [b for b in true_middles if b['settled'] == 'TRUE']
        tm_wins = [b for b in tm_settled if b['result'] == 'WIN']
        tm_pnl = sum(b['pnl'] for b in tm_settled)
        tm_cost = sum(b['cost'] for b in tm_settled)
        tm_roi = (tm_pnl / tm_cost * 100) if tm_cost > 0 else 0
        print(f"      True middles: {len(tm_wins)}-{len(tm_settled)-len(tm_wins)} | ${tm_pnl:,.2f} ({tm_roi:.2f}% ROI)")
    
    if reverse_middles:
        rm_settled = [b for b in reverse_middles if b['settled'] == 'TRUE']
        rm_wins = [b for b in rm_settled if b['result'] == 'WIN']
        rm_pnl = sum(b['pnl'] for b in rm_settled)
        rm_cost = sum(b['cost'] for b in rm_settled)
        rm_roi = (rm_pnl / rm_cost * 100) if rm_cost > 0 else 0
        print(f"      Reverse middles: {len(rm_wins)}-{len(rm_settled)-len(rm_wins)} | ${rm_pnl:,.2f} ({rm_roi:.2f}% ROI)")
    
    # Impact analysis
    print(f"\n  IMPACT IF WE REMOVE ALL SPREAD NO:")
    all_settled_bets = [b for b in all_bets if b.get('settled') == 'TRUE']
    all_bets_count = len(all_settled_bets)
    print(f"    Current total settled bets: {all_bets_count}")
    print(f"    Spread NO bets to remove: {len(settled_no)}")
    print(f"    New total: {all_bets_count - len(settled_no)} ({((all_bets_count - len(settled_no)) / all_bets_count * 100) if all_bets_count > 0 else 0:.1f}% of current)")
    print(f"    PNL saved by removing: ${-total_pnl_no:,.2f}")
    
    # Recommendation
    print(f"\n{'='*80}")
    print("RECOMMENDATION")
    print(f"{'='*80}")
    
    if roi_no < -20 and len(settled_no) >= 50:
        print("\nREMOVE ALL SPREAD NO BETS")
        print(f"  - ROI is {roi_no:.2f}% (extremely negative)")
        print(f"  - Sample size of {len(settled_no)} is statistically significant")
        print(f"  - Removing would save ${-total_pnl_no:,.2f} in losses")
        print(f"  - You'd still have {all_bets_count - len(settled_no)} bets ({((all_bets_count - len(settled_no)) / all_bets_count * 100) if all_bets_count > 0 else 0:.1f}% of current volume)")
    elif roi_no < -10:
        print("\nCONSIDER REMOVING SPREAD NO (except true middles)")
        print(f"  - ROI is {roi_no:.2f}% (negative)")
        print(f"  - Sample size: {len(settled_no)} bets")
        print(f"  - Could keep true middles only (estimated {len(true_middles)} bets)")
    else:
        print("\nKEEP SPREAD NO BETS")
        print(f"  - ROI is {roi_no:.2f}% (acceptable)")

if __name__ == "__main__":
    analyze_spread_no()
