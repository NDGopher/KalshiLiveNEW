"""Odds-API.io live stoppage detection. No BookieBeats. No invented baseball clock.

Timed sports (soccer / NBA / NFL): a live take is allowed only when
``event.clock`` exists and ``clock.running is False``, or ``statusDetail`` is
Halftime/Break. Missing clock is a fail — do not guess.

Baseball is untimed in the Odds-API docs (statusDetail like ``1st inning``).
Do not invent mid-inning or between-innings. The Stoppages Only checkbox
is hidden/disabled for baseball until a real field exists.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# Soccer / NBA / NFL only. Do not treat hockey, tennis, or baseball as timed.
_TIMED_SPORT_TOKENS = (
    "soccer",
    "football",  # Odds-API soccer slug is often "football"
    "nba",
    "basketball",
    "nfl",
    "americanfootball",
    "american football",
)
_BASEBALL_TOKENS = ("baseball", "mlb", "usa-mlb")
_BREAK_DETAILS = frozenset(
    {
        "halftime",
        "half time",
        "half-time",
        "ht",
        "break",
        "half",
    }
)


def _blob(event: Optional[Dict[str, Any]]) -> str:
    ev = event or {}
    parts: list = []
    for key in ("sport", "sport_slug", "league", "league_slug"):
        val = ev.get(key)
        if isinstance(val, dict):
            parts.extend(str(val.get(x) or "") for x in ("name", "slug", "sport"))
        else:
            parts.append(str(val or ""))
    return " ".join(parts).lower()


def is_baseball_event(event: Optional[Dict[str, Any]]) -> bool:
    blob = _blob(event)
    return any(tok in blob for tok in _BASEBALL_TOKENS)


def is_timed_sport_event(event: Optional[Dict[str, Any]]) -> bool:
    """Soccer / NBA / NFL. American football counts; soccer 'football' does too
    unless the blob is clearly baseball."""
    if is_baseball_event(event):
        return False
    blob = _blob(event)
    if "americanfootball" in blob or "american football" in blob or "nfl" in blob:
        return True
    if "nba" in blob or "basketball" in blob:
        return True
    if "soccer" in blob:
        return True
    # Odds-API soccer is often sport=football without "soccer".
    if "football" in blob and "american" not in blob:
        return True
    return False


def extract_clock(event: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    ev = event or {}
    clock = ev.get("clock")
    return clock if isinstance(clock, dict) else None


def extract_status_detail(event: Optional[Dict[str, Any]]) -> Optional[str]:
    ev = event or {}
    for key in ("statusDetail", "status_detail", "statusdetail"):
        raw = ev.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return None


def clock_running_flag(clock: Optional[Dict[str, Any]]) -> Optional[bool]:
    """True/False when ``running`` is present. None if clock or the flag is omitted."""
    if not isinstance(clock, dict) or "running" not in clock:
        return None
    val = clock.get("running")
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)) and val in (0, 1):
        return bool(val)
    s = str(val).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return None


def status_detail_is_break(detail: Optional[str]) -> bool:
    if not detail:
        return False
    norm = " ".join(str(detail).strip().lower().replace("_", " ").split())
    if norm in _BREAK_DETAILS:
        return True
    # "Halftime 2" / "Break — 1st" still count; innings never do.
    if "inning" in norm:
        return False
    return norm.startswith("halftime") or norm.startswith("half time") or norm == "break"


def merge_event_clock_fields(*sources: Any) -> Dict[str, Any]:
    """First non-empty clock / statusDetail / sport / league / status wins (last source overrides empties)."""
    out: Dict[str, Any] = {}
    for src in sources:
        if not isinstance(src, dict):
            continue
        for key in (
            "sport",
            "sport_slug",
            "league",
            "league_slug",
            "status",
            "state",
            "live",
            "isLive",
            "clock",
            "statusDetail",
            "status_detail",
            "home",
            "away",
            "id",
        ):
            val = src.get(key)
            if val is not None and val != "":
                out[key] = val
    return out


def stoppage_allows_live_take(event: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """Return (allowed, reason). Does not consult BookieBeats.

    Baseball / unknown sports fail closed (no real stoppage field).
    Timed sports require a stopped clock or Halftime/Break. Omitted clock fails.
    """
    ev = event or {}
    if is_baseball_event(ev):
        return False, "baseball_no_clock"
    if not is_timed_sport_event(ev):
        return False, "untimed_or_unknown_sport"
    detail = extract_status_detail(ev)
    if status_detail_is_break(detail):
        return True, "statusDetail_break"
    clock = extract_clock(ev)
    running = clock_running_flag(clock)
    if running is False:
        return True, "clock_stopped"
    if running is True:
        return False, "clock_running"
    return False, "clock_omitted"


def live_take_blocked_by_stoppage(
    event: Optional[Dict[str, Any]],
    *,
    enabled: bool,
) -> Optional[str]:
    """None if the row may proceed. Otherwise a short drop reason."""
    if not enabled:
        return None
    allowed, reason = stoppage_allows_live_take(event)
    return None if allowed else reason


def clock_fields_for_live_odds(*sources: Any) -> Dict[str, Any]:
    """Fields ``/api/live_odds`` forwards. Never invent a baseball clock."""
    view = merge_event_clock_fields(*sources)
    clock_obj = extract_clock(view)
    return {
        "clock": clock_obj,
        "clock_running": clock_running_flag(clock_obj),
        "statusDetail": extract_status_detail(view),
    }
