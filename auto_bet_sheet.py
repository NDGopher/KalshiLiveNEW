"""Auto-Bets sheet / CSV record shape.

The first 23 columns stay in the grade_bets.py / generate_summary.py order.
New fields are appended only. Auto-bet switch stays OFF.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

# Existing 23 sheet headers — do not reorder or rename.
AUTO_BET_SHEET_BASE_HEADERS: List[str] = [
    "Timestamp",
    "Order ID",
    "Ticker",
    "Side",
    "Teams",
    "Market Type",
    "Pick",
    "Qualifier",
    "EV %",
    "Expected Price (¢)",
    "Executed Price (¢)",
    "American Odds",
    "Contracts",
    "Cost ($)",
    "Payout ($)",
    "Win Amount ($)",
    "Sport",
    "Status",
    "Result",
    "PNL ($)",
    "Settled",
    "Filter Name",
    "Devig Books",
]

AUTO_BET_SHEET_EXTRA_HEADERS: List[str] = [
    "take_book",
    "power_books",
    "sharp_count",
    "line",
    "clock_running",
    "status_detail",
    "score",
    "live",
    "skip_reason",
]

AUTO_BET_SHEET_HEADERS: List[str] = AUTO_BET_SHEET_BASE_HEADERS + AUTO_BET_SHEET_EXTRA_HEADERS

AUTO_BET_CSV_BASE_FIELDS: List[str] = [
    "timestamp",
    "order_id",
    "ticker",
    "side",
    "teams",
    "market_type",
    "pick",
    "qualifier",
    "ev_percent",
    "expected_price_cents",
    "executed_price_cents",
    "american_odds",
    "contracts",
    "cost",
    "payout",
    "win_amount",
    "sport",
    "status",
    "result",
    "pnl",
    "settled",
    "filter_name",
    "devig_books",
]

AUTO_BET_CSV_EXTRA_FIELDS: List[str] = [
    "take_book",
    "power_books",
    "sharp_count",
    "line",
    "clock_running",
    "status_detail",
    "score",
    "live",
    "skip_reason",
]

AUTO_BET_CSV_FIELDNAMES: List[str] = AUTO_BET_CSV_BASE_FIELDS + AUTO_BET_CSV_EXTRA_FIELDS

# Display aliases for the named Devig Books list. BetMGM stays in the pack as MGM.
_SHEET_BOOK_ALIAS = {
    "draftkings": "DK",
    "fanduel": "FD",
    "betmgm": "MGM",
    "mgm": "MGM",
    "betfair exchange": "BF",
    "betfair": "BF",
    "caesars": "CZ",
    "novig": "NoVig",
    "bet365": "Bet365",
    "polymarket": "Poly",
    "kalshi": "Kalshi",
    "plive": "PLive",
}


def _book_key(name: Any) -> str:
    return "".join(ch.lower() for ch in str(name or "") if ch.isalnum())


def _sheet_book_label(name: Any) -> str:
    raw = str(name or "").strip()
    if not raw:
        return ""
    alias = _SHEET_BOOK_ALIAS.get(raw.lower())
    if alias:
        return alias
    compact = "".join(ch.lower() for ch in raw if ch.isalnum())
    for key, label in _SHEET_BOOK_ALIAS.items():
        if "".join(ch for ch in key if ch.isalnum()) == compact:
            return label
    return raw


def _fmt_sheet_american(odds: Any) -> str:
    if odds is None or odds == "":
        return ""
    try:
        n = int(round(float(str(odds).replace("+", "").strip())))
    except (TypeError, ValueError):
        return str(odds)
    return f"+{n}" if n > 0 else str(n)


def _sheet_bool(value: Any) -> str:
    if value is True or value in ("true", "True", "TRUE", 1, "1"):
        return "true"
    if value is False or value in ("false", "False", "FALSE", 0, "0"):
        return "false"
    return ""


def _is_moneyline_market(market_type: Any) -> bool:
    mt = str(market_type or "").strip().lower().replace(" ", "")
    return mt in ("ml", "moneyline", "money") or mt.startswith("moneyline")


def sheet_line_value(market_type: Any, line: Any = None, qualifier: Any = None) -> str:
    """Spread/total strike. Empty on ML."""
    if _is_moneyline_market(market_type):
        return ""
    if line is not None and str(line).strip() not in ("", "None"):
        return str(line).strip()
    q = str(qualifier or "").strip()
    return q


def format_event_score(event: Optional[Dict[str, Any]]) -> str:
    """Home-away score if present. Blank otherwise."""
    ev = event if isinstance(event, dict) else {}
    direct = ev.get("score")
    if direct is not None and str(direct).strip():
        text = str(direct).strip()
        if "-" in text or "–" in text:
            return text.replace("–", "-")
    scores = ev.get("scores") if ev.get("scores") is not None else ev.get("ss")
    if isinstance(scores, str) and scores.strip():
        return scores.strip().replace("–", "-")
    if isinstance(scores, dict):
        h, a = scores.get("home"), scores.get("away")
        if h is not None and a is not None:
            return f"{h}-{a}"
    if isinstance(scores, (list, tuple)) and len(scores) >= 2:
        return f"{scores[0]}-{scores[1]}"
    h = ev.get("home_score")
    if h is None:
        h = ev.get("homeScore")
    a = ev.get("away_score")
    if a is None:
        a = ev.get("awayScore")
    if h is not None and a is not None:
        return f"{h}-{a}"
    return ""


def live_context_from_event(event: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Clock / live / score from an odds event. Does not invent baseball clock.running."""
    ev = event if isinstance(event, dict) else {}
    clock = ev.get("clock")
    running = None
    if isinstance(clock, dict) and "running" in clock:
        running = bool(clock.get("running"))
    elif ev.get("clock_running") is not None:
        cr = ev.get("clock_running")
        if cr is True or cr is False:
            running = cr
        elif cr in ("true", "false", "True", "False"):
            running = str(cr).lower() == "true"
    live = ev.get("live")
    if live is None:
        live = ev.get("isLive")
    return {
        "live": live,
        "clock": clock,
        "clock_running": running,
        "status_detail": ev.get("statusDetail") or ev.get("status_detail") or "",
        "score": format_event_score(ev),
    }


def _display_rows_for_pick(display_books: Any, pick: Any) -> List[Dict[str, Any]]:
    if isinstance(display_books, str):
        return []
    if not isinstance(display_books, dict):
        return []
    if pick and pick in display_books and isinstance(display_books[pick], list):
        return [r for r in display_books[pick] if isinstance(r, dict)]
    for rows in display_books.values():
        if isinstance(rows, list) and rows:
            return [r for r in rows if isinstance(r, dict)]
    return []


def _odds_map_from_display(display_books: Any, pick: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for row in _display_rows_for_pick(display_books, pick):
        name = row.get("book") or row.get("name") or ""
        odds = row.get("odds")
        if row.get("american") is not None and odds is None:
            odds = row.get("american")
        key = _book_key(name)
        if key and odds is not None:
            out[key] = odds
            out[str(name).strip().lower()] = odds
    return out


def format_power_books(devig_books: Any) -> str:
    """Comma list of on-pack POWER sharps. PLive is never a sharp."""
    names: List[str] = []
    if isinstance(devig_books, str):
        raw = [p.strip() for p in devig_books.replace("|", ",").split(",") if p.strip()]
        # "Circa -110" blobs → keep the name token
        for part in raw:
            token = part.split()[0] if part.split() else part
            if token.lower() in ("plive",):
                continue
            names.append(token)
        return ", ".join(names)
    for raw in devig_books or []:
        name = str(raw or "").strip()
        if not name or _book_key(name) == "plive":
            continue
        names.append(name)
    return ", ".join(names)


def format_devig_books_named(
    devig_books: Any,
    display_books: Any = None,
    pick: Any = None,
) -> str:
    """Every POWER-pack book with its American: ``Circa -110 | NoVig -108 | DK -112``.

    If we cannot name the pack, keep a non-empty blob rather than dropping it.
    """
    odds_map = _odds_map_from_display(display_books, pick)
    names: List[Any] = []
    if isinstance(devig_books, str) and devig_books.strip():
        blob = devig_books.strip()
        if "|" in blob or ":" in blob:
            # Already a named list or leftover blob — keep if we cannot rebuild.
            if not odds_map:
                return blob
        parts = [p.strip() for p in blob.replace("|", ",").split(",") if p.strip()]
        for part in parts:
            token = part.split(":")[0].split()[0] if part else ""
            if token:
                names.append(token)
    else:
        names = list(devig_books or [])

    pieces: List[str] = []
    for raw in names:
        name = str(raw or "").strip()
        if not name:
            continue
        if _book_key(name) == "plive":
            continue
        label = _sheet_book_label(name)
        odds = odds_map.get(_book_key(name))
        if odds is None:
            odds = odds_map.get(name.lower())
        am = _fmt_sheet_american(odds)
        if am:
            pieces.append(f"{label} {am}")
        else:
            pieces.append(label)
    if pieces:
        return " | ".join(pieces)
    if isinstance(display_books, str) and display_books.strip():
        return display_books.strip()
    if isinstance(devig_books, str) and devig_books.strip():
        return devig_books.strip()
    return ""


def _attr_or_data(alert: Any, alert_data: Optional[Dict[str, Any]], key: str, default: Any = None) -> Any:
    data = alert_data if isinstance(alert_data, dict) else {}
    if data.get(key) is not None and data.get(key) != "":
        return data.get(key)
    if alert is not None and getattr(alert, key, None) is not None:
        return getattr(alert, key)
    return default


def take_book_label(alert: Any = None, alert_data: Optional[Dict[str, Any]] = None) -> str:
    raw = _attr_or_data(alert, alert_data, "take_book", "Kalshi")
    if str(raw or "").strip().lower() == "plive":
        return "PLive"
    return "Kalshi"


def build_auto_bet_sheet_record(
    *,
    alert: Any = None,
    alert_data: Optional[Dict[str, Any]] = None,
    fill: Optional[Dict[str, Any]] = None,
    skipped: bool = False,
    skip_reason: str = "",
) -> Dict[str, Any]:
    """Full sheet/CSV record. Extra telegram keys may sit on the dict; writers ignore them."""
    data = alert_data if isinstance(alert_data, dict) else {}
    extra = dict(fill or {})
    pick = extra.get("pick") or (getattr(alert, "pick", None) if alert is not None else None) or data.get("pick") or ""
    market_type = (
        extra.get("market_type")
        or (getattr(alert, "market_type", None) if alert is not None else None)
        or data.get("market_type")
        or ""
    )
    qualifier = (
        extra.get("qualifier")
        if extra.get("qualifier") is not None
        else (
            (getattr(alert, "qualifier", None) if alert is not None else None)
            or data.get("qualifier")
            or ""
        )
    )
    line = extra.get("line")
    if line is None:
        line = _attr_or_data(alert, data, "line", None)
    devig_books = extra.get("devig_books_names")
    if devig_books is None:
        devig_books = _attr_or_data(alert, data, "devig_books", [])
    display_books = extra.get("display_books")
    if display_books is None:
        display_books = _attr_or_data(alert, data, "display_books", {})

    if isinstance(extra.get("devig_books"), str) and extra.get("devig_books") and "|" in str(extra.get("devig_books")):
        devig_named = extra["devig_books"]
    else:
        devig_named = format_devig_books_named(devig_books, display_books, pick)

    power = extra.get("power_books")
    if power is None:
        power = format_power_books(devig_books)
    sharp_count = extra.get("sharp_count")
    if sharp_count is None:
        sharp_count = len([p for p in str(power).split(",") if p.strip()]) if power else 0

    clock_running = extra.get("clock_running")
    if clock_running in (None, ""):
        clock_running = _attr_or_data(alert, data, "clock_running", None)
        if clock_running is None:
            clock = _attr_or_data(alert, data, "clock", None)
            if isinstance(clock, dict) and "running" in clock:
                clock_running = clock.get("running")
    status_detail = extra.get("status_detail")
    if status_detail in (None, ""):
        status_detail = (
            _attr_or_data(alert, data, "status_detail", None)
            or _attr_or_data(alert, data, "statusDetail", None)
            or ""
        )
    score = extra.get("score")
    if score in (None, ""):
        score = _attr_or_data(alert, data, "score", None) or format_event_score(data.get("event") if isinstance(data, dict) else None)
    live = extra.get("live")
    if live in (None, ""):
        live = _attr_or_data(alert, data, "live", None)

    reason = skip_reason or extra.get("skip_reason") or ""
    status = extra.get("status") or ("SKIPPED" if skipped else "executed")
    if skipped:
        status = "SKIPPED"

    record: Dict[str, Any] = {
        "timestamp": extra.get("timestamp") or datetime.now().isoformat(),
        "order_id": extra.get("order_id") or "",
        "ticker": extra.get("ticker")
        or (getattr(alert, "ticker", None) if alert is not None else None)
        or data.get("ticker")
        or "",
        "side": extra.get("side") or data.get("side") or "",
        "teams": extra.get("teams")
        or (getattr(alert, "teams", None) if alert is not None else None)
        or data.get("teams")
        or "",
        "market_type": market_type,
        "pick": pick,
        "qualifier": qualifier or "",
        "ev_percent": extra.get("ev_percent")
        if extra.get("ev_percent") is not None
        else (
            getattr(alert, "ev_percent", None)
            if alert is not None
            else data.get("ev_percent", "")
        ),
        "expected_price_cents": extra.get("expected_price_cents")
        if extra.get("expected_price_cents") is not None
        else (data.get("price_cents") or extra.get("expected_price") or ""),
        "executed_price_cents": extra.get("executed_price_cents") or "",
        "american_odds": extra.get("american_odds")
        if extra.get("american_odds") is not None
        else (data.get("american_odds") or data.get("odds") or ""),
        "contracts": extra.get("contracts") if extra.get("contracts") is not None else ("" if skipped else ""),
        "cost": extra.get("cost") if extra.get("cost") is not None else ("" if skipped else ""),
        "payout": extra.get("payout") if extra.get("payout") is not None else "",
        "win_amount": extra.get("win_amount") if extra.get("win_amount") is not None else "",
        "sport": extra.get("sport") or "",
        "status": status,
        "result": extra.get("result") or ("" if skipped else "OPEN"),
        "pnl": extra.get("pnl") if extra.get("pnl") is not None else ("0.00" if not skipped else ""),
        "settled": extra.get("settled") if extra.get("settled") is not None else "FALSE",
        "filter_name": extra.get("filter_name")
        or (getattr(alert, "filter_name", None) if alert is not None else None)
        or data.get("filter_name")
        or "",
        "devig_books": devig_named,
        "take_book": extra.get("take_book") or take_book_label(alert, data),
        "power_books": power,
        "sharp_count": int(sharp_count or 0),
        "line": sheet_line_value(market_type, line, qualifier),
        "clock_running": _sheet_bool(clock_running),
        "status_detail": str(status_detail or ""),
        "score": str(score or ""),
        "live": _sheet_bool(live),
        "skip_reason": str(reason or "") if skipped or reason else "",
    }
    # Preserve extra telegram / fee keys used by send_auto_bet_alert.
    for key, value in extra.items():
        if key not in record:
            record[key] = value
    return record


def auto_bet_sheet_row(bet_data: Dict[str, Any]) -> List[Any]:
    """One Google Sheet row in header order (23 locked + 9 appended)."""
    return [
        bet_data.get("timestamp", ""),
        bet_data.get("order_id", ""),
        bet_data.get("ticker", ""),
        bet_data.get("side", ""),
        bet_data.get("teams", ""),
        bet_data.get("market_type", ""),
        bet_data.get("pick", ""),
        bet_data.get("qualifier", ""),
        bet_data.get("ev_percent", ""),
        bet_data.get("expected_price_cents", ""),
        bet_data.get("executed_price_cents", ""),
        bet_data.get("american_odds", ""),
        bet_data.get("contracts", ""),
        bet_data.get("cost", ""),
        bet_data.get("payout", ""),
        bet_data.get("win_amount", ""),
        bet_data.get("sport", ""),
        bet_data.get("status", ""),
        bet_data.get("result", ""),
        bet_data.get("pnl", ""),
        bet_data.get("settled", ""),
        bet_data.get("filter_name", ""),
        bet_data.get("devig_books", ""),
        bet_data.get("take_book", ""),
        bet_data.get("power_books", ""),
        bet_data.get("sharp_count", ""),
        bet_data.get("line", ""),
        bet_data.get("clock_running", ""),
        bet_data.get("status_detail", ""),
        bet_data.get("score", ""),
        bet_data.get("live", ""),
        bet_data.get("skip_reason", ""),
    ]


def ensure_sheet_extra_headers(existing_header: List[str]) -> List[str]:
    """Keep current headers; append any missing extra names at the end."""
    header = [str(h) for h in (existing_header or [])]
    if not header:
        return list(AUTO_BET_SHEET_HEADERS)
    have = {h.strip() for h in header}
    out = list(header)
    for name in AUTO_BET_SHEET_EXTRA_HEADERS:
        if name not in have:
            out.append(name)
    return out
