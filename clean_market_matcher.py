"""
Script to clean up market_matcher.py - removes bad dynamically extracted mappings
Run this before re-running abbreviation_finder.py
"""
import re
import os

def clean_market_matcher():
    """Remove all dynamically extracted college mappings from market_matcher.py"""
    market_matcher_path = os.path.join(os.path.dirname(__file__), 'market_matcher.py')
    
    with open(market_matcher_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Find and remove the dynamically extracted section
    # Look for the marker and remove everything until the closing brace
    pattern = r'(# College Basketball \(NCAAB\) & College Football \(NCAAF\) - Dynamically Extracted\n.*?)(\s*)(\})'
    
    # Replace with just a comment
    replacement = r'# College Basketball (NCAAB) & College Football (NCAAF) - Dynamically Extracted\n                # (Run abbreviation_finder.py to populate this section)\n\2\3'
    
    new_content = re.sub(pattern, replacement, content, flags=re.DOTALL)
    
    # Also remove any duplicate markers
    new_content = re.sub(r'# College Basketball \(NCAAB\) & College Football \(NCAAF\) - Dynamically Extracted\n.*?# College Basketball \(NCAAB\) & College Football \(NCAAF\) - Dynamically Extracted\n', 
                         '# College Basketball (NCAAB) & College Football (NCAAF) - Dynamically Extracted\n', 
                         new_content, flags=re.DOTALL)
    
    with open(market_matcher_path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    
    print("✅ Cleaned market_matcher.py - removed bad dynamically extracted mappings")
    print("   You can now re-run abbreviation_finder.py with the fixed extraction logic")

if __name__ == "__main__":
    clean_market_matcher()
