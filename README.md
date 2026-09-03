# Kalshi Live — Aggressive Betting Bot

Real-time **+EV** opportunities on Kalshi, driven by **[Odds-API.io](https://odds-api.io/)** WebSocket odds (REST for slate/resync only) plus optional **PLive** (Pandora) as a local sharp/display book. The **dashboard** (`dashboard.py` / `main.py`) is the main entry point.

## MLB tonight (ship slice)

Stephen’s Growth plan: **10 catalog books + WebSocket**. Put `ODDS_API_KEY` in `.env` yourself — never commit it.

```bash
ODDS_API_KEY=...
ODDS_API_WS=true
ODDS_API_SPORTS=baseball
ODDS_API_LEAGUE_MLB=usa-mlb
```

That combination is enough:

- WebSocket connects to `wss://api.odds-api.io/v3/ws?apiKey=...` with `sport=baseball`, `leagues=usa-mlb`, `markets=ML,Spread,Totals`.
- **No `status=` param** so you get **prematch + live** on the one connection (the API allows only `live` *or* `prematch`, not both).
- REST stays for `/events` + `/events/live` slate repair and `resync_required` snapshots (`includeSeq=true` → `X-OddsAPI-Seq` → reconnect `lastSeq`).
- The old per-poll `/odds/multi` hot loop is **not** the primary path when the WS is healthy (a 10-book REST poller will 429 a Growth WS account).
- **Global auto-bet stays OFF** at startup. Same existing filter knobs and dollar sizes. Enable auto-bet from the dashboard when you trust the feed.
- EvAlerts still come from `ev_calculator.py` (POWER / WORST_CASE / AVERAGE).

## 10 bookmakers (Odds-API.io catalog names)

Default `ODDS_API_BOOKMAKERS` (do **not** add BookMaker.eu — catalog-inactive):

`DraftKings, FanDuel, BetMGM, Betfair Exchange, Circa, Polymarket, Bet365, Caesars, Kalshi, NoVig`

Select those 10 in the [Odds-API.io dashboard](https://odds-api.io) (or set `ODDS_API_SELECT_BOOKS=true` once so the client `PUT /bookmakers/selected/select`). The WS welcome message lists account books; a mismatch vs `ODDS_API_BOOKMAKERS` is logged loudly.

### Betfair Exchange (do not remap to Sportsbook)

Requests send **`Betfair Exchange`** on the wire. `ODDS_API_BETFAIR_REQUEST_NAME` is an **override only** if your account catalog uses a different label (e.g. `Betfair Sportsbook`).

## WebSocket vs REST

| Path | Use |
|------|-----|
| **WebSocket** | Primary live + prematch lines. Replace markets per event+bookie (never merge). Track `seq`; reconnect with `lastSeq` (compacted latest-state replay). |
| **REST `/events`, `/events/live`** | Slate: which MLB games exist. |
| **REST `/odds` / `/odds/multi` + `includeSeq=true`** | Handoff + `resync_required` only. |
| **REST `/odds/updated`** | Fallback if WS is down (≤90s window, one book per call) to spare quota. |

Docs: [WebSockets](https://docs.odds-api.io/guides/websockets) · [API](https://docs.odds-api.io/)

`ODDS_API_WS` defaults **true when `ODDS_API_KEY` is set**. Set `ODDS_API_WS=false` to force REST.

## PLive (Pandora) — extra sharp/display book

Port of `NDGopher/UnifiedBetting` `backend/pandora_odds_subscriber.py`:

- Origin `https://plive.becoms.co`, URL `wss://pandora.ganchrow.com`, **no login**.
- MLB is PLive `#!/sport/1`.
- Lines are merged into the same EvAlert pipeline as book **`PLive`** (not sent to Odds-API.io).
- **No BetBCK scrape. No BookieBeats DOM.**

Disable with `PLIVE_ENABLED=false`.

## Setup

```bash
python -m venv venv
venv\Scripts\activate   # Windows
# source venv/bin/activate  # Linux/macOS
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set at least:

- `ODDS_API_KEY` (you provide this)
- `ODDS_API_SPORTS=baseball` and `ODDS_API_LEAGUE_MLB=usa-mlb` for the MLB slice
- `KALSHI_EMAIL` / `KALSHI_PASSWORD` (or keys `kalshi_client.py` expects)
- `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD`

See `TROUBLESHOOTING.md` and `docs/BOOKIEBEATS_ODDS_API_ROADMAP.md`.

## Run

```bash
python dashboard.py
```

or `python main.py` — then **http://localhost:5000**.

```bash
pytest tests/test_odds_api_ws.py tests/test_plive_pandora.py -q
```

CI / local tests use mocks only — no live key required.

## Disclaimer

Educational / experimental trading software. You are responsible for compliance, API limits, and capital at risk.
