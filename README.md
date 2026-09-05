# Kalshi Live — Aggressive Betting Bot

Real-time **+EV** opportunities on **Kalshi and PLive** (betting / take venues), driven by **[Odds-API.io](https://odds-api.io/)** WebSocket odds (REST for slate/resync only) plus the Origin-only **PLive** Pandora feed. Fair is the other configured pack — PLive is never a sharp. The **dashboard** (`dashboard.py` / `main.py`) is the main entry point.

## MLB tonight (ship slice)

Stephen’s Growth plan: **10 catalog books + WebSocket**. Put `ODDS_API_KEY` in `.env` yourself — never commit it.

```bash
ODDS_API_KEY=...
ODDS_API_WS=true
ODDS_API_SPORTS=baseball
ODDS_API_LEAGUE_MLB=usa-mlb
```

That combination is the **baseball-only** slice (`leagues=usa-mlb` is pinned only then).

Default / recommended multi-sport WS (keep MLB; add soccer + NFL/CFB):

```bash
ODDS_API_SPORTS=baseball,football,american-football
# Do not set ODDS_API_WS_LEAGUES=usa-mlb here — that would drop soccer and CFB/NFL.
```

- Unset / `all` sports also use `baseball,football,american-football` on the WS (no `usa-mlb` pin).
- Official soccer slug is **`football`**. American football (NFL/CFB) is **`american-football`**.
- WebSocket connects to `wss://api.odds-api.io/v3/ws?apiKey=...` with those sports, `markets=ML,Spread,Totals`, and **no `leagues=`** unless you are on the baseball-only slice.
- One connection per key. `leagues` and `eventIds` are mutually exclusive (`eventIds` max 50) — do not use eventIds for the multi-sport slate.
- Selected books ≠ books that actually return soccer prices. This repo does not invent Circa/Kalshi/Polymarket/NoVig/BetMGM soccer coverage.

That combination is enough for the baseball-only pin:

- WebSocket connects to `wss://api.odds-api.io/v3/ws?apiKey=...` with `sport=baseball`, `leagues=usa-mlb`, `markets=ML,Spread,Totals`.
- **No `status=` param** so you get **prematch + live** on the one connection (the API allows only `live` *or* `prematch`, not both).
- REST stays for `/events` + `/events/live` slate repair and `resync_required` snapshots (`includeSeq=true` → `X-OddsAPI-Seq` → reconnect `lastSeq`).
- The old per-poll `/odds/multi` hot loop is **not** the primary path when the WS is healthy (a 10-book REST poller will 429 a Growth WS account).
- **Global auto-bet defaults OFF** at process start (fail-closed). The last ON/OFF is restored from `user_filters_state.json` (`auto_bet_enabled`). Enable via dashboard `POST /api/set_auto_bet` or `bot_control enable_auto_bet`.
- EvAlerts still come from `ev_calculator.py` (POWER / WORST_CASE / AVERAGE).
- Default filter remains **Kalshi All Sports (3 Sharps Live)** (GAMELINES, minSharpBooks 3, POWER/AVERAGE vs Kalshi, bettingBooks=`[Kalshi]`). Auto-bet American band **-200..+200** (~30–70¢). CBB stays WORST_CASE / minSharp 2 / auto EV 10–25%. **Soccer Live (2 Sharps)** is a third dashboard filter (SOCCER_ALL, GAMELINES, minSharp 2, POWER/AVERAGE). BetMGM is excluded from the soccer **sharp pack only** (tiles / account selection stay). 1H / team totals are excluded. Soccer auto-bet stays OFF. Default auto-bet stake is **$25** when unset (`DEFAULT_AUTO_BET_AMOUNT`). Market-type sizes stay `151 / 101 / 75 / 202 / 404`, `user_max_bet=100`, PX+Novig 2x. Persisted in `user_filters_state.json`.

### `user_filters_state.json` keys

Single persist file (atomic replace via `_persist_filters_state` / `_load_filters_state`). Written by filter save/select and by `set_auto_bet` / `bot_control` enable-disable / Telegram start-stop.

| Key | Purpose | Missing-key default |
|-----|---------|---------------------|
| `saved_filters` | Named filter payloads | Built-in DEFAULT / CBB / Soccer |
| `selected_dashboard_filters` | Dashboard monitor list | Product filters |
| `selected_auto_bettor_filters` | Filters allowed to auto-bet | `[]` |
| `auto_bet_enabled` | Global auto-bet ON/OFF | `false` (fail-closed) |
| `auto_bet_amount` | Top-level stake | `25` |
| `auto_bet_ev_min` / `auto_bet_ev_max` | Top-level EV bounds | `5` / `25` |
| `auto_bet_odds_min` / `auto_bet_odds_max` | Top-level American odds band | `-200` / `200` |
| `auto_bet_settings_by_filter` | Per-filter EV / odds / amount / enabled | Amount `25`, enabled `false` |

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

## PLive (Pandora) — second betting / take venue

Origin-only Socket.IO. **No login. No cookies. No BetBCK. No BookieBeats DOM.**

PLive is **not** a sharp / minSharp / `devig_books` book. Betting books are **Kalshi and PLive**. Fair is the rec pack (DK / FD / Caesars / NV / Bet365 / Betfair / MGM / Circa). Kalshi cards never put PLive in fair (tile only). PLive cards may use Kalshi in fair/devig. If PLive is +EV vs that pack, the dashboard emits a PLive take card (PLive on the left; same min-EV whole-card hide). Auto-bet stays OFF and never fires on PLive cards.

Public handshake (bare connect is silent):

1. `wss://pandora.ganchrow.com/socket.io/?EIO=4&transport=websocket`
2. Header `Origin: https://plive.becoms.co`
3. After CONNECT emit `setSocketMetadata {partnerId: 113, flavor: "live"}`
4. Then `subscribeSystemEvents` + `subscribe` / `getCache` once for `live.sports` (names) and `live.main.<LINE_SET>.eventData` (directory). For each wanted live id (MLB sport 1 / league 8, soccer sport 5, Top Soccer 220) subscribe `eventCoefficients.{id}` and unsubscribe when finished. `live.events` is dead. `#!/event/{id}` is a client-side route — do not scrape the HTML.

- MLB is catalog **sport 1** (`#!/sport/1`). Soccer is native **sport 5** (`#!/sport/5`). `https://plive.becoms.co/live/?#!/sport/220` is the public-UI **Top Soccer** bucket. `live.sports` names both on the same connection. They appear as `s[5]` / `s[220]` on the **same** `eventData` tree — no extra sport-room. `s[220]` can be empty while native 5 still has live soccer.
- **Mapping boundary:** Odds-API event IDs already join Odds-API books. PLive uses Pandora ids and needs a **separate** join. Soccer matching requires normalized competition (when both sides publish a name), same-orientation home+away token identity (never a swap), and a start-time window (Odds-API `startTime`/`startsAt`/`commence_time`/`date` vs PLive eventData unix / `ip`) or both live. Stale `coeff_updated_at` is rejected. Zero or two-plus survivors emit **no** PLive markets and **no** EV. Team-name fuzzy-only is forbidden. MLB still uses the older swap-tolerant matcher.
- `eventData` list is **[home, away]** (stadium home first). Market 6 `[idx0, idx1]` is a 2-way decimal pair, not money/decimal.
- PLive run-line name/ticker: away keeps the **slot sign** (`line_style=american`) so a −1.5 slot is not painted as Sox +1.5. Kalshi/Odds-API still negate home `hdp` for away.
- Trust the `live.sports` catalog (1 Baseball, 2 Basketball, 3 Football, …). Do not use the old Selenium map that had nfl=2 / nba=3.
- Filter JSON still has `bettingBooks=[Kalshi]`; PLive take is a second venue in code. Same filters and dollar sizes. PLive is not a new filter.
- **No BetBCK scrape.**

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
