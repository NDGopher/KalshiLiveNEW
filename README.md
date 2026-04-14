# Kalshi Live — Aggressive Betting Bot

Real-time **+EV** opportunities on Kalshi, driven by **[Odds-API.io](https://odds-api.io/)** consensus and your configured book list. The **dashboard** (`dashboard.py` / `main.py`) is the main entry point: live alerts, filters, auto-bet, portfolio, dark mode, and Socket.IO updates.

## What it does

- Polls Odds-API.io on an interval you set (e.g. `ODDS_POLL_INTERVAL_SECONDS=8`).
- Devigs against your sharp books, matches events to Kalshi, and surfaces **Live Alerts** in the browser.
- Optional **auto-bet** when EV / odds / liquidity rules pass (configure carefully).
- **No BookieBeats / bearer tokens** — only `ODDS_API_KEY` and related env vars.

## Requirements

- Python 3.10+
- Kalshi credentials (see `.env` keys used by `kalshi_client.py`)
- Odds-API.io API key

## Setup

```bash
python -m venv venv
venv\Scripts\activate   # Windows
# source venv/bin/activate  # Linux/macOS
pip install -r requirements.txt
```

Copy or create `.env` with at least:

- `KALSHI_EMAIL`, `KALSHI_PASSWORD` (or keys your Kalshi client expects)
- `ODDS_API_KEY`
- `ODDS_API_BOOKMAKERS` (comma-separated, include `Kalshi` if you compare to Kalshi)
- `ODDS_POLL_INTERVAL_SECONDS` (e.g. `8`)
- `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD`
- Optional: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

See `TROUBLESHOOTING.md` for firewall, systemd, and common errors.

## Run

```bash
python dashboard.py
```

or

```bash
python main.py
```

Then open **http://localhost:5000** (HTTP basic auth if configured).

- **Live Alerts** — main feed; each card can show multi-book odds when `display_books` is present (your “live odds” view inline with alerts).
- **Control** — http://localhost:5000/control  
- **Logs** — http://localhost:5000/logs  

`/token-update` redirects to `/` (legacy URL).

## Production notes

- Use a process manager or **systemd** so the bot restarts on failure.
- Tune `ODDS_API_LIVE_ONLY`, `ODDS_DEBUG_MODE`, leagues, and devig settings in `.env` to match your API plan and risk tolerance.

## Disclaimer

Educational / experimental trading software. You are responsible for compliance, API limits, and capital at risk.
