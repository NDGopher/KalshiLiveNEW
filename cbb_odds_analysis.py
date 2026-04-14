"""
CBB Odds Range Analysis at 10%+ EV
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


def analyze_odds_at_10plus():
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
        win_amount_col = get_column_index(header, 'win amount')
        market_type_col = get_column_index(header, 'market type')
        ev_col = get_column_index(header, 'ev')
        american_odds_col = get_column_index(header, 'american odds')
        
        cbb_bets = []
        
        for row in data_rows:
            if len(row) <= max(ticker_col, side_col, cost_col, result_col, filter_name_col):
                continue
            
            filter_name = row[filter_name_col].strip() if filter_name_col < len(row) else ''
            if 'CBB' not in filter_name and 'cbb' not in filter_name.lower():
                continue
            
            result = row[result_col].strip().upper() if result_col < len(row) else ''
            settled = row[settled_col].strip().upper() if settled_col < len(row) else 'FALSE'
            cost = parse_float(row[cost_col] if cost_col < len(row) else '0')
            win_amount = parse_float(row[win_amount_col] if win_amount_col and win_amount_col < len(row) else '0')
            pnl_from_sheet = parse_float(row[pnl_col] if pnl_col and pnl_col < len(row) else '0')
            market_type = row[market_type_col].strip() if market_type_col and market_type_col < len(row) else 'Unknown'
            ev = parse_float(row[ev_col] if ev_col and ev_col < len(row) else '0')
            american_odds = parse_float(row[american_odds_col] if american_odds_col and american_odds_col < len(row) else 0)
            
            if settled == 'TRUE':
                if result == 'WIN':
                    calculated_pnl = win_amount if win_amount != 0 else pnl_from_sheet
                elif result == 'LOSS':
                    calculated_pnl = -cost
                else:
                    calculated_pnl = pnl_from_sheet if pnl_from_sheet != 0 else 0
            else:
                calculated_pnl = pnl_from_sheet
            
            if settled == 'TRUE' and ev >= 10:
                cbb_bets.append({
                    'market_type': market_type,
                    'cost': cost,
                    'pnl': calculated_pnl,
                    'result': result,
                    'win': result == 'WIN',
                    'loss': result == 'LOSS',
                    'american_odds': american_odds,
                })
        
        print("=" * 100)
        print("CBB ODDS RANGE ANALYSIS AT 10%+ EV")
        print("=" * 100)
        print()
        
        odds_ranges = [
            (-1000, -500, "Heavy Favorites (-1000 to -500)"),
            (-500, -200, "Favorites (-500 to -200)"),
            (-200, -110, "Moderate Favorites (-200 to -110)"),
            (-110, 110, "Near Even (-110 to +110)"),
            (110, 200, "Moderate Underdogs (+110 to +200)"),
            (200, 500, "Underdogs (+200 to +500)"),
            (500, 1000, "Heavy Underdogs (+500 to +1000)"),
        ]
        
        print("Overall at 10%+ EV:")
        for min_odds, max_odds, name in odds_ranges:
            filtered = [b for b in cbb_bets if min_odds <= b['american_odds'] < max_odds]
            if filtered:
                wins = sum(1 for b in filtered if b['win'])
                losses = sum(1 for b in filtered if b['loss'])
                total_cost = sum(b['cost'] for b in filtered)
                total_pnl = sum(b['pnl'] for b in filtered)
                roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
                win_rate = (wins / len(filtered) * 100) if filtered else 0
                
                print(f"{name}:")
                print(f"  {len(filtered)} bets, {wins}W/{losses}L, {win_rate:.1f}% win rate, "
                      f"${total_pnl:,.2f} P&L, {roi:.2f}% ROI")
        print()
        
        # By market type
        print("By Market Type at 10%+ EV:")
        print()
        for mt in ['Point Spread', 'Total Points', 'Moneyline']:
            mt_bets = [b for b in cbb_bets if b['market_type'] == mt]
            if not mt_bets:
                continue
            
            print(f"{mt}:")
            for min_odds, max_odds, name in odds_ranges:
                filtered = [b for b in mt_bets if min_odds <= b['american_odds'] < max_odds]
                if filtered:
                    wins = sum(1 for b in filtered if b['win'])
                    losses = sum(1 for b in filtered if b['loss'])
                    total_cost = sum(b['cost'] for b in filtered)
                    total_pnl = sum(b['pnl'] for b in filtered)
                    roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
                    win_rate = (wins / len(filtered) * 100) if filtered else 0
                    
                    print(f"  {name}: {len(filtered)} bets, {wins}W/{losses}L, {win_rate:.1f}% win rate, "
                          f"${total_pnl:,.2f} P&L, {roi:.2f}% ROI")
            print()
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    analyze_odds_at_10plus()
