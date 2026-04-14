@echo off
REM Daily Team Code Extraction Script
REM Run this for normal updates - MERGES new mappings with existing ones
REM This is the RECOMMENDED script for regular updates

echo ================================================================================
echo TEAM CODE EXTRACTION (MERGE MODE)
echo ================================================================================
echo.
echo This will:
echo   - Extract team codes from Kalshi API
echo   - MERGE with existing mappings (preserves good mappings)
echo   - DETECT changes if Kalshi updated team codes
echo   - Add new teams and codes
echo.
echo For cleaning bad mappings first, use: clean_and_rerun.bat
echo.
pause

cd /d "%~dp0"

REM Activate virtual environment if it exists
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

REM Run the extraction script (automatically merges and updates)
REM Output will show in console - also saving to file for reference
echo.
echo Extracting team codes from Kalshi API...
echo.
python abbreviation_finder.py
echo.
echo Saving full output to team_codes_output.txt...
python abbreviation_finder.py > team_codes_output.txt 2>&1

echo.
echo ================================================================================
echo EXTRACTION COMPLETE
echo ================================================================================
echo.
echo Summary shown above. Full output also saved to: team_codes_output.txt
echo.
echo Review the summary above for:
echo   - New teams added [NEW]
echo   - Primary code changes [CHANGE]
echo   - New codes added to existing teams [UPDATE]
echo   - Format differences (same team, different name) [INFO]
echo   - Codes no longer in Kalshi [WARNING]
echo.
echo market_matcher.py has been automatically updated!
echo.
pause
