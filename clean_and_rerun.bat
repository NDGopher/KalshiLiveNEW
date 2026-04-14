@echo off
REM Clean market_matcher.py and re-run team code extraction
REM This removes bad mappings and extracts fresh ones with improved logic
REM USE THIS ONLY IF: You have bad mappings that need to be cleaned first

echo ================================================================================
echo CLEANING AND RE-EXTRACTING TEAM CODES
echo ================================================================================
echo.
echo WARNING: This will REMOVE all existing dynamically extracted mappings first!
echo          Use this ONLY if you have bad mappings that need cleaning.
echo.
echo For normal updates (merging new mappings), use: extract_team_codes_daily.bat
echo.
pause

cd /d "%~dp0"

REM Activate virtual environment if it exists
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

echo.
echo Step 1: Cleaning market_matcher.py (removing all dynamic mappings)...
python clean_market_matcher.py

echo.
echo Step 2: Extracting team codes with improved logic...
python abbreviation_finder.py > team_codes_output.txt 2>&1

echo.
echo ================================================================================
echo COMPLETE
echo ================================================================================
echo.
echo Output saved to: team_codes_output.txt
echo.
echo Please review the output carefully:
echo   - Check that team names are actual team names (not "WINNER?" or "WINS BY")
echo   - Verify mappings make sense (e.g., "ALABAMA A&M" -> ['AAMU'])
echo   - Look for any remaining bad entries
echo   - Check for any "CHANGES DETECTED" warnings
echo.
pause
