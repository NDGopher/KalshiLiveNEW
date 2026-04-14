"""
CBB Filter Optimization Analysis:
1. Performance at 10%+ EV vs <10% EV
2. Spreads/Totals performance at 10%+ EV
3. Odds range analysis
4. Bet sizing opportunities (ProphetX/Novig)
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


def analyze_cbb_optimization():
    """Comprehensive CBB optimization analysis"""
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
        market_type_col = get_column_index(header, 'market type')
        ev_col = get_column_index(header, 'ev')
        american_odds_col = get_column_index(header, 'american odds')
        contracts_col = get_column_index(header, 'contracts')
        
        if any(col is None for col in [ticker_col, side_col, cost_col, result_col, filter_name_col]):
            print(f"ERROR: Missing required columns")
            return
        
        # Collect CBB bets only
        cbb_bets = []
        
        for row_idx, row in enumerate(data_rows, start=2 if is_header else 1):
            if len(row) <= max(ticker_col, side_col, cost_col, result_col, filter_name_col):
                continue
            
            filter_name = row[filter_name_col].strip() if filter_name_col < len(row) else ''
            
            # Only CBB filter bets
            if 'CBB' not in filter_name and 'cbb' not in filter_name.lower():
                continue
            
            ticker = row[ticker_col].strip() if ticker_col < len(row) else ''
            side = row[side_col].strip().lower() if side_col < len(row) else ''
            result = row[result_col].strip().upper() if result_col < len(row) else ''
            settled = row[settled_col].strip().upper() if settled_col < len(row) else 'FALSE'
            cost = parse_float(row[cost_col] if cost_col < len(row) else '0')
            win_amount = parse_float(row[win_amount_col] if win_amount_col and win_amount_col < len(row) else '0')
            pnl_from_sheet = parse_float(row[pnl_col] if pnl_col and pnl_col < len(row) else '0')
            market_type = row[market_type_col].strip() if market_type_col and market_type_col < len(row) else 'Unknown'
            ev = parse_float(row[ev_col] if ev_col and ev_col < len(row) else '0')
            american_odds = parse_float(row[american_odds_col] if american_odds_col and american_odds_col < len(row) else 0)
            contracts = parse_int(row[contracts_col] if contracts_col and contracts_col < len(row) else '0')
            devig_books = row[devig_books_col].strip() if devig_books_col and devig_books_col < len(row) else ''
            
            if not ticker or not side:
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
            
            # Parse devig books
            books_raw = [b.strip() for b in devig_books.split(',') if b.strip()] if devig_books else []
            book_names = []
            for book_raw in books_raw:
                if ':' in book_raw:
                    book_name = book_raw.split(':')[0].strip()
                else:
                    book_name = book_raw.strip()
                if book_name:
                    book_names.append(book_name)
            
            cbb_bets.append({
                'ev': ev,
                'market_type': market_type,
                'cost': cost,
                'pnl': calculated_pnl,
                'result': result,
                'settled': settled == 'TRUE',
                'win': result == 'WIN' and settled == 'TRUE',
                'loss': result == 'LOSS' and settled == 'TRUE',
                'book_names': book_names,
                'american_odds': american_odds,
                'contracts': contracts,
            })
        
        print("=" * 100)
        print("CBB FILTER OPTIMIZATION ANALYSIS")
        print("=" * 100)
        print(f"\nTotal CBB bets: {len(cbb_bets)}")
        print()
        
        # 1. EV THRESHOLD ANALYSIS (8% vs 10%+)
        print("=" * 100)
        print("1. EV THRESHOLD ANALYSIS")
        print("=" * 100)
        print()
        
        def analyze_ev_threshold(bets, threshold, name):
            filtered = [b for b in bets if b['ev'] >= threshold and b['settled']]
            if not filtered:
                print(f"{name}: No bets")
                return None
            
            wins = sum(1 for b in filtered if b['win'])
            losses = sum(1 for b in filtered if b['loss'])
            total_cost = sum(b['cost'] for b in filtered)
            total_pnl = sum(b['pnl'] for b in filtered)
            roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
            win_rate = (wins / len(filtered) * 100) if filtered else 0
            
            print(f"{name}:")
            print(f"  Bets: {len(filtered)}")
            print(f"  Wins: {wins}, Losses: {losses}")
            print(f"  Win Rate: {win_rate:.1f}%")
            print(f"  Total Cost: ${total_cost:,.2f}")
            print(f"  Total P&L: ${total_pnl:,.2f}")
            print(f"  ROI: {roi:.2f}%")
            print()
            
            return {
                'bets': len(filtered),
                'wins': wins,
                'losses': losses,
                'win_rate': win_rate,
                'total_cost': total_cost,
                'total_pnl': total_pnl,
                'roi': roi
            }
        
        all_settled = [b for b in cbb_bets if b['settled']]
        below_10 = analyze_ev_threshold(all_settled, 0, "All CBB bets (<10% EV)")
        at_10_plus = analyze_ev_threshold(all_settled, 10, "CBB bets at 10%+ EV")
        
        if below_10 and at_10_plus:
            reduction = ((below_10['bets'] - at_10_plus['bets']) / below_10['bets'] * 100) if below_10['bets'] > 0 else 0
            print(f"Moving to 10%+ EV would reduce bets by {reduction:.1f}% ({below_10['bets'] - at_10_plus['bets']} bets)")
            print(f"ROI improvement: {at_10_plus['roi'] - below_10['roi']:.2f} percentage points")
            print()
        
        # 2. MARKET TYPE PERFORMANCE AT 10%+ EV
        print("=" * 100)
        print("2. MARKET TYPE PERFORMANCE AT 10%+ EV")
        print("=" * 100)
        print()
        
        def analyze_market_type_at_ev(bets, ev_threshold, market_type_name):
            filtered = [b for b in bets if b['ev'] >= ev_threshold and b['market_type'] == market_type_name and b['settled']]
            if not filtered:
                return None
            
            wins = sum(1 for b in filtered if b['win'])
            losses = sum(1 for b in filtered if b['loss'])
            total_cost = sum(b['cost'] for b in filtered)
            total_pnl = sum(b['pnl'] for b in filtered)
            roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
            win_rate = (wins / len(filtered) * 100) if filtered else 0
            
            return {
                'bets': len(filtered),
                'wins': wins,
                'losses': losses,
                'win_rate': win_rate,
                'total_cost': total_cost,
                'total_pnl': total_pnl,
                'roi': roi
            }
        
        market_types = ['Point Spread', 'Total Points', 'Moneyline']
        
        print("Performance at 10%+ EV by Market Type:")
        print()
        for mt in market_types:
            stats_10plus = analyze_market_type_at_ev(all_settled, 10, mt)
            stats_all = analyze_market_type_at_ev(all_settled, 0, mt)
            
            if stats_10plus:
                print(f"{mt}:")
                print(f"  At 10%+ EV: {stats_10plus['bets']} bets, {stats_10plus['wins']}W/{stats_10plus['losses']}L, "
                      f"{stats_10plus['win_rate']:.1f}% win rate, ${stats_10plus['total_pnl']:,.2f} P&L, {stats_10plus['roi']:.2f}% ROI")
                if stats_all:
                    print(f"  All EV:     {stats_all['bets']} bets, {stats_all['wins']}W/{stats_all['losses']}L, "
                          f"{stats_all['win_rate']:.1f}% win rate, ${stats_all['total_pnl']:,.2f} P&L, {stats_all['roi']:.2f}% ROI")
                    improvement = stats_10plus['roi'] - stats_all['roi']
                    print(f"  Improvement: {improvement:+.2f} percentage points")
                print()
        
        # 3. ODDS RANGE ANALYSIS
        print("=" * 100)
        print("3. ODDS RANGE ANALYSIS")
        print("=" * 100)
        print()
        
        def analyze_odds_range(bets, min_odds, max_odds, name):
            filtered = [b for b in bets if min_odds <= b['american_odds'] < max_odds and b['settled']]
            if not filtered:
                return None
            
            wins = sum(1 for b in filtered if b['win'])
            losses = sum(1 for b in filtered if b['loss'])
            total_cost = sum(b['cost'] for b in filtered)
            total_pnl = sum(b['pnl'] for b in filtered)
            roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
            win_rate = (wins / len(filtered) * 100) if filtered else 0
            
            return {
                'bets': len(filtered),
                'wins': wins,
                'losses': losses,
                'win_rate': win_rate,
                'total_cost': total_cost,
                'total_pnl': total_pnl,
                'roi': roi
            }
        
        odds_ranges = [
            (-1000, -500, "Heavy Favorites (-1000 to -500)"),
            (-500, -200, "Favorites (-500 to -200)"),
            (-200, -110, "Moderate Favorites (-200 to -110)"),
            (-110, 110, "Near Even (-110 to +110)"),
            (110, 200, "Moderate Underdogs (+110 to +200)"),
            (200, 500, "Underdogs (+200 to +500)"),
            (500, 1000, "Heavy Underdogs (+500 to +1000)"),
        ]
        
        print("CBB Performance by Odds Range:")
        print()
        for min_odds, max_odds, name in odds_ranges:
            stats = analyze_odds_range(all_settled, min_odds, max_odds, name)
            if stats and stats['bets'] > 0:
                print(f"{name}:")
                print(f"  {stats['bets']} bets, {stats['wins']}W/{stats['losses']}L, "
                      f"{stats['win_rate']:.1f}% win rate, ${stats['total_pnl']:,.2f} P&L, {stats['roi']:.2f}% ROI")
        print()
        
        # 4. BET SIZING OPPORTUNITIES (ProphetX/Novig)
        print("=" * 100)
        print("4. BET SIZING OPPORTUNITIES (ProphetX/Novig)")
        print("=" * 100)
        print()
        
        def analyze_book_combination(bets, required_books, name):
            filtered = [b for b in bets if all(book in b['book_names'] for book in required_books) and b['settled']]
            if not filtered:
                return None
            
            wins = sum(1 for b in filtered if b['win'])
            losses = sum(1 for b in filtered if b['loss'])
            total_cost = sum(b['cost'] for b in filtered)
            total_pnl = sum(b['pnl'] for b in filtered)
            roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
            win_rate = (wins / len(filtered) * 100) if filtered else 0
            
            # Calculate average bet size
            avg_cost = total_cost / len(filtered) if filtered else 0
            
            return {
                'bets': len(filtered),
                'wins': wins,
                'losses': losses,
                'win_rate': win_rate,
                'total_cost': total_cost,
                'total_pnl': total_pnl,
                'roi': roi,
                'avg_cost': avg_cost
            }
        
        # Check ProphetX alone
        px_stats = analyze_book_combination(all_settled, ['ProphetX'], "ProphetX only")
        if px_stats:
            print("ProphetX (alone):")
            print(f"  {px_stats['bets']} bets, {px_stats['wins']}W/{px_stats['losses']}L, "
                  f"{px_stats['win_rate']:.1f}% win rate, ${px_stats['total_pnl']:,.2f} P&L, {px_stats['roi']:.2f}% ROI")
            print(f"  Avg bet size: ${px_stats['avg_cost']:.2f}")
            print()
        
        # Check Novig alone
        novig_stats = analyze_book_combination(all_settled, ['Novig'], "Novig only")
        if novig_stats:
            print("Novig (alone):")
            print(f"  {novig_stats['bets']} bets, {novig_stats['wins']}W/{novig_stats['losses']}L, "
                  f"{novig_stats['win_rate']:.1f}% win rate, ${novig_stats['total_pnl']:,.2f} P&L, {novig_stats['roi']:.2f}% ROI")
            print(f"  Avg bet size: ${novig_stats['avg_cost']:.2f}")
            print()
        
        # Check ProphetX AND Novig together
        px_novig_stats = analyze_book_combination(all_settled, ['ProphetX', 'Novig'], "ProphetX AND Novig")
        if px_novig_stats:
            print("ProphetX AND Novig (both present):")
            print(f"  {px_novig_stats['bets']} bets, {px_novig_stats['wins']}W/{px_novig_stats['losses']}L, "
                  f"{px_novig_stats['win_rate']:.1f}% win rate, ${px_novig_stats['total_pnl']:,.2f} P&L, {px_novig_stats['roi']:.2f}% ROI")
            print(f"  Avg bet size: ${px_novig_stats['avg_cost']:.2f}")
            print()
            
            # Statistical significance check (rough)
            if px_novig_stats['bets'] >= 30:
                print(f"  [YES] Sample size sufficient ({px_novig_stats['bets']} bets) for bet sizing decision")
            elif px_novig_stats['bets'] >= 20:
                print(f"  [MAYBE] Sample size moderate ({px_novig_stats['bets']} bets) - proceed with caution")
            else:
                print(f"  [NO] Sample size too small ({px_novig_stats['bets']} bets) - need more data")
            print()
        
        # Check at 10%+ EV with ProphetX/Novig
        settled_10plus = [b for b in all_settled if b['ev'] >= 10]
        px_novig_10plus = analyze_book_combination(settled_10plus, ['ProphetX', 'Novig'], "ProphetX AND Novig at 10%+ EV")
        if px_novig_10plus:
            print("ProphetX AND Novig at 10%+ EV:")
            print(f"  {px_novig_10plus['bets']} bets, {px_novig_10plus['wins']}W/{px_novig_10plus['losses']}L, "
                  f"{px_novig_10plus['win_rate']:.1f}% win rate, ${px_novig_10plus['total_pnl']:,.2f} P&L, {px_novig_10plus['roi']:.2f}% ROI")
            print()
        
        # 5. SUMMARY RECOMMENDATIONS
        print("=" * 100)
        print("5. SUMMARY & RECOMMENDATIONS")
        print("=" * 100)
        print()
        
        if below_10 and at_10_plus:
            print(f"Moving to 10%+ EV:")
            print(f"  - Reduces bets by {reduction:.1f}% ({below_10['bets'] - at_10_plus['bets']} bets)")
            print(f"  - Improves ROI from {below_10['roi']:.2f}% to {at_10_plus['roi']:.2f}%")
            print(f"  - Remaining: {at_10_plus['bets']} bets")
            print()
        
        if px_novig_stats and px_novig_stats['bets'] >= 20:
            print(f"Bet sizing for ProphetX + Novig:")
            print(f"  - {px_novig_stats['bets']} bets with both books")
            print(f"  - {px_novig_stats['roi']:.2f}% ROI ({px_novig_stats['win_rate']:.1f}% win rate)")
            print(f"  - Consider 2x bet size (202 contracts instead of 101)")
            print()
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    analyze_cbb_optimization()
