# BookieBeats-style Kalshi + Odds-API.io — goals and roadmap

This file is the **source of truth for product intent** so work stays aligned across sessions.  
Implementation lives in `dashboard.py`, `odds_ev_monitor.py`, `odds_api_client.py`, `ev_calculator.py`, and static templates.

---

## North star

Build a **BookieBeats-class** workflow on **Odds-API.io** data:

- **Real-time (eventually sub-second) odds + EV** vs your **saved devig rules** (sharps, method, min books, holds, limits).
- **Alerts** that respect **per-filter** payloads (sport/league, market types, EV/ROI, odds ranges, liquidity).
- **Auto-bettor** into **Kalshi** first; architecture should allow more execution venues later.
- **Pregame** filters are first-class (slower refresh is fine); **live** is where latency matters most.

Non-goals for v1: perfect parity with every BookieBeats edge case; burning REST quota on illiquid or non-Kalshi markets.

---

## What to cherry-pick from `Dev/` (beneficial, low regret)

| Item | Dev path / behavior | Why bring it | Caveat |
|------|---------------------|--------------|--------|
| **Filter persistence** | `USER_FILTERS_STATE_FILE`, load/save `saved_filters` + selections | Survives restarts; matches “build filters and test” | Wire load at startup; keep atomic write (Dev pattern). |
| **`redact_secrets_for_log`** | `Dev/odds_api_client.py` | Safer logs when echoing URLs/errors | Small copy or shared util. |
| **`secrets_bootstrap.py`** | `Dev/secrets_bootstrap.py` | Optional bundled creds in CI / locked-down deploy | Only if you use that workflow; keep try/except import. |
| **Separate live vs pregame event TTL** | `ODDS_API_LIVE_EVENTS_TTL_SEC` vs `ODDS_API_EVENTS_TTL_SEC` | Lets **live list** refresh faster **without** hammering pregame `/events` | Set live TTL **≥** live poll interval to avoid duplicate HTTP. |
| **Stronger 429 backoff** | Dev exponential retry + cap | Stability under burst | Must be paired with **higher poll interval** during REST phase; else you only sleep more. |
| **Control panel / templates** | `Dev/templates/control_panel.html`, logs, etc. | UX for filter lab | Merge selectively; avoid Dev’s **1s default poll** + **2s live TTL** defaults. |

**Do not blindly merge:** Dev defaults (`ODDS_POLL_INTERVAL_SECONDS` default **1s**, odds TTL **8s**, live events **2s**, `ALL_ODDS_API_SPORTS_SLUGS`, parallel pregame burst) — those are what **smoked** quota and asyncio timeouts. Keep **work repo defaults** until you explicitly tune.

---

## Coverage without “garbage” HTTP

**Principle:** Only pay HTTP for events you could **actually** trade on Kalshi with meaningful size.

1. **Liquidity default sports (when ``ODDS_API_SPORTS`` unset)** — ``odds_api_client.odds_api_sports_list()``  
   - **Slugs:** ``baseball``, ``basketball``, ``ice-hockey``, ``american-football``, ``football`` (soccer).  
   - **College vs pro:** Odds-API uses **coarse** ``sport`` values — **NCAAB shares ``basketball`` with NBA**; **NCAA football shares ``american-football`` with NFL**. There is no separate “college-only” slug, so defaults keep those two slugs **year-round** so you do not miss CBB/CFB when seasons turn on; off-season you mostly get empty or sparse rows (still cached, bounded cost).  
   - **Explicit ``ODDS_API_SPORTS``** overrides (comma list). ``all`` / ``*`` / ``everything`` resolve to the **same five** liquidity slugs (not the full API catalog), to cap pregame ``/events`` fan-out toward **5k/hr**.  
   - **Tier B** (tennis, MMA, …): add by name in ``ODDS_API_SPORTS`` when you want them.

2. **Gate around `/odds/multi`**  
   - Event must appear in **`/events/live`** for live path **or** explicit pregame list with **league** in your filter set.  
   - **Kalshi book** must have at least one **gameline** market row (ML / spread / total) with **decimals** and optional **min liquidity** from row `limit` / `stake` fields.  
   - Skip events where Kalshi has no line (you cannot bet them). **Broad diagnostic scan:** after batched `/odds/multi`, merged docs with no tradable Kalshi gameline are dropped from EV work (saves CPU; batch HTTP unchanged).

3. **Pregame cadence**  
   - **Slow** (e.g. 60–120s or on-demand + cache) independent of live poll.  
   - **Dashboard:** “Broad scan: include pregame” is **off by default**; pregame multi-sport ``/events`` runs only when that box is checked (class toggle + ``/api/broad_scan_pregame``).  
   - Never run full pregame on the same urgency as live unless you accept the extra HTTP.

4. **Book fan-out**  
   - Keep **batched** `/odds/multi` with **bounded** parallel book slices; avoid doubling calls without user opt-in.

---

## WebSocket — expectations

- **WS does not mean “zero HTTP.”** You still have connect/auth/reconnect, and many providers use REST for snapshots, history, or recovery.
- **WS does mean:** push updates instead of polling every N seconds → **lower average requests** and **lower end-to-end latency** once subscribed.
- **EV “lightning speed”** after WS: updates arrive faster, but **EV is still CPU work** (devig, multi-sharp, filters). That’s cheap compared to network; WS removes **wait-for-next-poll** delay.

**Trial timing:** Use REST until **data integrity** (side alignment, line matching, filter behavior) is proven on **10–20s** refresh; then enable WS trial when you’re ready to optimize latency, not to fix bad gating.

---

## Refresh strategy (feasible)

| Phase | Live poll | Live odds/event cache | Pregame |
|-------|------------|------------------------|---------|
| **Testing (now)** | **10–20 s** | **Slate:** long ``ODDS_API_LIVE_EVENTS_TTL_SEC`` (default ~20m). **Lines:** ``ODDS_API_LIVE_ODDS_TTL_SEC=0`` so each poll re-fetches merged /odds/multi for current live IDs. | **60–120 s** or manual |
| **Tighter live REST** | **5 s** | Same split: slate TTL ≫ poll; line cache off or ≤ poll | unchanged slow |
| **WS phase** | push-driven | minimal REST for repair | slow or on-demand |

**5 s live on REST alone:** **Yes, feasible** if (1) **cache TTL ≥ 5s** so you do not multiply identical calls, (2) **bounded** event IDs per cycle, (3) **no** parallel storm on pregame, (4) **one** asyncio loop owner for Odds client. Sub-second **average** latency on REST-only is **not** realistic; sub-second **after WS** for **price updates** is realistic.

---

## Tuning order (one knob at a time, stay under ~5k/hr)

Change **one** setting, observe logs / rate-limit headers, then the next. **Live slate** (which events exist) and **live lines** (prices for those IDs) use **different** TTLs: the slate can be 15–30 minutes while polls re-merge ``/odds/multi`` every cycle (default ``ODDS_API_LIVE_ODDS_TTL_SEC=0``).

1. **`ODDS_POLL_INTERVAL_SECONDS`** — How often monitors refresh **lines** (merged ``/odds/multi`` for current live event IDs). Set first; use ``[HTTP BUDGET]`` log (once per filter) to sanity-check ~multi/hour.  
2. **`ODDS_API_LIVE_EVENTS_TTL_SEC`** — Cache for **which games are live** (``/events/live``). Default **~20 min** (≫ poll). Lower (e.g. 60–120s) only when you need faster discovery of **new** live games (first pitch).  
3. **`ODDS_API_LIVE_ODDS_TTL_SEC`** — Cache for monitor ``/odds/multi`` merges. Default **0** = fresh books every poll (aligned snapshot via parallel per-book requests + merge).  
4. **`ODDS_API_ODDS_TTL_SEC`** — Other paths (e.g. ``/odds`` single-event, dashboard helpers) — not the monitor live merge when (3) is 0.  
5. **`ODDS_API_EVENTS_TTL_SEC`** — Pregame ``/events`` (by sport).  
6. **`ODDS_API_MAX_REQUESTS_PER_HOUR`** — Client-side soft throttle (default **5000**; set lower on free tiers).  
7. **`ODDS_API_MULTI_PARALLEL_LIMIT`** — Cap concurrent per-book ``/odds/multi`` slices (default 12).

**Pregame:** Leave “Broad scan: include pregame” off until live REST is stable; it adds **one ``/events`` per sport** in ``odds_api_sports_list()`` each cycle (plus TTL).

---

## Auto-bettor and filters

- **Per-filter** payloads already align with BookieBeats-style EV gating (`strict_pass`, min sharps, devig method).  
- **Persisted named filters** (from Dev) reduce friction when testing multiple setups.  
- Auto-bet amounts per filter / EV band / sport / market type should remain **explicitly configured** and default **off** until you trust the feed.

---

## Next steps (ordered)

1. ~~**Port filter disk persistence**~~ — Done: `USER_FILTERS_STATE_FILE` in `dashboard.py` (atomic write; load after defaults).  
2. ~~**Live slate vs live lines**~~ — ``ODDS_API_LIVE_EVENTS_TTL_SEC`` defaults **~20m** (slow slate). ``ODDS_API_LIVE_ODDS_TTL_SEC`` defaults **0** (fresh merged books each poll). Broad scan serialized across filters with a lock.  
3. ~~**“Kalshi line required”**~~ — Done in broad-scan path: after `/odds/multi` merge, events with no Kalshi gameline decimals are dropped from the EV scan (batch HTTP unchanged); `[PIPELINE]` logs drop count.  
4. ~~**Liquidity default sport list**~~ — Done: ``odds_api_sports_list()`` / ``LIQUIDITY_DEFAULT_ODDS_API_SPORTS`` in ``odds_api_client.py``; monitor pregame slugs aligned; pregame broad scan **off** by default.  
5. **Optional:** add `.env.example` (or README) line pointing at this doc’s **Tuning order** section.  
6. **After stable 10–20s + clean logs:** prototype **WS client** module (isolated from Flask thread), subscribe live sports only, feed same EV pipeline.

### WebSocket handoff (next implementation)

- **Push path:** WS delivers price deltas → build the same **merged ``bookmakers`` dict** shape as today’s ``get_odds_multi`` merge, then call the existing EV row builder (no duplicate devig math).  
- **REST path:** keep **occasional** ``/events/live`` (long TTL) for slate repair + **snapshot** ``/odds/multi`` after reconnect.  
- **Hook:** isolate a small ``OddsFeedAdapter`` (REST now, WS later) used only by ``OddsEVMonitor._fetch_alerts_live_broad_scan`` so the monitor loop stays unchanged.

---

## Repo layout reminder

- **Production / work tree:** repo root (`kalshilivenew`).  
- **Reference snapshots:** `Dev/` folder (manual copy of `KalshiLiveNEWDev`); diff with `git diff --no-index Dev\file.py file.py` or your IDE.

---

## One-line success criteria

**REST phase:** No sustained 429; alerts appear when filters say they should; ML/spread/total on tier-A sports; pregame does not starve live.  
**WS phase:** Sub-second **price update** latency for subscribed live events; EV recalculates on each relevant tick; auto-bettor still gated by `strict_pass` and your caps.
