"""
AbbreviationFinder - Extracts team codes from Kalshi API and updates market_matcher.py
Run this periodically (e.g., daily) to update team mappings
"""
import asyncio
import re
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from KalshiLiveBetting.kalshi_client import KalshiClient

async def extract_team_codes():
    """Extract team codes from Kalshi API for all sports"""
    client = KalshiClient()
    await client.init()
    
    print("=" * 80)
    print("ABBREVIATION FINDER - Extracting Team Codes from Kalshi API")
    print("=" * 80)
    
    # Define all sport series
    sport_series = {
        'NCAAB': ['KXNCAAMBGAME', 'KXNCAAMBSPREAD', 'KXNCAAMBTOTAL'],
        'NCAAF': ['KXNCAAMBGAME', 'KXNCAAMBSPREAD', 'KXNCAAMBTOTAL'],  # College Football (same series)
    }
    
    all_team_mappings = {}
    
    for sport_name, series_list in sport_series.items():
        print(f"\n[{sport_name}] Extracting team codes...")
        sport_mapping = {}
        seen_codes = set()
        
        for series in series_list:
            try:
                query_params = {
                    'series_ticker': series,
                    'limit': 500,
                }
                
                markets = await client.search_markets(query_params)
                print(f"  {series}: Found {len(markets)} markets")
                
                for market in markets:
                    ticker = market.get('ticker', '').upper()
                    if not ticker:
                        continue
                    
                    # Extract team codes from ticker
                    parts = ticker.split('-')
                    if len(parts) >= 3:
                        team_code_part = parts[-1]
                        
                        # Extract base code (remove numbers)
                        base_code = re.sub(r'\d+$', '', team_code_part)
                        
                        if base_code and len(base_code) >= 2:
                            # Get team name from market data
                            # CRITICAL: Kalshi uses 'yes_sub_title' and 'no_sub_title' (with underscore)
                            yes_subtitle = (market.get('yes_sub_title', '') or market.get('yes_subtitle', '') or '').upper().strip()
                            no_subtitle = (market.get('no_sub_title', '') or market.get('no_subtitle', '') or '').upper().strip()
                            market_title = (market.get('title', '') or '').upper().strip()
                            
                            # Extract team names - ONLY from clean sources
                            team_names = set()
                            
                            # METHOD 1: Extract from market title (ONLY if it has "Team1 @ Team2" format)
                            # This is the MOST RELIABLE source - titles like "Alabama A&M @ TCU"
                            if market_title:
                                # Remove common prefixes
                                title_clean = re.sub(r'^(MENS|WOMENS|NCAA|COLLEGE|BASKETBALL|FOOTBALL|MENS COLLEGE|WOMENS COLLEGE):\s*', '', market_title, flags=re.IGNORECASE).strip()
                                
                                # CRITICAL: Only process if title contains "@" or "VS" (indicates two teams)
                                # Skip titles like "A WINNER?" or "A WINS BY OVER 2.5 POINTS?"
                                if '@' in title_clean or ' VS ' in title_clean or ' VS. ' in title_clean:
                                    # Split by @ or vs
                                    title_parts = re.split(r'\s*[@|VS|VS\.]\s*', title_clean, maxsplit=1)
                                    if len(title_parts) == 2:
                                        team1_raw = title_parts[0].strip()
                                        team2_raw = title_parts[1].strip()
                                        
                                        # Remove market-specific suffixes (WINNER?, WINS BY, etc.)
                                        team1_clean = re.sub(r'\s+(WINNER\?|WINS BY|BY OVER|BY UNDER|TOTAL|POINTS|GOALS|SPREAD).*$', '', team1_raw, flags=re.IGNORECASE).strip()
                                        team2_clean = re.sub(r'\s+(WINNER\?|WINS BY|BY OVER|BY UNDER|TOTAL|POINTS|GOALS|SPREAD).*$', '', team2_raw, flags=re.IGNORECASE).strip()
                                        
                                        # Remove question marks and other punctuation
                                        team1_clean = re.sub(r'[?\.]+$', '', team1_clean).strip()
                                        team2_clean = re.sub(r'[?\.]+$', '', team2_clean).strip()
                                        
                                        # Remove common suffixes (but keep main name)
                                        team1_short = re.sub(r'\s+(ST|STATE|UNIVERSITY|UNIV|U|ST\.|STATE\.|COLLEGE)$', '', team1_clean, flags=re.IGNORECASE).strip()
                                        team2_short = re.sub(r'\s+(ST|STATE|UNIVERSITY|UNIV|U|ST\.|STATE\.|COLLEGE)$', '', team2_clean, flags=re.IGNORECASE).strip()
                                        
                                        # Validate: team name must be meaningful (not just "A" or "ADA" or single letter)
                                        def is_valid_team_name(name):
                                            if not name or len(name) < 3:
                                                return False
                                            # Skip single letters or very short codes
                                            if len(name) <= 2:
                                                return False
                                            # Skip if it's just a question mark or market description
                                            if name in ['WINNER', 'WINS', 'BY', 'OVER', 'UNDER', 'TOTAL', 'POINTS', 'GOALS']:
                                                return False
                                            # Skip if it contains market-specific words
                                            if any(word in name for word in ['WINNER', 'WINS BY', 'BY OVER', 'BY UNDER']):
                                                return False
                                            return True
                                        
                                        # Add full names
                                        if is_valid_team_name(team1_clean):
                                            team_names.add(team1_clean)
                                        if is_valid_team_name(team2_clean):
                                            team_names.add(team2_clean)
                                        
                                        # Add short names (without suffixes) if different
                                        if is_valid_team_name(team1_short) and team1_short != team1_clean:
                                            team_names.add(team1_short)
                                        if is_valid_team_name(team2_short) and team2_short != team2_clean:
                                            team_names.add(team2_short)
                            
                            # METHOD 2: Extract from YES subtitle (ONLY for moneylines, and ONLY if clean)
                            # Skip if subtitle contains market descriptions like "WINNER?", "WINS BY", etc.
                            if yes_subtitle and 'WINNER' not in yes_subtitle and 'WINS BY' not in yes_subtitle:
                                # Remove common suffixes
                                clean_yes = re.sub(r'\s+(WINS|WINS BY|BY OVER|BY UNDER|TOTAL|POINTS|GOALS|SPREAD).*$', '', yes_subtitle, flags=re.IGNORECASE).strip()
                                clean_yes = re.sub(r'\s+\d+.*$', '', clean_yes).strip()  # Remove numbers
                                clean_yes = re.sub(r'[?\.]+$', '', clean_yes).strip()  # Remove question marks
                                
                                # Only add if it's a valid team name (not just a code or market description)
                                if (clean_yes and len(clean_yes) > 2 and 
                                    clean_yes != base_code and 
                                    not clean_yes.isdigit() and
                                    'WINNER' not in clean_yes and
                                    'WINS BY' not in clean_yes):
                                    team_names.add(clean_yes)
                            
                            # METHOD 3: Extract from NO subtitle (same validation)
                            if no_subtitle and 'WINNER' not in no_subtitle and 'WINS BY' not in no_subtitle:
                                clean_no = re.sub(r'\s+(WINS|WINS BY|BY OVER|BY UNDER|TOTAL|POINTS|GOALS|SPREAD).*$', '', no_subtitle, flags=re.IGNORECASE).strip()
                                clean_no = re.sub(r'\s+\d+.*$', '', clean_no).strip()
                                clean_no = re.sub(r'[?\.]+$', '', clean_no).strip()
                                
                                if (clean_no and len(clean_no) > 2 and 
                                    clean_no != base_code and 
                                    not clean_no.isdigit() and
                                    'WINNER' not in clean_no and
                                    'WINS BY' not in clean_no):
                                    team_names.add(clean_no)
                            
                            # Only add mappings if we found actual, valid team names
                            if team_names:
                                for team_name in team_names:
                                    # Final validation: must be meaningful team name
                                    if (team_name != base_code and 
                                        len(team_name) >= 3 and  # At least 3 characters
                                        not team_name.isdigit() and
                                        '?' not in team_name and
                                        'WINNER' not in team_name and
                                        'WINS BY' not in team_name):
                                        if team_name not in sport_mapping:
                                            sport_mapping[team_name] = []
                                        if base_code not in sport_mapping[team_name]:
                                            sport_mapping[team_name].append(base_code)
                                            if base_code not in seen_codes:
                                                seen_codes.add(base_code)
                
                await asyncio.sleep(0.2)
            except Exception as e:
                print(f"  Error with {series}: {e}")
                continue
        
        all_team_mappings[sport_name] = sport_mapping
        print(f"✓ Extracted {len(sport_mapping)} team entries, {len(seen_codes)} unique codes")
    
    # Merge all sports
    merged_mapping = {}
    for sport_name, mapping in all_team_mappings.items():
        for team_name, codes in mapping.items():
            if team_name not in merged_mapping:
                merged_mapping[team_name] = []
            for code in codes:
                if code not in merged_mapping[team_name]:
                    merged_mapping[team_name].append(code)
    
    print(f"\n{'=' * 80}")
    print(f"EXTRACTION COMPLETE: {len(merged_mapping)} team entries extracted")
    print(f"{'=' * 80}")
    
    await client.close()
    return merged_mapping

def update_market_matcher_file(team_mapping):
    """
    Update market_matcher.py with extracted team codes
    MERGES new mappings with existing ones (doesn't overwrite)
    """
    market_matcher_path = os.path.join(os.path.dirname(__file__), 'market_matcher.py')
    
    # Read the file
    with open(market_matcher_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Extract existing dynamically extracted mappings from file
    existing_mappings = {}
    # Also create a normalized lookup for fuzzy matching
    existing_mappings_normalized = {}  # normalized_name -> (original_name, codes)
    
    pattern_existing = r"# College Basketball \(NCAAB\) & College Football \(NCAAF\) - Dynamically Extracted\n(.*?)(\s*)(\})"
    match_existing = re.search(pattern_existing, content, flags=re.DOTALL)
    if match_existing:
        existing_section = match_existing.group(1)
        # Parse existing mappings: 'TEAM NAME': ['CODE1', 'CODE2'],
        for line in existing_section.split('\n'):
            line = line.strip()
            if line.startswith("'") and ':' in line:
                try:
                    # Extract team name and codes
                    # Format: 'TEAM NAME': ['CODE1', 'CODE2'],
                    team_match = re.match(r"'([^']+)':\s*\[(.*?)\]", line)
                    if team_match:
                        team_name = team_match.group(1)
                        codes_str = team_match.group(2)
                        # Parse codes: 'CODE1', 'CODE2'
                        codes = [c.strip().strip("'\"") for c in codes_str.split(',') if c.strip()]
                        if codes:
                            existing_mappings[team_name] = codes
                            
                            # Create normalized version for fuzzy matching
                            def normalize_team_name(name):
                                # Remove apostrophes (both straight and curly), periods, spaces
                                name = re.sub(r"[''`]", '', name.upper())
                                name = re.sub(r'[.\s]', '', name)
                                name = re.sub(r'ST$', '', name)  # Remove trailing ST
                                return name
                            
                            normalized = normalize_team_name(team_name)
                            if normalized not in existing_mappings_normalized:
                                existing_mappings_normalized[normalized] = []
                            existing_mappings_normalized[normalized].append((team_name, codes))
                except:
                    pass  # Skip malformed lines
    
    # Filter out code-only entries from new mappings
    meaningful_new_mappings = {}
    for team_name, codes in team_mapping.items():
        # Skip if team_name is just one of its codes (useless mapping)
        if not (team_name in codes and len(team_name) <= 4):
            meaningful_new_mappings[team_name] = codes
    
    # MERGE: Combine existing and new mappings with change detection
    merged_mappings = existing_mappings.copy()
    
    # Track changes for reporting
    new_count = 0
    updated_count = 0
    changed_count = 0
    changes_detected = []  # List of (team_name, old_codes, new_codes, change_type)
    
    # Helper function to normalize team names for fuzzy matching
    def normalize_team_name(name):
        # Remove apostrophes (both straight and curly), periods, spaces
        name = re.sub(r"[''`]", '', name.upper())
        name = re.sub(r'[.\s]', '', name)
        name = re.sub(r'ST$', '', name)  # Remove trailing ST
        return name
    
    for team_name, new_codes in meaningful_new_mappings.items():
        # First check exact match
        if team_name in merged_mappings:
            # Team exists - check for changes and merge codes
            existing_codes = set(merged_mappings[team_name])
            new_codes_set = set(new_codes)
            
            # Detect changes:
            # 1. New codes found (additions)
            new_codes_found = new_codes_set - existing_codes
            # 2. Codes that disappeared (removals - Kalshi might have changed them)
            removed_codes = existing_codes - new_codes_set
            # 3. Primary code changed (first code in list is usually primary)
            primary_changed = False
            if existing_codes and new_codes:
                old_primary = list(existing_codes)[0] if existing_codes else None
                new_primary = new_codes[0] if new_codes else None
                if old_primary != new_primary and new_primary not in existing_codes:
                    primary_changed = True
            
            # Merge: Add new codes, but keep old ones as fallbacks (in case Kalshi reverts)
            # However, prioritize new codes (put them first) if primary changed
            if primary_changed or new_codes_found:
                if primary_changed:
                    # Primary code changed - update with new primary first, then old codes as fallbacks
                    merged_codes = new_codes.copy()  # Start with new codes (prioritize new primary)
                    for old_code in merged_mappings[team_name]:
                        if old_code not in merged_codes:
                            merged_codes.append(old_code)  # Keep old codes as fallbacks
                    merged_mappings[team_name] = merged_codes
                    changed_count += 1
                    changes_detected.append((team_name, list(existing_codes), new_codes, 'PRIMARY_CHANGED'))
                    # Don't print here - will be shown in summary
                else:
                    # Just new codes added (no primary change)
                    for code in new_codes_found:
                        merged_mappings[team_name].append(code)
                        updated_count += 1
                
                # Track removed codes (will be shown in summary)
                if removed_codes:
                    changes_detected.append((team_name, list(existing_codes), new_codes, 'CODES_REMOVED'))
        else:
            # New team - but first check for fuzzy match (might be duplicate with different formatting)
            team_normalized = normalize_team_name(team_name)
            matching_existing = None
            
            # Check normalized lookup
            if team_normalized in existing_mappings_normalized:
                # Found a match with normalized name - it's the same team with different formatting
                for existing_team, existing_codes in existing_mappings_normalized[team_normalized]:
                    # Merge codes (add new codes to existing team)
                    existing_codes_set = set(existing_codes)
                    new_codes_to_add = []
                    for code in new_codes:
                        if code not in existing_codes_set:
                            existing_codes.append(code)
                            new_codes_to_add.append(code)
                            updated_count += 1
                    
                    # Update the merged mappings (use existing team name, not new one)
                    merged_mappings[existing_team] = existing_codes
                    matching_existing = existing_team
                    
                    # Track this as a formatting difference (not a new team)
                    if new_codes_to_add:
                        changes_detected.append((team_name, new_codes, existing_codes, 'FORMAT_DIFFERENCE'))
                    else:
                        # Same codes, just different formatting - still track it
                        changes_detected.append((team_name, new_codes, existing_codes, 'FORMAT_DIFFERENCE'))
                    break
            
            if not matching_existing:
                # Truly new team - add it
                merged_mappings[team_name] = new_codes
                new_count += 1
    
    # Generate the merged mappings code
    mappings_lines = ["                # College Basketball (NCAAB) & College Football (NCAAF) - Dynamically Extracted\n"]
    
    # Sort by team name for consistency
    sorted_teams = sorted(merged_mappings.items())
    
    for team_name, codes in sorted_teams:
        # Format: 'TEAM NAME': ['CODE1', 'CODE2'],
        # Escape single quotes in team names to prevent syntax errors
        escaped_team_name = team_name.replace("'", "\\'")
        codes_str = ', '.join([f"'{code}'" for code in codes])
        mappings_lines.append(f"                '{escaped_team_name}': [{codes_str}],\n")
    
    new_mappings_text = ''.join(mappings_lines)
    
    # Find and replace the dynamically extracted section
    pattern = r"(# College Basketball \(NCAAB\) & College Football \(NCAAF\) - Dynamically Extracted\n)(.*?)(\s*)(\})"
    replacement = r"\1" + new_mappings_text + r"\3\4"
    new_content = re.sub(pattern, replacement, content, flags=re.DOTALL)
    
    # If pattern not found, try to find where to insert
    if new_content == content:
        # Look for the end of MLB section
        pattern2 = r"('WASHINGTON NATIONALS': \['WSH'\], 'WASHINGTON': \['WSH', 'WAS'\], 'NATIONALS': \['WSH'\],\n)(\s*)(# College Basketball)"
        if re.search(pattern2, content):
            # Insert before existing college section
            new_mappings_text_with_header = "# College Basketball (NCAAB) & College Football (NCAAF) - Dynamically Extracted\n" + new_mappings_text
            replacement2 = r"\1" + new_mappings_text_with_header + r"\2\3"
            new_content = re.sub(pattern2, replacement2, content, flags=re.DOTALL)
        else:
            # Find the end of the team_code_map dict
            pattern3 = r"('WASHINGTON NATIONALS': \['WSH'\], 'WASHINGTON': \['WSH', 'WAS'\], 'NATIONALS': \['WSH'\],\n)(\s*)(\})"
            new_mappings_text_with_header = "# College Basketball (NCAAB) & College Football (NCAAF) - Dynamically Extracted\n" + new_mappings_text
            replacement3 = r"\1" + new_mappings_text_with_header + r"\2\3"
            new_content = re.sub(pattern3, replacement3, content, flags=re.DOTALL)
    
    # Write the updated file
    with open(market_matcher_path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    
    # Prepare summary for console output
    print(f"\n{'=' * 80}")
    print(f"📊 UPDATE SUMMARY")
    print(f"{'=' * 80}")
    
    # Show only what changed or was added (not existing unchanged)
    has_changes = new_count > 0 or updated_count > 0 or changed_count > 0
    
    if not has_changes:
        print(f"[OK] No changes detected - all mappings are up to date!")
        print(f"   Total mappings: {len(merged_mappings)}")
        print(f"{'=' * 80}\n")
    else:
        # Show new teams added (with fuzzy matching to detect similar names)
        if new_count > 0:
            print(f"\n[NEW] NEW TEAMS ADDED ({new_count}):")
            print(f"{'-' * 80}")
            new_teams = []
            potential_duplicates = []
            
            # Helper to normalize team names
            def normalize_for_display(name):
                return normalize_team_name(name)
            
            for team, codes in meaningful_new_mappings.items():
                # Check if it's truly new (not in existing, and not a fuzzy match)
                team_normalized = normalize_for_display(team)
                
                # Check exact match first
                if team in existing_mappings:
                    # Already exists exactly - skip (will be shown in updates section)
                    continue
                
                # Check fuzzy match (normalized name)
                is_fuzzy_match = False
                if team_normalized in existing_mappings_normalized:
                    for existing_team, existing_codes in existing_mappings_normalized[team_normalized]:
                        # Same team, different formatting - show as format difference
                        potential_duplicates.append((team, existing_team, codes, existing_codes))
                        is_fuzzy_match = True
                        break
                
                if not is_fuzzy_match:
                    # Truly new team
                    new_teams.append((team, codes))
            
            # Show truly new teams
            for team_name, codes in sorted(new_teams)[:20]:  # Show first 20
                print(f"  + {team_name:40} -> {codes}")
            if len(new_teams) > 20:
                print(f"  ... and {len(new_teams) - 20} more new teams")
            
            # Show format differences (same team, different name formatting - already merged)
            if potential_duplicates:
                print(f"\n[INFO] FORMAT DIFFERENCES ({len(potential_duplicates)}):")
                print(f"{'-' * 80}")
                print(f"  Same team found with different name formatting (merged into existing):")
                for new_name, existing_name, new_codes, existing_codes in potential_duplicates:
                    print(f"  [FORMAT] '{new_name}' (extracted) = '{existing_name}' (existing)")
                    if set(new_codes) & set(existing_codes):
                        print(f"     -> Same codes - merged successfully (no duplicate added)")
                    else:
                        print(f"     -> Codes: {new_codes} merged into existing: {existing_codes}")
        
        # Show teams with primary code changes
        primary_changes = [c for c in changes_detected if c[3] == 'PRIMARY_CHANGED']
        if primary_changes:
            print(f"\n[CHANGE] PRIMARY CODE CHANGES ({len(primary_changes)}):")
            print(f"{'-' * 80}")
            for team_name, old_codes, new_codes, _ in primary_changes:
                old_primary = old_codes[0] if old_codes else 'N/A'
                new_primary = new_codes[0] if new_codes else 'N/A'
                print(f"  [CHANGE] {team_name:40}")
                print(f"     Primary: {old_primary} -> {new_primary}")
                print(f"     Full mapping: {old_codes} -> {new_codes}")
        
        # Show teams with new codes added (but no primary change)
        if updated_count > 0:
            print(f"\n[UPDATE] NEW CODES ADDED TO EXISTING TEAMS ({updated_count} codes):")
            print(f"{'-' * 80}")
            updated_teams = []
            for team_name, new_codes in meaningful_new_mappings.items():
                if team_name in existing_mappings:
                    existing_codes = set(existing_mappings[team_name])
                    new_codes_set = set(new_codes)
                    new_codes_found = new_codes_set - existing_codes
                    if new_codes_found and team_name not in [c[0] for c in primary_changes]:
                        updated_teams.append((team_name, list(existing_codes), list(new_codes), list(new_codes_found)))
            
            for team_name, old_codes, new_codes, added_codes in sorted(updated_teams)[:20]:  # Show first 20
                print(f"  + {team_name:40}")
                print(f"     Added codes: {added_codes}")
                print(f"     Full mapping: {old_codes} -> {new_codes}")
            if len(updated_teams) > 20:
                print(f"  ... and {len(updated_teams) - 20} more teams with new codes")
        
        # Show teams with removed codes (warnings)
        removed_changes = [c for c in changes_detected if c[3] == 'CODES_REMOVED']
        if removed_changes:
            print(f"\n[WARNING] CODES NO LONGER IN KALSHI ({len(removed_changes)} teams):")
            print(f"{'-' * 80}")
            for team_name, old_codes, new_codes, _ in removed_changes:
                removed = set(old_codes) - set(new_codes)
                print(f"  [WARNING] {team_name:40}")
                print(f"     Removed codes: {list(removed)} (kept as fallbacks)")
        
        # Final summary
        print(f"\n{'=' * 80}")
        print(f"📈 STATISTICS:")
        print(f"   Existing mappings: {len(existing_mappings)}")
        print(f"   New teams added: {new_count}")
        print(f"   Teams with new codes: {updated_count}")
        print(f"   Teams with primary code changes: {changed_count}")
        print(f"   Total mappings: {len(merged_mappings)}")
        print(f"{'=' * 80}")
        print(f"[OK] market_matcher.py has been updated!")
        print(f"{'=' * 80}\n")

async def main():
    """Main function"""
    try:
        team_mapping = await extract_team_codes()
        
        # Update the file (summary will be shown in update_market_matcher_file)
        print("\n" + "=" * 80)
        print("UPDATING market_matcher.py...")
        print("=" * 80)
        update_market_matcher_file(team_mapping)
        
        print("\n" + "=" * 80)
        print("[OK] COMPLETE - Team mappings updated in market_matcher.py")
        print("=" * 80)
        
    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
