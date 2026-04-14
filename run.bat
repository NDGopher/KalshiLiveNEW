@echo off
title Kalshi Live Betting System
color 0A

:START
cls
echo ============================================================
echo KALSHI LIVE BETTING SYSTEM (Odds-API.io)
echo ============================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH!
    pause
    exit /b 1
)

if not exist .env (
    echo Creating empty .env - add ODDS_API_KEY and Kalshi credentials.
    echo. > .env
)

echo Checking ODDS_API_KEY in .env...
python -c "import os; from dotenv import load_dotenv; load_dotenv(); exit(0 if (os.getenv('ODDS_API_KEY') or '').strip() else 1)" 2>&1
if errorlevel 1 (
    python -c "from dotenv import load_dotenv" >nul 2>&1
    if errorlevel 1 (
        echo ERROR: python-dotenv is not installed. Run: pip install -r requirements.txt
        pause
        exit /b 1
    )
    echo.
    echo ODDS_API_KEY is missing or empty in .env. Add your Odds-API.io key, then run again.
    echo.
    pause
    exit /b 1
)

echo Starting dashboard...
echo.

python main.py
set EXIT_CODE=%ERRORLEVEL%

if %EXIT_CODE% NEQ 0 (
    echo.
    echo Dashboard exited with error (code: %EXIT_CODE%).
    choice /C YN /M "Restart dashboard"
    if errorlevel 2 goto END
    if errorlevel 1 goto START
)

:END
echo.
echo Dashboard stopped.
pause
