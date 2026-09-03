"""Connect to Pandora and log whether MLB or soccer prices arrive. No BookieBeats."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plive_pandora import PlivePandoraFeed, handshake_emits


async def main() -> int:
    emits = handshake_emits()
    print(f"[PLIVE] probe handshake={emits[0][1]} sports=1,5,220")
    feed = PlivePandoraFeed()
    await feed.start()
    deadline = asyncio.get_event_loop().time() + 25.0
    last = 0
    while asyncio.get_event_loop().time() < deadline:
        snap = feed.status_snapshot()
        if snap.get("events_received") != last:
            feed.log_status(prefix="[PLIVE] probe")
            last = snap.get("events_received") or 0
        # Empty MLB is not a miss. Soccer coeffs / mapped prices count.
        if snap.get("receiving_prices") or snap.get("receiving_coeffs"):
            feed.log_status(prefix="[PLIVE] PROOF")
            await feed.stop()
            print(
                "[PLIVE] PROOF ok: connected and receiving events with prices "
                f"(mlb_priced={snap.get('mlb_with_prices') or 0} "
                f"soccer_priced={snap.get('soccer_with_prices') or 0} "
                f"soccer_coeffs={snap.get('soccer_with_coeffs') or 0})"
            )
            return 0
        await asyncio.sleep(0.5)
    feed.log_status(prefix="[PLIVE] probe timeout")
    await feed.stop()
    has_slate = bool(feed.store.mlb_events() or feed.store.soccer_events())
    return 1 if not has_slate else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
