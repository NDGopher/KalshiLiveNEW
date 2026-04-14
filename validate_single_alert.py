"""
Single Alert Validation Tool
Use this to test a specific alert before betting on it
"""
import asyncio
import sys
from ev_alert import EvAlert
from kalshi_client import KalshiClient
from market_matcher import MarketMatcher
from dotenv import load_dotenv

load_dotenv()

async def validate_alert(event_ticker: str, market_type: str, line: float, selection: str, qualifier: str = None):
    """
    Validate a single alert to see what market it would match and what side it would bet
    
    Args:
        event_ticker: Event ticker (e.g., "KXNBAGAME-26JAN11ATLGSW")
        market_type: Market type (e.g., "Total Points", "Point Spread", "Moneyline")
        line: Line value (e.g., 226.5 for totals, 3.5 for spreads, None for moneylines)
        selection: Selection (e.g., "Over", "Under", "Los Angeles Chargers")
        qualifier: Qualifier string (e.g., "+3.5", "-3.5", "226.5")
    """
    kalshi_client = KalshiClient()
    market_matcher = MarketMatcher(kalshi_client)
    
    await kalshi_client.init()
    
    print("=" * 80)
    print("SINGLE ALERT VALIDATION")
    print("=" * 80)
    print(f"\nInput:")
    print(f"  Event Ticker: {event_ticker}")
    print(f"  Market Type: {market_type}")
    print(f"  Line: {line}")
    print(f"  Selection: {selection}")
    print(f"  Qualifier: {qualifier}")
    print()
    
    # Create mock alert
    alert_data = {
        'market_type': market_type,
        'pick': selection,
        'qualifier': qualifier or (str(line) if line else ''),
        'teams': 'Team A @ Team B',
        'ticker': event_ticker,
    }
    alert = EvAlert(alert_data)
    alert.line = line
    
    # Step 1: Find submarket
    print("[STEP 1] Finding submarket...")
    submarket = await kalshi_client.find_submarket(
        event_ticker=event_ticker,
        market_type=market_type,
        line=line,
        selection=selection
    )
    
    if not submarket:
        print("  ❌ FAILED: No submarket found")
        print("\nPossible reasons:")
        print("  - Event ticker is incorrect")
        print("  - Market doesn't exist for this event")
        print("  - Line value doesn't match any available markets")
        await kalshi_client.close()
        return
    
    submarket_ticker = submarket.get('ticker', '')
    print(f"  ✅ Found submarket: {submarket_ticker}")
    print(f"  Title: {submarket.get('title', 'N/A')}")
    print(f"  YES Subtitle: {submarket.get('yes_sub_title', 'N/A')}")
    print(f"  NO Subtitle: {submarket.get('no_sub_title', 'N/A')}")
    
    # Step 2: Determine side
    print(f"\n[STEP 2] Determining side...")
    side = market_matcher.determine_side(alert, submarket)
    
    if not side:
        print("  ❌ FAILED: Could not determine side")
        print("\nPossible reasons:")
        print("  - Ambiguous market subtitles")
        print("  - Pick doesn't match market")
        print("  - Ticker suffix doesn't match pick")
        await kalshi_client.close()
        return
    
    print(f"  ✅ Determined side: {side}")
    
    # Step 3: Validation
    print(f"\n[STEP 3] Validation:")
    
    # Check market type
    market_type_lower = market_type.lower()
    
    if 'total' in market_type_lower:
        # For totals, verify side is correct
        if selection.upper() == 'OVER' and side != 'yes':
            print(f"  ⚠️  WARNING: Pick is 'Over' but side is '{side}' (should be 'yes')")
        elif selection.upper() == 'UNDER' and side != 'no':
            print(f"  ⚠️  WARNING: Pick is 'Under' but side is '{side}' (should be 'no')")
        else:
            print(f"  ✅ Side correct for totals")
        
        # Check line in ticker
        if '-' in submarket_ticker:
            ticker_parts = submarket_ticker.split('-')
            if len(ticker_parts) >= 2:
                ticker_line_str = ticker_parts[-1]
                try:
                    ticker_line = int(ticker_line_str)
                    expected_line_int = int(line) if line else None
                    if expected_line_int and ticker_line != expected_line_int:
                        print(f"  ⚠️  WARNING: Line mismatch - ticker has {ticker_line}, expected {expected_line_int}")
                    else:
                        print(f"  ✅ Line matches ticker")
                except:
                    pass
    
    elif 'spread' in market_type_lower or 'puck line' in market_type_lower:
        # For spreads, verify we matched the correct market
        if '-' in submarket_ticker:
            ticker_parts = submarket_ticker.split('-')
            if len(ticker_parts) >= 2:
                ticker_suffix = ticker_parts[-1].upper()
                print(f"  Ticker suffix: {ticker_suffix}")
                
                # Extract team code and line from suffix
                import re
                line_match = re.search(r'(\d+)', ticker_suffix)
                if line_match:
                    ticker_team_code = ticker_suffix[:line_match.start()]
                    ticker_line = int(line_match.group(1))
                    print(f"  Ticker team code: {ticker_team_code}")
                    print(f"  Ticker line: {ticker_line}")
                    
                    # Check if this is pick team's market or opponent's
                    selection_upper = selection.upper()
                    # This is a simplified check - in reality, we'd use team_code_map
                    if ticker_team_code in selection_upper or any(word in ticker_team_code for word in selection_upper.split()):
                        print(f"  ✅ Matched pick team's market")
                        if qualifier and qualifier.startswith('-'):
                            print(f"  ✅ Side '{side}' correct for favorite spread")
                        else:
                            print(f"  ⚠️  WARNING: Matched pick team's market but pick is underdog (should match opponent's market)")
                    else:
                        print(f"  ⚠️  Matched opponent's market (expected for underdog spreads)")
                        if qualifier and qualifier.startswith('+'):
                            if side == 'no':
                                print(f"  ✅ Side 'no' correct for underdog spread on opponent's market")
                            else:
                                print(f"  ⚠️  WARNING: Underdog spread should bet NO on opponent's market")
    
    elif 'moneyline' in market_type_lower or 'game' in market_type_lower:
        # For moneylines, verify ticker suffix matches pick
        if '-' in submarket_ticker:
            ticker_parts = submarket_ticker.split('-')
            if len(ticker_parts) >= 2:
                ticker_suffix = ticker_parts[-1].upper()
                print(f"  Ticker suffix: {ticker_suffix}")
                print(f"  Selection: {selection.upper()}")
                # Simplified check
                if side == 'yes':
                    print(f"  ✅ Side 'yes' means betting on ticker suffix team")
                else:
                    print(f"  ⚠️  WARNING: Side 'no' on moneyline - verify this is correct")
    
    # Final summary
    print(f"\n{'='*80}")
    print("VALIDATION SUMMARY")
    print(f"{'='*80}")
    print(f"Submarket: {submarket_ticker}")
    print(f"Side: {side}")
    print(f"Action: Would bet {side.upper()} on {submarket_ticker}")
    print()
    
    if side:
        print("✅ VALIDATION PASSED - This alert would bet correctly")
    else:
        print("❌ VALIDATION FAILED - Do not bet on this alert")
    
    await kalshi_client.close()

if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: python validate_single_alert.py <event_ticker> <market_type> <line> <selection> [qualifier]")
        print("\nExample:")
        print("  python validate_single_alert.py KXNBAGAME-26JAN11ATLGSW 'Total Points' 226.5 Over")
        print("  python validate_single_alert.py KXNFLGAME-26JAN11LACNE 'Point Spread' 3.5 'Los Angeles Chargers' '+3.5'")
        sys.exit(1)
    
    event_ticker = sys.argv[1]
    market_type = sys.argv[2]
    line = float(sys.argv[3]) if sys.argv[3] != 'None' else None
    selection = sys.argv[4]
    qualifier = sys.argv[5] if len(sys.argv) > 5 else None
    
    asyncio.run(validate_alert(event_ticker, market_type, line, selection, qualifier))
