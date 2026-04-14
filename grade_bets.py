"""
Standalone Bet Grading Script
Updates Google Sheets with win/loss results for auto-bets
Run this separately from the dashboard to grade bets
"""
import asyncio
import os
import sys
from dotenv import load_dotenv
from kalshi_client import KalshiClient

# Load environment variables
load_dotenv()

# Google Sheets configuration
# If path is relative, make it relative to this script's directory
_credentials_file = os.getenv('GOOGLE_SHEETS_CREDENTIALS_FILE', 'credentials.json')
if not os.path.isabs(_credentials_file):
    _credentials_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), _credentials_file)
GOOGLE_SHEETS_CREDENTIALS_FILE = _credentials_file
GOOGLE_SHEETS_SPREADSHEET_ID = os.getenv('GOOGLE_SHEETS_SPREADSHEET_ID', '')
GOOGLE_SHEETS_WORKSHEET_NAME = os.getenv('GOOGLE_SHEETS_WORKSHEET_NAME', 'Auto-Bets')


async def update_bet_results_in_sheets():
    """Check positions and update Google Sheets with win/loss results"""
    
    if not GOOGLE_SHEETS_SPREADSHEET_ID:
        print("ERROR: GOOGLE_SHEETS_SPREADSHEET_ID not set in .env file")
        return
    
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        
        # Initialize Google Sheets client
        if not os.path.exists(GOOGLE_SHEETS_CREDENTIALS_FILE):
            print(f"ERROR: Credentials file not found: {GOOGLE_SHEETS_CREDENTIALS_FILE}")
            return
        
        scope = ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_file(GOOGLE_SHEETS_CREDENTIALS_FILE, scopes=scope)
        google_sheets_client = gspread.authorize(creds)
        
        # Initialize Kalshi client
        kalshi_client = KalshiClient()
        await kalshi_client.init()
        
        # Get current positions from Kalshi
        print("[BET GRADING] Fetching positions from Kalshi...")
        positions = await kalshi_client.get_positions()
        print(f"[BET GRADING] Found {len(positions)} open positions")
        
        # Fetch all settlements (for closed positions)
        print("[BET GRADING] Fetching settlements from Kalshi...")
        all_settlements = []
        cursor = None
        page_count = 0
        while True:
            settlements, next_cursor = await kalshi_client.get_settlements(limit=100, cursor=cursor)
            all_settlements.extend(settlements)
            page_count += 1
            if not next_cursor or len(settlements) == 0:
                break
            cursor = next_cursor
            if page_count >= 10:  # Safety limit - max 1000 settlements
                print(f"[BET GRADING] WARNING: Reached page limit (10 pages), stopping settlements fetch")
                break
        
        print(f"[BET GRADING] Found {len(all_settlements)} settlements")
        
        # Create settlements dict for quick lookup: ticker -> list of settlements
        # Settlements might have multiple entries per ticker (YES and NO sides)
        settlements_dict = {}
        for sett in all_settlements:
            ticker = sett.get('ticker', '').upper()
            if ticker:
                if ticker not in settlements_dict:
                    settlements_dict[ticker] = []
                settlements_dict[ticker].append(sett)
        
        # Debug: Print first few settlements to see structure
        if all_settlements:
            print(f"[BET GRADING] Sample settlement structure: {list(all_settlements[0].keys())}")
            sample = all_settlements[0]
            print(f"[BET GRADING] Sample settlement: ticker={sample.get('ticker')}, realized_pnl={sample.get('realized_pnl')}, "
                  f"position={sample.get('position')}, side={sample.get('side')}")
            # Print a few more to see patterns
            if len(all_settlements) > 1:
                print(f"[BET GRADING] Second settlement: ticker={all_settlements[1].get('ticker')}, realized_pnl={all_settlements[1].get('realized_pnl')}, "
                      f"position={all_settlements[1].get('position')}, side={all_settlements[1].get('side')}")
        
        # Open spreadsheet
        spreadsheet = google_sheets_client.open_by_key(GOOGLE_SHEETS_SPREADSHEET_ID)
        worksheet = spreadsheet.worksheet(GOOGLE_SHEETS_WORKSHEET_NAME)
        
        # Get all rows
        rows = worksheet.get_all_values()
        if len(rows) == 0:
            print("[BET GRADING] No rows in sheet")
            return
        
        # Improved header detection - check if first row contains multiple expected column names
        first_row = [col.strip() if col else '' for col in rows[0]]
        first_row_lower = [col.lower() for col in first_row]
        # Check if it looks like a header (has multiple expected column names)
        expected_headers = ['ticker', 'side', 'contracts', 'result', 'pnl', 'settled', 'cost', 'timestamp', 'order_id']
        header_matches = sum(1 for h in expected_headers if h in first_row_lower)
        is_header = header_matches >= 3  # If 3+ expected headers found, it's likely a header row
        
        if is_header:
            # First row is header, data starts at row 2
            header = first_row
            data_rows = rows[1:]
            print(f"[BET GRADING] Found header row, using column names")
            print(f"[BET GRADING] Header columns: {header[:10]}... (showing first 10)")
        else:
            # No header row - use positional indexing based on NEW write_auto_bet_to_sheets order
            # NEW ORDER (2026): timestamp(0), order_id(1), ticker(2), side(3), teams(4), market_type(5), pick(6), qualifier(7),
            #        ev_percent(8), expected_price_cents(9), executed_price_cents(10), american_odds(11),
            #        contracts(12), cost(13), payout(14), win_amount(15), sport(16), status(17), result(18), pnl(19), settled(20),
            #        filter_name(21), devig_books(22)
            header = ['Timestamp', 'Order ID', 'Ticker', 'Side', 'Teams', 'Market Type', 'Pick', 'Qualifier',
                     'EV %', 'Expected Price (¢)', 'Executed Price (¢)', 'American Odds',
                     'Contracts', 'Cost ($)', 'Payout ($)', 'Win Amount ($)', 'Sport', 'Status', 'Result', 'PNL ($)', 'Settled', 'Filter Name', 'Devig Books']
            data_rows = rows  # All rows are data
            print(f"[BET GRADING] No header row found, using positional indexing with NEW format")
        
        # Find column indices (use positional if no header)
        if is_header:
            try:
                # Try to find columns by name (case-insensitive)
                header_lower = [h.lower() for h in header]
                ticker_col = header_lower.index('ticker') if 'ticker' in header_lower else None
                side_col = header_lower.index('side') if 'side' in header_lower else None
                contracts_col = header_lower.index('contracts') if 'contracts' in header_lower else None
                result_col = header_lower.index('result') if 'result' in header_lower else None
                # Try 'pnl' or 'pnl ($)'
                pnl_col = None
                if 'pnl' in header_lower:
                    # Find first occurrence of 'pnl' (might be 'pnl ($)')
                    for idx, h in enumerate(header_lower):
                        if 'pnl' in h:
                            pnl_col = idx
                            break
                settled_col = header_lower.index('settled') if 'settled' in header_lower else None
                # Try 'cost' or 'cost ($)'
                cost_col = None
                if 'cost' in header_lower:
                    # Find first occurrence of 'cost' (might be 'cost ($)')
                    for idx, h in enumerate(header_lower):
                        if 'cost' in h:
                            cost_col = idx
                            break
                
                # Validate all required columns found
                if any(col is None for col in [ticker_col, side_col, contracts_col, result_col, pnl_col, settled_col, cost_col]):
                    missing = []
                    if ticker_col is None: missing.append('ticker')
                    if side_col is None: missing.append('side')
                    if contracts_col is None: missing.append('contracts')
                    if result_col is None: missing.append('result')
                    if pnl_col is None: missing.append('pnl')
                    if settled_col is None: missing.append('settled')
                    if cost_col is None: missing.append('cost')
                    raise ValueError(f"Missing required columns: {', '.join(missing)}")
            except (ValueError, AttributeError) as e:
                print(f"[BET GRADING] ERROR: Missing required column: {e}")
                print(f"[BET GRADING] Available columns: {header}")
                return
        else:
            # Use positional indexing (based on NEW write_auto_bet_to_sheets order)
            ticker_col = 2
            side_col = 3
            contracts_col = 12
            result_col = 18  # Column S (0-indexed = 18)
            pnl_col = 19
            settled_col = 20
            cost_col = 13
            print(f"[BET GRADING] Using positional columns: ticker={ticker_col}, side={side_col}, contracts={contracts_col}, result={result_col}, pnl={pnl_col}, settled={settled_col}, cost={cost_col}")
        
        # Print column mapping for debugging
        # Check if we're processing all bets or just OPEN
        process_all = os.getenv('GRADE_ALL_BETS', 'false').lower() == 'true'
        filter_msg = "Processing ALL rows (re-grading)" if process_all else "Only processing rows where Result='OPEN'"
        
        print(f"[BET GRADING] Column mapping:")
        print(f"  - Ticker: column {ticker_col + 1} ({chr(65 + ticker_col)})")
        print(f"  - Side: column {side_col + 1} ({chr(65 + side_col)})")
        print(f"  - Contracts: column {contracts_col + 1} ({chr(65 + contracts_col)})")
        print(f"  - Result: column {result_col + 1} ({chr(65 + result_col)}) [FILTER: {filter_msg}]")
        print(f"  - PNL: column {pnl_col + 1} ({chr(65 + pnl_col)})")
        print(f"  - Settled: column {settled_col + 1} ({chr(65 + settled_col)})")
        print(f"  - Cost: column {cost_col + 1} ({chr(65 + cost_col)})")
        
        updated_count = 0
        re_graded_count = 0
        newly_graded_count = 0
        skipped_count = 0
        open_rows_count = 0
        
        # Process each row
        # If we have a header, start at row 2, otherwise start at row 1
        start_row = 2 if is_header else 1
        for row_idx, row in enumerate(data_rows, start=start_row):
            if len(row) <= max(ticker_col, side_col, contracts_col, result_col, pnl_col, settled_col):
                continue  # Skip incomplete rows
            
            ticker = row[ticker_col].strip().upper() if ticker_col < len(row) else ''
            side = row[side_col].strip().lower() if side_col < len(row) else ''
            contracts_str = row[contracts_col] if contracts_col < len(row) else '0'
            settled_str = row[settled_col].strip().lower() if settled_col < len(row) else 'false'
            current_result = row[result_col].strip().upper() if result_col < len(row) else ''
            current_pnl = row[pnl_col].strip() if pnl_col < len(row) else ''
            
            # Check if we should process this row
            # If process_all is True, process all rows. Otherwise, only process OPEN rows.
            process_all = os.getenv('GRADE_ALL_BETS', 'false').lower() == 'true'
            
            if not process_all:
                # Only process rows where Result column (column S) is "OPEN"
                if current_result != 'OPEN':
                    skipped_count += 1
                    continue  # Skip this row - already graded or not ready for grading
                open_rows_count += 1
            else:
                # Process all rows (for re-grading)
                if current_result == 'OPEN':
                    open_rows_count += 1
                else:
                    skipped_count += 1  # Count non-OPEN as "already processed" for stats
            
            # Parse contracts
            try:
                contracts = int(float(contracts_str or 0))
            except (ValueError, TypeError):
                contracts = 0
            
            if not ticker or not side or contracts == 0:
                continue
            
            # Parse cost early so it's available in all code paths
            cost = float(row[cost_col] if cost_col < len(row) else 0)
            
            # Find matching position
            matching_position = None
            closed_position_fallback = None  # For closed positions (position=0)
            
            for pos in positions:
                pos_ticker = pos.get('ticker', '').upper()
                pos_position = pos.get('position', 0)  # Positive = YES, Negative = NO
                realized_pnl = pos.get('realized_pnl', 0)  # Realized P&L in dollars
                
                if pos_ticker == ticker:
                    if pos_position > 0:
                        # Open YES position
                        if side == 'yes':
                            matching_position = pos
                            break
                    elif pos_position < 0:
                        # Open NO position
                        if side == 'no':
                            matching_position = pos
                            break
                    else:
                        # Closed position (position = 0) - could be cashed out early
                        # Check if realized_pnl suggests this was our bet (positive P&L = likely cashed out at profit)
                        # Save as fallback - we'll use it if no open position matches
                        if closed_position_fallback is None:
                            closed_position_fallback = pos
                        # If realized_pnl is positive and significant, this might be our cashed-out bet
                        # Prefer this closed position if it has positive P&L (cashed out at profit)
                        elif realized_pnl > 0.01 and pos.get('realized_pnl', 0) > closed_position_fallback.get('realized_pnl', 0):
                            closed_position_fallback = pos
            
            # If no open position found but we have a closed position with matching ticker, use it
            if matching_position is None and closed_position_fallback is not None:
                matching_position = closed_position_fallback
                realized_pnl = matching_position.get('realized_pnl', 0)
                print(f"[BET GRADING] Using closed position (position=0) for {ticker} {side} - realized_pnl={realized_pnl:.2f}")
            
            # Determine result based on MARKET SETTLEMENT (not position/cashout status)
            # CRITICAL: We want to know if the market itself won/lost, not what happened with cashouts
            new_result = None
            new_pnl = None
            new_settled = None
            
            # FIRST: Check settlements for market_result (YES/NO) - this is the source of truth
            # We want to grade based on what the MARKET settled as, not cashout status
            ticker_settlements = settlements_dict.get(ticker.upper(), [])
            market_settled = False
            market_result = None
            
            if ticker_settlements:
                # Get market_result from any settlement (all should have same result)
                for sett in ticker_settlements:
                    mr = sett.get('market_result', '').upper()
                    if mr in ['YES', 'NO']:
                        market_result = mr
                        market_settled = True
                        break
            
            # If market is settled, grade based on market_result vs our bet side (IGNORE cashouts/positions)
            if market_settled and market_result:
                # Compare market_result to our bet side
                if (side.lower() == 'yes' and market_result == 'YES') or (side.lower() == 'no' and market_result == 'NO'):
                    # We won - market settled in our favor
                    new_result = 'WIN'
                    # Calculate P&L: contracts * $1 - cost (actual win, not cashout P&L)
                    win_payout = contracts * 1.0
                    new_pnl = f"{win_payout - cost:.2f}"
                    new_settled = 'True'
                    print(f"[BET GRADING] ✅ WIN: {ticker} {side} - Market settled {market_result}, we bet {side.upper()} (P&L: ${win_payout - cost:.2f})")
                else:
                    # We lost - market settled against us
                    new_result = 'LOSS'
                    new_pnl = f"-{cost:.2f}"
                    new_settled = 'True'
                    print(f"[BET GRADING] ❌ LOSS: {ticker} {side} - Market settled {market_result}, we bet {side.upper()} (P&L: -${cost:.2f})")
            elif matching_position:
                position_count = abs(matching_position.get('position', 0))
                market_exposure = matching_position.get('market_exposure', 0)  # Current value in dollars
                realized_pnl = matching_position.get('realized_pnl', 0)  # Realized P&L in dollars
                # cost already defined above
                
                # Market not settled yet - use position status as fallback
                # If position is closed (position = 0), check realized_pnl
                if position_count == 0:
                    # Position closed - use realized_pnl to determine win/loss
                    # CRITICAL: For early cashouts, realized_pnl can be positive even if small
                    # Any positive realized_pnl means we cashed out at a profit = WIN
                    if realized_pnl > 0.001:  # Won (any positive profit, even 0.1 cent)
                        new_result = 'WIN'
                        new_pnl = f"{realized_pnl:.2f}"
                        new_settled = 'True'
                        print(f"[BET GRADING] ✅ WIN: {ticker} {side} cashed out with realized_pnl={realized_pnl:.2f}")
                    elif realized_pnl < -0.001:  # Lost (any loss)
                        new_result = 'LOSS'
                        new_pnl = f"{realized_pnl:.2f}"
                        new_settled = 'True'
                        print(f"[BET GRADING] ❌ LOSS: {ticker} {side} closed with realized_pnl={realized_pnl:.2f}")
                    else:
                        # realized_pnl is ~0 - might be a push, rounding issue, or very small cashout
                        # Check if we had contracts - if so, and cost > 0, check if we can infer from market_exposure
                        # If market_exposure was high when we cashed out, it was likely a win
                        market_exposure = matching_position.get('market_exposure', 0)
                        if market_exposure > cost * 0.5:  # If exposure was > 50% of cost, likely cashed out at profit
                            new_result = 'WIN'
                            new_pnl = f"{max(0.01, market_exposure - cost):.2f}"  # Small win
                            new_settled = 'True'
                            print(f"[BET GRADING] ✅ WIN (inferred): {ticker} {side} cashed out with market_exposure={market_exposure:.2f} > cost*0.5")
                        elif contracts > 0 and cost > 0:
                            # Conservative: if we can't determine, mark as loss
                            new_result = 'LOSS'
                            new_pnl = f"-{cost:.2f}"
                            new_settled = 'True'
                            print(f"[BET GRADING] ⚠️  LOSS (conservative): {ticker} {side} closed with realized_pnl≈0, marking as loss")
                        else:
                            new_result = 'PENDING'
                            new_pnl = '0.00'
                            new_settled = 'False'
                            print(f"[BET GRADING] ⏳ PENDING: {ticker} {side} closed with realized_pnl≈0, insufficient data")
                else:
                    # Position still open - check current value
                    # If market_exposure ≈ contracts * $1, it won (market settled)
                    # If market_exposure ≈ $0, it lost (market settled)
                    expected_win_value = position_count * 1.0
                    
                    if abs(market_exposure - expected_win_value) < 0.10:  # Within 10 cents
                        new_result = 'WIN'
                        new_pnl = f"{expected_win_value - cost:.2f}"
                        new_settled = 'True'
                    elif market_exposure < 0.10:  # Essentially $0
                        new_result = 'LOSS'
                        new_pnl = f"-{cost:.2f}"
                        new_settled = 'True'
                    else:
                        # Market not settled yet - update unrealized P&L
                        new_result = 'OPEN'
                        new_pnl = f"{market_exposure - cost:.2f}"
                        new_settled = 'False'
            else:
                # No matching position found - market should already be checked above
                # If market is settled, we already handled it. Otherwise, mark as pending.
                if not market_settled:
                    # No settlement found or market not settled
                    new_result = 'PENDING'
                    new_pnl = '0.00'
                    new_settled = 'False'
                    print(f"[BET GRADING] WARNING: Position {ticker} {side} not found in positions and market not settled - PENDING")
                # If market_settled is True, we already set new_result above, so nothing to do here
                else:
                    # No settlement found - check if there's a closed position with position=0
                    closed_position = None
                    for pos in positions:
                        if pos.get('ticker', '').upper() == ticker:
                            pos_position = abs(pos.get('position', 0))
                            if pos_position == 0:  # Closed position
                                closed_position = pos
                                break
                    
                    if closed_position:
                        # Found closed position with position=0 - use realized_pnl
                        realized_pnl = closed_position.get('realized_pnl', 0)
                        market_exposure = closed_position.get('market_exposure', 0)
                        re_grade_note = f" (re-grading from {current_result})" if settled_str == 'true' else ""
                        print(f"[BET GRADING] Using closed position for {ticker} {side}{re_grade_note}: realized_pnl={realized_pnl:.2f}, market_exposure={market_exposure:.2f}")
                        # CRITICAL: For early cashouts, any positive realized_pnl = WIN
                        if realized_pnl > 0.001:  # Any positive profit
                            new_result = 'WIN'
                            new_pnl = f"{realized_pnl:.2f}"
                            new_settled = 'True'
                            print(f"[BET GRADING] ✅ WIN: {ticker} {side} cashed out with realized_pnl={realized_pnl:.2f}")
                        elif realized_pnl < -0.001:  # Any loss
                            new_result = 'LOSS'
                            new_pnl = f"{realized_pnl:.2f}"
                            new_settled = 'True'
                            print(f"[BET GRADING] ❌ LOSS: {ticker} {side} closed with realized_pnl={realized_pnl:.2f}")
                        else:
                            # realized_pnl ≈ 0 - check market_exposure to infer result
                            if market_exposure > cost * 0.5:  # If exposure was high, likely cashed out at profit
                                new_result = 'WIN'
                                new_pnl = f"{max(0.01, market_exposure - cost):.2f}"
                                new_settled = 'True'
                                print(f"[BET GRADING] ✅ WIN (inferred): {ticker} {side} cashed out with market_exposure={market_exposure:.2f}")
                            elif cost > 0:
                                new_result = 'LOSS'
                                new_pnl = f"-{cost:.2f}"
                                new_settled = 'True'
                                print(f"[BET GRADING] ⚠️  LOSS (conservative): {ticker} {side} closed with realized_pnl≈0")
                            else:
                                new_result = 'PENDING'
                                new_pnl = '0.00'
                                new_settled = 'False'
                                print(f"[BET GRADING] ⏳ PENDING: {ticker} {side} closed with realized_pnl≈0, insufficient data")
                    else:
                        # Position completely gone - can't determine win/loss
                        new_result = 'PENDING'
                        new_pnl = '0.00'
                        new_settled = 'False'
                        print(f"[BET GRADING] WARNING: Position {ticker} {side} not found in positions or settlements - PENDING")
            
            # Update row if we have new values
            if new_result is not None:
                # Update the row
                if result_col < len(row):
                    row[result_col] = new_result
                else:
                    # Extend row if needed
                    while len(row) <= result_col:
                        row.append('')
                    row[result_col] = new_result
                
                if pnl_col < len(row):
                    row[pnl_col] = new_pnl
                else:
                    while len(row) <= pnl_col:
                        row.append('')
                    row[pnl_col] = new_pnl
                
                if settled_col < len(row):
                    row[settled_col] = new_settled
                else:
                    while len(row) <= settled_col:
                        row.append('')
                    row[settled_col] = new_settled
                
                # Update the row in Google Sheets (fix deprecation warning - use named args)
                # Add rate limiting to avoid hitting Google Sheets API quota
                range_str = f'A{row_idx}:{chr(64 + len(header))}{row_idx}'
                try:
                    worksheet.update(range_name=range_str, values=[row])
                    updated_count += 1
                    # Small delay to avoid rate limiting (60 requests per minute = 1 per second)
                    import time
                    time.sleep(1.1)  # Slightly more than 1 second to be safe
                except Exception as e:
                    if '429' in str(e) or 'Quota exceeded' in str(e):
                        print(f"[BET GRADING] Rate limit hit, waiting 60 seconds before continuing...")
                        import time
                        time.sleep(60)
                        # Retry once
                        try:
                            worksheet.update(range_name=range_str, values=[row])
                            updated_count += 1
                        except Exception as e2:
                            print(f"[BET GRADING] ERROR: Failed to update {ticker} {side} after retry: {e2}")
                    else:
                        print(f"[BET GRADING] ERROR: Failed to update {ticker} {side}: {e}")
                        raise
                
                # Track re-graded vs newly graded
                if settled_str == 'true':
                    re_graded_count += 1
                else:
                    newly_graded_count += 1
                
                print(f"[BET GRADING] Updated {ticker} {side}: {new_result} (P&L: ${new_pnl})")
        
        print(f"\n[BET GRADING] ========== SUMMARY ==========")
        print(f"[BET GRADING] Found {open_rows_count} row(s) with Result='OPEN'")
        print(f"[BET GRADING] Skipped {skipped_count} row(s) (Result not 'OPEN' - already graded)")
        
        if updated_count > 0:
            print(f"[BET GRADING] ✅ Updated {updated_count} bet result(s) in Google Sheets")
            if re_graded_count > 0:
                print(f"[BET GRADING]   - Re-graded: {re_graded_count} bet(s)")
            if newly_graded_count > 0:
                print(f"[BET GRADING]   - Newly graded: {newly_graded_count} bet(s)")
        else:
            if open_rows_count > 0:
                print(f"[BET GRADING] ⚠️  Found {open_rows_count} OPEN row(s) but couldn't match them to Kalshi positions/settlements")
            else:
                print(f"[BET GRADING] ℹ️  No OPEN bets found to grade")
        
        # CRITICAL: Recalculate totals from ENTIRE sheet (not just what we graded)
        print(f"\n[BET GRADING] ========== RECALCULATING TOTALS FROM ENTIRE SHEET ==========")
        try:
            # Re-read all rows to get updated values
            all_rows = worksheet.get_all_values()
            if is_header:
                all_data_rows = all_rows[1:]
            else:
                all_data_rows = all_rows
            
            total_pnl = 0.0
            total_cost = 0.0
            settled_count = 0
            win_count = 0
            loss_count = 0
            open_count = 0
            
            for row in all_data_rows:
                if len(row) <= max(ticker_col, side_col, contracts_col, result_col, pnl_col, cost_col):
                    continue
                
                result = row[result_col].strip().upper() if result_col < len(row) else ''
                settled_str = row[settled_col].strip().lower() if settled_col < len(row) else 'false'
                settled = settled_str == 'true'
                
                # Parse PNL and cost
                try:
                    pnl_val = float(str(row[pnl_col]).replace('$', '').replace(',', '').strip() or '0')
                    cost_val = float(str(row[cost_col]).replace('$', '').replace(',', '').strip() or '0')
                except (ValueError, TypeError):
                    pnl_val = 0.0
                    cost_val = 0.0
                
                total_cost += cost_val
                
                if settled:
                    total_pnl += pnl_val
                    settled_count += 1
                    if result == 'WIN':
                        win_count += 1
                    elif result == 'LOSS':
                        loss_count += 1
                elif result == 'OPEN':
                    open_count += 1
            
            roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0
            win_rate = (win_count / settled_count * 100) if settled_count > 0 else 0.0
            
            print(f"[BET GRADING] 📊 OVERALL STATISTICS (from entire sheet):")
            print(f"[BET GRADING]   Total PNL: ${total_pnl:,.2f}")
            print(f"[BET GRADING]   Total Cost: ${total_cost:,.2f}")
            print(f"[BET GRADING]   ROI: {roi:.2f}%")
            print(f"[BET GRADING]   Settled Bets: {settled_count}")
            print(f"[BET GRADING]   Wins: {win_count}")
            print(f"[BET GRADING]   Losses: {loss_count}")
            print(f"[BET GRADING]   Win Rate: {win_rate:.1f}%")
            print(f"[BET GRADING]   Open Bets: {open_count}")
        except Exception as e:
            print(f"[BET GRADING] ⚠️  WARNING: Failed to recalculate totals: {e}")
            import traceback
            traceback.print_exc()
        
        await kalshi_client.close()
        
        # Generate summary report after grading
        print("\n" + "=" * 80)
        print("[SUMMARY] Generating bet results summary...")
        print("=" * 80)
        try:
            # Add current directory to path to import generate_summary
            current_dir = os.path.dirname(os.path.abspath(__file__))
            if current_dir not in sys.path:
                sys.path.insert(0, current_dir)
            from generate_summary import generate_summary
            generate_summary()
        except Exception as e:
            print(f"[SUMMARY] WARNING: Failed to generate summary: {e}")
            import traceback
            traceback.print_exc()
    
    except Exception as e:
        print(f"[BET GRADING] ERROR: {e}")
        import traceback
        traceback.print_exc()


async def main():
    """Main function"""
    print("=" * 80)
    print("Bet Grading Script - Updating Google Sheets with win/loss results")
    print("=" * 80)
    
    await update_bet_results_in_sheets()
    
    print("=" * 80)
    print("Bet grading complete!")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())

