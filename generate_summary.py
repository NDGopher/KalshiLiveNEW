"""
Comprehensive Bet Results Summary Generator
Reads from Google Sheets and generates detailed analytics HTML report
"""
import os
import sys
from datetime import datetime
from collections import defaultdict
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Google Sheets configuration
_credentials_file = os.getenv('GOOGLE_SHEETS_CREDENTIALS_FILE', 'credentials.json')
if not os.path.isabs(_credentials_file):
    _credentials_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), _credentials_file)
GOOGLE_SHEETS_CREDENTIALS_FILE = _credentials_file
GOOGLE_SHEETS_SPREADSHEET_ID = os.getenv('GOOGLE_SHEETS_SPREADSHEET_ID', '')
GOOGLE_SHEETS_WORKSHEET_NAME = os.getenv('GOOGLE_SHEETS_WORKSHEET_NAME', 'Auto-Bets')


def parse_float(value, default=0.0):
    """Safely parse float value"""
    if not value or value == '':
        return default
    try:
        return float(str(value).replace('$', '').replace(',', '').strip())
    except (ValueError, TypeError):
        return default


def parse_int(value, default=0):
    """Safely parse int value"""
    if not value or value == '':
        return default
    try:
        return int(float(str(value).replace(',', '').strip()))
    except (ValueError, TypeError):
        return default


def get_column_index(header, column_name):
    """Find column index by name (case-insensitive)"""
    header_lower = [h.lower() if h else '' for h in header]
    column_lower = column_name.lower()
    
    # Try exact match first
    if column_lower in header_lower:
        return header_lower.index(column_lower)
    
    # Try partial match
    for idx, h in enumerate(header_lower):
        if column_lower in h or h in column_lower:
            return idx
    
    return None


def read_bets_from_sheets():
    """Read all bets from Google Sheets"""
    if not GOOGLE_SHEETS_SPREADSHEET_ID:
        print("ERROR: GOOGLE_SHEETS_SPREADSHEET_ID not set in .env file")
        return []
    
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        
        if not os.path.exists(GOOGLE_SHEETS_CREDENTIALS_FILE):
            print(f"ERROR: Credentials file not found: {GOOGLE_SHEETS_CREDENTIALS_FILE}")
            return []
        
        scope = ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_file(GOOGLE_SHEETS_CREDENTIALS_FILE, scopes=scope)
        google_sheets_client = gspread.authorize(creds)
        
        spreadsheet = google_sheets_client.open_by_key(GOOGLE_SHEETS_SPREADSHEET_ID)
        worksheet = spreadsheet.worksheet(GOOGLE_SHEETS_WORKSHEET_NAME)
        
        rows = worksheet.get_all_values()
        if len(rows) == 0:
            print("[SUMMARY] No rows in sheet")
            return []
        
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
            # Use positional indexing (new format)
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
        sport_col = get_column_index(header, 'sport')
        market_type_col = get_column_index(header, 'market type')
        filter_name_col = get_column_index(header, 'filter name')
        devig_books_col = get_column_index(header, 'devig books')
        timestamp_col = get_column_index(header, 'timestamp')
        ev_col = get_column_index(header, 'ev')
        payout_col = get_column_index(header, 'payout')
        win_amount_col = get_column_index(header, 'win amount')
        
        if any(col is None for col in [ticker_col, side_col, contracts_col, cost_col, result_col]):
            print(f"[SUMMARY] ERROR: Missing required columns")
            print(f"[SUMMARY] Found columns: {header}")
            return []
        
        bets = []
        start_row = 2 if is_header else 1
        
        for row_idx, row in enumerate(data_rows, start=start_row):
            if len(row) <= max(ticker_col, side_col, contracts_col, cost_col, result_col, pnl_col):
                continue
            
            ticker = row[ticker_col].strip() if ticker_col < len(row) else ''
            side = row[side_col].strip().lower() if side_col < len(row) else ''
            result = row[result_col].strip().upper() if result_col < len(row) else ''
            settled = row[settled_col].strip().upper() if settled_col < len(row) else 'FALSE'
            
            if not ticker or not side:
                continue
            
            cost = parse_float(row[cost_col] if cost_col < len(row) else '0')
            win_amount = parse_float(row[win_amount_col] if win_amount_col and win_amount_col < len(row) else '0')
            pnl_from_sheet = parse_float(row[pnl_col] if pnl_col and pnl_col < len(row) else '0')
            
            # CRITICAL: Recalculate P&L properly
            # For WIN: Use column P (Win Amount) directly - it's already the net profit
            # For LOSS: Use negative column N (Cost) = -Cost
            # For OPEN/PENDING: Use PNL from sheet (might be unrealized P&L)
            if settled == 'TRUE':
                if result == 'WIN':
                    # Win Amount (column P) is already net profit (payout - cost)
                    calculated_pnl = win_amount if win_amount != 0 else pnl_from_sheet
                elif result == 'LOSS':
                    # Loss = negative cost (column N)
                    calculated_pnl = -cost
                else:
                    # PUSH or other - use PNL from sheet or default to 0
                    calculated_pnl = pnl_from_sheet if pnl_from_sheet != 0 else 0
            else:
                # OPEN/PENDING - use PNL from sheet (might be unrealized P&L)
                calculated_pnl = pnl_from_sheet
            
            bet = {
                'ticker': ticker,
                'side': side,
                'result': result,
                'settled': settled == 'TRUE',
                'cost': cost,
                'pnl': calculated_pnl,  # Use recalculated P&L
                'contracts': parse_int(row[contracts_col] if contracts_col < len(row) else '0'),
                'sport': row[sport_col].strip() if sport_col and sport_col < len(row) else 'Unknown',
                'market_type': row[market_type_col].strip() if market_type_col and market_type_col < len(row) else 'Unknown',
                'filter_name': row[filter_name_col].strip() if filter_name_col and filter_name_col < len(row) else 'Unknown',
                'devig_books': row[devig_books_col].strip() if devig_books_col and devig_books_col < len(row) else '',
                'timestamp': row[timestamp_col].strip() if timestamp_col and timestamp_col < len(row) else '',
                'ev': parse_float(row[ev_col] if ev_col and ev_col < len(row) else '0'),
            }
            
            bets.append(bet)
        
        print(f"[SUMMARY] Read {len(bets)} bets from Google Sheets")
        return bets
    
    except Exception as e:
        print(f"[SUMMARY] ERROR reading from Google Sheets: {e}")
        import traceback
        traceback.print_exc()
        return []


def analyze_bets(bets):
    """Perform comprehensive analysis on bets"""
    if not bets:
        return {}
    
    # Overall stats
    total_bets = len(bets)
    settled_bets = [b for b in bets if b['settled']]
    settled_count = len(settled_bets)
    wins = [b for b in settled_bets if b['result'] == 'WIN']
    losses = [b for b in settled_bets if b['result'] == 'LOSS']
    open_bets = [b for b in bets if b['result'] == 'OPEN']
    
    total_pnl = sum(b['pnl'] for b in settled_bets)
    total_cost = sum(b['cost'] for b in bets)
    win_count = len(wins)
    loss_count = len(losses)
    win_rate = (win_count / settled_count * 100) if settled_count > 0 else 0
    roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
    
    # Breakdown by filter
    filter_stats = defaultdict(lambda: {
        'bets': 0, 'settled': 0, 'wins': 0, 'losses': 0, 'open': 0,
        'pnl': 0.0, 'cost': 0.0, 'win_rate': 0.0, 'roi': 0.0
    })
    
    for bet in bets:
        filter_name = bet['filter_name'] or 'Unknown'
        filter_stats[filter_name]['bets'] += 1
        filter_stats[filter_name]['cost'] += bet['cost']
        
        if bet['settled']:
            filter_stats[filter_name]['settled'] += 1
            filter_stats[filter_name]['pnl'] += bet['pnl']
            if bet['result'] == 'WIN':
                filter_stats[filter_name]['wins'] += 1
            elif bet['result'] == 'LOSS':
                filter_stats[filter_name]['losses'] += 1
        else:
            filter_stats[filter_name]['open'] += 1
    
    for filter_name in filter_stats:
        stats = filter_stats[filter_name]
        if stats['settled'] > 0:
            stats['win_rate'] = (stats['wins'] / stats['settled'] * 100)
        if stats['cost'] > 0:
            stats['roi'] = (stats['pnl'] / stats['cost'] * 100)
    
    # Breakdown by sport
    sport_stats = defaultdict(lambda: {
        'bets': 0, 'settled': 0, 'wins': 0, 'losses': 0, 'open': 0,
        'pnl': 0.0, 'cost': 0.0, 'win_rate': 0.0, 'roi': 0.0
    })
    
    for bet in bets:
        sport = bet['sport']
        sport_stats[sport]['bets'] += 1
        sport_stats[sport]['cost'] += bet['cost']
        
        if bet['settled']:
            sport_stats[sport]['settled'] += 1
            sport_stats[sport]['pnl'] += bet['pnl']
            if bet['result'] == 'WIN':
                sport_stats[sport]['wins'] += 1
            elif bet['result'] == 'LOSS':
                sport_stats[sport]['losses'] += 1
        else:
            sport_stats[sport]['open'] += 1
    
    for sport in sport_stats:
        stats = sport_stats[sport]
        if stats['settled'] > 0:
            stats['win_rate'] = (stats['wins'] / stats['settled'] * 100)
        if stats['cost'] > 0:
            stats['roi'] = (stats['pnl'] / stats['cost'] * 100)
    
    # Breakdown by market type
    market_stats = defaultdict(lambda: {
        'bets': 0, 'settled': 0, 'wins': 0, 'losses': 0, 'open': 0,
        'pnl': 0.0, 'cost': 0.0, 'win_rate': 0.0, 'roi': 0.0
    })
    
    for bet in bets:
        market_type = bet['market_type']
        market_stats[market_type]['bets'] += 1
        market_stats[market_type]['cost'] += bet['cost']
        
        if bet['settled']:
            market_stats[market_type]['settled'] += 1
            market_stats[market_type]['pnl'] += bet['pnl']
            if bet['result'] == 'WIN':
                market_stats[market_type]['wins'] += 1
            elif bet['result'] == 'LOSS':
                market_stats[market_type]['losses'] += 1
        else:
            market_stats[market_type]['open'] += 1
    
    for market_type in market_stats:
        stats = market_stats[market_type]
        if stats['settled'] > 0:
            stats['win_rate'] = (stats['wins'] / stats['settled'] * 100)
        if stats['cost'] > 0:
            stats['roi'] = (stats['pnl'] / stats['cost'] * 100)
    
    # Breakdown by side
    side_stats = defaultdict(lambda: {
        'bets': 0, 'settled': 0, 'wins': 0, 'losses': 0, 'open': 0,
        'pnl': 0.0, 'cost': 0.0, 'win_rate': 0.0, 'roi': 0.0
    })
    
    for bet in bets:
        side = bet['side'].upper()
        side_stats[side]['bets'] += 1
        side_stats[side]['cost'] += bet['cost']
        
        if bet['settled']:
            side_stats[side]['settled'] += 1
            side_stats[side]['pnl'] += bet['pnl']
            if bet['result'] == 'WIN':
                side_stats[side]['wins'] += 1
            elif bet['result'] == 'LOSS':
                side_stats[side]['losses'] += 1
        else:
            side_stats[side]['open'] += 1
    
    for side in side_stats:
        stats = side_stats[side]
        if stats['settled'] > 0:
            stats['win_rate'] = (stats['wins'] / stats['settled'] * 100)
        if stats['cost'] > 0:
            stats['roi'] = (stats['pnl'] / stats['cost'] * 100)
    
    # Breakdown by devig books (extract book names from devig_books field)
    book_stats = defaultdict(lambda: {
        'bets': 0, 'settled': 0, 'wins': 0, 'losses': 0, 'open': 0,
        'pnl': 0.0, 'cost': 0.0, 'win_rate': 0.0, 'roi': 0.0
    })
    
    for bet in bets:
        devig_books = bet['devig_books'] or ''
        # Parse book names (format: "Book1 (+150), Book2 (+200)")
        books = []
        if devig_books:
            # Split by comma and extract book names
            parts = devig_books.split(',')
            for part in parts:
                # Extract book name (before the first parenthesis or colon)
                book_name = part.split('(')[0].split(':')[0].strip()
                if book_name:
                    books.append(book_name.strip())
        
        if not books:
            books = ['Unknown']
        
        for book in books:
            book_stats[book]['bets'] += 1
            book_stats[book]['cost'] += bet['cost'] / len(books)  # Distribute cost across books
        
        if bet['settled']:
            for book in books:
                book_stats[book]['settled'] += 1
                book_stats[book]['pnl'] += bet['pnl'] / len(books)  # Distribute P&L across books
                if bet['result'] == 'WIN':
                    book_stats[book]['wins'] += 1
                elif bet['result'] == 'LOSS':
                    book_stats[book]['losses'] += 1
        else:
            for book in books:
                book_stats[book]['open'] += 1
    
    for book in book_stats:
        stats = book_stats[book]
        if stats['settled'] > 0:
            stats['win_rate'] = (stats['wins'] / stats['settled'] * 100)
        if stats['cost'] > 0:
            stats['roi'] = (stats['pnl'] / stats['cost'] * 100)
    
    # Filter + Sport combination
    filter_sport_stats = defaultdict(lambda: {
        'bets': 0, 'settled': 0, 'wins': 0, 'losses': 0, 'open': 0,
        'pnl': 0.0, 'cost': 0.0, 'win_rate': 0.0, 'roi': 0.0
    })
    
    for bet in bets:
        key = f"{bet['filter_name']} - {bet['sport']}"
        filter_sport_stats[key]['bets'] += 1
        filter_sport_stats[key]['cost'] += bet['cost']
        
        if bet['settled']:
            filter_sport_stats[key]['settled'] += 1
            filter_sport_stats[key]['pnl'] += bet['pnl']
            if bet['result'] == 'WIN':
                filter_sport_stats[key]['wins'] += 1
            elif bet['result'] == 'LOSS':
                filter_sport_stats[key]['losses'] += 1
        else:
            filter_sport_stats[key]['open'] += 1
    
    for key in filter_sport_stats:
        stats = filter_sport_stats[key]
        if stats['settled'] > 0:
            stats['win_rate'] = (stats['wins'] / stats['settled'] * 100)
        if stats['cost'] > 0:
            stats['roi'] = (stats['pnl'] / stats['cost'] * 100)
    
    # Filter + Market Type combination
    filter_market_stats = defaultdict(lambda: {
        'bets': 0, 'settled': 0, 'wins': 0, 'losses': 0, 'open': 0,
        'pnl': 0.0, 'cost': 0.0, 'win_rate': 0.0, 'roi': 0.0
    })
    
    for bet in bets:
        key = f"{bet['filter_name']} - {bet['market_type']}"
        filter_market_stats[key]['bets'] += 1
        filter_market_stats[key]['cost'] += bet['cost']
        
        if bet['settled']:
            filter_market_stats[key]['settled'] += 1
            filter_market_stats[key]['pnl'] += bet['pnl']
            if bet['result'] == 'WIN':
                filter_market_stats[key]['wins'] += 1
            elif bet['result'] == 'LOSS':
                filter_market_stats[key]['losses'] += 1
        else:
            filter_market_stats[key]['open'] += 1
    
    for key in filter_market_stats:
        stats = filter_market_stats[key]
        if stats['settled'] > 0:
            stats['win_rate'] = (stats['wins'] / stats['settled'] * 100)
        if stats['cost'] > 0:
            stats['roi'] = (stats['pnl'] / stats['cost'] * 100)
    
    # P&L timeline (by date)
    pnl_timeline = defaultdict(float)
    for bet in settled_bets:
        if bet['timestamp']:
            try:
                # Parse timestamp (ISO format or similar)
                date_str = bet['timestamp'].split('T')[0] if 'T' in bet['timestamp'] else bet['timestamp'].split(' ')[0]
                pnl_timeline[date_str] += bet['pnl']
            except:
                pass
    
    return {
        'overall': {
            'total_bets': total_bets,
            'settled_count': settled_count,
            'win_count': win_count,
            'loss_count': loss_count,
            'open_count': len(open_bets),
            'total_pnl': total_pnl,
            'total_cost': total_cost,
            'win_rate': win_rate,
            'roi': roi
        },
        'by_filter': dict(filter_stats),
        'by_sport': dict(sport_stats),
        'by_market_type': dict(market_stats),
        'by_side': dict(side_stats),
        'by_book': dict(book_stats),
        'by_filter_sport': dict(filter_sport_stats),
        'by_filter_market': dict(filter_market_stats),
        'pnl_timeline': dict(pnl_timeline)
    }


def generate_html(analysis):
    """Generate HTML report from analysis"""
    overall = analysis['overall']
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bet Results Summary - Comprehensive Analytics</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 20px;
            min-height: 100vh;
        }}
        .container {{
            max-width: 1600px;
            margin: 0 auto;
            background: white;
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            padding: 40px;
        }}
        h1 {{
            color: #1f2937;
            font-size: 2.5em;
            margin-bottom: 10px;
            text-align: center;
        }}
        .subtitle {{
            text-align: center;
            color: #6b7280;
            margin-bottom: 40px;
            font-size: 0.9em;
        }}
        .overview {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 40px;
        }}
        .stat-card {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 25px;
            border-radius: 15px;
            text-align: center;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }}
        .stat-card.positive {{
            background: linear-gradient(135deg, #10b981 0%, #059669 100%);
        }}
        .stat-card.negative {{
            background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
        }}
        .stat-value {{
            font-size: 2.5em;
            font-weight: bold;
            margin-bottom: 5px;
        }}
        .stat-label {{
            font-size: 0.9em;
            opacity: 0.9;
        }}
        .section {{
            margin-bottom: 50px;
        }}
        .section-title {{
            font-size: 1.8em;
            color: #1f2937;
            margin-bottom: 20px;
            padding-bottom: 10px;
            border-bottom: 3px solid #667eea;
        }}
        .chart-container {{
            background: #f9fafb;
            padding: 20px;
            border-radius: 15px;
            margin-bottom: 30px;
            height: 400px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
            background: white;
            border-radius: 10px;
            overflow: hidden;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        th {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 15px;
            text-align: left;
            font-weight: 600;
        }}
        td {{
            padding: 12px 15px;
            border-bottom: 1px solid #e5e7eb;
        }}
        tr:hover {{
            background: #f9fafb;
        }}
        .positive-num {{
            color: #10b981;
            font-weight: 600;
        }}
        .negative-num {{
            color: #ef4444;
            font-weight: 600;
        }}
        .footer {{
            text-align: center;
            color: #6b7280;
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid #e5e7eb;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 Bet Results Summary - Comprehensive Analytics</h1>
        <div class="subtitle">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
        
        <div class="overview">
            <div class="stat-card {'positive' if overall['total_pnl'] >= 0 else 'negative'}">
                <div class="stat-value">${overall['total_pnl']:,.2f}</div>
                <div class="stat-label">Total P&L</div>
            </div>
            <div class="stat-card {'positive' if overall['roi'] >= 0 else 'negative'}">
                <div class="stat-value">{overall['roi']:.2f}%</div>
                <div class="stat-label">ROI</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{overall['win_rate']:.1f}%</div>
                <div class="stat-label">Win Rate</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{overall['total_bets']}</div>
                <div class="stat-label">Total Bets</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{overall['settled_count']}</div>
                <div class="stat-label">Settled</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">${overall['total_cost']:,.2f}</div>
                <div class="stat-label">Total Cost</div>
            </div>
        </div>
        
        <div class="section">
            <h2 class="section-title">📈 P&L Timeline</h2>
            <div class="chart-container">
                <canvas id="pnlChart"></canvas>
            </div>
        </div>
        
        <div class="section">
            <h2 class="section-title">🎯 Performance by Filter</h2>
            <table>
                <thead>
                    <tr>
                        <th>Filter</th>
                        <th>Bets</th>
                        <th>Wins</th>
                        <th>Losses</th>
                        <th>Open</th>
                        <th>Win Rate</th>
                        <th>P&L</th>
                        <th>Cost</th>
                        <th>ROI</th>
                    </tr>
                </thead>
                <tbody>
"""
    
    # Sort filters by P&L
    sorted_filters = sorted(analysis['by_filter'].items(), key=lambda x: x[1]['pnl'], reverse=True)
    for filter_name, stats in sorted_filters:
        pnl_class = 'positive-num' if stats['pnl'] >= 0 else 'negative-num'
        roi_class = 'positive-num' if stats['roi'] >= 0 else 'negative-num'
        html += f"""
                    <tr>
                        <td><strong>{filter_name}</strong></td>
                        <td>{stats['bets']}</td>
                        <td>{stats['wins']}</td>
                        <td>{stats['losses']}</td>
                        <td>{stats['open']}</td>
                        <td>{stats['win_rate']:.1f}%</td>
                        <td class="{pnl_class}">${stats['pnl']:,.2f}</td>
                        <td>${stats['cost']:,.2f}</td>
                        <td class="{roi_class}">{stats['roi']:.2f}%</td>
                    </tr>
"""
    
    html += """
                </tbody>
            </table>
        </div>
        
        <div class="section">
            <h2 class="section-title">🏀 Performance by Sport</h2>
            <table>
                <thead>
                    <tr>
                        <th>Sport</th>
                        <th>Bets</th>
                        <th>Wins</th>
                        <th>Losses</th>
                        <th>Open</th>
                        <th>Win Rate</th>
                        <th>P&L</th>
                        <th>Cost</th>
                        <th>ROI</th>
                    </tr>
                </thead>
                <tbody>
"""
    
    sorted_sports = sorted(analysis['by_sport'].items(), key=lambda x: x[1]['pnl'], reverse=True)
    for sport, stats in sorted_sports:
        pnl_class = 'positive-num' if stats['pnl'] >= 0 else 'negative-num'
        roi_class = 'positive-num' if stats['roi'] >= 0 else 'negative-num'
        html += f"""
                    <tr>
                        <td><strong>{sport}</strong></td>
                        <td>{stats['bets']}</td>
                        <td>{stats['wins']}</td>
                        <td>{stats['losses']}</td>
                        <td>{stats['open']}</td>
                        <td>{stats['win_rate']:.1f}%</td>
                        <td class="{pnl_class}">${stats['pnl']:,.2f}</td>
                        <td>${stats['cost']:,.2f}</td>
                        <td class="{roi_class}">{stats['roi']:.2f}%</td>
                    </tr>
"""
    
    html += """
                </tbody>
            </table>
        </div>
        
        <div class="section">
            <h2 class="section-title">🎯 Performance by Market Type</h2>
            <table>
                <thead>
                    <tr>
                        <th>Market Type</th>
                        <th>Bets</th>
                        <th>Wins</th>
                        <th>Losses</th>
                        <th>Open</th>
                        <th>Win Rate</th>
                        <th>P&L</th>
                        <th>Cost</th>
                        <th>ROI</th>
                    </tr>
                </thead>
                <tbody>
"""
    
    sorted_markets = sorted(analysis['by_market_type'].items(), key=lambda x: x[1]['pnl'], reverse=True)
    for market_type, stats in sorted_markets:
        pnl_class = 'positive-num' if stats['pnl'] >= 0 else 'negative-num'
        roi_class = 'positive-num' if stats['roi'] >= 0 else 'negative-num'
        html += f"""
                    <tr>
                        <td><strong>{market_type}</strong></td>
                        <td>{stats['bets']}</td>
                        <td>{stats['wins']}</td>
                        <td>{stats['losses']}</td>
                        <td>{stats['open']}</td>
                        <td>{stats['win_rate']:.1f}%</td>
                        <td class="{pnl_class}">${stats['pnl']:,.2f}</td>
                        <td>${stats['cost']:,.2f}</td>
                        <td class="{roi_class}">{stats['roi']:.2f}%</td>
                    </tr>
"""
    
    html += """
                </tbody>
            </table>
        </div>
        
        <div class="section">
            <h2 class="section-title">📚 Performance by Devig Book</h2>
            <table>
                <thead>
                    <tr>
                        <th>Book</th>
                        <th>Bets</th>
                        <th>Wins</th>
                        <th>Losses</th>
                        <th>Open</th>
                        <th>Win Rate</th>
                        <th>P&L</th>
                        <th>Cost</th>
                        <th>ROI</th>
                    </tr>
                </thead>
                <tbody>
"""
    
    sorted_books = sorted(analysis['by_book'].items(), key=lambda x: x[1]['pnl'], reverse=True)
    for book, stats in sorted_books:
        if stats['bets'] > 0:  # Only show books with bets
            pnl_class = 'positive-num' if stats['pnl'] >= 0 else 'negative-num'
            roi_class = 'positive-num' if stats['roi'] >= 0 else 'negative-num'
            html += f"""
                    <tr>
                        <td><strong>{book}</strong></td>
                        <td>{stats['bets']}</td>
                        <td>{stats['wins']}</td>
                        <td>{stats['losses']}</td>
                        <td>{stats['open']}</td>
                        <td>{stats['win_rate']:.1f}%</td>
                        <td class="{pnl_class}">${stats['pnl']:,.2f}</td>
                        <td>${stats['cost']:,.2f}</td>
                        <td class="{roi_class}">{stats['roi']:.2f}%</td>
                    </tr>
"""
    
    html += """
                </tbody>
            </table>
        </div>
        
        <div class="section">
            <h2 class="section-title">⚖️ Performance by Side</h2>
            <table>
                <thead>
                    <tr>
                        <th>Side</th>
                        <th>Bets</th>
                        <th>Wins</th>
                        <th>Losses</th>
                        <th>Open</th>
                        <th>Win Rate</th>
                        <th>P&L</th>
                        <th>Cost</th>
                        <th>ROI</th>
                    </tr>
                </thead>
                <tbody>
"""
    
    sorted_sides = sorted(analysis['by_side'].items(), key=lambda x: x[1]['pnl'], reverse=True)
    for side, stats in sorted_sides:
        pnl_class = 'positive-num' if stats['pnl'] >= 0 else 'negative-num'
        roi_class = 'positive-num' if stats['roi'] >= 0 else 'negative-num'
        html += f"""
                    <tr>
                        <td><strong>{side}</strong></td>
                        <td>{stats['bets']}</td>
                        <td>{stats['wins']}</td>
                        <td>{stats['losses']}</td>
                        <td>{stats['open']}</td>
                        <td>{stats['win_rate']:.1f}%</td>
                        <td class="{pnl_class}">${stats['pnl']:,.2f}</td>
                        <td>${stats['cost']:,.2f}</td>
                        <td class="{roi_class}">{stats['roi']:.2f}%</td>
                    </tr>
"""
    
    html += """
                </tbody>
            </table>
        </div>
        
        <div class="section">
            <h2 class="section-title">🎯 Filter × Sport Performance</h2>
            <table>
                <thead>
                    <tr>
                        <th>Filter × Sport</th>
                        <th>Bets</th>
                        <th>Wins</th>
                        <th>Losses</th>
                        <th>Open</th>
                        <th>Win Rate</th>
                        <th>P&L</th>
                        <th>Cost</th>
                        <th>ROI</th>
                    </tr>
                </thead>
                <tbody>
"""
    
    sorted_filter_sport = sorted(analysis['by_filter_sport'].items(), key=lambda x: x[1]['pnl'], reverse=True)
    for key, stats in sorted_filter_sport:
        if stats['bets'] >= 3:  # Only show combinations with at least 3 bets
            pnl_class = 'positive-num' if stats['pnl'] >= 0 else 'negative-num'
            roi_class = 'positive-num' if stats['roi'] >= 0 else 'negative-num'
            html += f"""
                    <tr>
                        <td><strong>{key}</strong></td>
                        <td>{stats['bets']}</td>
                        <td>{stats['wins']}</td>
                        <td>{stats['losses']}</td>
                        <td>{stats['open']}</td>
                        <td>{stats['win_rate']:.1f}%</td>
                        <td class="{pnl_class}">${stats['pnl']:,.2f}</td>
                        <td>${stats['cost']:,.2f}</td>
                        <td class="{roi_class}">{stats['roi']:.2f}%</td>
                    </tr>
"""
    
    html += """
                </tbody>
            </table>
        </div>
        
        <div class="section">
            <h2 class="section-title">🎯 Filter × Market Type Performance</h2>
            <table>
                <thead>
                    <tr>
                        <th>Filter × Market</th>
                        <th>Bets</th>
                        <th>Wins</th>
                        <th>Losses</th>
                        <th>Open</th>
                        <th>Win Rate</th>
                        <th>P&L</th>
                        <th>Cost</th>
                        <th>ROI</th>
                    </tr>
                </thead>
                <tbody>
"""
    
    sorted_filter_market = sorted(analysis['by_filter_market'].items(), key=lambda x: x[1]['pnl'], reverse=True)
    for key, stats in sorted_filter_market:
        if stats['bets'] >= 3:  # Only show combinations with at least 3 bets
            pnl_class = 'positive-num' if stats['pnl'] >= 0 else 'negative-num'
            roi_class = 'positive-num' if stats['roi'] >= 0 else 'negative-num'
            html += f"""
                    <tr>
                        <td><strong>{key}</strong></td>
                        <td>{stats['bets']}</td>
                        <td>{stats['wins']}</td>
                        <td>{stats['losses']}</td>
                        <td>{stats['open']}</td>
                        <td>{stats['win_rate']:.1f}%</td>
                        <td class="{pnl_class}">${stats['pnl']:,.2f}</td>
                        <td>${stats['cost']:,.2f}</td>
                        <td class="{roi_class}">{stats['roi']:.2f}%</td>
                    </tr>
"""
    
    # Generate P&L timeline chart data
    timeline_dates = sorted(analysis['pnl_timeline'].keys())
    timeline_pnl = [analysis['pnl_timeline'][d] for d in timeline_dates]
    cumulative_pnl = []
    running_total = 0
    for pnl in timeline_pnl:
        running_total += pnl
        cumulative_pnl.append(running_total)
    
    # Format JavaScript arrays
    timeline_dates_js = '["' + '","'.join(timeline_dates) + '"]'
    cumulative_pnl_js = '[' + ','.join(str(p) for p in cumulative_pnl) + ']'
    
    html += f"""
                </tbody>
            </table>
        </div>
        
        <div class="footer">
            <p>Report generated automatically after bet grading</p>
        </div>
    </div>
    
    <script>
        const ctx = document.getElementById('pnlChart');
        const timelineDates = {timeline_dates_js};
        const cumulativePnl = {cumulative_pnl_js};
        
        new Chart(ctx, {{
            type: 'line',
            data: {{
                labels: timelineDates,
                datasets: [{{
                    label: 'Cumulative P&L',
                    data: cumulativePnl,
                    borderColor: '#667eea',
                    backgroundColor: 'rgba(102, 126, 234, 0.1)',
                    borderWidth: 3,
                    fill: true,
                    tension: 0.4
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    legend: {{
                        display: true,
                        position: 'top'
                    }},
                    title: {{
                        display: true,
                        text: 'Cumulative P&L Over Time'
                    }}
                }},
                scales: {{
                    y: {{
                        beginAtZero: false,
                        ticks: {{
                            callback: function(value) {{
                                return '$' + value.toFixed(2);
                            }}
                        }}
                    }}
                }}
            }}
        }});
    </script>
</body>
</html>
"""
    
    return html


def generate_summary():
    """Main function to generate summary"""
    print("[SUMMARY] Starting comprehensive bet results analysis...")
    
    # Read bets from Google Sheets
    bets = read_bets_from_sheets()
    if not bets:
        print("[SUMMARY] No bets found - cannot generate summary")
        return
    
    # Analyze bets
    print("[SUMMARY] Analyzing bets...")
    analysis = analyze_bets(bets)
    
    # Generate HTML
    print("[SUMMARY] Generating HTML report...")
    html = generate_html(analysis)
    
    # Write to file
    output_file = 'bet_results_summary.html'
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html)
    
    print(f"[SUMMARY] ✅ Generated comprehensive summary: {output_file}")
    print(f"[SUMMARY] Overall stats: {analysis['overall']['total_bets']} bets, ${analysis['overall']['total_pnl']:,.2f} P&L, {analysis['overall']['roi']:.2f}% ROI")


if __name__ == "__main__":
    generate_summary()
