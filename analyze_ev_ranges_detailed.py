"""
Detailed analysis of EV ranges to verify ROI actually increases with higher EV
This will help determine if EV-based bet sizing is supported by the data
"""
import os
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from collections import defaultdict

load_dotenv()

_credentials_file = os.getenv('GOOGLE_SHEETS_CREDENTIALS_FILE', 'credentials.json')
if not os.path.isabs(_credentials_file):
    _credentials_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), _credentials_file)
GOOGLE_SHEETS_CREDENTIALS_FILE = _credentials_file
GOOGLE_SHEETS_SPREADSHEET_ID = os.getenv('GOOGLE_SHEETS_SPREADSHEET_ID', '')

def parse_float(value, default=0.0):
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
                    'filter_name': 21, 'market_type': 5
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
            
            # Skip spread NO bets for this analysis
            if 'spread' in market_type.lower() and side.lower() == 'no':
                continue
            
            bet = {
                'ticker': ticker,
                'side': side,
                'market_type': market_type,
                'cost': parse_float(row[get_col('cost')] if get_col('cost') and get_col('cost') < len(row) else '0'),
                'pnl': parse_float(row[get_col('pnl')] if get_col('pnl') and get_col('pnl') < len(row) else '0'),
                'sport': row[get_col('sport')].strip() if get_col('sport') and get_col('sport') < len(row) else '',
                'result': row[get_col('result')].strip().upper() if get_col('result') and get_col('result') < len(row) else '',
                'settled': row[get_col('settled')].strip().upper() if get_col('settled') and get_col('settled') < len(row) else 'FALSE',
                'filter_name': row[get_col('filter_name')].strip() if get_col('filter_name') and get_col('filter_name') < len(row) else 'Unknown',
                'ev': parse_float(row[get_col('ev')] if get_col('ev') and get_col('ev') < len(row) else '0')
            }
            
            bets.append(bet)
        
        return bets
    
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return []

def analyze_ev_ranges():
    """Analyze EV ranges in detail"""
    print(f"\n{'='*80}")
    print("DETAILED EV RANGE ANALYSIS (Excluding Spread NO)")
    print(f"{'='*80}")
    
    bets = get_all_bets()
    if not bets:
        print("No bets found!")
        return
    
    settled = [b for b in bets if b['settled'] == 'TRUE']
    print(f"\nTotal Settled Bets (excluding spread NO): {len(settled)}")
    
    # Define EV ranges
    ev_ranges = [
        (0, 5, "0-5%"),
        (5, 8, "5-8%"),
        (8, 10, "8-10%"),
        (10, 12, "10-12%"),
        (12, 15, "12-15%"),
        (15, 18, "15-18%"),
        (18, 20, "18-20%"),
        (20, 25, "20-25%"),
        (25, 100, "25%+")
    ]
    
    print(f"\n{'EV Range':<12} {'Bets':<8} {'Wins':<8} {'Losses':<8} {'Win Rate':<10} {'PNL':<12} {'Cost':<12} {'ROI':<10}")
    print("-" * 90)
    
    results = []
    for min_ev, max_ev, label in ev_ranges:
        range_bets = [b for b in settled if min_ev <= b['ev'] < max_ev]
        if not range_bets:
            continue
        
        wins = [b for b in range_bets if b['result'] == 'WIN']
        losses = [b for b in range_bets if b['result'] == 'LOSS']
        total_pnl = sum(b['pnl'] for b in range_bets)
        total_cost = sum(b['cost'] for b in range_bets)
        roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
        win_rate = (len(wins) / len(range_bets) * 100) if range_bets else 0
        
        results.append({
            'label': label,
            'min_ev': min_ev,
            'max_ev': max_ev,
            'bets': len(range_bets),
            'wins': len(wins),
            'losses': len(losses),
            'win_rate': win_rate,
            'pnl': total_pnl,
            'cost': total_cost,
            'roi': roi
        })
        
        print(f"{label:<12} {len(range_bets):<8} {len(wins):<8} {len(losses):<8} {win_rate:<10.1f} ${total_pnl:<11,.2f} ${total_cost:<11,.2f} {roi:<10.2f}%")
    
    # Analyze if ROI increases with EV
    print(f"\n{'='*80}")
    print("EV-BASED BET SIZING VALIDATION")
    print(f"{'='*80}")
    
    # Group into proposed bet sizing tiers
    tiers = {
        '5-10%': [r for r in results if 5 <= r['min_ev'] < 10],
        '10-15%': [r for r in results if 10 <= r['min_ev'] < 15],
        '15-20%': [r for r in results if 15 <= r['min_ev'] < 20],
        '20%+': [r for r in results if r['min_ev'] >= 20]
    }
    
    print(f"\nProposed Bet Sizing Tiers:")
    tier_results = {}
    for tier_name, tier_ranges in tiers.items():
        if not tier_ranges:
            continue
        
        combined_bets = sum(r['bets'] for r in tier_ranges)
        combined_wins = sum(r['wins'] for r in tier_ranges)
        combined_losses = sum(r['losses'] for r in tier_ranges)
        combined_pnl = sum(r['pnl'] for r in tier_ranges)
        combined_cost = sum(r['cost'] for r in tier_ranges)
        combined_roi = (combined_pnl / combined_cost * 100) if combined_cost > 0 else 0
        combined_wr = (combined_wins / combined_bets * 100) if combined_bets > 0 else 0
        
        tier_results[tier_name] = {
            'bets': combined_bets,
            'wins': combined_wins,
            'losses': combined_losses,
            'pnl': combined_pnl,
            'cost': combined_cost,
            'roi': combined_roi,
            'win_rate': combined_wr
        }
        
        print(f"\n  {tier_name} EV Range:")
        print(f"    Bets: {combined_bets} ({combined_wins}-{combined_losses}, {combined_wr:.1f}% WR)")
        print(f"    PNL: ${combined_pnl:,.2f}")
        print(f"    Cost: ${combined_cost:,.2f}")
        print(f"    ROI: {combined_roi:.2f}%")
    
    # Check if ROI increases with EV
    print(f"\n{'='*80}")
    print("ROI TREND ANALYSIS")
    print(f"{'='*80}")
    
    if len(tier_results) >= 2:
        tier_names = sorted(tier_results.keys(), key=lambda x: float(x.split('-')[0].replace('%', '').replace('+', '')))
        rois = [tier_results[t]['roi'] for t in tier_names]
        
        print(f"\nROI by EV Tier:")
        for tier_name in tier_names:
            print(f"  {tier_name}: {tier_results[tier_name]['roi']:.2f}%")
        
        # Check if ROI increases
        is_increasing = all(rois[i] <= rois[i+1] for i in range(len(rois)-1))
        is_decreasing = all(rois[i] >= rois[i+1] for i in range(len(rois)-1))
        
        if is_increasing:
            print(f"\n[SUPPORTED] ROI INCREASES with higher EV - EV-based bet sizing is SUPPORTED!")
        elif is_decreasing:
            print(f"\n[NOT SUPPORTED] ROI DECREASES with higher EV - EV-based bet sizing is NOT supported")
        else:
            print(f"\n[MIXED] ROI does NOT consistently increase with EV - Mixed results")
            print(f"   Consider more granular analysis or market-type specific EV ranges")
    
    # By market type
    print(f"\n{'='*80}")
    print("EV RANGES BY MARKET TYPE")
    print(f"{'='*80}")
    
    for market_type in ['Moneyline', 'Total Points', 'Point Spread']:
        mt_bets = [b for b in settled if market_type.lower() in b['market_type'].lower()]
        if not mt_bets:
            continue
        
        print(f"\n{market_type}:")
        mt_results = []
        for min_ev, max_ev, label in ev_ranges:
            range_bets = [b for b in mt_bets if min_ev <= b['ev'] < max_ev]
            if not range_bets:
                continue
            
            wins = [b for b in range_bets if b['result'] == 'WIN']
            total_pnl = sum(b['pnl'] for b in range_bets)
            total_cost = sum(b['cost'] for b in range_bets)
            roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
            
            mt_results.append({
                'label': label,
                'min_ev': min_ev,
                'bets': len(range_bets),
                'roi': roi
            })
            
            if len(range_bets) >= 5:  # Only show if meaningful sample
                print(f"  {label}: {len(range_bets)} bets, {roi:.2f}% ROI")
        
        # Check trend for this market type
        if len(mt_results) >= 2:
            mt_results_sorted = sorted(mt_results, key=lambda x: x['min_ev'])
            mt_rois = [r['roi'] for r in mt_results_sorted if r['bets'] >= 5]
            if len(mt_rois) >= 2:
                is_increasing = all(mt_rois[i] <= mt_rois[i+1] for i in range(len(mt_rois)-1))
                if is_increasing:
                    print(f"  [SUPPORTED] ROI increases with EV for {market_type}")
                else:
                    print(f"  [MIXED] ROI does not consistently increase with EV for {market_type}")

if __name__ == "__main__":
    analyze_ev_ranges()
