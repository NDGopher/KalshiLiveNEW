#!/usr/bin/env python3
"""Live Odds-API book coverage census.

Requires ODDS_API_KEY in the environment (Cloud Agent secret or local .env).
Does not print the key.

Usage:
  python tools/diag_odds_book_coverage.py
  python tools/diag_odds_book_coverage.py --seconds 45
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True, encoding="utf-8-sig")
load_dotenv(Path.cwd() / ".env", override=True, encoding="utf-8-sig")


def _key() -> str:
    return (os.getenv("ODDS_API_KEY") or os.getenv("ODDS_API_IO_KEY") or "").strip()


def _book_has_markets(markets: Any) -> bool:
    return isinstance(markets, list) and any(isinstance(m, dict) for m in markets)


def _match_book(bks: Dict[str, Any], name: str) -> Any:
    want = name.lower()
    for k, v in bks.items():
        kl = str(k).lower()
        if kl == want or want in kl or kl in want:
            return v
    return None


def _census(docs: List[Dict[str, Any]], master: List[str]) -> None:
    per_book = Counter()
    per_book_markets: Dict[str, Counter] = defaultdict(Counter)
    empty_events = 0
    for doc in docs:
        bks = doc.get("bookmakers") or {}
        if not isinstance(bks, dict) or not bks:
            empty_events += 1
            continue
        for name in master:
            mk = _match_book(bks, name)
            if mk is None:
                continue
            markets = mk if isinstance(mk, list) else (mk.get("markets") if isinstance(mk, dict) else None)
            if not _book_has_markets(markets):
                continue
            per_book[name] += 1
            for m in markets:
                if not isinstance(m, dict):
                    continue
                mid = str(m.get("name") or m.get("id") or "?")
                per_book_markets[name][mid] += 1
    n = len(docs) or 1
    print(f"events={len(docs)} empty_bookmakers={empty_events}")
    for name in master:
        hit = per_book.get(name, 0)
        pct = 100.0 * hit / n
        top = ", ".join(f"{m}={c}" for m, c in per_book_markets[name].most_common(4)) or "—"
        print(f"  {name:18s} events_with_markets={hit:3d}/{len(docs)} ({pct:5.1f}%)  top={top}")


async def _run(seconds: float) -> int:
    key = _key()
    if not key:
        print("ODDS_API_KEY missing — add it as a Cloud Agent secret (or local .env) and rerun.")
        print("Expected secret name: ODDS_API_KEY (optional alias: ODDS_API_IO_KEY)")
        return 2

    # Default WS on for this census unless explicitly disabled.
    if "ODDS_API_WS" not in os.environ:
        os.environ["ODDS_API_WS"] = "true"

    from odds_api_client import OddsAPIClient, odds_api_master_bookmakers
    from odds_api_ws import OddsApiWsFeed, odds_api_ws_wanted

    master = list(odds_api_master_bookmakers())
    print(f"master_books ({len(master)}): {', '.join(master)}")
    print(f"ODDS_API_WS wanted={odds_api_ws_wanted()} key_prefix={key[:6]}… len={len(key)}")

    rest = OddsAPIClient(api_key=key)
    try:
        selected = await rest.get_selected_bookmakers()
    except Exception as exc:
        selected = []
        print(f"selected bookmakers fetch failed: {exc}")
    if selected:
        print(f"account selected ({len(selected)}): {', '.join(selected)}")
        sel_l = {x.lower() for x in selected}
        missing = [b for b in master if b.lower() not in sel_l]
        master_l = {x.lower() for x in master}
        extra = [b for b in selected if b.lower() not in master_l]
        if missing:
            print(f"MISSING from account selection vs master: {missing}")
        if extra:
            print(f"EXTRA on account vs master: {extra}")
    else:
        print("account selected: (empty or unavailable)")

    if not odds_api_ws_wanted():
        print("WS disabled (ODDS_API_WS=false) — enable WS for live census.")
        await rest.close()
        return 1

    feed = OddsApiWsFeed(rest_client=rest, api_key=key)
    await feed.maybe_select_books()
    await feed.start()
    print(f"WS starting; sampling for {seconds:.0f}s…")
    t0 = time.time()
    last_report = 0.0
    saw_welcome = False
    while time.time() - t0 < seconds:
        await asyncio.sleep(2.0)
        welcome = feed.store.welcome
        if welcome and not saw_welcome:
            saw_welcome = True
            wb = welcome.get("bookmakers") if isinstance(welcome, dict) else None
            if isinstance(wb, list):
                print(f"welcome.bookmakers ({len(wb)}): {', '.join(str(x) for x in wb)}")
            else:
                print(f"welcome keys: {sorted(welcome.keys()) if isinstance(welcome, dict) else type(welcome)}")
        docs = feed.store.merged_docs()
        if time.time() - last_report < 8 and docs:
            continue
        last_report = time.time()
        print(
            f"t+{time.time() - t0:.0f}s connected={feed.connected} welcome_ok={feed.welcome_ok} "
            f"healthy={feed.healthy} last_error={feed.last_error!r} store_books={len(feed.store.books)}"
        )
        if docs:
            _census(docs, master)
        else:
            print("  (no merged docs yet — waiting for handoff/welcome)")

    docs = feed.store.merged_docs()
    print("--- final ---")
    print(
        f"connected={feed.connected} welcome_ok={feed.welcome_ok} healthy={feed.healthy} "
        f"last_close={feed.last_close_code} last_error={feed.last_error!r}"
    )
    if docs:
        _census(docs, master)
    else:
        print("No odds docs accumulated — WS never became healthy or slate empty.")

    await feed.stop()
    await rest.close()
    print("done")
    return 0 if docs else 1


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--seconds", type=float, default=40.0)
    args = p.parse_args()
    raise SystemExit(asyncio.run(_run(args.seconds)))


if __name__ == "__main__":
    main()
