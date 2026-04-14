"""Analyze daily performance from Google Sheets"""
import os
from datetime import datetime, timedelta
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

# Google Sheets configuration
_credentials_file = os.getenv('GOOGLE_SHEETS_CREDENTIALS_FILE', 'credentials.json')
if not os.path.isabs(_credentials_file):
    _credentials_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), _credentials_file)
GOOGLE_SHEETS_CREDENTIALS_FILE = _credentials_file
GOOGLE_SHEETS_SPREADSHEET_ID = os.getenv('GOOGLE_SHEETS_SPREADSHEET_ID', '')
GOOGLE_SHEETS_WORKSHEET_NAME = os.getenv('GOOGLE_SHEETS_WORKSHEET_NAME', 'Auto-Bets')


def parse_float(value, default=0.0):
    if not value or value == '':
        return default
    try:
        return float(str(value).replace('$', '').replace(',', '').strip())
    except (ValueError, TypeError):
        return default


def get_column_index(header, column_name):
    header_lower = [h.lower() if h else '' for h in header]
    column_lower = column_name.lower()
    if column_lower in header_lower:
        return header_lower.index(column_lower)
    for idx, h in enumerate(header_lower):
        if column_lower in h or h in column_lower:
            return idx
    return None


def parse_date(date_str):
    """Parse date from timestamp string"""
    if not date_str:
        return None
    try:
        # Simple: extract date part from ISO format (YYYY-MM-DD)
        if 'T' in date_str:
            date_part = date_str.split('T')[0]
            # Validate it's YYYY-MM-DD format
            if len(date_part) == 10 and date_part.count('-') == 2:
                return datetime.strptime(date_part, '%Y-%m-%d').date()
        # Try other formats
        date_part = date_str.split()[0] if ' ' in date_str else date_str
        for fmt in ['%Y-%m-%d', '%m/%d/%Y']:
            try:
                return datetime.strptime(date_part, fmt).date()
            except:
                continue
    except:
        pass
    return None


def analyze_by_date():
    """Read and analyze bets by date"""
    if not GOOGLE_SHEETS_SPREADSHEET_ID:
        print("ERROR: GOOGLE_SHEETS_SPREADSHEET_ID not set")
        return
    
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        
        if not os.path.exists(GOOGLE_SHEETS_CREDENTIALS_FILE):
            print(f"ERROR: Credentials file not found: {GOOGLE_SHEETS_CREDENTIALS_FILE}")
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
        expected_headers = ['ticker', 'side', 'result', 'pnl', 'timestamp']
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
        print(f"Header row: {header[:10]}...")  # Show first 10 columns
        
        ticker_col = get_column_index(header, 'ticker')
        side_col = get_column_index(header, 'side')
        cost_col = get_column_index(header, 'cost')
        result_col = get_column_index(header, 'result')
        pnl_col = get_column_index(header, 'pnl')
        settled_col = get_column_index(header, 'settled')
        sport_col = get_column_index(header, 'sport')
        market_type_col = get_column_index(header, 'market type')
        filter_name_col = get_column_index(header, 'filter name')
        devig_books_col = get_column_index(header, 'devig books')
        timestamp_col = get_column_index(header, 'timestamp')
        win_amount_col = get_column_index(header, 'win amount')
        
        print(f"Column indices - Ticker: {ticker_col}, Side: {side_col}, Cost: {cost_col}, Result: {result_col}, Timestamp: {timestamp_col}")
        
        # Group by date
        bets_by_date = defaultdict(list)
        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        
        print(f"Looking for dates: Today={today}, Yesterday={yesterday}")
        print(f"Total rows to process: {len(data_rows)}")
        
        # Check sample data
        valid_rows = 0
        date_samples = []
        for i, row in enumerate(data_rows[:20]):  # Sample first 20 rows
            if len(row) > timestamp_col and row[timestamp_col]:
                date_samples.append((i, row[timestamp_col]))
                valid_rows += 1
        
        print(f"Valid rows with timestamps: {valid_rows}")
        print(f"Sample timestamps (first 5): {date_samples[:5]}")
        
        # Process all rows, not just today/yesterday - we'll filter later
        for row in data_rows:
            if len(row) <= max([c for c in [ticker_col, side_col, cost_col, result_col] if c is not None]):
                continue
            
            timestamp_str = row[timestamp_col].strip() if timestamp_col and timestamp_col < len(row) else ''
            if not timestamp_str:
                continue
                
            trade_date = parse_date(timestamp_str)
            
            if not trade_date:
                # Debug: show what failed to parse
                if len(bets_by_date) < 5:  # Only show first few failures
                    print(f"Failed to parse date: '{timestamp_str}'")
                continue
            
            # Include all dates
            
            ticker = row[ticker_col].strip() if ticker_col < len(row) else ''
            side = row[side_col].strip().lower() if side_col < len(row) else ''
            result = row[result_col].strip().upper() if result_col < len(row) else ''
            settled = row[settled_col].strip().upper() if settled_col < len(row) else 'FALSE'
            
            if not ticker or not side:
                continue
            
            cost = parse_float(row[cost_col] if cost_col < len(row) else '0')
            win_amount = parse_float(row[win_amount_col] if win_amount_col and win_amount_col < len(row) else '0')
            pnl_from_sheet = parse_float(row[pnl_col] if pnl_col and pnl_col < len(row) else '0')
            
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
            
            bet = {
                'date': trade_date,
                'ticker': ticker,
                'side': side,
                'result': result,
                'settled': settled == 'TRUE',
                'cost': cost,
                'pnl': calculated_pnl,
                'sport': row[sport_col].strip() if sport_col and sport_col < len(row) else 'Unknown',
                'market_type': row[market_type_col].strip() if market_type_col and market_type_col < len(row) else 'Unknown',
                'filter_name': row[filter_name_col].strip() if filter_name_col and filter_name_col < len(row) else 'Unknown',
                'devig_books': row[devig_books_col].strip() if devig_books_col and devig_books_col < len(row) else '',
            }
            
            bets_by_date[trade_date].append(bet)
            
            # Debug: show first few bets being added
            if len(bets_by_date[trade_date]) <= 3:
                print(f"Added bet: date={trade_date}, ticker={ticker[:20]}, filter={bet['filter_name']}")
        
        # Find the most recent dates in the data
        all_dates = sorted(bets_by_date.keys(), reverse=True)
        print(f"\nFound bets on dates: {all_dates[:10]}")  # Show first 10 dates
        
        # Analyze the 2 most recent days
        if len(all_dates) >= 2:
            recent_dates = all_dates[:2]
            print(f"Analyzing 2 most recent days: {recent_dates[0]} (most recent) vs {recent_dates[1]} (previous)")
        elif len(all_dates) == 1:
            recent_dates = all_dates
            print(f"Only one day of data: {recent_dates[0]}")
        else:
            print("No data found!")
            return
        
        # Analyze each date
        for date in recent_dates:
            bets = bets_by_date[date]
            date_str = date.strftime('%Y-%m-%d')
            is_today = date == today
            is_yesterday = date == yesterday
            label = 'TODAY' if is_today else ('YESTERDAY' if is_yesterday else f'({(today - date).days} days ago)')
            print(f"\n{'='*80}")
            print(f"📅 DATE: {date_str} {label}")
            print(f"{'='*80}\n")
            
            # Overall stats
            settled_bets = [b for b in bets if b['settled']]
            wins = [b for b in settled_bets if b['result'] == 'WIN']
            losses = [b for b in settled_bets if b['result'] == 'LOSS']
            
            total_pnl = sum(b['pnl'] for b in settled_bets)
            total_cost = sum(b['cost'] for b in bets)
            win_rate = (len(wins) / len(settled_bets) * 100) if settled_bets else 0
            roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
            
            print(f"📊 OVERALL:")
            print(f"   Total Bets: {len(bets)}")
            print(f"   Settled: {len(settled_bets)}")
            print(f"   Wins: {len(wins)} | Losses: {len(losses)}")
            print(f"   Win Rate: {win_rate:.1f}%")
            print(f"   Total P&L: ${total_pnl:,.2f}")
            print(f"   Total Cost: ${total_cost:,.2f}")
            print(f"   ROI: {roi:.2f}%")
            
            # By filter
            print(f"\n🎯 BY FILTER:")
            filter_stats = defaultdict(lambda: {'bets': [], 'settled': [], 'wins': [], 'losses': []})
            for bet in bets:
                filter_name = bet['filter_name'] if bet['filter_name'] != 'Unknown' else 'Unknown'
                filter_stats[filter_name]['bets'].append(bet)
                if bet['settled']:
                    filter_stats[filter_name]['settled'].append(bet)
                    if bet['result'] == 'WIN':
                        filter_stats[filter_name]['wins'].append(bet)
                    elif bet['result'] == 'LOSS':
                        filter_stats[filter_name]['losses'].append(bet)
            
            for filter_name in sorted(filter_stats.keys()):
                stats = filter_stats[filter_name]
                settled = stats['settled']
                if not settled:
                    continue
                
                filter_pnl = sum(b['pnl'] for b in settled)
                filter_cost = sum(b['cost'] for b in stats['bets'])
                filter_win_rate = (len(stats['wins']) / len(settled) * 100) if settled else 0
                filter_roi = (filter_pnl / filter_cost * 100) if filter_cost > 0 else 0
                
                print(f"   {filter_name}:")
                print(f"      Bets: {len(stats['bets'])} | Settled: {len(settled)} | Wins: {len(stats['wins'])} | Losses: {len(stats['losses'])}")
                print(f"      Win Rate: {filter_win_rate:.1f}% | P&L: ${filter_pnl:,.2f} | ROI: {filter_roi:.2f}%")
            
            # By devig book (check if Pinnacle was used)
            print(f"\n📚 BY DEVIG BOOK:")
            book_stats = defaultdict(lambda: {'bets': [], 'settled': [], 'wins': [], 'losses': []})
            for bet in bets:
                devig_books = bet['devig_books']
                if not devig_books:
                    continue
                # Parse books (might be comma-separated)
                books = [b.strip() for b in devig_books.split(',')]
                for book in books:
                    if book:
                        book_stats[book]['bets'].append(bet)
                        if bet['settled']:
                            book_stats[book]['settled'].append(bet)
                            if bet['result'] == 'WIN':
                                book_stats[book]['wins'].append(bet)
                            elif bet['result'] == 'LOSS':
                                book_stats[book]['losses'].append(bet)
            
            for book in sorted(book_stats.keys()):
                stats = book_stats[book]
                settled = stats['settled']
                if not settled:
                    continue
                
                book_pnl = sum(b['pnl'] for b in settled)
                book_cost = sum(b['cost'] for b in stats['bets'])
                book_win_rate = (len(stats['wins']) / len(settled) * 100) if settled else 0
                book_roi = (book_pnl / book_cost * 100) if book_cost > 0 else 0
                
                print(f"   {book}:")
                print(f"      Bets: {len(stats['bets'])} | Settled: {len(settled)} | Wins: {len(stats['wins'])} | Losses: {len(stats['losses'])}")
                print(f"      Win Rate: {book_win_rate:.1f}% | P&L: ${book_pnl:,.2f} | ROI: {book_roi:.2f}%")
            
            # By sport
            print(f"\n🏀 BY SPORT:")
            sport_stats = defaultdict(lambda: {'bets': [], 'settled': [], 'wins': [], 'losses': []})
            for bet in bets:
                sport = bet['sport']
                sport_stats[sport]['bets'].append(bet)
                if bet['settled']:
                    sport_stats[sport]['settled'].append(bet)
                    if bet['result'] == 'WIN':
                        sport_stats[sport]['wins'].append(bet)
                    elif bet['result'] == 'LOSS':
                        sport_stats[sport]['losses'].append(bet)
            
            for sport in sorted(sport_stats.keys()):
                stats = sport_stats[sport]
                settled = stats['settled']
                if not settled:
                    continue
                
                sport_pnl = sum(b['pnl'] for b in settled)
                sport_cost = sum(b['cost'] for b in stats['bets'])
                sport_win_rate = (len(stats['wins']) / len(settled) * 100) if settled else 0
                sport_roi = (sport_pnl / sport_cost * 100) if sport_cost > 0 else 0
                
                print(f"   {sport}:")
                print(f"      Bets: {len(stats['bets'])} | Settled: {len(settled)} | Wins: {len(stats['wins'])} | Losses: {len(stats['losses'])}")
                print(f"      Win Rate: {sport_win_rate:.1f}% | P&L: ${sport_pnl:,.2f} | ROI: {sport_roi:.2f}%")
        
        # Compare most recent 2 days
        if len(recent_dates) >= 2:
            print(f"\n{'='*80}")
            print(f"📊 COMPARISON: TODAY vs YESTERDAY")
            print(f"{'='*80}\n")
            
            today_bets = bets_by_date[recent_dates[0]]
            yesterday_bets = bets_by_date[recent_dates[1]]
            
            today_settled = [b for b in today_bets if b['settled']]
            yesterday_settled = [b for b in yesterday_bets if b['settled']]
            
            today_pnl = sum(b['pnl'] for b in today_settled)
            yesterday_pnl = sum(b['pnl'] for b in yesterday_settled)
            pnl_change = today_pnl - yesterday_pnl
            
            print(f"P&L Change: ${pnl_change:,.2f} (Today: ${today_pnl:,.2f} vs Yesterday: ${yesterday_pnl:,.2f})")
            print(f"Volume Change: {len(today_bets) - len(yesterday_bets)} bets (Today: {len(today_bets)} vs Yesterday: {len(yesterday_bets)})")
            
            # Filter comparison
            print(f"\nFilter Performance Change:")
            for filter_name in ['Kalshi All Sports (3 Sharps Live)', 'CBB EV Filter (Live - Kalshi)']:
                today_filter = [b for b in today_settled if b['filter_name'] == filter_name]
                yesterday_filter = [b for b in yesterday_settled if b['filter_name'] == filter_name]
                
                today_filter_pnl = sum(b['pnl'] for b in today_filter)
                yesterday_filter_pnl = sum(b['pnl'] for b in yesterday_filter)
                filter_change = today_filter_pnl - yesterday_filter_pnl
                
                print(f"   {filter_name}:")
                print(f"      P&L Change: ${filter_change:,.2f} (Today: ${today_filter_pnl:,.2f} vs Yesterday: ${yesterday_filter_pnl:,.2f})")
                print(f"      Volume: Today {len(today_filter)} vs Yesterday {len(yesterday_filter)}")
    
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    analyze_by_date()
