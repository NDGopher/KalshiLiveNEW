"""Odds-API.io live stoppage detection. No BookieBeats. No invented baseball clock.

Timed sports (soccer / NBA / NFL): a live take is allowed only when
``event.clock`` exists and ``clock.running is False``, or ``statusDetail`` is
Halftime/Break. Missing clock is a fail — do not guess.

Baseball is untimed in the Odds-API docs (statusDetail like ``1st inning``).
Do not invent mid-inning or between-innings. The Stoppages Only checkbox
is hidden/disabled for baseball until a real field exists.
"""
from __future__ import annotations

import re
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
_SOCCER_TOKENS = ("soccer",)
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
    if (
        "americanfootball" in blob
        or "american football" in blob
        or "nfl" in blob
        or "ncaaf" in blob
        or "cfb" in blob
    ):
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
    clock = ev.get("clock")
    if isinstance(clock, dict):
        for key in ("statusDetail", "status_detail"):
            raw = clock.get(key)
            if raw is not None and str(raw).strip():
                return str(raw).strip()
    return None


def _as_int(val: Any) -> Optional[int]:
    if val is None or val == "" or isinstance(val, bool):
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def extract_score_home_away(event: Optional[Dict[str, Any]]) -> Tuple[Optional[int], Optional[int]]:
    """(home, away) from scores.home/away first. Never invent a score."""
    if not isinstance(event, dict):
        return None, None
    scores = event.get("scores")
    if isinstance(scores, dict):
        home = _as_int(scores.get("home") if scores.get("home") is not None else scores.get("home_score"))
        away = _as_int(scores.get("away") if scores.get("away") is not None else scores.get("away_score"))
        if home is not None and away is not None:
            return home, away
    score = event.get("score")
    if isinstance(score, dict):
        home = _as_int(score.get("home") if score.get("home") is not None else score.get("home_score"))
        away = _as_int(score.get("away") if score.get("away") is not None else score.get("away_score"))
        if home is not None and away is not None:
            return home, away
    if isinstance(score, str) and "-" in score:
        left, _, right = score.partition("-")
        home, away = _as_int(left), _as_int(right)
        if home is not None and away is not None:
            return home, away
    ss = event.get("ss")
    if isinstance(ss, str) and "-" in ss:
        left, _, right = ss.partition("-")
        home, away = _as_int(left), _as_int(right)
        if home is not None and away is not None:
            return home, away
    home = _as_int(event.get("home_score") if event.get("home_score") is not None else event.get("homeScore"))
    away = _as_int(event.get("away_score") if event.get("away_score") is not None else event.get("awayScore"))
    if home is not None and away is not None:
        return home, away
    return None, None


def _format_clock_mmss(played_seconds: Optional[int], minute: Optional[int]) -> str:
    if played_seconds is not None and played_seconds >= 0:
        return f"{played_seconds // 60}:{played_seconds % 60:02d}"
    if minute is not None and minute >= 0:
        return f"{minute}:00"
    return ""


def _soccer_minute_label(clock: Dict[str, Any]) -> str:
    minute = _as_int(clock.get("minute"))
    played = _as_int(
        clock.get("playedSeconds") if clock.get("playedSeconds") is not None else clock.get("played_seconds")
    )
    injury = _as_int(
        clock.get("injuryTime") if clock.get("injuryTime") is not None else clock.get("injury_time")
    )
    if minute is None and played is not None:
        minute = played // 60
    if minute is None:
        return ""
    if injury and injury > 0:
        return f"{minute}'+{injury}"
    return f"{minute}'"


def _is_soccer_blob(blob: str) -> bool:
    if any(tok in blob for tok in _SOCCER_TOKENS):
        return True
    return "football" in blob and "american" not in blob


def _period_label(event: Dict[str, Any], clock: Dict[str, Any], blob: str) -> str:
    period_raw = clock.get("period")
    if period_raw is None:
        period_raw = event.get("period")
    period_s = str(period_raw).strip() if period_raw is not None else ""
    if not period_s or period_s.lower() in ("none", "null"):
        return ""
    if period_s.isdigit():
        n = int(period_s)
        if "basketball" in blob or "nba" in blob:
            return f"Q{n}"
        if (
            "americanfootball" in blob
            or "american football" in blob
            or "nfl" in blob
            or "ncaaf" in blob
            or "cfb" in blob
        ):
            return f"Q{n}"
        if _is_soccer_blob(blob):
            if n <= 1:
                return "1st half"
            if n == 2:
                return "2nd half"
            return f"P{n}"
        return f"P{n}"
    return period_s


def _mlb_inning_from_status(status_detail: str) -> str:
    """Display-only inning from statusDetail. Never invent; never a stoppage."""
    if not status_detail or "inning" not in status_detail.lower():
        return ""
    m = re.search(r"(\d+)(?:st|nd|rd|th)?\s+inning", status_detail.lower())
    if not m:
        return ""
    n = int(m.group(1))
    if 11 <= n % 100 <= 13:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def format_game_status_line(event: Optional[Dict[str, Any]]) -> str:
    """Compact card line: '14-11 · Q3 4:12 · STOPPED'. Empty if no score/clock."""
    if not isinstance(event, dict):
        return ""
    blob = _blob(event)
    home, away = extract_score_home_away(event)
    clock = event.get("clock") if isinstance(event.get("clock"), dict) else {}
    running = clock_running_flag(clock)
    if running is None and event.get("clock_running") is not None:
        cr = event.get("clock_running")
        if cr is True or cr is False:
            running = cr
    status_detail = extract_status_detail(event) or ""

    parts: list[str] = []
    if home is not None and away is not None:
        parts.append(f"{home}-{away}")

    if is_baseball_event(event):
        inning = _mlb_inning_from_status(status_detail)
        if inning:
            parts.append(inning)
        return " · ".join(parts)

    if _is_soccer_blob(blob):
        minute_lab = _soccer_minute_label(clock)
        period_lab = _period_label(event, clock, blob)
        if minute_lab:
            parts.append(minute_lab)
        if period_lab:
            parts.append(period_lab)
        elif status_detail and status_detail.lower() not in ("in progress", "live"):
            parts.append(status_detail)
        if running is False:
            parts.append("STOPPED")
        return " · ".join(parts)

    period_lab = _period_label(event, clock, blob)
    time_raw = clock.get("time")
    if isinstance(time_raw, str) and time_raw.strip() and ":" in time_raw:
        clock_lab = time_raw.strip()
    else:
        played = _as_int(
            clock.get("playedSeconds") if clock.get("playedSeconds") is not None else clock.get("played_seconds")
        )
        minute = _as_int(clock.get("minute"))
        clock_lab = _format_clock_mmss(played, minute)
    clock_chunk = " ".join(p for p in (period_lab, clock_lab) if p)
    if clock_chunk:
        parts.append(clock_chunk)
    elif status_detail and status_detail.lower() not in ("in progress", "live"):
        parts.append(status_detail)
    if is_timed_sport_event(event) and running is False:
        parts.append("STOPPED")
    return " · ".join(parts)


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
            "scores",
            "score",
            "ss",
            "home_score",
            "away_score",
            "homeScore",
            "awayScore",
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
    """Fields ``/api/live_odds`` and alert cards forward. Never invent a baseball clock."""
    view = merge_event_clock_fields(*sources)
    clock_obj = extract_clock(view)
    home, away = extract_score_home_away(view)
    scores = view.get("scores") if isinstance(view.get("scores"), dict) else None
    score_raw = view.get("score")
    if isinstance(score_raw, str) and score_raw.strip():
        score_out = score_raw.strip().replace("–", "-")
    elif home is not None and away is not None:
        score_out = f"{home}-{away}"
    else:
        score_out = ""
    return {
        "clock": clock_obj,
        "clock_running": clock_running_flag(clock_obj),
        "statusDetail": extract_status_detail(view),
        "scores": scores,
        "score": score_out,
        "game_status": format_game_status_line(view),
    }
