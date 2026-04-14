@echo off
echo ========================================
echo Re-Grading ALL Bets (including already graded)
echo ========================================
echo.
echo This will re-grade ALL bets in the sheet based on market settlement results.
echo It will update bets that were previously graded incorrectly.
echo.
pause

cd /d "%~dp0"
set GRADE_ALL_BETS=true
python grade_bets.py
pause
