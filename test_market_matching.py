"""
Comprehensive Market Matching Test Suite
Tests all market types across all sports to ensure bulletproof matching
"""
import asyncio
from ev_alert import EvAlert
from kalshi_client import KalshiClient
from market_matcher import MarketMatcher
import os
from dotenv import load_dotenv

load_dotenv()

# Test cases: (market_type, line, selection, expected_ticker_pattern, expected_side, description)
TEST_CASES = [
    # NBA Totals
    {
        'market_type': 'Total Points',
        'line': 226.5,
        'selection': 'Over',
        'expected_ticker_pattern': 'KXNBATOTAL-.*-226',
        'expected_side': 'yes',
        'description': 'NBA Total Over 226.5'
    },
    {
        'market_type': 'Total Points',
        'line': 226.5,
        'selection': 'Under',
        'expected_ticker_pattern': 'KXNBATOTAL-.*-226',
        'expected_side': 'no',
        'description': 'NBA Total Under 226.5'
    },
    
    # NFL Spreads
    {
        'market_type': 'Point Spread',
        'line': 3.5,
        'selection': 'Los Angeles Chargers',
        'qualifier': '+3.5',  # Underdog
        'expected_ticker_pattern': 'KXNFLSPREAD-.*-NE3',  # Should match opponent's market
        'expected_side': 'no',  # NO on NE3 = Chargers +3.5
        'description': 'NFL Spread: Chargers +3.5 (underdog)'
    },
    {
        'market_type': 'Point Spread',
        'line': -3.5,
        'selection': 'New England Patriots',
        'qualifier': '-3.5',  # Favorite
        'expected_ticker_pattern': 'KXNFLSPREAD-.*-NE3',  # Should match NE's market
        'expected_side': 'yes',  # YES on NE3 = NE -3.5
        'description': 'NFL Spread: Patriots -3.5 (favorite)'
    },
    
    # NHL Moneylines
    {
        'market_type': 'Moneyline',
        'line': None,
        'selection': 'Nashville Predators',
        'expected_ticker_pattern': 'KXNHLGAME-.*-NSH',
        'expected_side': 'yes',
        'description': 'NHL Moneyline: Nashville'
    },
    
    # NHL Totals
    {
        'market_type': 'Total Goals',
        'line': 6.5,
        'selection': 'Over',
        'expected_ticker_pattern': 'KXNHLTOTAL-.*-6',
        'expected_side': 'yes',
        'description': 'NHL Total Over 6.5'
    },
    
    # NHL Puck Line (Spread)
    {
        'market_type': 'Puck Line',
        'line': 1.5,
        'selection': 'Vegas Golden Knights',
        'qualifier': '-1.5',
        'expected_ticker_pattern': 'KXNHLSPREAD-.*-VGK1',
        'expected_side': 'yes',
        'description': 'NHL Puck Line: Vegas -1.5'
    },
    
    # MLB Totals
    {
        'market_type': 'Total Runs',
        'line': 9.5,
        'selection': 'Over',
        'expected_ticker_pattern': 'KXMLBTOTAL-.*-9',
        'expected_side': 'yes',
        'description': 'MLB Total Over 9.5'
    },
    
    # NCAAB Spreads
    {
        'market_type': 'Point Spread',
        'line': 13.5,
        'selection': 'TCU',
        'qualifier': '+13.5',
        'expected_ticker_pattern': 'KXNCAAMBSPREAD-.*',
        'expected_side': 'no',  # Underdog spread
        'description': 'NCAAB Spread: TCU +13.5'
    },
]

async def test_market_matching():
    """Test market matching for all test cases"""
    kalshi_client = KalshiClient()
    market_matcher = MarketMatcher(kalshi_client)
    
    await kalshi_client.init()
    
    print("=" * 80)
    print("COMPREHENSIVE MARKET MATCHING TEST SUITE")
    print("=" * 80)
    print()
    
    # Get a real event ticker for testing (use a recent NBA game)
    # You'll need to update this with a real event ticker
    test_event_ticker = "KXNBAGAME-26JAN11ATLGSW"  # Example: Atlanta @ Golden State
    
    results = {
        'passed': 0,
        'failed': 0,
        'skipped': 0,
        'errors': []
    }
    
    for i, test_case in enumerate(TEST_CASES, 1):
        print(f"\n{'='*80}")
        print(f"TEST {i}/{len(TEST_CASES)}: {test_case['description']}")
        print(f"{'='*80}")
        
        try:
            # Create mock alert
            alert_data = {
                'market_type': test_case['market_type'],
                'pick': test_case['selection'],
                'qualifier': test_case.get('qualifier', str(test_case.get('line', ''))),
                'teams': 'Team A @ Team B',  # Mock teams
                'ticker': test_event_ticker,  # Will need real event ticker
            }
            alert = EvAlert(alert_data)
            alert.line = test_case.get('line')
            
            # Test find_submarket
            print(f"\n[STEP 1] Finding submarket...")
            print(f"  Market Type: {test_case['market_type']}")
            print(f"  Line: {test_case.get('line')}")
            print(f"  Selection: {test_case['selection']}")
            print(f"  Event Ticker: {test_event_ticker}")
            
            submarket = await kalshi_client.find_submarket(
                event_ticker=test_event_ticker,
                market_type=test_case['market_type'],
                line=test_case.get('line'),
                selection=test_case['selection']
            )
            
            if not submarket:
                print(f"  ❌ FAILED: No submarket found")
                results['failed'] += 1
                results['errors'].append({
                    'test': test_case['description'],
                    'error': 'No submarket found'
                })
                continue
            
            submarket_ticker = submarket.get('ticker', '')
            print(f"  ✅ Found submarket: {submarket_ticker}")
            
            # Check ticker pattern
            import re
            if not re.search(test_case['expected_ticker_pattern'], submarket_ticker):
                print(f"  ⚠️  WARNING: Ticker pattern mismatch")
                print(f"     Expected pattern: {test_case['expected_ticker_pattern']}")
                print(f"     Actual ticker: {submarket_ticker}")
            
            # Test determine_side
            print(f"\n[STEP 2] Determining side...")
            side = market_matcher.determine_side(alert, submarket)
            
            if not side:
                print(f"  ❌ FAILED: Could not determine side")
                results['failed'] += 1
                results['errors'].append({
                    'test': test_case['description'],
                    'error': 'Could not determine side',
                    'ticker': submarket_ticker
                })
                continue
            
            print(f"  ✅ Determined side: {side}")
            
            # Verify side
            if side.lower() == test_case['expected_side'].lower():
                print(f"  ✅ PASSED: Side matches expected ({test_case['expected_side']})")
                results['passed'] += 1
            else:
                print(f"  ❌ FAILED: Side mismatch")
                print(f"     Expected: {test_case['expected_side']}")
                print(f"     Got: {side}")
                results['failed'] += 1
                results['errors'].append({
                    'test': test_case['description'],
                    'error': f'Side mismatch: expected {test_case["expected_side"]}, got {side}',
                    'ticker': submarket_ticker
                })
            
            # Show market details
            print(f"\n[STEP 3] Market Details:")
            print(f"  Ticker: {submarket_ticker}")
            print(f"  Title: {submarket.get('title', 'N/A')}")
            print(f"  YES Subtitle: {submarket.get('yes_sub_title', 'N/A')}")
            print(f"  NO Subtitle: {submarket.get('no_sub_title', 'N/A')}")
            print(f"  Determined Side: {side}")
            
        except Exception as e:
            print(f"  ❌ ERROR: {e}")
            import traceback
            traceback.print_exc()
            results['failed'] += 1
            results['errors'].append({
                'test': test_case['description'],
                'error': str(e)
            })
    
    await kalshi_client.close()
    
    # Print summary
    print(f"\n{'='*80}")
    print("TEST SUMMARY")
    print(f"{'='*80}")
    print(f"Total Tests: {len(TEST_CASES)}")
    print(f"✅ Passed: {results['passed']}")
    print(f"❌ Failed: {results['failed']}")
    print(f"⏭️  Skipped: {results['skipped']}")
    
    if results['errors']:
        print(f"\n❌ ERRORS:")
        for error in results['errors']:
            print(f"  - {error['test']}: {error['error']}")
    
    return results

if __name__ == "__main__":
    asyncio.run(test_market_matching())
