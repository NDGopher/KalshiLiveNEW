"""Connect to Pandora and log whether MLB prices arrive. No BookieBeats."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plive_pandora import PlivePandoraFeed, handshake_emits


async def main() -> int:
    emits = handshake_emits()
    print(f"[PLIVE] probe handshake={emits[0][1]} sport=1 baseball")
    feed = PlivePandoraFeed()
    await feed.start()
    deadline = asyncio.get_event_loop().time() + 25.0
    last = 0
    while asyncio.get_event_loop().time() < deadline:
        snap = feed.status_snapshot()
        if snap.get("events_received") != last:
            feed.log_status(prefix="[PLIVE] probe")
            last = snap.get("events_received") or 0
        if snap.get("receiving_prices"):
            feed.log_status(prefix="[PLIVE] PROOF")
            await feed.stop()
            print("[PLIVE] PROOF ok: connected and receiving events with prices")
            return 0
        await asyncio.sleep(0.5)
    feed.log_status(prefix="[PLIVE] probe timeout")
    await feed.stop()
    return 1 if not feed.store.mlb_events() else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
