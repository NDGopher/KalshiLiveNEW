"""
One-shot: scan Odds-API /events/live + /odds/multi for a Kalshi total similar to a PTO-style play
(e.g. NBA Under ~211.5 with long Kalshi american). Requires ODDS_API_KEY in .env.

Usage:
  py -3 tools/scan_live_kalshi_play.py
  py -3 tools/scan_live_kalshi_play.py --teams "Magic,76ers"
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True, encoding="utf-8-sig")
load_dotenv(Path.cwd() / ".env", override=True, encoding="utf-8-sig")
load_dotenv(ROOT / ".env.env", override=False, encoding="utf-8-sig")
load_dotenv(Path.cwd() / ".env.env", override=False, encoding="utf-8-sig")


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _american_from_dec(d: float) -> int:
    if d <= 1.0:
        return 0
    if d >= 2.0:
        return int(round((d - 1.0) * 100))
    return int(round(-100 / (d - 1.0)))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--teams",
        default="",
        help="Comma-separated substrings to match on home/away (e.g. Magic,76ers)",
    )
    args = ap.parse_args()
    team_parts = [x.strip() for x in args.teams.split(",") if x.strip()]

    from odds_api_client import (
        get_shared_odds_client,
        odds_api_master_bookmakers,
        reset_shared_odds_client,
        _canonical_odds_api_bookmaker,
    )
    from ev_calculator import EVCalculator, _fair_prob_power_relaxed_two_way

    await reset_shared_odds_client()
    client = await get_shared_odds_client()
    if not (client.api_key or "").strip():
        print("[scan] ODDS_API_KEY missing — cannot call API.")
        sys.exit(2)

    books = odds_api_master_bookmakers()
    liv = await client.list_live_events(None)
    if not isinstance(liv, list) or not liv:
        print("[scan] No live events returned.")
        await client.close()
        return

    picks: list[dict] = []
    for ev in liv:
        home = str(ev.get("home") or "")
        away = str(ev.get("away") or "")
        blob = f"{home} {away}"
        if team_parts:
            if not all(p.lower() in blob.lower() for p in team_parts):
                continue
        sp = ev.get("sport")
        if isinstance(sp, dict):
            slug = str(sp.get("slug") or "")
        else:
            slug = str(sp or "")
        if "basket" not in slug.lower():
            continue
        eid = ev.get("id")
        if eid is None:
            continue
        picks.append({"id": int(eid), "home": home, "away": away, "slug": slug})

    if not picks:
        print(f"[scan] No live basketball events matching teams={team_parts!r}.")
        print(f"[scan] Live count={len(liv)}; sample: ", end="")
        for ev in liv[:5]:
            print(f"  id={ev.get('id')} {ev.get('away')} @ {ev.get('home')}", end="")
        print()
        await client.close()
        return

    def _float_dec(x) -> Optional[float]:
        try:
            if x is None:
                return None
            return float(str(x).strip())
        except (TypeError, ValueError):
            return None

    def _blocks_for_book(bks: dict, name: str):
        want = _canonical_odds_api_bookmaker(name).lower()
        for k, v in bks.items():
            if str(k).strip().lower() == want:
                return v
        return bks.get(name)

    calc = EVCalculator({})
    found = False
    for row in picks[:20]:
        eid = row["id"]
        multi = await client.get_odds_multi([eid], books, odds_cache_ttl=0.0)
        if not multi:
            print(f"[scan] id={eid} no multi payload")
            continue
        doc = multi[0]
        bks = doc.get("bookmakers") or {}
        if not isinstance(bks, dict):
            continue
        kal = bks.get("Kalshi") or []
        sharp_names = [b for b in books if _norm(b) != "kalshi"]
        for mk in kal:
            mname = str(mk.get("name") or "")
            u = mname.upper()
            if "PLAYER" in u:
                continue
            if "TOTAL" not in u and not ("OVER" in u and "UNDER" in u):
                continue
            krows = mk.get("odds") or []
            if not krows or not isinstance(krows, list):
                continue
            kr0 = krows[0] if isinstance(krows[0], dict) else {}
            ko = kr0.get("over")
            ku = kr0.get("under")
            if ko is None or ku is None:
                continue
            d_o = _float_dec(ko)
            d_u = _float_dec(ku)
            if not d_o or not d_u or d_o <= 1.0 or d_u <= 1.0:
                continue
            k_am_u = _american_from_dec(d_u)
            if k_am_u < 250:
                continue
            panels = []
            for sn in sharp_names:
                blocks = _blocks_for_book(bks, sn)
                if not blocks:
                    continue
                if not isinstance(blocks, list):
                    continue
                om = None
                for cand in blocks:
                    if str(cand.get("name") or "").strip() == mname.strip():
                        om = cand
                        break
                if not om:
                    continue
                orows = om.get("odds") or []
                if not orows or not isinstance(orows[0], dict):
                    continue
                r0 = orows[0]
                so = r0.get("over")
                su = r0.get("under")
                bo = _float_dec(so)
                bu = _float_dec(su)
                if bo and bu and bo > 1.0 and bu > 1.0:
                    panels.append((bo, bu, sn))
            if len(panels) < 3:
                continue
            # Under side: first arg = under decimal, second = over (two-way pick = under).
            pick_probs = [_fair_prob_power_relaxed_two_way(calc, bu, bo) for bo, bu, _ in panels]
            fair_p = sum(pick_probs) / len(pick_probs)
            price_cents = int(max(1, min(99, round(100.0 / d_u))))
            evp = calc.ev_percent_vs_kalshi(fair_p, price_cents)
            fair_am = _american_from_dec(1.0 / fair_p)
            print(
                f"[scan] MATCH id={eid} | {row['away']} @ {row['home']} | market={mname!r}\n"
                f"       Kalshi under dec={d_u:.3f} (~{k_am_u}) | fair~+{fair_am} | EV%~{evp:.2f} | sharp_panels={len(panels)}"
            )
            found = True
    if not found:
        print(
            f"[scan] No Kalshi total (under >= +250) with 3+ sharp books in first {min(20, len(picks))} "
            f"basketball live events scanned."
        )
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
