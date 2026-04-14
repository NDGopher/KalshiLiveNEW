# CBB Filter: Why No Alerts on Dashboard?

You're seeing Kalshi 3 Sports alerts but **zero** from the CBB filter, even though the dashboard shows 0% EV min (so any CBB alert should appear). Here’s how the pipeline works and what to check.

---

## How an alert gets to the dashboard

1. **BookieBeats API** – The CBB monitor polls with the CBB filter payload (`leagues: ["NCAAB"]`, `minRoi=10`, `devig.minEv=10`, `minSharpBooks=2`, etc.). Only bets that pass **all** of that are returned.
2. **Link / ticker** – Each bet has a `link`. We need a **Kalshi** link so we can extract the event ticker (e.g. `KXNCAAMBGAME-26JAN04ORSTWSU`). If `link` is missing or not a Kalshi URL, we skip the alert and never show it.
3. **Matching** – We call `find_submarket(event_ticker, market_type, line, selection)` and, if needed, `match_alert_to_kalshi(alert)`. If we can’t find a Kalshi submarket, we emit `alert_match_failed` and don’t show the alert on the dashboard (the UI doesn’t currently display match failures).

So “no CBB on dashboard” can be:

- No CBB alerts from BookieBeats, or  
- CBB alerts with no Kalshi link, or  
- CBB alerts we can’t match to a Kalshi submarket.

---

## Likely causes (in order)

### 1. BookieBeats returned no NCAAB alerts

The CBB filter is strict:

- **minRoi=10**, **devig.minEv=10**, **minSharpBooks=2**
- Excluded: quarter/half markets
- **oddsRanges**: -200 to 200
- **minLimits** / **minSharpLimits** per book

If last night no NCAAB games had 2+ sharp books with 10%+ EV and met all of that, the API would return **zero** CBB rows. Then we’d never call `handle_new_alert` for CBB and you’d see nothing from that filter.

**Check:** On BookieBeats (or their UI), do you see NCAAB + Kalshi with your CBB filter when you expect games? If they show nothing, our bot will show nothing too.

---

### 2. CBB alerts have no Kalshi link

We only use alerts where we can get a ticker from the link:

- `event_ticker = alert.ticker or alert.extract_ticker_from_link(link)`
- If the link is empty or not a Kalshi URL, we skip and **don’t** show the alert.

**Check logs for:**

- `[CBB] ⚠️ Alert skipped: no Kalshi ticker in link ...`  
  → BookieBeats is sending CBB alerts but the link isn’t a Kalshi link (or is missing). You’ll see `teams`, `pick`, and the link (or lack of it).

If you see that line, the fix is on the BookieBeats side (link type / book selection) or we’d need a different way to resolve NCAAB → Kalshi (no support for that today).

---

### 3. Matching fails (event or submarket)

If we have a Kalshi ticker but can’t find the submarket:

- You’ll see **`[NCAAB] WARNING: Could not find submarket for college basketball:`** and event/type/line/selection.
- Or **`❌ [NCAAB] Fallback matching also failed for: ...`**

Then we emit `alert_match_failed`; the dashboard doesn’t show these.

**Check logs for:**

- `⚠️  [NCAAB] WARNING: Could not find submarket`
- `❌ [NCAAB] Fallback matching also failed`

If those appear, BookieBeats **is** sending CBB and we have a link, but we’re failing to match (event format, line/selection, or Kalshi series naming). Fix would be in `find_submarket` / `match_alert_to_kalshi` or Kalshi event structure for NCAAB.

---

### 4. CBB filter not selected for dashboard

If “CBB EV Filter (Live - Kalshi)” is **unchecked** for the dashboard, we skip all alerts from that filter and don’t emit them.

**Check:** Dashboard filter checkboxes – ensure CBB is selected for “dashboard” (and for “auto-bettor” if you want auto-bets).

---

## Quick diagnostic checklist

1. **Logs**
   - `[HANDLE ALERT] 🏀 NCAAB ALERT DETECTED` → We’re getting CBB alerts **and** a Kalshi ticker; next step is matching.
   - `[CBB] ⚠️ Alert skipped: no Kalshi ticker in link` → CBB alerts exist but link isn’t Kalshi (or missing).
   - `[NCAAB] WARNING: Could not find submarket` / `❌ [NCAAB] Fallback matching also failed` → Matching failed.
   - None of the above → BookieBeats likely returned no CBB alerts (filter too strict or no qualifying games).

2. **Filter**
   - Confirm CBB filter is selected for dashboard (and auto-bettor if desired).

3. **BookieBeats**
   - Confirm they show NCAAB + Kalshi with your CBB filter when games are on.

4. **Relax CBB filter (test only)**
   - Temporarily lower `minRoi` / `devig.minEv` (e.g. to 0) in the CBB payload to see if **any** CBB alerts start appearing. If they do, the issue is “no qualifying alerts” rather than link/matching.

---

## Summary

- **No CBB on dashboard** = either no CBB alerts from BookieBeats, or we skip them (no Kalshi link), or we can’t match them (no submarket).
- Use the log lines above to see which step fails; the new `[CBB] ⚠️ Alert skipped: no Kalshi ticker in link` line makes “no Kalshi link” easy to spot.
