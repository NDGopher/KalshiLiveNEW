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

# Sharp-panel screen (Kalshi All Sports POWER+AVERAGE). Applied to every non-Kalshi
# book dict, including a future PLive row — name is not special-cased.
SHARP_ABS_AMERICAN_CAP = 1000
SHARP_OUTLIER_IMPLIED_FLOOR = 0.08
SHARP_OUTLIER_MAD_MULT = 2.5
# Cluster is sided (not a pick'em) when |median implied - 0.5| is at least this.
# Opposite-side books are off-market / sign-flips (NV -154 on a +170 pack).
SHARP_SIGN_FLIP_CLUSTER = 0.04
# Kalshi-adjacent pack: seed within this of Kalshi, grow by this gap. Do not
# use a global median on a bimodal board (that drops the close rec).
SHARP_ADJACENT_SEED = 0.04
SHARP_ADJACENT_GROW = 0.03
# One egregious screen: |implied(book) − implied(Kalshi)| > 10 cents, or sign flip.
# Replaces a global-median / pack-median outlier screen. DK +228 vs +245 (~1.5c) stays.
JUNK_VS_KALSHI_CENTS = 0.10
SHARP_EGREGIOUS_GAP = JUNK_VS_KALSHI_CENTS
BETTER_BOOKS_KILL = 3
MEDIAN_GATE_TOL = 0.005
# Identity band. KEEP boards with ~8c of juice (57c vs 65c) must not match this.
TIGHT_CLUSTER_BAND = 0.04
TIGHT_CLUSTER_EV_ABS = 2.0


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


def _median_floats(xs: List[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    if n % 2:
        return s[n // 2]
    return 0.5 * (s[n // 2 - 1] + s[n // 2])


def _coerce_book_american(book: Dict[str, Any], decimal_pick: float) -> Optional[int]:
    raw = book.get("american")
    if raw is not None and raw != "":
        try:
            return int(round(float(raw)))
        except (TypeError, ValueError):
            pass
    if decimal_pick > 1.0:
        return decimal_to_american(decimal_pick)
    return None


def american_is_strictly_better(book_american: int, kalshi_american: int) -> bool:
    """Bettor-favorable: higher decimal. -117 > -127, +192 > +122, +107 > -127."""
    return american_to_decimal(int(book_american)) > american_to_decimal(int(kalshi_american)) + 1e-12


def _book_implied(book: Dict[str, Any]) -> float:
    return 1.0 / float(book["decimal_pick"])


def implied_prob_from_american(american: int) -> Optional[float]:
    dec = american_to_decimal(int(american))
    if dec <= 1.0:
        return None
    return 1.0 / dec


def is_sign_flip_american(book_american: int, kalshi_american: int) -> bool:
    """Plus vs minus on the same pick (NV −154 vs Kalshi +186). Even money is not a flip."""
    b, k = int(book_american), int(kalshi_american)
    if b == 0 or k == 0:
        return False
    return (b > 0) != (k > 0)


def is_junk_vs_kalshi(
    book_american: int,
    kalshi_american: int,
    *,
    gap: float = JUNK_VS_KALSHI_CENTS,
) -> bool:
    """Display and fair share this test. True → gray tile and drop from POWER/AVERAGE.

    Junk if the book flips sign vs Kalshi, or |implied − Kalshi implied| > 10 cents.
    """
    if is_sign_flip_american(book_american, kalshi_american):
        return True
    bp = implied_prob_from_american(book_american)
    kp = implied_prob_from_american(kalshi_american)
    if bp is None or kp is None:
        return True
    return abs(bp - kp) > float(gap) + 1e-12


def kalshi_adjacent_pack(
    books: List[Dict[str, Any]],
    kalshi_american: int,
    *,
    seed: float = SHARP_ADJACENT_SEED,
    grow: float = SHARP_ADJACENT_GROW,
) -> List[Dict[str, Any]]:
    """Consensus recs next to Kalshi. Does not use a global all-book median.

    Seed = books within ``seed`` of Kalshi implied (same side of 0.50).
    Grow by attaching books within ``grow`` of someone already in the pack.
    A far steam cluster is a second mode and stays out.
    """
    rows = [b for b in (books or []) if float(b.get("decimal_pick") or 0) > 1.0]
    if not rows:
        return []
    k_dec = american_to_decimal(int(kalshi_american))
    k_imp = (1.0 / k_dec) if k_dec > 1.0 else 0.5
    scored = [(b, _book_implied(b)) for b in rows]
    pack = [
        b
        for b, p in scored
        if abs(p - k_imp) <= seed and (p - 0.5) * (k_imp - 0.5) >= 0
    ]
    if not pack:
        same = [(b, p) for b, p in scored if (p - 0.5) * (k_imp - 0.5) >= 0]
        same.sort(key=lambda t: abs(t[1] - k_imp))
        pack = [b for b, _ in same[:1]] if same else []
    if not pack:
        return []
    pack_imps = [_book_implied(b) for b in pack]
    changed = True
    while changed:
        changed = False
        for b, p in scored:
            if any(b is x for x in pack):
                continue
            if abs(p - k_imp) > seed:
                continue
            if min(abs(p - q) for q in pack_imps) <= grow:
                pack.append(b)
                pack_imps.append(p)
                changed = True
    return pack


def _eligible_sharp_books(books: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    eligible: List[Dict[str, Any]] = []
    for raw in books or []:
        if not isinstance(raw, dict):
            continue
        if raw.get("match_failed") is True:
            continue
        try:
            d_pick = float(raw.get("decimal_pick") or 0.0)
            d_opp = float(raw.get("decimal_opp") or 0.0)
        except (TypeError, ValueError):
            continue
        if d_pick <= 1.0 or d_opp <= 1.0:
            continue
        am = _coerce_book_american(raw, d_pick)
        if am is None:
            continue
        if abs(am) >= SHARP_ABS_AMERICAN_CAP:
            continue
        row = dict(raw)
        row["american"] = am
        row["decimal_pick"] = d_pick
        row["decimal_opp"] = d_opp
        eligible.append(row)
    return eligible


def filter_sharp_panel(
    books: List[Dict[str, Any]],
    kalshi_american: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Drop unmatched, incomplete, spiked, off-market, and sign-flipped quotes.

    Each book is ``{name, american, decimal_pick, decimal_opp, match_failed?}``.
    Name is ignored (PLive / NV / DK use the same screen). Surviving rows keep
    their original keys so the caller can still read decimals / american.

    When ``kalshi_american`` is set, egregious quotes use ``is_junk_vs_kalshi``
    (10c from Kalshi or sign flip) — not a global median.
    """
    eligible = _eligible_sharp_books(books)
    if not eligible:
        return []

    if kalshi_american is not None:
        # 10c-from-Kalshi (or sign flip) is the only egregious screen — not a global median.
        surviving: List[Dict[str, Any]] = []
        for book in eligible:
            if is_junk_vs_kalshi(int(book["american"]), int(kalshi_american)):
                continue
            surviving.append(book)
        return surviving

    implied = [_book_implied(b) for b in eligible]
    med = _median_floats(implied)
    mad = _median_floats([abs(p - med) for p in implied])
    thresh = max(SHARP_OUTLIER_IMPLIED_FLOOR, SHARP_OUTLIER_MAD_MULT * mad)
    surviving = []
    sided = abs(med - 0.5) >= SHARP_SIGN_FLIP_CLUSTER
    for book, p in zip(eligible, implied):
        if abs(p - med) > thresh:
            continue
        if sided and (p - 0.5) * (med - 0.5) < 0:
            continue
        surviving.append(book)
    return surviving


def _american_sign(am: int) -> int:
    if am > 0:
        return 1
    if am < 0:
        return -1
    return 0


def fair_books_for_panel(survivors: List[Dict[str, Any]], kalshi_american: int) -> List[Dict[str, Any]]:
    """POWER/AVERAGE uses the Kalshi-adjacent pack, never a far second cluster.

    Same-sign juice that is not egregious may remain in ``survivors`` (minSharp /
    3-better). It must not pull fair. If no adjacent pack exists, fall back to
    same-sign survivors, then the full set.
    """
    rows = [
        b
        for b in (survivors or [])
        if not is_junk_vs_kalshi(int(b.get("american") or 0), int(kalshi_american))
    ]
    if not rows:
        return []
    pack = kalshi_adjacent_pack(rows, int(kalshi_american))
    if pack:
        return pack
    k_sign = _american_sign(int(kalshi_american))
    same = [b for b in rows if _american_sign(int(b.get("american") or 0)) == k_sign]
    if same:
        return same
    return rows


def count_better_than_kalshi(survivors: List[Dict[str, Any]], kalshi_american: int) -> int:
    n = 0
    for book in survivors or []:
        try:
            am = int(book.get("american"))
        except (TypeError, ValueError):
            continue
        if american_is_strictly_better(am, int(kalshi_american)):
            n += 1
    return n


def power_average_fair_prob(survivors: List[Dict[str, Any]], calc: Optional[EVCalculator] = None) -> Optional[float]:
    """Mean of per-book POWER fairs. Same as type=AVERAGE over method=POWER.

    Floor at the pack's mean pick implied. POWER relaxation can collapse a
    two-way onto AVERAGE and erase a real best-price edge vs close recs
    (KEEP ~+2% vs -139/-141/-142).
    """
    rows = [b for b in (survivors or []) if float(b.get("decimal_pick") or 0) > 1.0 and float(b.get("decimal_opp") or 0) > 1.0]
    if not rows:
        return None
    c = calc or EVCalculator({})
    fairs = [
        _fair_prob_power_relaxed_two_way(c, float(b["decimal_pick"]), float(b["decimal_opp"]))
        for b in rows
    ]
    raw = [_book_implied(b) for b in rows]
    if not fairs:
        return sum(raw) / float(len(raw)) if raw else None
    pwr = sum(fairs) / float(len(fairs))
    raw_mean = sum(raw) / float(len(raw))
    return max(pwr, raw_mean)


def apply_ev_hard_gates(
    ev_percent: float,
    kalshi_american: int,
    survivors: List[Dict[str, Any]],
    *,
    used_fallback: bool = False,
    min_sharp_books: int = 3,
) -> Dict[str, Any]:
    """Median / tight-cluster / 3-better / fallback gates. Math, not UI hide."""
    reasons: List[str] = []
    ev = float(ev_percent)
    allow_plus = True
    k_dec = american_to_decimal(int(kalshi_american))
    k_imp = (1.0 / k_dec) if k_dec > 1.0 else 1.0
    surv_imps = [1.0 / float(b["decimal_pick"]) for b in (survivors or []) if float(b.get("decimal_pick") or 0) > 1.0]
    better = count_better_than_kalshi(survivors, kalshi_american)

    if used_fallback:
        return {
            "ev_percent": 0.0,
            "plus_alert": False,
            "allow_plus": False,
            "reasons": ["fallback"],
            "better_count": better,
            "kalshi_implied": k_imp,
        }

    if len(survivors or []) < int(min_sharp_books):
        return {
            "ev_percent": ev,
            "plus_alert": False,
            "allow_plus": False,
            "reasons": ["min_sharp"],
            "better_count": better,
            "kalshi_implied": k_imp,
        }

    if surv_imps:
        med_s = _median_floats(surv_imps)
        # Kalshi worse than (or ~equal to) the surviving median cannot print a plus.
        if k_imp + 1e-12 >= med_s - MEDIAN_GATE_TOL:
            if ev > 0:
                ev = min(ev, 0.0)
            allow_plus = False
            reasons.append("median_gate")

        band = [k_imp] + surv_imps
        if max(band) - min(band) <= TIGHT_CLUSTER_BAND:
            if abs(ev) > TIGHT_CLUSTER_EV_ABS:
                ev = 0.0
                allow_plus = False
                reasons.append("tight_cluster")

    if better >= BETTER_BOOKS_KILL:
        ev = min(ev, 0.0)
        allow_plus = False
        reasons.append("better_books")

    plus_alert = bool(allow_plus and ev > 0.0)
    return {
        "ev_percent": ev,
        "plus_alert": plus_alert,
        "allow_plus": allow_plus,
        "reasons": reasons,
        "better_count": better,
        "kalshi_implied": k_imp,
        "survivor_median_implied": _median_floats(surv_imps) if surv_imps else None,
    }


def evaluate_sharp_panel_ev(
    books: List[Dict[str, Any]],
    kalshi_american: int,
    *,
    min_sharp_books: int = 3,
    method: str = "POWER",
    used_fallback: bool = False,
) -> Dict[str, Any]:
    """Filter → POWER/AVERAGE fair → EV vs Kalshi → hard gates.

    Returns surviving books, EV, and whether a plus alert may print.
    Does not mention teams or tickers — callers pass anonymous boards.
    """
    surviving = filter_sharp_panel(books, kalshi_american=kalshi_american)
    calc = EVCalculator({})
    k_dec = american_to_decimal(int(kalshi_american))
    price_cents = int(max(1, min(99, round(100.0 / k_dec)))) if k_dec > 1.0 else 0
    fair: Optional[float] = None
    ev = 0.0
    if used_fallback:
        gated = apply_ev_hard_gates(
            0.0, kalshi_american, surviving, used_fallback=True, min_sharp_books=min_sharp_books
        )
    elif len(surviving) < int(min_sharp_books):
        gated = apply_ev_hard_gates(
            0.0, kalshi_american, surviving, used_fallback=False, min_sharp_books=min_sharp_books
        )
    else:
        fair_src = fair_books_for_panel(surviving, kalshi_american)
        if (method or "POWER").upper() == "POWER":
            fair = power_average_fair_prob(fair_src, calc)
        else:
            fairs = [
                calc.fair_probs_two_way(float(b["decimal_pick"]), float(b["decimal_opp"]), method)[0]
                for b in fair_src
            ]
            fair = sum(fairs) / float(len(fairs)) if fairs else None
        ev = calc.ev_percent_vs_kalshi(fair, price_cents) if fair is not None else -999.0
        gated = apply_ev_hard_gates(
            ev, kalshi_american, surviving, used_fallback=False, min_sharp_books=min_sharp_books
        )
    out = {
        "surviving": surviving,
        "surviving_names": [str(b.get("name") or "") for b in surviving],
        "fair_prob": fair,
        "raw_ev_percent": ev,
        "ev_percent": gated["ev_percent"],
        "plus_alert": gated["plus_alert"],
        "better_count": gated["better_count"],
        "reasons": gated["reasons"],
        "kalshi_implied": gated["kalshi_implied"],
        "survivor_median_implied": gated.get("survivor_median_implied"),
        "used_fallback": used_fallback,
    }
    return out


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

    - POWER: mean of per-panel POWER fairs (same as type=AVERAGE over method=POWER).
      Not panel[0] — a stale first book must not own the POWER column.
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
    power_fairs = [_fair_prob_power_relaxed_two_way(calc, a, b) for a, b in panels if a > 1.0 and b > 1.0]
    if power_fairs:
        out["POWER"] = calc.ev_percent_vs_kalshi(sum(power_fairs) / len(power_fairs), price_cents)
    else:
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
