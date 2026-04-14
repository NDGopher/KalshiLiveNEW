"""
Devig + EV helpers aligned with BookieBeats-style filter knobs.

FREE TIER: With only FanDuel (reference) + Kalshi (target), multi-sharp logic collapses
to a single reference panel; minSharpBooks is satisfied when FanDuel posts both sides.

Methods (devigFilter.method):
  POWER      — power / multiplicative devig on implied probs (two-way and three-way).
  WORST_CASE — per-outcome minimum fair probability across sharp books, renormalized.
  AVERAGE    — mean fair probability per outcome across sharp books, renormalized.

devigFilter.type:
  AVERAGE — combine multiple sharp books at the probability level (see WORST_CASE / AVERAGE).

Debug / multi-sharp:
  ev_percent_three_methods_two_way | ev_percent_three_methods_three_way — canonical 3-method EV vs Kalshi.
  ev_percent_three_methods_multi_sharp — several sharp two-ways (same outcomes); WORST_CASE/AVERAGE pool across books.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

# Fair probability above this on POWER devig uses AVERAGE implied instead (debug + three-method EV).
_POWER_FAIR_CLIP_HI = 0.99
_POWER_FAIR_CLIP_LO = 0.01


def decimal_to_american(d: float) -> int:
    if d <= 1.0:
        return 0
    if d >= 2.0:
        return int(round((d - 1.0) * 100))
    return int(round(-100 / (d - 1.0)))


def american_to_decimal(a: int) -> float:
    if a == 0:
        return 1.0
    if a > 0:
        return 1.0 + a / 100.0
    return 1.0 + 100.0 / abs(a)


def implied_probs(decimals: List[float]) -> List[float]:
    return [1.0 / d for d in decimals if d > 1.0]


def hold_from_decimals(decimals: List[float]) -> float:
    """Overround as fraction (e.g. 0.045 == 4.5% hold)."""
    ips = implied_probs(decimals)
    if not ips:
        return 1.0
    return max(0.0, sum(ips) - 1.0)


def devig_power(implied: List[float]) -> List[float]:
    """Find exponent a>1 so sum(p_i**a)==1; fair_i = p_i**a."""
    s = sum(implied)
    if s <= 0:
        return [1.0 / len(implied)] * len(implied) if implied else []
    p = [x / s for x in implied]
    if len(p) == 1:
        return [1.0]
    lo, hi = 1.0001, 50.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        sm = sum(math.pow(x, mid) for x in p)
        if sm > 1.0:
            hi = mid
        else:
            lo = mid
    a = (lo + hi) / 2.0
    w = [math.pow(x, a) for x in p]
    sw = sum(w)
    return [x / sw for x in w]


def devig_additive(implied: List[float]) -> List[float]:
    """Classic additive (balanced book) devig."""
    s = sum(implied)
    if s <= 0:
        return [1.0 / len(implied)] * len(implied) if implied else []
    excess = s - 1.0
    n = len(implied)
    adj = [max(implied[i] - excess / n, 1e-9) for i in range(n)]
    sa = sum(adj)
    return [x / sa for x in adj]


def devig_normalized_implied(implied: List[float]) -> List[float]:
    """Proportional / multiplicative removal of overround (implied probs renormalized). AVERAGE single-panel."""
    s = sum(implied)
    if s <= 0:
        n = len(implied) or 1
        return [1.0 / n] * len(implied) if implied else []
    return [x / s for x in implied]


def _passes_hold(decimals: List[float], hold_rules: List[Dict[str, Any]]) -> bool:
    h = hold_from_decimals(decimals)
    for rule in hold_rules or []:
        mx = float(rule.get("max", 999))
        if h * 100.0 > mx + 1e-9:
            return False
    return True


def _passes_odds_range(american: int, odds_ranges: List[Dict[str, Any]]) -> bool:
    if not odds_ranges:
        return True
    for r in odds_ranges:
        if str(r.get("book", "Any")).lower() != "any":
            continue
        lo = int(r.get("min", -10**12))
        hi = int(r.get("max", 10**12))
        if lo <= american <= hi:
            return True
    return False


def _min_limit_for_book(min_limits: List[Dict[str, Any]], book: str) -> float:
    for r in min_limits or []:
        if str(r.get("book", "")).lower() == book.lower():
            return float(r.get("min", 0))
    for r in min_limits or []:
        if str(r.get("book", "Any")).lower() == "any":
            return float(r.get("min", 0))
    return 0.0


class EVCalculator:
    """Bookmaker-style devig + EV vs Kalshi price (cents)."""

    def __init__(self, filter_payload: Optional[Dict[str, Any]] = None):
        self.filter_payload = filter_payload or {}

    def set_filter(self, filter_payload: Dict[str, Any]) -> None:
        self.filter_payload = filter_payload

    def fair_probs_two_way(
        self,
        dec_a: float,
        dec_b: float,
        method: str,
    ) -> Tuple[float, float]:
        implied = implied_probs([dec_a, dec_b])
        m = (method or "POWER").upper()
        if m == "POWER":
            fair = devig_power(implied)
        elif m == "WORST_CASE":
            fair = devig_additive(implied)
        elif m == "AVERAGE":
            fair = devig_normalized_implied(implied)
        else:
            fair = devig_normalized_implied(implied)
        return fair[0], fair[1]

    def fair_probs_three_way(self, dec_h: float, dec_d: float, dec_a: float, method: str) -> Tuple[float, float, float]:
        implied = implied_probs([dec_h, dec_d, dec_a])
        m = (method or "POWER").upper()
        if m == "POWER":
            f = devig_power(implied)
        elif m == "WORST_CASE":
            f = devig_additive(implied)
        elif m == "AVERAGE":
            f = devig_normalized_implied(implied)
        else:
            f = devig_normalized_implied(implied)
        return f[0], f[1], f[2]

    def ev_percent_vs_kalshi(self, fair_prob: float, kalshi_price_cents: int) -> float:
        if kalshi_price_cents is None or kalshi_price_cents <= 0 or kalshi_price_cents >= 100:
            return -999.0
        p_offer = kalshi_price_cents / 100.0
        if fair_prob <= 0 or fair_prob >= 1:
            return -999.0
        return (fair_prob / p_offer - 1.0) * 100.0

    def passes_devig_filter(
        self,
        sharp_decimals: List[float],
        ev_percent: float,
        kalshi_limit_usd: float,
        sharp_books_count: int,
    ) -> bool:
        df = self.filter_payload.get("devigFilter") or {}
        min_ev = float(df.get("minEv", 0))
        min_limit = float(df.get("minLimit", 0))
        min_sharp = int(df.get("minSharpBooks", 1))
        if sharp_books_count < min_sharp:
            return False
        if ev_percent + 1e-9 < min_ev:
            return False
        if kalshi_limit_usd + 1e-9 < min_limit:
            return False
        if not _passes_hold(sharp_decimals, df.get("hold") or []):
            return False
        return True

    def passes_roi(self, ev_percent: float) -> bool:
        min_roi = float(self.filter_payload.get("minRoi", 0))
        # Small epsilon so recomputed EV at ~0% (float noise) still passes minRoi=0 filters.
        return ev_percent + 1e-6 >= min_roi

    def passes_min_limits_kalshi(self, kalshi_limit_usd: float) -> bool:
        need = _min_limit_for_book(self.filter_payload.get("minLimits") or [], "Kalshi")
        if need <= 0:
            return True
        return kalshi_limit_usd + 1e-9 >= need

    def passes_odds_ranges(self, american_kalshi: int) -> bool:
        return _passes_odds_range(american_kalshi, self.filter_payload.get("oddsRanges") or [])


def _fair_prob_power_relaxed_two_way(calc: EVCalculator, dec_pick: float, dec_opp: float) -> float:
    """POWER fair prob for pick; fall back to AVERAGE when POWER implies extreme mass on one side."""
    fp_p, _ = calc.fair_probs_two_way(dec_pick, dec_opp, "POWER")
    if fp_p > _POWER_FAIR_CLIP_HI or fp_p < _POWER_FAIR_CLIP_LO:
        fp_p, _ = calc.fair_probs_two_way(dec_pick, dec_opp, "AVERAGE")
    return fp_p


def _ev_vs_kalshi_power_relaxed_two_way(calc: EVCalculator, dec_pick: float, dec_opp: float, price_cents: int) -> float:
    fp = _fair_prob_power_relaxed_two_way(calc, dec_pick, dec_opp)
    ev = calc.ev_percent_vs_kalshi(fp, price_cents)
    if ev <= -998.0:
        fpa, _ = calc.fair_probs_two_way(dec_pick, dec_opp, "AVERAGE")
        ev = calc.ev_percent_vs_kalshi(fpa, price_cents)
    if ev <= -998.0:
        ev = -100.0
    return ev


def _fair_prob_power_relaxed_three_way(
    calc: EVCalculator, dec_home: float, dec_draw: float, dec_away: float, outcome_idx: int
) -> float:
    fh, fd, fa = calc.fair_probs_three_way(dec_home, dec_draw, dec_away, "POWER")
    fp = (fh, fd, fa)[outcome_idx]
    if fp > _POWER_FAIR_CLIP_HI or fp < _POWER_FAIR_CLIP_LO:
        fh, fd, fa = calc.fair_probs_three_way(dec_home, dec_draw, dec_away, "AVERAGE")
        fp = (fh, fd, fa)[outcome_idx]
    return fp


def _ev_vs_kalshi_power_relaxed_three_way(
    calc: EVCalculator,
    dec_home: float,
    dec_draw: float,
    dec_away: float,
    outcome_idx: int,
    price_cents: int,
) -> float:
    fp = _fair_prob_power_relaxed_three_way(calc, dec_home, dec_draw, dec_away, outcome_idx)
    ev = calc.ev_percent_vs_kalshi(fp, price_cents)
    if ev <= -998.0:
        fh, fd, fa = calc.fair_probs_three_way(dec_home, dec_draw, dec_away, "AVERAGE")
        fpa = (fh, fd, fa)[outcome_idx]
        ev = calc.ev_percent_vs_kalshi(fpa, price_cents)
    if ev <= -998.0:
        ev = -100.0
    return ev


def format_ev_percent_display(x: float) -> str:
    """Cap visible range to +/-100%; show overflow tags for extremes (debug tables)."""
    if x != x or x in (float("inf"), float("-inf")):  # NaN / inf
        return "   —   "
    if x > 100.0:
        return ">+100%"
    if x < -100.0:
        return "<-100%"
    return f"{x:+6.1f}%"


def _fair_probs_two_way_multi_aggregate(
    panels: List[Tuple[float, float]], kind: str
) -> Tuple[float, float]:
    """
    Combine multiple sharp books' two-way quotes (same outcome order per panel).
    kind='AVERAGE' — mean implied prob per outcome, renormalized.
    kind='WORST_CASE' — min implied per outcome, renormalized.
    """
    valid: List[Tuple[float, float]] = [(a, b) for a, b in panels if a > 1.0 and b > 1.0]
    if not valid:
        return 0.5, 0.5
    if (kind or "").upper() == "WORST_CASE":
        ia = min(1.0 / a for a, _ in valid)
        ib = min(1.0 / b for _, b in valid)
    else:
        ia = sum(1.0 / a for a, _ in valid) / len(valid)
        ib = sum(1.0 / b for _, b in valid) / len(valid)
    s = ia + ib
    if s <= 0:
        return 0.5, 0.5
    return ia / s, ib / s


def ev_percent_three_methods_multi_sharp(
    sharp_panels: List[Tuple[float, float]],
    kalshi_dec: float,
) -> Dict[str, float]:
    """
    Three EV% methods vs Kalshi when one or more sharp books post the same two-way.

    Each panel is (decimal_outcome_A, decimal_outcome_B). Kalshi decimal must be for outcome A.

    - POWER: first panel only, with the same POWER relaxation as ``ev_percent_three_methods_two_way``.
    - WORST_CASE / AVERAGE: aggregate implieds across panels, then EV vs Kalshi for side A.

    Single panel delegates to ``ev_percent_three_methods_two_way``.
    """
    if kalshi_dec <= 1.0:
        return {"POWER": -999.0, "WORST_CASE": -999.0, "AVERAGE": -999.0}
    panels = [(float(a), float(b)) for a, b in sharp_panels if a is not None and b is not None]
    if not panels:
        return {"POWER": -999.0, "WORST_CASE": -999.0, "AVERAGE": -999.0}
    if len(panels) == 1:
        a, b = panels[0]
        return ev_percent_three_methods_two_way(a, b, kalshi_dec)
    a0, b0 = panels[0]
    if a0 <= 1.0 or b0 <= 1.0:
        return {"POWER": -999.0, "WORST_CASE": -999.0, "AVERAGE": -999.0}
    price_cents = int(max(1, min(99, round(100.0 / kalshi_dec))))
    calc = EVCalculator({})
    out: Dict[str, float] = {}
    out["POWER"] = _ev_vs_kalshi_power_relaxed_two_way(calc, a0, b0, price_cents)
    f_wa, f_wb = _fair_probs_two_way_multi_aggregate(panels, "WORST_CASE")
    f_aa, f_ab = _fair_probs_two_way_multi_aggregate(panels, "AVERAGE")
    out["WORST_CASE"] = calc.ev_percent_vs_kalshi(f_wa, price_cents)
    out["AVERAGE"] = calc.ev_percent_vs_kalshi(f_aa, price_cents)
    return out


def ev_percent_three_methods_two_way(
    dec_pick: float,
    dec_opp: float,
    kalshi_dec: float,
) -> Dict[str, float]:
    """
    Single sharp two-way: canonical three-method EV vs Kalshi (used by debug tables and multi-sharp).

    dec_pick / dec_opp are sharp decimals for the two-way (Kalshi is on dec_pick).
    kalshi_dec is European decimal for Kalshi on the pick side.
    """
    if dec_pick <= 1.0 or dec_opp <= 1.0 or kalshi_dec <= 1.0:
        return {"POWER": -999.0, "WORST_CASE": -999.0, "AVERAGE": -999.0}
    price_cents = int(max(1, min(99, round(100.0 / kalshi_dec))))
    calc = EVCalculator({})
    out: Dict[str, float] = {}
    out["POWER"] = _ev_vs_kalshi_power_relaxed_two_way(calc, dec_pick, dec_opp, price_cents)
    for m in ("WORST_CASE", "AVERAGE"):
        fp, _fo = calc.fair_probs_two_way(dec_pick, dec_opp, m)
        out[m] = calc.ev_percent_vs_kalshi(fp, price_cents)
    return out


def ev_percent_three_methods_three_way(
    dec_home: float,
    dec_draw: float,
    dec_away: float,
    outcome_idx: int,
    kalshi_dec: float,
) -> Dict[str, float]:
    """outcome_idx 0=home 1=draw 2=away."""
    if min(dec_home, dec_draw, dec_away) <= 1.0 or kalshi_dec <= 1.0:
        return {"POWER": -999.0, "WORST_CASE": -999.0, "AVERAGE": -999.0}
    price_cents = int(max(1, min(99, round(100.0 / kalshi_dec))))
    calc = EVCalculator({})
    out: Dict[str, float] = {}
    out["POWER"] = _ev_vs_kalshi_power_relaxed_three_way(
        calc, dec_home, dec_draw, dec_away, outcome_idx, price_cents
    )
    for m in ("WORST_CASE", "AVERAGE"):
        fh, f_draw, fa = calc.fair_probs_three_way(dec_home, dec_draw, dec_away, m)
        fp = (fh, f_draw, fa)[outcome_idx]
        out[m] = calc.ev_percent_vs_kalshi(fp, price_cents)
    return out
