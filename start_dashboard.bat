@echo off
cd /d "%~dp0"

title Kalshi Live - starting
echo ============================================================
echo  Kalshi Live dashboard (Odds-API.io)
echo ============================================================
echo.

where py >nul 2>&1
if errorlevel 1 (
  set "PYEXE=python"
) else (
  set "PYEXE=py"
)

%PYEXE% --version >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python not found. Install Python 3.10+ and ensure "py" or "python" is on PATH.
  pause
  exit /b 1
)

REM Pick a free TCP port (bind 0 = ephemeral), then release it for dashboard to use
for /f "delims=" %%p in ('powershell -NoProfile -Command "$l = New-Object System.Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0); $l.Start(); $p = $l.LocalEndpoint.Port; $l.Stop(); Write-Output $p"') do set "DASHBOARD_PORT=%%p"
if "%DASHBOARD_PORT%"=="" (
  echo ERROR: Could not allocate a port via PowerShell.
  pause
  exit /b 1
)

echo Dashboard URL: http://127.0.0.1:%DASHBOARD_PORT%/
echo (If the page fails to load, wait a few seconds and refresh.)
echo.

REM Require ODDS_API_KEY from .env and/or .env.env (cwd is project dir)
%PYEXE% -c "from pathlib import Path; import os, sys; from dotenv import load_dotenv; r=Path.cwd(); load_dotenv(r/'.env', override=True, encoding='utf-8-sig'); load_dotenv(r/'.env.env', override=False, encoding='utf-8-sig'); sys.exit(0 if (os.getenv('ODDS_API_KEY') or '').strip() else 1)" 2>nul
if errorlevel 1 (
  echo ERROR: ODDS_API_KEY is missing. Set it in .env or .env.env in this folder, then run again.
  pause
  exit /b 1
)

REM Open browser after a short delay (separate process so the server can bind first)
start "" cmd /c "timeout /t 5 /nobreak >nul && start http://127.0.0.1:%DASHBOARD_PORT%/"

echo Starting dashboard.py on port %DASHBOARD_PORT% ...
echo Close this window or press Ctrl+C to stop the server.
echo.

%PYEXE% dashboard.py
set EC=%ERRORLEVEL%

echo.
echo Server exited (code %EC%).
pause
exit /b %EC%
