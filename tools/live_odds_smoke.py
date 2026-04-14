"""One-off: confirm Odds-API value-bets + /odds/multi have lines for live MLB/NHL (uses .env)."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()


def _is_live(ev: dict) -> bool:
    if ev.get("live") is True or ev.get("isLive") is True:
        return True
    st = str(ev.get("status", "") or ev.get("state", "") or "").lower().replace(" ", "")
    return st in ("live", "inprogress", "inplay", "started", "running")


def _league_u(ev: dict) -> str:
    lg = ev.get("league")
    if isinstance(lg, dict):
        return str(lg.get("name") or lg.get("slug") or "").upper()
    return str(lg or "").upper()


async def main() -> None:
    from odds_api_client import get_shared_odds_client, _norm_book

    key = (os.getenv("ODDS_API_KEY") or "").strip()
    if not key:
        print("No ODDS_API_KEY in environment")
        return

    books = [x.strip() for x in os.getenv("ODDS_API_BOOKMAKERS", "Kalshi,FanDuel").split(",") if x.strip()]
    norm_books = []
    seen = set()
    for b in books:
        nb = _norm_book(b)
        if nb.lower() not in seen:
            seen.add(nb.lower())
            norm_books.append(nb)

    client = await get_shared_odds_client()
    vb = await client.get_value_bets("Kalshi", True)
    mlb_ids: list[int] = []
    nhl_ids: list[int] = []
    for row in vb:
        ev = row.get("event") or {}
        if not _is_live(ev):
            continue
        lg = _league_u(ev)
        eid = row.get("eventId")
        if eid is None:
            continue
        try:
            eid_i = int(eid)
        except (TypeError, ValueError):
            continue
        if "MLB" in lg or "MAJOR LEAGUE" in lg:
            if eid_i not in mlb_ids:
                mlb_ids.append(eid_i)
        if "NHL" in lg:
            if eid_i not in nhl_ids:
                nhl_ids.append(eid_i)

    print(f"ODDS_API_BOOKMAKERS ({len(norm_books)}): {', '.join(norm_books)}")
    print(f"Live-ish value-bet events: MLB ids={mlb_ids[:5]}, NHL ids={nhl_ids[:5]}")

    async def check(label: str, eids: list[int]) -> None:
        if not eids:
            print(f"{label}: no event ids from value-bets in this snapshot")
            return
        eid = eids[0]
        multi = await client.get_odds_multi([eid], norm_books)
        doc = multi[0] if multi else {}
        bks = (doc or {}).get("bookmakers") or {}
        print(f"{label} eventId={eid}: bookmaker keys in /odds/multi = {list(bks.keys())}")
        nonempty = [k for k, v in bks.items() if isinstance(v, list) and len(v) > 0]
        print(f"  non-empty market lists: {len(nonempty)}/{len(bks)}")

    await check("MLB", mlb_ids)
    await check("NHL", nhl_ids)
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
