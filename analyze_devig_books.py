"""
Analyze devig book performance for Kalshi 3 Sharps filter
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
        # Remove currency symbols, commas, and whitespace
        cleaned = str(s).replace('$', '').replace(',', '').replace(' ', '').strip()
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


def analyze_devig_books():
    """Analyze devig book performance for Kalshi 3 Sharps filter"""
    if not GOOGLE_SHEETS_SPREADSHEET_ID:
        print("ERROR: GOOGLE_SHEETS_SPREADSHEET_ID not set in .env file")
        return
    
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        
        if not os.path.exists(GOOGLE_SHEETS_CREDENTIALS_FILE):
            print(f"ERROR: Credentials file not found: {GOOGLE_SHEETS_CREDENTIALS_FILE}")
            return
        
        scope = ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_file(GOOGLE_SHEETS_CREDENTIALS_FILE, scopes=scope)
        google_sheets_client = gspread.authorize(creds)
        
        spreadsheet = google_sheets_client.open_by_key(GOOGLE_SHEETS_SPREADSHEET_ID)
        worksheet = spreadsheet.worksheet(GOOGLE_SHEETS_WORKSHEET_NAME)
        
        rows = worksheet.get_all_values()
        if len(rows) == 0:
            print("No rows in sheet")
            return
        
        # Detect header row
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
        
        # Find column indices
        ticker_col = get_column_index(header, 'ticker')
        side_col = get_column_index(header, 'side')
        contracts_col = get_column_index(header, 'contracts')
        cost_col = get_column_index(header, 'cost')
        result_col = get_column_index(header, 'result')
        pnl_col = get_column_index(header, 'pnl')
        settled_col = get_column_index(header, 'settled')
        filter_name_col = get_column_index(header, 'filter name')
        devig_books_col = get_column_index(header, 'devig books')
        win_amount_col = get_column_index(header, 'win amount')
        sport_col = get_column_index(header, 'sport')
        
        if any(col is None for col in [ticker_col, side_col, contracts_col, cost_col, result_col, filter_name_col, devig_books_col]):
            print(f"ERROR: Missing required columns")
            print(f"Found columns: {header}")
            return
        
        # Filter for Kalshi 3 Sharps only
        kalshi_3_sharps_bets = []
        
        for row_idx, row in enumerate(data_rows, start=2 if is_header else 1):
            if len(row) <= max(ticker_col, side_col, contracts_col, cost_col, result_col, filter_name_col, devig_books_col):
                continue
            
            filter_name = row[filter_name_col].strip() if filter_name_col < len(row) else ''
            devig_books = row[devig_books_col].strip() if devig_books_col < len(row) else ''
            
            # Check if this is Kalshi 3 Sharps filter
            if '3 Sharps' in filter_name or '3 sharps' in filter_name.lower():
                ticker = row[ticker_col].strip() if ticker_col < len(row) else ''
                side = row[side_col].strip().lower() if side_col < len(row) else ''
                result = row[result_col].strip().upper() if result_col < len(row) else ''
                settled = row[settled_col].strip().upper() if settled_col < len(row) else 'FALSE'
                cost = parse_float(row[cost_col] if cost_col < len(row) else '0')
                win_amount = parse_float(row[win_amount_col] if win_amount_col and win_amount_col < len(row) else '0')
                pnl_from_sheet = parse_float(row[pnl_col] if pnl_col and pnl_col < len(row) else '0')
                sport = row[sport_col].strip() if sport_col and sport_col < len(row) else 'Unknown'
                
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
                
                # Parse devig books (can be comma-separated, format: "BookName:Odds" or just "BookName")
                books_raw = [b.strip() for b in devig_books.split(',') if b.strip()] if devig_books else ['Unknown']
                
                # Extract book name (before colon if present)
                books = []
                for book_raw in books_raw:
                    if ':' in book_raw:
                        book_name = book_raw.split(':')[0].strip()
                    else:
                        book_name = book_raw.strip()
                    if book_name:
                        books.append(book_name)
                
                if not books:
                    books = ['Unknown']
                
                for book in books:
                    kalshi_3_sharps_bets.append({
                        'book': book,
                        'ticker': ticker,
                        'side': side,
                        'result': result,
                        'cost': cost,
                        'pnl': calculated_pnl,
                        'sport': sport,
                        'settled': settled == 'TRUE',
                    })
        
        print("=" * 80)
        print("DEVIG BOOK ANALYSIS - KALSHI 3 SHARPS FILTER")
        print("=" * 80)
        print(f"\nTotal bets analyzed: {len(kalshi_3_sharps_bets)}")
        print()
        
        # Group by book
        book_stats = {}
        for bet in kalshi_3_sharps_bets:
            book = bet['book']
            if book not in book_stats:
                book_stats[book] = {
                    'bets': 0,
                    'wins': 0,
                    'losses': 0,
                    'open': 0,
                    'total_cost': 0.0,
                    'total_pnl': 0.0,
                    'sports': set(),
                }
            
            stats = book_stats[book]
            stats['bets'] += 1
            stats['total_cost'] += bet['cost']
            stats['total_pnl'] += bet['pnl']
            stats['sports'].add(bet['sport'])
            
            if bet['settled']:
                if bet['result'] == 'WIN':
                    stats['wins'] += 1
                elif bet['result'] == 'LOSS':
                    stats['losses'] += 1
            else:
                stats['open'] += 1
        
        # Calculate ROI and sort
        book_results = []
        for book, stats in book_stats.items():
            settled_bets = stats['wins'] + stats['losses']
            win_rate = (stats['wins'] / settled_bets * 100) if settled_bets > 0 else 0
            roi = (stats['total_pnl'] / stats['total_cost'] * 100) if stats['total_cost'] > 0 else 0
            
            book_results.append({
                'book': book,
                'bets': stats['bets'],
                'wins': stats['wins'],
                'losses': stats['losses'],
                'open': stats['open'],
                'win_rate': win_rate,
                'total_cost': stats['total_cost'],
                'total_pnl': stats['total_pnl'],
                'roi': roi,
                'sports': sorted(stats['sports']),
            })
        
        # Sort by ROI (descending)
        book_results.sort(key=lambda x: x['roi'], reverse=True)
        
        # Print results
        print("PERFORMANCE BY DEVIG BOOK (Kalshi 3 Sharps Filter Only):")
        print("-" * 80)
        print(f"{'Book':<20} {'Bets':<8} {'Wins':<8} {'Losses':<8} {'Win%':<8} {'Cost':<12} {'P&L':<12} {'ROI':<10} {'Sports':<30}")
        print("-" * 80)
        
        for result in book_results:
            win_rate_str = f"{result['win_rate']:.1f}%" if result['wins'] + result['losses'] > 0 else "N/A"
            roi_str = f"{result['roi']:.2f}%"
            pnl_str = f"${result['total_pnl']:,.2f}"
            cost_str = f"${result['total_cost']:,.2f}"
            sports_str = ", ".join(result['sports'][:3]) + ("..." if len(result['sports']) > 3 else "")
            
            print(f"{result['book']:<20} {result['bets']:<8} {result['wins']:<8} {result['losses']:<8} "
                  f"{win_rate_str:<8} {cost_str:<12} {pnl_str:<12} {roi_str:<10} {sports_str:<30}")
        
        print()
        print("=" * 80)
        print("RECOMMENDATIONS:")
        print("=" * 80)
        print()
        
        # Identify best and worst performers
        profitable_books = [r for r in book_results if r['roi'] > 0 and r['bets'] >= 5]
        unprofitable_books = [r for r in book_results if r['roi'] < 0 and r['bets'] >= 5]
        
        if profitable_books:
            print("TOP PERFORMING BOOKS (ROI > 0%, min 5 bets):")
            for result in profitable_books[:5]:
                print(f"  - {result['book']}: {result['roi']:.2f}% ROI ({result['bets']} bets, {result['win_rate']:.1f}% win rate)")
            print()
        
        if unprofitable_books:
            print("UNDERPERFORMING BOOKS (ROI < 0%, min 5 bets):")
            for result in sorted(unprofitable_books, key=lambda x: x['roi'])[:5]:
                print(f"  - {result['book']}: {result['roi']:.2f}% ROI ({result['bets']} bets, {result['win_rate']:.1f}% win rate)")
            print()
        
        # Check if FD/DK are problematic
        fd_result = next((r for r in book_results if 'FanDuel' in r['book'] or 'FD' in r['book']), None)
        dk_result = next((r for r in book_results if 'DraftKings' in r['book'] or 'DK' in r['book']), None)
        
        if fd_result:
            print(f"FANDUEL PERFORMANCE:")
            print(f"  - ROI: {fd_result['roi']:.2f}%")
            print(f"  - Bets: {fd_result['bets']}")
            print(f"  - Win Rate: {fd_result['win_rate']:.1f}%")
            if fd_result['roi'] < 0:
                print(f"  - [WARNING] FanDuel is losing money for Kalshi 3 Sharps filter")
            print()
        
        if dk_result:
            print(f"DRAFTKINGS PERFORMANCE:")
            print(f"  - ROI: {dk_result['roi']:.2f}%")
            print(f"  - Bets: {dk_result['bets']}")
            print(f"  - Win Rate: {dk_result['win_rate']:.1f}%")
            if dk_result['roi'] < 0:
                print(f"  - [WARNING] DraftKings is losing money for Kalshi 3 Sharps filter")
            print()
        
        # Overall summary
        total_pnl = sum(r['total_pnl'] for r in book_results)
        total_cost = sum(r['total_cost'] for r in book_results)
        overall_roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
        
        print(f"OVERALL KALSHI 3 SHARPS PERFORMANCE:")
        print(f"  - Total P&L: ${total_pnl:,.2f}")
        print(f"  - Total Cost: ${total_cost:,.2f}")
        print(f"  - Overall ROI: {overall_roi:.2f}%")
        print()
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    analyze_devig_books()
