"""
Check ProphetX/Novig sample size in Kalshi 3 Sharps filter
"""
import os
from dotenv import load_dotenv

load_dotenv()

GOOGLE_SHEETS_CREDENTIALS_FILE = os.getenv('GOOGLE_SHEETS_CREDENTIALS_FILE', 'credentials.json')
GOOGLE_SHEETS_SPREADSHEET_ID = os.getenv('GOOGLE_SHEETS_SPREADSHEET_ID', '')
GOOGLE_SHEETS_WORKSHEET_NAME = os.getenv('GOOGLE_SHEETS_WORKSHEET_NAME', 'Auto-Bets')


def parse_float(s):
    if not s or s.strip() == '':
        return 0.0
    try:
        cleaned = str(s).replace('$', '').replace(',', '').replace(' ', '').replace('%', '').strip()
        return float(cleaned)
    except:
        return 0.0


def get_column_index(header, column_name):
    column_name_lower = column_name.lower()
    for idx, col in enumerate(header):
        if column_name_lower in col.lower():
            return idx
    return None


def check_px_novig():
    if not GOOGLE_SHEETS_SPREADSHEET_ID:
        print("ERROR: GOOGLE_SHEETS_SPREADSHEET_ID not set")
        return
    
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_file(GOOGLE_SHEETS_CREDENTIALS_FILE, scopes=scope)
        google_sheets_client = gspread.authorize(creds)
        
        spreadsheet = google_sheets_client.open_by_key(GOOGLE_SHEETS_SPREADSHEET_ID)
        worksheet = spreadsheet.worksheet(GOOGLE_SHEETS_WORKSHEET_NAME)
        rows = worksheet.get_all_values()
        
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
        
        ticker_col = get_column_index(header, 'ticker')
        side_col = get_column_index(header, 'side')
        cost_col = get_column_index(header, 'cost')
        result_col = get_column_index(header, 'result')
        pnl_col = get_column_index(header, 'pnl')
        settled_col = get_column_index(header, 'settled')
        filter_name_col = get_column_index(header, 'filter name')
        devig_books_col = get_column_index(header, 'devig books')
        win_amount_col = get_column_index(header, 'win amount')
        
        kalshi_bets = []
        
        for row in data_rows:
            if len(row) <= max(ticker_col, side_col, cost_col, result_col, filter_name_col):
                continue
            
            filter_name = row[filter_name_col].strip() if filter_name_col < len(row) else ''
            if '3 Sharps' not in filter_name and '3 sharps' not in filter_name.lower():
                continue
            
            result = row[result_col].strip().upper() if result_col < len(row) else ''
            settled = row[settled_col].strip().upper() if settled_col < len(row) else 'FALSE'
            cost = parse_float(row[cost_col] if cost_col < len(row) else '0')
            win_amount = parse_float(row[win_amount_col] if win_amount_col and win_amount_col < len(row) else '0')
            pnl_from_sheet = parse_float(row[pnl_col] if pnl_col and pnl_col < len(row) else '0')
            devig_books = row[devig_books_col].strip() if devig_books_col and devig_books_col < len(row) else ''
            
            if settled == 'TRUE':
                if result == 'WIN':
                    calculated_pnl = win_amount if win_amount != 0 else pnl_from_sheet
                elif result == 'LOSS':
                    calculated_pnl = -cost
                else:
                    calculated_pnl = pnl_from_sheet if pnl_from_sheet != 0 else 0
            else:
                calculated_pnl = pnl_from_sheet
            
            books_raw = [b.strip() for b in devig_books.split(',') if b.strip()] if devig_books else []
            book_names = []
            for book_raw in books_raw:
                if ':' in book_raw:
                    book_name = book_raw.split(':')[0].strip()
                else:
                    book_name = book_raw.strip()
                if book_name:
                    book_names.append(book_name)
            
            kalshi_bets.append({
                'cost': cost,
                'pnl': calculated_pnl,
                'result': result,
                'settled': settled == 'TRUE',
                'win': result == 'WIN' and settled == 'TRUE',
                'loss': result == 'LOSS' and settled == 'TRUE',
                'book_names': book_names,
            })
        
        # Analyze ProphetX/Novig combinations
        settled = [b for b in kalshi_bets if b['settled']]
        
        px_only = [b for b in settled if 'ProphetX' in b['book_names'] and 'Novig' not in b['book_names']]
        novig_only = [b for b in settled if 'Novig' in b['book_names'] and 'ProphetX' not in b['book_names']]
        both = [b for b in settled if 'ProphetX' in b['book_names'] and 'Novig' in b['book_names']]
        
        def calc_stats(bets, name):
            if not bets:
                return
            wins = sum(1 for b in bets if b['win'])
            losses = sum(1 for b in bets if b['loss'])
            total_cost = sum(b['cost'] for b in bets)
            total_pnl = sum(b['pnl'] for b in bets)
            roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
            win_rate = (wins / len(bets) * 100) if bets else 0
            avg_cost = total_cost / len(bets) if bets else 0
            
            print(f"{name}:")
            print(f"  {len(bets)} bets, {wins}W/{losses}L, {win_rate:.1f}% win rate")
            print(f"  ${total_pnl:,.2f} P&L, {roi:.2f}% ROI")
            print(f"  Avg bet size: ${avg_cost:.2f}")
            if len(bets) >= 30:
                print(f"  [YES] Sample size sufficient for bet sizing decision")
            elif len(bets) >= 20:
                print(f"  [MAYBE] Sample size moderate - proceed with caution")
            else:
                print(f"  [NO] Sample size too small - need more data")
            print()
        
        print("=" * 80)
        print("PROPHETX/NOVIG ANALYSIS - KALSHI 3 SHARPS FILTER")
        print("=" * 80)
        print()
        
        calc_stats(px_only, "ProphetX only")
        calc_stats(novig_only, "Novig only")
        calc_stats(both, "ProphetX AND Novig (both present)")
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    check_px_novig()
