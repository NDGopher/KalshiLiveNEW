"""
Analyze betting performance day over day and reverse middle bets
"""
import os
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from collections import defaultdict

load_dotenv()

# Google Sheets configuration
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

def analyze_reverse_middles():
    """Analyze reverse middle bets from Google Sheets"""
    if not GOOGLE_SHEETS_SPREADSHEET_ID:
        print("ERROR: GOOGLE_SHEETS_SPREADSHEET_ID not set")
        return
    
    try:
        scope = ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_file(GOOGLE_SHEETS_CREDENTIALS_FILE, scopes=scope)
        google_sheets_client = gspread.authorize(creds)
        
        spreadsheet = google_sheets_client.open_by_key(GOOGLE_SHEETS_SPREADSHEET_ID)
        worksheet = spreadsheet.worksheet('Auto-Bets')
        
        rows = worksheet.get_all_values()
        if len(rows) == 0:
            print("No rows in sheet")
            return
        
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
            # Positional: ticker(2), side(3), result(18), pnl(19), cost(13), filter_name(21)
            header = ['Timestamp', 'Order ID', 'Ticker', 'Side', 'Teams', 'Market Type', 'Pick', 'Qualifier',
                     'EV %', 'Expected Price (¢)', 'Executed Price (¢)', 'American Odds',
                     'Contracts', 'Cost ($)', 'Payout ($)', 'Win Amount ($)', 'Sport', 'Status', 'Result', 'PNL ($)', 'Settled', 'Filter Name', 'Devig Books']
            data_rows = rows
        
        # Find columns
        def get_col(name):
            if is_header:
                header_lower = [h.lower() if h else '' for h in header]
                for idx, h in enumerate(header_lower):
                    if name.lower() in h:
                        return idx
            else:
                col_map = {'ticker': 2, 'side': 3, 'result': 18, 'pnl': 19, 'cost': 13, 'filter_name': 21, 'devig_books': 22}
                return col_map.get(name)
            return None
        
        ticker_col = get_col('ticker')
        side_col = get_col('side')
        result_col = get_col('result')
        pnl_col = get_col('pnl')
        cost_col = get_col('cost')
        filter_col = get_col('filter_name')
        devig_col = get_col('devig_books')
        
        # Group bets by event (extract event ticker from market ticker)
        event_bets = defaultdict(list)
        
        for row in data_rows:
            if len(row) <= max(ticker_col, side_col, result_col, pnl_col, cost_col, filter_col):
                continue
            
            ticker = row[ticker_col].strip().upper() if ticker_col < len(row) else ''
            side = row[side_col].strip().lower() if side_col < len(row) else ''
            result = row[result_col].strip().upper() if result_col < len(row) else ''
            settled = row[get_col('settled')].strip().upper() if get_col('settled') and get_col('settled') < len(row) else 'FALSE'
            
            if not ticker or not side:
                continue
            
            # Extract event ticker (everything before the last dash)
            # Format: KXNBAGAME-26JAN21OKCMIL-TOTAL-O234.5 -> event = KXNBAGAME-26JAN21OKCMIL
            parts = ticker.split('-')
            if len(parts) >= 3:
                event_ticker = '-'.join(parts[:-1])  # Everything except last part
            else:
                event_ticker = ticker
            
            cost = parse_float(row[cost_col] if cost_col < len(row) else '0')
            pnl = parse_float(row[pnl_col] if pnl_col < len(row) else '0')
            filter_name = row[filter_col].strip() if filter_col and filter_col < len(row) else 'Unknown'
            devig_books = row[devig_col].strip() if devig_col and devig_col < len(row) else ''
            
            event_bets[event_ticker].append({
                'ticker': ticker,
                'side': side,
                'result': result,
                'settled': settled == 'TRUE',
                'cost': cost,
                'pnl': pnl,
                'filter_name': filter_name,
                'devig_books': devig_books
            })
        
        # Find reverse middles (same event, opposite sides)
        reverse_middles = []
        for event_ticker, bets in event_bets.items():
            if len(bets) < 2:
                continue
            
            # Check for YES and NO bets on same event
            yes_bets = [b for b in bets if b['side'] == 'yes']
            no_bets = [b for b in bets if b['side'] == 'no']
            
            if yes_bets and no_bets:
                # This is a reverse middle
                for yes_bet in yes_bets:
                    for no_bet in no_bets:
                        reverse_middles.append({
                            'event': event_ticker,
                            'yes_bet': yes_bet,
                            'no_bet': no_bet,
                            'total_cost': yes_bet['cost'] + no_bet['cost'],
                            'total_pnl': yes_bet['pnl'] + no_bet['pnl'],
                            'both_settled': yes_bet['settled'] and no_bet['settled']
                        })
        
        print(f"\n{'='*80}")
        print("REVERSE MIDDLE ANALYSIS")
        print(f"{'='*80}")
        print(f"\nFound {len(reverse_middles)} reverse middle pairs")
        
        if reverse_middles:
            settled_middles = [rm for rm in reverse_middles if rm['both_settled']]
            print(f"Settled reverse middles: {len(settled_middles)}")
            
            if settled_middles:
                wins = [rm for rm in settled_middles if rm['total_pnl'] > 0]
                losses = [rm for rm in settled_middles if rm['total_pnl'] < 0]
                pushes = [rm for rm in settled_middles if abs(rm['total_pnl']) < 1.0]
                
                total_pnl = sum(rm['total_pnl'] for rm in settled_middles)
                total_cost = sum(rm['total_cost'] for rm in settled_middles)
                roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
                
                print(f"\nReverse Middle Performance:")
                print(f"  Total PNL: ${total_pnl:,.2f}")
                print(f"  Total Cost: ${total_cost:,.2f}")
                print(f"  ROI: {roi:.2f}%")
                print(f"  Wins: {len(wins)} ({len(wins)/len(settled_middles)*100:.1f}%)")
                print(f"  Losses: {len(losses)} ({len(losses)/len(settled_middles)*100:.1f}%)")
                print(f"  Pushes: {len(pushes)} ({len(pushes)/len(settled_middles)*100:.1f}%)")
                
                print(f"\nSample Reverse Middles:")
                for i, rm in enumerate(settled_middles[:10], 1):
                    print(f"\n  {i}. Event: {rm['event']}")
                    print(f"     YES: {rm['yes_bet']['ticker']} - {rm['yes_bet']['result']} (${rm['yes_bet']['pnl']:.2f})")
                    print(f"     NO:  {rm['no_bet']['ticker']} - {rm['no_bet']['result']} (${rm['no_bet']['pnl']:.2f})")
                    print(f"     Total PNL: ${rm['total_pnl']:.2f} | Cost: ${rm['total_cost']:.2f}")
        
        return reverse_middles
    
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return []

def analyze_px_novig_multiplier():
    """Analyze ProphetX + Novig multiplier bets"""
    if not GOOGLE_SHEETS_SPREADSHEET_ID:
        return
    
    try:
        scope = ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_file(GOOGLE_SHEETS_CREDENTIALS_FILE, scopes=scope)
        google_sheets_client = gspread.authorize(creds)
        
        spreadsheet = google_sheets_client.open_by_key(GOOGLE_SHEETS_SPREADSHEET_ID)
        worksheet = spreadsheet.worksheet('Auto-Bets')
        
        rows = worksheet.get_all_values()
        if len(rows) == 0:
            return
        
        first_row = [col.strip() if col else '' for col in rows[0]]
        first_row_lower = [col.lower() for col in first_row]
        expected_headers = ['ticker', 'side', 'result', 'pnl', 'cost', 'devig books']
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
                col_map = {'ticker': 2, 'side': 3, 'result': 18, 'pnl': 19, 'cost': 13, 'devig_books': 22}
                return col_map.get(name)
            return None
        
        ticker_col = get_col('ticker')
        side_col = get_col('side')
        result_col = get_col('result')
        pnl_col = get_col('pnl')
        cost_col = get_col('cost')
        devig_col = get_col('devig_books')
        
        px_novig_bets = []
        other_bets = []
        
        for row in data_rows:
            if len(row) <= max(ticker_col, side_col, result_col, pnl_col, cost_col, devig_col):
                continue
            
            ticker = row[ticker_col].strip().upper() if ticker_col < len(row) else ''
            side = row[side_col].strip().lower() if side_col < len(row) else ''
            result = row[result_col].strip().upper() if result_col < len(row) else ''
            settled = row[get_col('settled')].strip().upper() if get_col('settled') and get_col('settled') < len(row) else 'FALSE'
            
            if not ticker or not side:
                continue
            
            cost = parse_float(row[cost_col] if cost_col < len(row) else '0')
            pnl = parse_float(row[pnl_col] if pnl_col < len(row) else '0')
            devig_books = row[devig_col].strip() if devig_col and devig_col < len(row) else ''
            
            # Check if both ProphetX and Novig are in devig_books
            has_px = 'ProphetX' in devig_books or 'PX' in devig_books
            has_novig = 'Novig' in devig_books or 'NV' in devig_books
            
            bet_data = {
                'ticker': ticker,
                'side': side,
                'result': result,
                'settled': settled == 'TRUE',
                'cost': cost,
                'pnl': pnl,
                'devig_books': devig_books
            }
            
            if has_px and has_novig:
                px_novig_bets.append(bet_data)
            else:
                other_bets.append(bet_data)
        
        print(f"\n{'='*80}")
        print("PROPHETX + NOVIG MULTIPLIER ANALYSIS")
        print(f"{'='*80}")
        
        if px_novig_bets:
            settled_px_novig = [b for b in px_novig_bets if b['settled']]
            if settled_px_novig:
                wins = [b for b in settled_px_novig if b['result'] == 'WIN']
                losses = [b for b in settled_px_novig if b['result'] == 'LOSS']
                total_pnl = sum(b['pnl'] for b in settled_px_novig)
                total_cost = sum(b['cost'] for b in settled_px_novig)
                roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
                win_rate = (len(wins) / len(settled_px_novig) * 100) if settled_px_novig else 0
                
                print(f"\nProphetX + Novig Bets (1.5x multiplier):")
                print(f"  Total Bets: {len(px_novig_bets)} ({len(settled_px_novig)} settled)")
                print(f"  Wins: {len(wins)}")
                print(f"  Losses: {len(losses)}")
                print(f"  Win Rate: {win_rate:.1f}%")
                print(f"  Total PNL: ${total_pnl:,.2f}")
                print(f"  Total Cost: ${total_cost:,.2f}")
                print(f"  ROI: {roi:.2f}%")
                
                # Compare to other bets
                settled_other = [b for b in other_bets if b['settled']]
                if settled_other:
                    other_wins = [b for b in settled_other if b['result'] == 'WIN']
                    other_pnl = sum(b['pnl'] for b in settled_other)
                    other_cost = sum(b['cost'] for b in settled_other)
                    other_roi = (other_pnl / other_cost * 100) if other_cost > 0 else 0
                    other_wr = (len(other_wins) / len(settled_other) * 100) if settled_other else 0
                    
                    print(f"\nComparison to Other Bets:")
                    print(f"  Other Bets: {len(other_bets)} ({len(settled_other)} settled)")
                    print(f"  Other Win Rate: {other_wr:.1f}%")
                    print(f"  Other ROI: {other_roi:.2f}%")
                    print(f"  Other PNL: ${other_pnl:,.2f}")
                    
                    print(f"\nMultiplier Performance:")
                    print(f"  PX+Novig ROI: {roi:.2f}% vs Other: {other_roi:.2f}% (diff: {roi - other_roi:+.2f}%)")
                    print(f"  PX+Novig Win Rate: {win_rate:.1f}% vs Other: {other_wr:.1f}% (diff: {win_rate - other_wr:+.1f}%)")
        else:
            print("\nNo ProphetX + Novig bets found")
    
    except Exception as e:
        print(f"ERROR analyzing PX+Novig: {e}")
        import traceback
        traceback.print_exc()

def analyze_day_over_day():
    """Compare performance across the three HTML files"""
    print(f"\n{'='*80}")
    print("DAY OVER DAY PERFORMANCE ANALYSIS")
    print(f"{'='*80}")
    
    # Data extracted from HTML files
    days = {
        '2026-01-19': {
            'total_pnl': 946.43,
            'roi': 1.86,
            'win_rate': 48.0,
            'total_bets': 519,
            'settled': 515,
            'total_cost': 50773.02,
            'kalshi_3_sharps': {'pnl': 3453.85, 'cost': 21606.60, 'roi': 15.99, 'bets': 209, 'wins': 119, 'losses': 86},
            'cbb_filter': {'pnl': -2440.67, 'cost': 28865.67, 'roi': -8.46, 'bets': 307, 'wins': 127, 'losses': 180},
            'spreads': {'pnl': -4252.01, 'cost': 20641.69, 'roi': -20.60, 'bets': 215, 'wins': 76, 'losses': 137},
            'moneylines': {'pnl': 4155.11, 'cost': 9231.16, 'roi': 45.01, 'bets': 92, 'wins': 67, 'losses': 24},
            'novig': {'pnl': 884.44, 'cost': 3314.08, 'roi': 26.69, 'bets': 103, 'wins': 66, 'losses': 35},
            'prophetx': {'pnl': 382.22, 'cost': 1923.87, 'roi': 19.87, 'bets': 60, 'wins': 39, 'losses': 20}
        },
        '2026-01-20': {
            'total_pnl': 1078.72,
            'roi': 1.85,
            'win_rate': 48.0,
            'total_bets': 598,
            'settled': 598,
            'total_cost': 58236.28,
            'kalshi_3_sharps': {'pnl': 2793.60, 'cost': 24748.40, 'roi': 11.29, 'bets': 243, 'wins': 136, 'losses': 107},
            'cbb_filter': {'pnl': -1859.29, 'cost': 32986.29, 'roi': -5.64, 'bets': 350, 'wins': 148, 'losses': 202},
            'spreads': {'pnl': -3510.52, 'cost': 24228.52, 'roi': -14.49, 'bets': 255, 'wins': 98, 'losses': 157},
            'moneylines': {'pnl': 3422.87, 'cost': 10203.13, 'roi': 33.55, 'bets': 102, 'wins': 69, 'losses': 33},
            'novig': {'pnl': 856.62, 'cost': 3605.23, 'roi': 23.76, 'bets': 116, 'wins': 73, 'losses': 43},
            'prophetx': {'pnl': 397.15, 'cost': 1923.87, 'roi': 20.64, 'bets': 60, 'wins': 40, 'losses': 20}
        },
        '2026-01-22': {
            'total_pnl': 2745.09,
            'roi': 4.25,
            'win_rate': 48.8,
            'total_bets': 666,
            'settled': 666,
            'total_cost': 64649.91,
            'kalshi_3_sharps': {'pnl': 4449.28, 'cost': 29360.72, 'roi': 15.15, 'bets': 290, 'wins': 165, 'losses': 125},
            'cbb_filter': {'pnl': -1704.19, 'cost': 35289.19, 'roi': -4.83, 'bets': 376, 'wins': 160, 'losses': 216},
            'spreads': {'pnl': -3195.41, 'cost': 27214.41, 'roi': -11.74, 'bets': 288, 'wins': 114, 'losses': 174},
            'moneylines': {'pnl': 4108.74, 'cost': 11096.26, 'roi': 37.03, 'bets': 110, 'wins': 76, 'losses': 34},
            'novig': {'pnl': 1115.77, 'cost': 4190.00, 'roi': 26.63, 'bets': 135, 'wins': 86, 'losses': 49},
            'prophetx': {'pnl': 481.69, 'cost': 2020.63, 'roi': 23.84, 'bets': 65, 'wins': 43, 'losses': 22}
        }
    }
    
    print(f"\nDAY OVER DAY COMPARISON")
    print(f"{'-'*80}")
    
    for date, data in days.items():
        print(f"\n{date}:")
        print(f"  Total PNL: ${data['total_pnl']:,.2f} | ROI: {data['roi']:.2f}% | Win Rate: {data['win_rate']:.1f}%")
        print(f"  Total Bets: {data['total_bets']} | Cost: ${data['total_cost']:,.2f}")
        print(f"  Kalshi 3 Sharps: ${data['kalshi_3_sharps']['pnl']:,.2f} ({data['kalshi_3_sharps']['roi']:.2f}% ROI)")
        print(f"  CBB Filter: ${data['cbb_filter']['pnl']:,.2f} ({data['cbb_filter']['roi']:.2f}% ROI)")
    
    # Calculate changes
    print(f"\nCHANGES FROM JAN 19 TO JAN 22:")
    jan19 = days['2026-01-19']
    jan22 = days['2026-01-22']
    
    pnl_change = jan22['total_pnl'] - jan19['total_pnl']
    roi_change = jan22['roi'] - jan19['roi']
    bets_change = jan22['total_bets'] - jan19['total_bets']
    
    print(f"  PNL: ${jan19['total_pnl']:,.2f} -> ${jan22['total_pnl']:,.2f} (${pnl_change:+,.2f})")
    print(f"  ROI: {jan19['roi']:.2f}% -> {jan22['roi']:.2f}% ({roi_change:+.2f}%)")
    print(f"  Total Bets: {jan19['total_bets']} -> {jan22['total_bets']} (+{bets_change})")
    print(f"  Kalshi 3 Sharps PNL: ${jan19['kalshi_3_sharps']['pnl']:,.2f} -> ${jan22['kalshi_3_sharps']['pnl']:,.2f} (${jan22['kalshi_3_sharps']['pnl'] - jan19['kalshi_3_sharps']['pnl']:+,.2f})")
    print(f"  CBB Filter PNL: ${jan19['cbb_filter']['pnl']:,.2f} -> ${jan22['cbb_filter']['pnl']:,.2f} (${jan22['cbb_filter']['pnl'] - jan19['cbb_filter']['pnl']:+,.2f})")
    
    # Analyze what's working
    print(f"\n{'='*80}")
    print("WHAT'S WORKING")
    print(f"{'='*80}")
    
    print(f"\nSTRONG PERFORMERS (Jan 22):")
    print(f"  1. Moneylines: ${jan22['moneylines']['pnl']:,.2f} ({jan22['moneylines']['roi']:.2f}% ROI) - {jan22['moneylines']['bets']} bets")
    print(f"  2. Kalshi 3 Sharps: ${jan22['kalshi_3_sharps']['pnl']:,.2f} ({jan22['kalshi_3_sharps']['roi']:.2f}% ROI) - {jan22['kalshi_3_sharps']['bets']} bets")
    print(f"  3. Novig: ${jan22['novig']['pnl']:,.2f} ({jan22['novig']['roi']:.2f}% ROI) - {jan22['novig']['bets']} bets")
    print(f"  4. ProphetX: ${jan22['prophetx']['pnl']:,.2f} ({jan22['prophetx']['roi']:.2f}% ROI) - {jan22['prophetx']['bets']} bets")
    
    print(f"\nWEAK PERFORMERS (Jan 22):")
    print(f"  1. Point Spreads: ${jan22['spreads']['pnl']:,.2f} ({jan22['spreads']['roi']:.2f}% ROI) - {jan22['spreads']['bets']} bets")
    print(f"  2. CBB Filter: ${jan22['cbb_filter']['pnl']:,.2f} ({jan22['cbb_filter']['roi']:.2f}% ROI) - {jan22['cbb_filter']['bets']} bets")
    print(f"     - CBB Spreads: -16.99% ROI (184 bets)")
    print(f"     - CBB Totals: -1.62% ROI (159 bets)")
    print(f"     - CBB Moneylines: +44.96% ROI (33 bets) - EXCELLENT!")

if __name__ == "__main__":
    analyze_day_over_day()
    reverse_middles = analyze_reverse_middles()
    analyze_px_novig_multiplier()
    
    print(f"\n{'='*80}")
    print("RECOMMENDATIONS")
    print(f"{'='*80}")
    print("""
Based on the analysis:

1. **Kalshi 3 Sharps Filter is CRUSHING IT** (15.15% ROI)
   - This is your bread and butter - keep it as is
   - NBA performance is exceptional (19.97% ROI)
   - Moneylines are printing money (33.73% ROI)

2. **CBB Filter is Improving** (-8.46% -> -4.83% ROI)
   - The 10% EV threshold change is helping
   - CBB Moneylines are excellent (44.96% ROI) - consider focusing here
   - CBB Spreads are killing you (-16.99% ROI) - consider removing or raising EV threshold further
   - CBB Totals are slightly negative (-1.62% ROI) - acceptable but not great

3. **ProphetX + Novig Multiplier**
   - Need to check actual performance in Google Sheets
   - If working well, consider increasing multiplier or expanding usage

4. **Point Spreads are Problematic** (-11.74% ROI overall)
   - This is your biggest weakness across all filters
   - Consider:
     a) Removing spreads entirely
     b) Raising EV threshold significantly for spreads (15%+)
     c) Requiring more sharp books for spreads
     d) Lowering bet amounts for spreads

5. **Moneylines are Your Strength** (37.03% ROI)
   - Consider increasing bet amounts for moneylines
   - Focus filters on moneylines when possible

6. **Reverse Middles**
   - Need to analyze if these are helping or hurting
   - If they're working, consider a dedicated reverse middle strategy
   - If not, consider preventing reverse middles

7. **Devig Books Performance**
   - Novig (26.63% ROI) and ProphetX (23.84% ROI) are excellent
   - Pinnacle (2.73% ROI) is barely profitable - consider if it's worth including
   - FanDuel (-8.55% ROI) and DraftKings (-5.30% ROI) are losing money
   - Consider removing public books (FD/DK) or requiring them to be paired with sharp books
""")
