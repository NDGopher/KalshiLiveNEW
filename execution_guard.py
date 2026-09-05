"""Fail-closed Kalshi execution identity and limit-order helpers.

Cards may be painted from Odds-API plus public Kalshi market GETs.
Private-key credentials are required only to submit orders.

Never place when ticker, side, line, event identity, or price is missing
or mismatched. Away spreads must already be -hdp (home-centric).

Create Order V2 mapping (see ``build_limit_order_payload``):
POST /trade-api/v2/portfolio/events/orders. Buy YES → bid at yes dollars;
buy NO → ask at 1 − no dollars. ``price``/``count`` are fp strings. Taker
IOC. Legacy ``yes_price`` / ``POST /portfolio/orders`` is 410.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

_TICKER_RE = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)+$")
_SUFFIX_RE = re.compile(r"^([A-Z]+)?(\d+)$")


@dataclass(frozen=True)
class ParsedTicker:
    raw: str
    series: str
    event_key: str
    event_ticker: str
    family: str  # moneyline | spread | total
    suffix: Optional[str]
    team_code: Optional[str]
    line_int: Optional[int]
    is_market: bool


@dataclass
class ExecutionCheck:
    ok: bool
    reasons: List[str] = field(default_factory=list)
    ticker: Optional[str] = None
    event_ticker: Optional[str] = None
    side: Optional[str] = None
    price_cents: Optional[int] = None
    line: Optional[float] = None
    family: Optional[str] = None

    def deny(self, reason: str) -> "ExecutionCheck":
        self.ok = False
        if reason and reason not in self.reasons:
            self.reasons.append(reason)
        return self


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _upper(value: Any) -> str:
    return _clean(value).upper()


def is_plive_venue(take_book: Any = None, ticker: Any = None) -> bool:
    if _upper(take_book) == "PLIVE":
        return True
    return _upper(ticker).startswith("PLIVE")


def is_kalshi_ticker(ticker: Any) -> bool:
    t = _upper(ticker)
    if not t or is_plive_venue(ticker=t):
        return False
    if t.startswith("KXSCAN"):
        return False
    if t.startswith("KALSHI|"):
        return False
    return bool(_TICKER_RE.match(t)) and t.startswith("KX")


def is_paper_kalshi_ticker(ticker: Any) -> bool:
    """Display-only Kalshi identity. Never executable."""
    return _upper(ticker).startswith("KALSHI|")


def paper_kalshi_ticker(teams: Any, pick: Any, qualifier: Any) -> str:
    return f"KALSHI|{teams}|{pick}|{qualifier}"


def series_family(series: str) -> str:
    s = _upper(series)
    if "SPREAD" in s:
        return "spread"
    if "TOTAL" in s:
        return "total"
    return "moneyline"


def to_game_series(series: str) -> str:
    s = _upper(series)
    return s.replace("SPREAD", "GAME").replace("TOTAL", "GAME")


def parse_kalshi_ticker(ticker: Any) -> Optional[ParsedTicker]:
    raw = _upper(ticker)
    if not is_kalshi_ticker(raw):
        return None
    parts = raw.split("-")
    if len(parts) < 2:
        return None
    series = parts[0]
    event_key = parts[1]
    suffix = parts[2] if len(parts) >= 3 else None
    family = series_family(series)
    team_code = None
    line_int = None
    if suffix:
        m = _SUFFIX_RE.match(suffix)
        if m:
            team_code = m.group(1) or None
            line_int = int(m.group(2))
        elif suffix.isalpha():
            team_code = suffix
    event_ticker = f"{to_game_series(series)}-{event_key}"
    return ParsedTicker(
        raw=raw,
        series=series,
        event_key=event_key,
        event_ticker=event_ticker,
        family=family,
        suffix=suffix,
        team_code=team_code,
        line_int=line_int,
        is_market=suffix is not None,
    )


def event_ticker_from_any(ticker: Any) -> Optional[str]:
    """Accept an event or market ticker; always return the GAME event ticker."""
    parsed = parse_kalshi_ticker(ticker)
    return parsed.event_ticker if parsed else None


def same_event(left: Any, right: Any) -> bool:
    a = parse_kalshi_ticker(left)
    b = parse_kalshi_ticker(right)
    if not a or not b:
        return False
    return a.event_key == b.event_key and to_game_series(a.series) == to_game_series(b.series)


def format_hms_us(ts: Optional[float] = None) -> str:
    """Local H:M:S.microseconds for auto-bet timing logs.

    ``time.strftime`` does not support ``%f`` and raises
    ``ValueError: Invalid format string``. Use ``datetime``.
    """
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S.%f")


def kalshi_line_int(line: Any) -> Optional[int]:
    """Kalshi ticker suffix is ``ceil(|line|)`` — the YES-over integer threshold.

    Live Kalshi Trade API (2026-09-05), titles + ``floor_strike``:

    * ``KXNCAAFTOTAL-26SEP05DUQAFA-39`` — title "Over 38.5 points scored",
      ``floor_strike=38.5``. Suffix ``-38`` does **not** exist.
    * ``KXNCAAFTOTAL-26SEP05BALLOSU-60`` — title "Over 59.5 points scored",
      ``floor_strike=59.5``. Suffix ``-59`` is the **neighbor** "Over 58.5".
    * Same ceil rule on NFL/MLB/EPL/UCL totals and CFB/NFL/MLB spreads
      (e.g. "wins by over 1.5" → ``…DET2``, "over 2.5 goals" → ``…-3``).

    ``int(abs(line))`` / floor is the neighboring strike and must fail closed.
    Exact integers (``7.0``) stay themselves (``ceil(7)=7``). Quarter lines
    such as 1.75 encode as 2, but ``floor_strike`` validation must still
    refuse unless the fetched market strike equals the alert line.

    Odds-API href is a hint, not authority: trust it only when its suffix
    already equals this function. Rebuild from the alert line otherwise.
    """
    try:
        if line is None or line == "":
            return None
        return int(math.ceil(abs(float(line))))
    except (TypeError, ValueError):
        return None


def market_floor_strike_matches_alert(
    market: Any,
    line: Any = None,
    qualifier: Any = None,
) -> bool:
    """True when Kalshi ``floor_strike`` equals the sportsbook alert line.

    Kalshi titles the contract from ``floor_strike`` (always ``X.5`` in
    sampled sports markets). A 59.5 alert on a market whose
    ``floor_strike`` is 58.5 is the sister line — fail closed.
    Missing ``floor_strike`` is not a pass; caller must already have
    matched the ticker suffix. This helper returns False if the strike
    is present and disagrees, True if absent (suffix is the only check).
    """
    if not isinstance(market, dict):
        return True
    raw = market.get("floor_strike")
    if raw is None or raw == "":
        return True
    alert_line = parse_alert_line(line, qualifier)
    if alert_line is None:
        return False
    try:
        return abs(float(raw) - abs(float(alert_line))) < 1e-6
    except (TypeError, ValueError):
        return False


def href_ticker_agrees_with_alert(
    href_ticker: Any,
    line: Any = None,
    qualifier: Any = None,
) -> bool:
    """Odds-API href is usable only when it already encodes ceil(|alert line|)."""
    parsed = parse_kalshi_ticker(href_ticker)
    if not parsed or not parsed.is_market:
        return False
    if parsed.family not in ("spread", "total"):
        return True
    alert_line = parse_alert_line(line, qualifier)
    if alert_line is None or parsed.line_int is None:
        return False
    return parsed.line_int == kalshi_line_int(alert_line)


def ticker_line_matches_alert(
    ticker: Any,
    line: Any = None,
    qualifier: Any = None,
) -> bool:
    """True when the ticker has no line (ML) or its line_int matches ceil(|alert|)."""
    parsed = parse_kalshi_ticker(ticker)
    if not parsed:
        return False
    if parsed.family not in ("spread", "total"):
        return True
    alert_line = parse_alert_line(line, qualifier)
    if alert_line is None or parsed.line_int is None:
        return False
    return parsed.line_int == kalshi_line_int(alert_line)


def parse_alert_line(line: Any, qualifier: Any = None) -> Optional[float]:
    for raw in (line, qualifier):
        if raw is None or raw == "":
            continue
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
        s = str(raw).replace("*", "").replace("+", "").strip() if False else str(raw).replace("*", "").strip()
        try:
            return float(s)
        except (TypeError, ValueError):
            continue
    return None


def away_inverted_line(home_hdp: Any, bet_side: str) -> Optional[float]:
    """Home-centric hdp. Away is always -hdp. BookieBeats / KalshiBB rule."""
    try:
        hf = float(home_hdp)
    except (TypeError, ValueError):
        return None
    if (bet_side or "").lower() == "away":
        return -hf
    return hf


def split_home_away(teams: Any) -> Tuple[Optional[str], Optional[str]]:
    """Alert teams are 'Away @ Home'."""
    s = _clean(teams)
    if not s:
        return None, None
    parts = re.split(r"\s*[@]\s*|\s+VS\.?\s+", s, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return None, None
    return parts[0].strip(), parts[1].strip()


def name_matches_code(name: Any, code: Any) -> bool:
    n = _upper(name)
    c = _upper(code)
    if not n or not c:
        return False
    compact = re.sub(r"[^A-Z0-9]", "", n)
    if c and c in compact:
        return True
    words = [w for w in re.split(r"[^A-Z0-9]+", n) if w]
    initials = "".join(w[0] for w in words) if words else ""
    if words and initials[: len(c)] == c:
        return True
    # Air Force → AFA, Ohio State → OSU: code starts with the initials.
    if len(initials) >= 2 and (c.startswith(initials) or initials.startswith(c)):
        return True
    for w in words:
        if w == c or (len(w) >= 3 and (c.startswith(w[:3]) or w.startswith(c))):
            return True
    return False


def pick_is_away(pick: Any, teams: Any) -> Optional[bool]:
    away, home = split_home_away(teams)
    if not away or not home:
        return None
    p = _upper(pick)
    if name_matches_code(away, p) or p in _upper(away) or _upper(away) in p:
        if p in _upper(home) and p not in _upper(away):
            return False
        return True
    if name_matches_code(home, p) or p in _upper(home) or _upper(home) in p:
        return False
    away_hit = any(w in p for w in _upper(away).split() if len(w) > 3)
    home_hit = any(w in p for w in _upper(home).split() if len(w) > 3)
    if away_hit and not home_hit:
        return True
    if home_hit and not away_hit:
        return False
    return None


def _family_from_market_type(market_type: Any) -> str:
    m = _upper(market_type)
    if "TOTAL" in m or m in ("OVER", "UNDER"):
        return "total"
    if "SPREAD" in m or "PUCK" in m or "HANDICAP" in m or "RUN LINE" in m:
        return "spread"
    return "moneyline"


def is_executable_market_ticker(
    ticker: Any,
    market_type: Any = None,
    line: Any = None,
    qualifier: Any = None,
) -> bool:
    """True only for a real market KX (GAME-TEAM / SPREAD-TEAM{ceil} / TOTAL-{ceil}).

    Bare GAME event tickers are not executable identity. Neighbor ceil suffixes fail.
    """
    parsed = parse_kalshi_ticker(ticker)
    if not parsed or not parsed.is_market:
        return False
    if market_type:
        family = _family_from_market_type(market_type)
        if parsed.family != family:
            return False
        if family in ("spread", "total"):
            return ticker_line_matches_alert(parsed.raw, line, qualifier)
    return True


def prefer_market_ticker(
    attached: Any,
    built: Any,
    market_type: Any,
    line: Any = None,
    qualifier: Any = None,
) -> Optional[str]:
    """Prefer public-attach / catalog market ticker when family+ceil match.

    Else the built ticker. Event-only is not returned — find_submarket must
    rebuild from the GAME event. Never invent a neighbor line.
    """
    if is_executable_market_ticker(attached, market_type, line, qualifier):
        parsed = parse_kalshi_ticker(attached)
        return parsed.raw if parsed else None
    if is_executable_market_ticker(built, market_type, line, qualifier):
        parsed = parse_kalshi_ticker(built)
        return parsed.raw if parsed else None
    return None


def is_bare_game_event_ticker(ticker: Any) -> bool:
    """True for a KX*GAME event with no TEAM / line suffix (not executable)."""
    parsed = parse_kalshi_ticker(ticker)
    return bool(parsed and not parsed.is_market)


def expected_side_for_alert(
    *,
    market_type: Any,
    pick: Any,
    line: Optional[float],
    ticker: Any,
    teams: Any = None,
    qualifier: Any = None,
) -> Optional[str]:
    """Locked YES/NO for an executable Kalshi take.

    * Totals: Over=YES, Under=NO (same TOTAL-{ceil} ticker).
    * Spreads: favorite (line<0)=YES on that team's ticker; dog (line>0)=NO
      on the favorite's ticker. Event-only still uses that convention because
      ``build_market_ticker`` always stamps the fav suffix.
    * Moneyline: pick=YES on the pick's GAME-TEAM ticker. Event-only → YES
      (caller must stamp the pick's market ticker).
    Never returns '' — unresolved is None. Do not place with an empty side.
    """
    family = _family_from_market_type(market_type)
    pick_u = _upper(pick)
    parsed = parse_kalshi_ticker(ticker)
    if family == "total":
        if "UNDER" in pick_u:
            return "no"
        if "OVER" in pick_u:
            return "yes"
        return None
    if family == "spread":
        if line is None:
            line = parse_alert_line(None, qualifier)
        if line is None:
            return None
        try:
            lf = float(line)
        except (TypeError, ValueError):
            return None
        if parsed and parsed.is_market and parsed.team_code:
            pick_on_ticker = bool(name_matches_code(pick, parsed.team_code))
            if lf < 0:
                # Favorite: Kalshi ticker is this team's market. YES = favorite covers.
                return "yes" if pick_on_ticker else None
            if lf > 0:
                # Underdog: ticker is the favorite (opponent). NO = underdog covers.
                return "no" if not pick_on_ticker else None
            return None
        # Event-only / no team suffix: we always stamp the favorite's ticker.
        if lf < 0:
            return "yes"
        if lf > 0:
            return "no"
        return None
    # moneyline: suffix is the YES team
    if parsed and parsed.team_code:
        if name_matches_code(pick, parsed.team_code):
            return "yes"
        away, home = split_home_away(teams)
        opp = None
        if away and home:
            if name_matches_code(away, parsed.team_code) or _upper(away).find(parsed.team_code) >= 0:
                opp = home
            elif name_matches_code(home, parsed.team_code) or _upper(home).find(parsed.team_code) >= 0:
                opp = away
        if opp and (name_matches_code(pick, opp) or pick_u in _upper(opp) or _upper(opp) in pick_u):
            return "no"
        if not name_matches_code(pick, parsed.team_code):
            return "no"
        return None
    # Event-only: ML pick is YES once the pick's GAME-TEAM ticker is stamped.
    if pick_u:
        return "yes"
    return None


def has_trading_credentials(priv: Any, key_id: Any) -> bool:
    return bool(priv) and bool(_clean(key_id))


def public_get_headers(priv: Any, key_id: Any, signed_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Market/event/orderbook GETs are public. Sign only when a key is loaded."""
    if has_trading_credentials(priv, key_id) and signed_headers:
        return dict(signed_headers)
    return {}


# Official Create Order V2 (docs.kalshi.com/api-reference/orders/create-order-v2).
# Servers are …/trade-api/v2; this repo signs and POSTs the full path from root.
KALSHI_CREATE_ORDER_V2_PATH = "/trade-api/v2/portfolio/events/orders"
KALSHI_CANCEL_ORDER_V2_PATH_TMPL = "/trade-api/v2/portfolio/events/orders/{order_id}"


def validate_limit_price(price_cents: Any) -> Optional[int]:
    try:
        n = int(price_cents)
    except (TypeError, ValueError):
        return None
    if n < 1 or n > 99:
        return None
    return n


def dollars_fp_from_cents(price_cents: int) -> str:
    """Fixed-point dollar string for Create Order V2 (e.g. 43 → ``\"0.4300\"``)."""
    return f"{int(price_cents) / 100:.4f}"


def count_fp(count: int) -> str:
    """Fixed-point contract count for Create Order V2 (e.g. 10 → ``\"10.00\"``)."""
    return f"{int(count):.2f}"


def yes_leg_limit_cents(*, side: str, price_cents: int) -> int:
    """YES-leg cents for V2 ``price``.

    Create Order V2 quotes the YES book only: ``bid`` = buy YES, ``ask`` = sell YES
    (economically buy NO at ``1 - price``). Buy YES at P¢ → bid P¢. Buy NO at P¢
    → ask at (100 − P)¢.
    """
    if side == "yes":
        return int(price_cents)
    return 100 - int(price_cents)


def yes_leg_to_side_cents(*, side: Any, yes_leg_cents: Any) -> Optional[int]:
    """Map a V2 YES-leg fill/quote to yes/no cost-basis cents.

    Official BookSide (docs.kalshi.com/api-reference/orders/create-order-v2):
    everything is quoted on the YES book. ``average_fill_price`` for an ask
    that bought NO @ 34¢ is ``0.6600`` (YES-leg). The NO cost basis is 34¢,
    not 66¢. Reporting 66 as executed_price was the 2026-09-05 Under incident.
    """
    px = validate_limit_price(yes_leg_cents)
    if px is None:
        return None
    side_l = _clean(side).lower()
    if side_l == "yes":
        return px
    if side_l == "no":
        return validate_limit_price(100 - px)
    return None


def v2_fill_side_economics(
    *,
    side: Any,
    yes_leg_cents: Any,
    fill_count: Any,
    fees_cents: Any = 0,
) -> Tuple[Optional[int], Optional[int]]:
    """YES-leg fill → ``(side_cents, total_cost_cents)`` including fees.

    Buy NO @ 34¢ filling at YES-leg 66¢ → executed 34, cost ``34 * n + fees``.
    Never ``66 * n``. Buy YES @ 47¢ is unchanged.
    """
    side_cents = yes_leg_to_side_cents(side=side, yes_leg_cents=yes_leg_cents)
    try:
        n = float(fill_count)
    except (TypeError, ValueError):
        n = 0.0
    try:
        fees = int(fees_cents or 0)
    except (TypeError, ValueError):
        fees = 0
    if side_cents is None or n <= 0:
        return side_cents, None
    return side_cents, int(round(side_cents * n)) + max(0, fees)


def no_payload_quotes_yes_leg_complement(price_cents: Any, payload: Any) -> bool:
    """Fail-closed: NO take at P¢ must be V2 ``ask`` at ``(100-P)/100`` dollars."""
    px = validate_limit_price(price_cents)
    if px is None or not isinstance(payload, dict):
        return False
    want = dollars_fp_from_cents(yes_leg_limit_cents(side="no", price_cents=px))
    return payload.get("side") == "ask" and payload.get("price") == want


def is_complement_no_cost(take_cents: Any, executed_side_cents: Any) -> bool:
    """True if executed NO cost is ``100 − T`` (never allowed for a T take)."""
    take = validate_limit_price(take_cents)
    exe = validate_limit_price(executed_side_cents)
    if take is None or exe is None:
        return False
    return exe == 100 - take


def build_limit_order_payload(
    *,
    ticker: Any,
    side: Any,
    count: Any,
    price_cents: Any,
    post_only: bool = False,
    client_order_id: Any = None,
    expiration_time: Any = None,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Create Order V2 limit at the displayed actionable price. Never a market order.

    Official request (https://docs.kalshi.com/api-reference/orders/create-order-v2):
    ``POST /trade-api/v2/portfolio/events/orders`` with required
    ``ticker``, ``side`` (``bid``|``ask``), ``count`` (fp string), ``price``
    (dollar fp string), ``time_in_force``, ``self_trade_prevention_type``.

    Mapping from this bot's yes/no + alert cents:

    * buy YES @ P¢ → ``side=bid``, ``price="{P/100:.4f}"``
    * buy NO  @ P¢ → ``side=ask``, ``price="{(100-P)/100:.4f}"`` (YES-leg quote)

    Taker path uses ``immediate_or_cancel`` (limit-at-alert-cents, not market).
    Post-only uses ``good_till_canceled`` and may set ``expiration_time``.
    Fail-closed if ticker, side, count, or price is missing/invalid.
    """
    reasons: List[str] = []
    parsed = parse_kalshi_ticker(ticker)
    if not parsed:
        reasons.append("missing_or_invalid_ticker")
    side_l = _clean(side).lower()
    if side_l not in ("yes", "no"):
        reasons.append("missing_or_invalid_side")
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        reasons.append("missing_or_invalid_count")
    px = validate_limit_price(price_cents)
    if px is None:
        reasons.append("missing_or_invalid_price")
    if reasons:
        return None, reasons
    yes_cents = yes_leg_limit_cents(side=side_l, price_cents=px)
    if validate_limit_price(yes_cents) is None:
        return None, ["missing_or_invalid_price"]
    book_side = "bid" if side_l == "yes" else "ask"
    payload: Dict[str, Any] = {
        "ticker": parsed.raw,
        "side": book_side,
        "count": count_fp(n),
        "price": dollars_fp_from_cents(yes_cents),
        "time_in_force": "good_till_canceled" if post_only else "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
    }
    cid = _clean(client_order_id)
    if cid:
        payload["client_order_id"] = cid
    if post_only:
        payload["post_only"] = True
        if expiration_time is not None:
            try:
                exp = int(expiration_time)
            except (TypeError, ValueError):
                exp = 0
            if exp > 0:
                payload["expiration_time"] = exp
    return payload, []


def validate_execution_intent(
    *,
    ticker: Any,
    side: Any,
    price_cents: Any,
    market_type: Any,
    pick: Any,
    teams: Any = None,
    line: Any = None,
    qualifier: Any = None,
    event_ticker: Any = None,
    rebuilt_ticker: Any = None,
    take_book: Any = "Kalshi",
    home_hdp: Any = None,
    bet_side: Any = None,
    require_credentials: bool = False,
    has_credentials: bool = False,
    market_floor_strike: Any = None,
) -> ExecutionCheck:
    """Refuse the order unless identity + price are complete and consistent."""
    check = ExecutionCheck(ok=True)
    if is_plive_venue(take_book, ticker):
        return check.deny("plive_not_executable")
    if require_credentials and not has_credentials:
        return check.deny("credentials_required_for_orders")

    parsed = parse_kalshi_ticker(ticker)
    if not parsed:
        return check.deny("missing_or_invalid_ticker")
    check.ticker = parsed.raw
    check.event_ticker = parsed.event_ticker
    check.family = parsed.family

    side_l = _clean(side).lower()
    if side_l not in ("yes", "no"):
        return check.deny("missing_or_invalid_side")
    check.side = side_l

    px = validate_limit_price(price_cents)
    if px is None:
        return check.deny("missing_or_invalid_price")
    check.price_cents = px

    if not _clean(pick):
        return check.deny("missing_pick")

    family = _family_from_market_type(market_type) or parsed.family
    if parsed.family != family:
        check.deny("wrong_market_family")

    alert_line = parse_alert_line(line, qualifier)
    check.line = alert_line

    if family in ("spread", "total"):
        if alert_line is None:
            check.deny("missing_line")
        elif parsed.line_int is None:
            check.deny("ticker_missing_line")
        elif parsed.line_int != kalshi_line_int(alert_line):
            check.deny("wrong_line")
        elif not market_floor_strike_matches_alert(
            {"floor_strike": market_floor_strike} if market_floor_strike is not None else {},
            alert_line,
        ):
            check.deny("wrong_line")

    if home_hdp is not None and bet_side:
        expected = away_inverted_line(home_hdp, str(bet_side))
        if expected is None:
            check.deny("missing_hdp")
        elif alert_line is None or abs(float(alert_line) - float(expected)) > 1e-6:
            check.deny("away_side_not_inverted")

    if event_ticker:
        if not same_event(parsed.raw, event_ticker):
            check.deny("event_mismatch")
    if rebuilt_ticker:
        rebuilt = parse_kalshi_ticker(rebuilt_ticker)
        if not rebuilt:
            check.deny("stale_or_mismatched_ticker")
        elif rebuilt.raw != parsed.raw:
            check.deny("stale_or_mismatched_ticker")
        elif not same_event(parsed.raw, rebuilt.raw):
            check.deny("event_mismatch")

    expected_side = expected_side_for_alert(
        market_type=market_type,
        pick=pick,
        line=alert_line,
        ticker=parsed.raw,
        teams=teams,
        qualifier=qualifier,
    )
    if expected_side is None:
        check.deny("side_unresolved")
    elif expected_side != side_l:
        check.deny("wrong_side")

    if check.reasons:
        check.ok = False
    return check


def prepare_executable_order(
    alert: Dict[str, Any],
    *,
    rebuilt_ticker: Any = None,
    home_hdp: Any = None,
    bet_side: Any = None,
    require_credentials: bool = True,
    has_credentials: bool = False,
) -> ExecutionCheck:
    """Shared gate for click-to-bet and auto-bet. Fail closed."""
    return validate_execution_intent(
        ticker=alert.get("ticker"),
        side=alert.get("side"),
        price_cents=alert.get("price_cents"),
        market_type=alert.get("market_type"),
        pick=alert.get("pick"),
        teams=alert.get("teams"),
        line=alert.get("line"),
        qualifier=alert.get("qualifier"),
        event_ticker=alert.get("event_ticker"),
        rebuilt_ticker=rebuilt_ticker,
        take_book=alert.get("take_book") or "Kalshi",
        home_hdp=home_hdp if home_hdp is not None else alert.get("home_hdp"),
        bet_side=bet_side or alert.get("bet_side"),
        require_credentials=require_credentials,
        has_credentials=has_credentials,
        market_floor_strike=(
            alert.get("floor_strike")
            if alert.get("floor_strike") is not None
            else (alert.get("market_data") or {}).get("floor_strike")
        ),
    )
