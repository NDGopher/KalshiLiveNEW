"""
Comprehensive diagnostic analysis to understand filter performance differences
"""
import os
from dotenv import load_dotenv

load_dotenv()

GOOGLE_SHEETS_CREDENTIALS_FILE = os.getenv('GOOGLE_SHEETS_CREDENTIALS_FILE', 'credentials.json')
GOOGLE_SHEETS_SPREADSHEET_ID = os.getenv('GOOGLE_SHEETS_SPREADSHEET_ID', '')
GOOGLE_SHEETS_WORKSHEET_NAME = os.getenv('GOOGLE_SHEETS_WORKSHEET_NAME', 'Auto-Bets')


def parse_float(s):
    """Parse float, handling currency symbols and commas"""
    if not s or s.strip() == '':
        return 0.0
    try:
        cleaned = str(s).replace('$', '').replace(',', '').replace(' ', '').replace('%', '').strip()
        return float(cleaned)
    except:
        return 0.0


def parse_int(s):
    """Parse integer"""
    if not s or s.strip() == '':
        return 0
    try:
        return int(float(str(s).replace(',', '').strip()))
    except:
        return 0


def get_column_index(header, column_name):
    """Find column index by name (case-insensitive, partial match)"""
    column_name_lower = column_name.lower()
    for idx, col in enumerate(header):
        if column_name_lower in col.lower():
            return idx
    return None


def analyze_filters():
    """Comprehensive diagnostic analysis"""
    if not GOOGLE_SHEETS_SPREADSHEET_ID:
        print("ERROR: GOOGLE_SHEETS_SPREADSHEET_ID not set")
        return
    
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        
        if not os.path.exists(GOOGLE_SHEETS_CREDENTIALS_FILE):
            print(f"ERROR: Credentials file not found")
            return
        
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_file(GOOGLE_SHEETS_CREDENTIALS_FILE, scopes=scope)
        google_sheets_client = gspread.authorize(creds)
        
        spreadsheet = google_sheets_client.open_by_key(GOOGLE_SHEETS_SPREADSHEET_ID)
        worksheet = spreadsheet.worksheet(GOOGLE_SHEETS_WORKSHEET_NAME)
        
        rows = worksheet.get_all_values()
        if len(rows) == 0:
            print("No rows in sheet")
            return
        
        # Detect header
        first_row = [col.strip() if col else '' for col in rows[0]]
        first_row_lower = [col.lower() for col in first_row]
        expected_headers = ['ticker', 'side', 'contracts', 'result', 'pnl', 'settled', 'cost', 'timestamp']
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
        
        # Find columns
        ticker_col = get_column_index(header, 'ticker')
        side_col = get_column_index(header, 'side')
        cost_col = get_column_index(header, 'cost')
        result_col = get_column_index(header, 'result')
        pnl_col = get_column_index(header, 'pnl')
        settled_col = get_column_index(header, 'settled')
        filter_name_col = get_column_index(header, 'filter name')
        devig_books_col = get_column_index(header, 'devig books')
        win_amount_col = get_column_index(header, 'win amount')
        sport_col = get_column_index(header, 'sport')
        market_type_col = get_column_index(header, 'market type')
        ev_col = get_column_index(header, 'ev')
        executed_price_col = get_column_index(header, 'executed price')
        expected_price_col = get_column_index(header, 'expected price')
        contracts_col = get_column_index(header, 'contracts')
        
        if any(col is None for col in [ticker_col, side_col, cost_col, result_col, filter_name_col]):
            print(f"ERROR: Missing required columns")
            return
        
        # Collect all bets
        all_bets = []
        
        for row_idx, row in enumerate(data_rows, start=2 if is_header else 1):
            if len(row) <= max(ticker_col, side_col, cost_col, result_col, filter_name_col):
                continue
            
            filter_name = row[filter_name_col].strip() if filter_name_col < len(row) else ''
            ticker = row[ticker_col].strip() if ticker_col < len(row) else ''
            side = row[side_col].strip().lower() if side_col < len(row) else ''
            result = row[result_col].strip().upper() if result_col < len(row) else ''
            settled = row[settled_col].strip().upper() if settled_col < len(row) else 'FALSE'
            cost = parse_float(row[cost_col] if cost_col < len(row) else '0')
            win_amount = parse_float(row[win_amount_col] if win_amount_col and win_amount_col < len(row) else '0')
            pnl_from_sheet = parse_float(row[pnl_col] if pnl_col and pnl_col < len(row) else '0')
            sport = row[sport_col].strip() if sport_col and sport_col < len(row) else 'Unknown'
            market_type = row[market_type_col].strip() if market_type_col and market_type_col < len(row) else 'Unknown'
            ev = parse_float(row[ev_col] if ev_col and ev_col < len(row) else '0')
            executed_price = parse_float(row[executed_price_col] if executed_price_col and executed_price_col < len(row) else '0')
            expected_price = parse_float(row[expected_price_col] if expected_price_col and expected_price_col < len(row) else '0')
            contracts = parse_int(row[contracts_col] if contracts_col and contracts_col < len(row) else '0')
            devig_books = row[devig_books_col].strip() if devig_books_col and devig_books_col < len(row) else ''
            
            if not ticker or not side or not filter_name:
                continue
            
            # Calculate P&L
            if settled == 'TRUE':
                if result == 'WIN':
                    calculated_pnl = win_amount if win_amount != 0 else pnl_from_sheet
                elif result == 'LOSS':
                    calculated_pnl = -cost
                else:
                    calculated_pnl = pnl_from_sheet if pnl_from_sheet != 0 else 0
            else:
                calculated_pnl = pnl_from_sheet
            
            # Determine filter category
            if '3 Sharps' in filter_name or '3 sharps' in filter_name.lower():
                filter_category = 'Kalshi_3_Sharps'
            elif 'CBB' in filter_name or 'cbb' in filter_name.lower():
                filter_category = 'CBB_Filter'
            else:
                filter_category = 'Other'
            
            # Parse devig books (extract book names)
            books_raw = [b.strip() for b in devig_books.split(',') if b.strip()] if devig_books else []
            book_names = []
            for book_raw in books_raw:
                if ':' in book_raw:
                    book_name = book_raw.split(':')[0].strip()
                else:
                    book_name = book_raw.strip()
                if book_name:
                    book_names.append(book_name)
            
            # Calculate price difference (executed vs expected)
            price_diff = executed_price - expected_price if executed_price > 0 and expected_price > 0 else 0
            
            all_bets.append({
                'filter_category': filter_category,
                'filter_name': filter_name,
                'sport': sport,
                'market_type': market_type,
                'ev': ev,
                'cost': cost,
                'pnl': calculated_pnl,
                'result': result,
                'settled': settled == 'TRUE',
                'win': result == 'WIN' and settled == 'TRUE',
                'loss': result == 'LOSS' and settled == 'TRUE',
                'book_names': book_names,
                'executed_price': executed_price,
                'expected_price': expected_price,
                'price_diff': price_diff,
                'contracts': contracts,
            })
        
        print("=" * 100)
        print("COMPREHENSIVE FILTER DIAGNOSTIC ANALYSIS")
        print("=" * 100)
        print()
        
        # Filter by category
        kalshi_bets = [b for b in all_bets if b['filter_category'] == 'Kalshi_3_Sharps']
        cbb_bets = [b for b in all_bets if b['filter_category'] == 'CBB_Filter']
        
        print(f"KALSHI 3 SHARPS: {len(kalshi_bets)} bets")
        print(f"CBB FILTER: {len(cbb_bets)} bets")
        print()
        
        # 1. SAMPLE SIZE ANALYSIS
        print("=" * 100)
        print("1. SAMPLE SIZE ANALYSIS")
        print("=" * 100)
        print()
        
        kalshi_settled = [b for b in kalshi_bets if b['settled']]
        cbb_settled = [b for b in cbb_bets if b['settled']]
        
        print(f"Kalshi 3 Sharps - Settled: {len(kalshi_settled)} bets")
        print(f"CBB Filter - Settled: {len(cbb_settled)} bets")
        print()
        print("Sample size assessment:")
        print(f"  - Kalshi 3 Sharps: {'SUFFICIENT' if len(kalshi_settled) >= 100 else 'SMALL'} sample ({len(kalshi_settled)} bets)")
        print(f"  - CBB Filter: {'SUFFICIENT' if len(cbb_settled) >= 100 else 'SMALL'} sample ({len(cbb_settled)} bets)")
        print()
        
        # 2. EV DISTRIBUTION ANALYSIS
        print("=" * 100)
        print("2. EV DISTRIBUTION ANALYSIS")
        print("=" * 100)
        print()
        
        def analyze_ev_ranges(bets, name):
            ev_ranges = {
                '0-5%': [],
                '5-8%': [],
                '8-10%': [],
                '10-15%': [],
                '15%+': []
            }
            
            for bet in bets:
                ev = bet['ev']
                if ev < 5:
                    ev_ranges['0-5%'].append(bet)
                elif ev < 8:
                    ev_ranges['5-8%'].append(bet)
                elif ev < 10:
                    ev_ranges['8-10%'].append(bet)
                elif ev < 15:
                    ev_ranges['10-15%'].append(bet)
                else:
                    ev_ranges['15%+'].append(bet)
            
            print(f"{name} - EV Distribution:")
            for range_name, range_bets in ev_ranges.items():
                if range_bets:
                    settled_bets = [b for b in range_bets if b['settled']]
                    wins = sum(1 for b in settled_bets if b['win'])
                    losses = sum(1 for b in settled_bets if b['loss'])
                    total_cost = sum(b['cost'] for b in range_bets)
                    total_pnl = sum(b['pnl'] for b in range_bets)
                    roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
                    win_rate = (wins / len(settled_bets) * 100) if settled_bets else 0
                    
                    print(f"  {range_name:8} EV: {len(range_bets):4} bets, {len(settled_bets):4} settled, "
                          f"{wins:3}W/{losses:3}L ({win_rate:5.1f}%), "
                          f"${total_pnl:8.2f} P&L, {roi:6.2f}% ROI")
            print()
            
            return ev_ranges
        
        kalshi_ev_ranges = analyze_ev_ranges(kalshi_bets, "Kalshi 3 Sharps")
        cbb_ev_ranges = analyze_ev_ranges(cbb_bets, "CBB Filter")
        
        # 3. SPORT BREAKDOWN
        print("=" * 100)
        print("3. SPORT BREAKDOWN")
        print("=" * 100)
        print()
        
        def analyze_sport(bets, name):
            sports = {}
            for bet in bets:
                sport = bet['sport']
                if sport not in sports:
                    sports[sport] = []
                sports[sport].append(bet)
            
            print(f"{name} - By Sport:")
            for sport, sport_bets in sorted(sports.items(), key=lambda x: len(x[1]), reverse=True):
                settled = [b for b in sport_bets if b['settled']]
                wins = sum(1 for b in settled if b['win'])
                losses = sum(1 for b in settled if b['loss'])
                total_cost = sum(b['cost'] for b in sport_bets)
                total_pnl = sum(b['pnl'] for b in sport_bets)
                roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
                win_rate = (wins / len(settled) * 100) if settled else 0
                
                print(f"  {sport:30} {len(sport_bets):4} bets, {len(settled):4} settled, "
                      f"{wins:3}W/{losses:3}L ({win_rate:5.1f}%), "
                      f"${total_pnl:8.2f} P&L, {roi:6.2f}% ROI")
            print()
        
        analyze_sport(kalshi_bets, "Kalshi 3 Sharps")
        analyze_sport(cbb_bets, "CBB Filter")
        
        # 4. MARKET TYPE BREAKDOWN
        print("=" * 100)
        print("4. MARKET TYPE BREAKDOWN")
        print("=" * 100)
        print()
        
        def analyze_market_type(bets, name):
            market_types = {}
            for bet in bets:
                mt = bet['market_type']
                if mt not in market_types:
                    market_types[mt] = []
                market_types[mt].append(bet)
            
            print(f"{name} - By Market Type:")
            for mt, mt_bets in sorted(market_types.items(), key=lambda x: len(x[1]), reverse=True):
                settled = [b for b in mt_bets if b['settled']]
                wins = sum(1 for b in settled if b['win'])
                losses = sum(1 for b in settled if b['loss'])
                total_cost = sum(b['cost'] for b in mt_bets)
                total_pnl = sum(b['pnl'] for b in mt_bets)
                roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
                win_rate = (wins / len(settled) * 100) if settled else 0
                
                print(f"  {mt:30} {len(mt_bets):4} bets, {len(settled):4} settled, "
                      f"{wins:3}W/{losses:3}L ({win_rate:5.1f}%), "
                      f"${total_pnl:8.2f} P&L, {roi:6.2f}% ROI")
            print()
        
        analyze_market_type(kalshi_bets, "Kalshi 3 Sharps")
        analyze_market_type(cbb_bets, "CBB Filter")
        
        # 5. PRICE EXECUTION ANALYSIS
        print("=" * 100)
        print("5. PRICE EXECUTION ANALYSIS")
        print("=" * 100)
        print()
        
        def analyze_price_execution(bets, name):
            bets_with_prices = [b for b in bets if b['executed_price'] > 0 and b['expected_price'] > 0]
            
            if not bets_with_prices:
                print(f"{name}: No price data available")
                return
            
            price_diffs = [b['price_diff'] for b in bets_with_prices]
            avg_diff = sum(price_diffs) / len(price_diffs) if price_diffs else 0
            
            # Group by price difference ranges
            ranges = {
                'Better than expected (>5¢)': [],
                'Better (0-5¢)': [],
                'Worse (0 to -5¢)': [],
                'Much worse (<-5¢)': []
            }
            
            for bet in bets_with_prices:
                diff = bet['price_diff']
                if diff > 5:
                    ranges['Better than expected (>5¢)'].append(bet)
                elif diff > 0:
                    ranges['Better (0-5¢)'].append(bet)
                elif diff > -5:
                    ranges['Worse (0 to -5¢)'].append(bet)
                else:
                    ranges['Much worse (<-5¢)'].append(bet)
            
            print(f"{name} - Price Execution:")
            print(f"  Average price difference: {avg_diff:.2f}¢ (positive = got better price)")
            print()
            
            for range_name, range_bets in ranges.items():
                if range_bets:
                    settled = [b for b in range_bets if b['settled']]
                    wins = sum(1 for b in settled if b['win'])
                    losses = sum(1 for b in settled if b['loss'])
                    total_cost = sum(b['cost'] for b in range_bets)
                    total_pnl = sum(b['pnl'] for b in range_bets)
                    roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
                    
                    print(f"  {range_name:30} {len(range_bets):4} bets, "
                          f"${total_pnl:8.2f} P&L, {roi:6.2f}% ROI")
            print()
        
        analyze_price_execution(kalshi_bets, "Kalshi 3 Sharps")
        analyze_price_execution(cbb_bets, "CBB Filter")
        
        # 6. DEVIG BOOK COMPARISON
        print("=" * 100)
        print("6. DEVIG BOOK USAGE COMPARISON")
        print("=" * 100)
        print()
        
        def analyze_books(bets, name):
            book_usage = {}
            for bet in bets:
                for book in bet['book_names']:
                    if book not in book_usage:
                        book_usage[book] = []
                    book_usage[book].append(bet)
            
            print(f"{name} - Devig Book Usage:")
            for book, book_bets in sorted(book_usage.items(), key=lambda x: len(x[1]), reverse=True):
                settled = [b for b in book_bets if b['settled']]
                wins = sum(1 for b in settled if b['win'])
                losses = sum(1 for b in settled if b['loss'])
                total_cost = sum(b['cost'] for b in book_bets)
                total_pnl = sum(b['pnl'] for b in book_bets)
                roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
                win_rate = (wins / len(settled) * 100) if settled else 0
                
                print(f"  {book:20} {len(book_bets):4} bets ({len(book_bets)/len(bets)*100:5.1f}%), "
                      f"{wins:3}W/{losses:3}L ({win_rate:5.1f}%), "
                      f"${total_pnl:8.2f} P&L, {roi:6.2f}% ROI")
            print()
        
        analyze_books(kalshi_bets, "Kalshi 3 Sharps")
        analyze_books(cbb_bets, "CBB Filter")
        
        # 7. KEY DIFFERENCES SUMMARY
        print("=" * 100)
        print("7. KEY DIFFERENCES SUMMARY")
        print("=" * 100)
        print()
        
        kalshi_settled = [b for b in kalshi_bets if b['settled']]
        cbb_settled = [b for b in cbb_bets if b['settled']]
        
        kalshi_wins = sum(1 for b in kalshi_settled if b['win'])
        cbb_wins = sum(1 for b in cbb_settled if b['win'])
        
        kalshi_win_rate = (kalshi_wins / len(kalshi_settled) * 100) if kalshi_settled else 0
        cbb_win_rate = (cbb_wins / len(cbb_settled) * 100) if cbb_settled else 0
        
        kalshi_total_cost = sum(b['cost'] for b in kalshi_bets)
        cbb_total_cost = sum(b['cost'] for b in cbb_bets)
        
        kalshi_total_pnl = sum(b['pnl'] for b in kalshi_bets)
        cbb_total_pnl = sum(b['pnl'] for b in cbb_bets)
        
        kalshi_roi = (kalshi_total_pnl / kalshi_total_cost * 100) if kalshi_total_cost > 0 else 0
        cbb_roi = (cbb_total_pnl / cbb_total_cost * 100) if cbb_total_cost > 0 else 0
        
        kalshi_avg_ev = sum(b['ev'] for b in kalshi_bets) / len(kalshi_bets) if kalshi_bets else 0
        cbb_avg_ev = sum(b['ev'] for b in cbb_bets) / len(cbb_bets) if cbb_bets else 0
        
        print(f"Kalshi 3 Sharps:")
        print(f"  - Win Rate: {kalshi_win_rate:.1f}%")
        print(f"  - ROI: {kalshi_roi:.2f}%")
        print(f"  - Avg EV: {kalshi_avg_ev:.2f}%")
        print(f"  - Sample: {len(kalshi_settled)} settled bets")
        print()
        
        print(f"CBB Filter:")
        print(f"  - Win Rate: {cbb_win_rate:.1f}%")
        print(f"  - ROI: {cbb_roi:.2f}%")
        print(f"  - Avg EV: {cbb_avg_ev:.2f}%")
        print(f"  - Sample: {len(cbb_settled)} settled bets")
        print()
        
        print(f"Difference:")
        print(f"  - Win Rate Gap: {kalshi_win_rate - cbb_win_rate:.1f} percentage points")
        print(f"  - ROI Gap: {kalshi_roi - cbb_roi:.2f} percentage points")
        print(f"  - EV Gap: {kalshi_avg_ev - cbb_avg_ev:.2f} percentage points")
        print()
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    analyze_filters()
