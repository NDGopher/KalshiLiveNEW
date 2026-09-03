"""Tile paint lock: take green left, on-pack better red, worse unshaded."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
JS = (REPO / "static" / "script.js").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def _eval_paint():
    node = shutil.which("node")
    if not node:
        return None
    snippet = r"""
const fs = require('fs');
const src = fs.readFileSync('static/script.js','utf8');
eval(src.slice(src.indexOf('function formatLiquidityUsd'), src.indexOf('function toTitleCaseWords')));
eval(src.slice(src.indexOf('function normalizeBookKey'), src.indexOf('function parseFreshnessTs')));
function priceToAmericanOdds(){ return null; }
const tigersAlert = { take_book: 'PLive', book_price: 369 };
const tigersBooks = [
  {book:'Kalshi', odds:317},
  {book:'DraftKings', odds:307},
  {book:'Caesars', odds:333},
  {book:'Bet365', odds:475},
  {book:'NoVig', odds:525},
  {book:'PLive', odds:369},
  {book:'Circa', odds:0},
];
function bookCellClassString(paint) {
  // Mirror createAlertCard (script.js): take = kalshi-book take-book (green), better = red.
  if (paint.skip) return null;
  return `book-cell ${paint.take ? 'kalshi-book take-book' : ''} ${paint.better ? 'better-than-kalshi' : ''}`;
}
const tigersTake = takeAmericanFromAlert(tigersAlert, tigersBooks);
const tigers = {};
const tigersClasses = {};
for (const b of tigersBooks) {
  tigers[b.book] = tilePaintState(b.book, b.odds, tigersAlert, tigersTake);
  tigersClasses[b.book] = bookCellClassString(tigers[b.book]);
}
const twinsAlert = { take_book: 'PLive', book_price: 306 };
const twinsBooks = [
  {book:'DraftKings', odds:250},
  {book:'FanDuel', odds:280},
  {book:'NoVig', odds:178},
  {book:'Polymarket', odds:335},
  {book:'PLive', odds:306},
];
const twinsTake = takeAmericanFromAlert(twinsAlert, twinsBooks);
const twins = {};
for (const b of twinsBooks) twins[b.book] = tilePaintState(b.book, b.odds, twinsAlert, twinsTake);
const royalsAlert = { take_book: 'Kalshi', book_price: 163 };
const royalsBooks = [
  {book:'Kalshi', odds:163},
  {book:'Betfair Exchange', odds:134},
  {book:'DraftKings', odds:105},
  {book:'FanDuel', odds:116},
  {book:'Bet365', odds:100},
  {book:'Caesars', odds:110},
  {book:'PLive', odds:118},
  {book:'Polymarket', odds:-455},
];
const royalsTake = takeAmericanFromAlert(royalsAlert, royalsBooks);
const royals = {};
for (const b of royalsBooks) royals[b.book] = tilePaintState(b.book, b.odds, royalsAlert, royalsTake);
console.log(JSON.stringify({
  tigersTake, tigers, tigersClasses,
  tigersOrder: orderBooksTakeFirst(tigersBooks, tigersAlert).map(b=>b.book),
  tigersRed: Object.entries(tigers).filter(([,p]) => p.better && !p.skip).map(([n]) => n).sort(),
  tigersGreen: Object.entries(tigers).filter(([,p]) => p.take && !p.skip).map(([n]) => n),
  tigersPainted: Object.entries(tigers).filter(([,p]) => !p.skip).map(([n]) => n),
  twinsTake, twins, twinsOrder: orderBooksTakeFirst(twinsBooks, twinsAlert).map(b=>b.book),
  royalsTake, royals,
  noFallbackKalshi: isCardTakeBook('Kalshi', {take_book:'PLive'}),
  missingTakeDoesNotGreenKalshi: isCardTakeBook('Kalshi', {}),
  kalshiTakeGreensKalshi: isCardTakeBook('Kalshi', {take_book:'Kalshi'}),
  oppSkip: tilePaintState('Polymarket', 170, {take_book:'PLive'}, -285).skip,
  junkSignFlipSkip: tilePaintState('Pinnacle', -455, {take_book:'Kalshi'}, 163).skip,
  nvWorseJunkSkip: tilePaintState('NoVig', 178, {take_book:'PLive'}, 306),
  junk10cSkipsWorse: isJunkVsKalshi(178, 306) === true
    && tilePaintState('NoVig', 178, {take_book:'PLive'}, 306).skip === true,
  onPackPolyPaints: tilePaintState('Polymarket', 158, {take_book:'Kalshi'}, 163),
  junkPoly455Skip: tilePaintState('Polymarket', -455, {take_book:'Kalshi'}, 163).skip,
  junkPolyUnicodeSkip: tilePaintState('Polymarket', '−455', {take_book:'Kalshi'}, 163).skip,
  junkPoly178Skip: tilePaintState('Polymarket', -178, {take_book:'Kalshi'}, -104).skip,
  pickemNotJunk: isJunkVsKalshi(-110, 113) === false
    && tilePaintState('FanDuel', -110, {take_book:'Kalshi'}, 113).skip === false,
  polyOnPackRed: tilePaintState('Polymarket', 335, {take_book:'PLive'}, 306),
  polyJunkSkip: tilePaintState('Polymarket', -455, {take_book:'Kalshi'}, 163),
  bfJunkSkip: tilePaintState('Betfair Exchange', 567, {take_book:'Kalshi'}, 163),
  liqEmpty: formatLiquidityUsd(0),
}));
"""
    proc = subprocess.run(
        [node, "-e", snippet],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except json.JSONDecodeError:
        return None


def test_liq_ascii_dash():
    assert "if (!x || x <= 0 || Number.isNaN(x)) return '-'" in JS
    assert "Liq ${escapeHtml(liqBadge)}" in JS
    assert "\u2014" not in JS[JS.index("function formatLiquidityUsd") : JS.index("function toTitleCaseWords")]


def _class_tokens(class_string: str | None) -> set[str]:
    if not class_string:
        return set()
    return {tok for tok in class_string.split() if tok}


def test_tigers_paint_fixture():
    data = _eval_paint()
    if data is None:
        pytest.skip("node not available")
    t = data["tigers"]
    assert data["tigersTake"] == 369
    assert t["PLive"] == {"skip": False, "take": True, "better": False}
    assert t["Bet365"] == {"skip": False, "take": False, "better": True}
    assert t["NoVig"] == {"skip": False, "take": False, "better": True}
    assert t["Caesars"] == {"skip": False, "take": False, "better": False}
    assert t["DraftKings"] == {"skip": False, "take": False, "better": False}
    assert t["Kalshi"] == {"skip": False, "take": False, "better": False}
    assert data["tigersOrder"][0] == "PLive"
    assert data["noFallbackKalshi"] is False


def test_denny_tigers_minus15_paint_fixture():
    """Designer Denny: Tigers −1.5 away. Green is take (PLive +369), not the best price.

    Four locks in one UI/unit test:
    1. Away sign: Tigers are away on home hdp +1.5 → label −hdp (−1.5).
    2. Two-way POWER: sister required (plus-only must not invent EV).
    3. Totals still print: DET@MIN Over/Under 11.5 from PLive market 5.
    4. Paint: take green left / full color; red ONLY on-pack better; worse unshaded;
       Kalshi is not green; PLive is not gray/faded; Circa empty skips.
    """
    from ev_calculator import two_way_power_ev
    from odds_ev_monitor import _pick_qualifier_line_for_side
    from tests.test_plive_totals_alerts import (
        DET_MIN_EID,
        TOTALS_11,
        _totals_monitor,
        det_min_plive_totals_doc,
    )

    # 1. Away sign — Tigers −1.5 (label is −hdp, never the painted +1.5).
    pick, qual, line = _pick_qualifier_line_for_side(
        "Minnesota Twins",
        "Detroit Tigers",
        "Spread",
        "away",
        {"hdp": 1.5, "home": 1.28, "away": 4.69},
    )
    assert pick == "Detroit Tigers"
    assert line == -1.5
    assert qual == "-1.5"

    # 2. Two-way POWER still requires a sister. Do not drop this lock.
    assert two_way_power_ev(228, 0, 300) is None
    assert two_way_power_ev(240, 0, 300) is None

    # 3. Totals still print — DET@MIN Over/Under 11.5, PLive take, no Kalshi ticker.
    over, oq, ol = _pick_qualifier_line_for_side(
        "Minnesota Twins", "Detroit Tigers", "Totals", "over", TOTALS_11
    )
    under, uq, ul = _pick_qualifier_line_for_side(
        "Minnesota Twins", "Detroit Tigers", "Totals", "under", TOTALS_11
    )
    assert over == "Over" and under == "Under"
    assert oq == "11.5" and uq == "11.5"
    assert ol == 11.5 and ul == 11.5
    mon = _totals_monitor()
    vbs = mon.live_scan_value_bets_from_docs({DET_MIN_EID: det_min_plive_totals_doc()})
    plive_sides = {r.get("betSide") for r in vbs if r.get("_take_only") == "PLive"}
    assert "over" in plive_sides
    assert "under" in plive_sides

    # 4. Paint — createAlertCard class string + CSS. Green = take book, not best.
    data = _eval_paint()
    if data is None:
        pytest.skip("node not available")
    t = data["tigers"]
    classes = data["tigersClasses"]
    assert data["tigersTake"] == 369
    assert t["PLive"] == {"skip": False, "take": True, "better": False}
    assert t["Bet365"] == {"skip": False, "take": False, "better": True}
    assert t["NoVig"] == {"skip": False, "take": False, "better": True}
    assert t["Caesars"] == {"skip": False, "take": False, "better": False}
    assert t["DraftKings"] == {"skip": False, "take": False, "better": False}
    assert t["Kalshi"] == {"skip": False, "take": False, "better": False}
    assert t["Circa"] == {"skip": True, "take": False, "better": False}
    assert data["tigersOrder"][0] == "PLive"
    assert set(data["tigersGreen"]) == {"PLive"}
    assert set(data["tigersRed"]) == {"Bet365", "NoVig"}
    assert "Circa" not in data["tigersPainted"]

    plive_cls = _class_tokens(classes["PLive"])
    assert plive_cls == {"book-cell", "kalshi-book", "take-book"}
    assert "better-than-kalshi" not in plive_cls
    assert "grayed-out" not in plive_cls
    assert "junk-tile" not in plive_cls

    assert _class_tokens(classes["Bet365"]) == {"book-cell", "better-than-kalshi"}
    assert _class_tokens(classes["NoVig"]) == {"book-cell", "better-than-kalshi"}
    for worse in ("Caesars", "DraftKings", "Kalshi"):
        worse_cls = _class_tokens(classes[worse])
        assert worse_cls == {"book-cell"}, worse
        assert "kalshi-book" not in worse_cls
        assert "take-book" not in worse_cls
        assert "better-than-kalshi" not in worse_cls
        assert "grayed-out" not in worse_cls
    assert classes["Circa"] is None
    # Kalshi is a rec tile, not the take book — never auto-green.
    assert "Kalshi" not in data["tigersGreen"]
    # Best prices are red, not green. Green is the take book.
    assert "Bet365" not in data["tigersGreen"]
    assert "NoVig" not in data["tigersGreen"]

    cell = JS[JS.index("booksTableHtml += ") : JS.index("booksTableHtml += '</div></div>'")]
    assert "grayed-out" not in cell
    assert "junk-tile" not in cell
    assert "kalshi-book take-book" in cell
    assert "better-than-kalshi" in cell

    base = CSS[CSS.index(".book-cell {") : CSS.index(".book-cell:hover")]
    assert "opacity: 1" in base
    take_css = CSS[CSS.index(".book-cell.kalshi-book {") : CSS.index("body.dark .book-cell.kalshi-book")]
    assert "var(--accent)" in take_css
    assert "--accent: #1FD59A" in CSS
    red_css = CSS[
        CSS.index(".book-cell.better-than-kalshi {") : CSS.index(
            ".book-cell.grayed-out.better-than-kalshi"
        )
    ]
    assert "#ff4444" in red_css
    faded = CSS[CSS.index(".book-cell.grayed-out,") : CSS.index(".book-cell.grayed-out .book-logo")]
    assert "opacity: 0.4" in faded
    worse_css = CSS[CSS.index(".book-cell.worse-than-kalshi {") :]
    assert "opacity: 1" in worse_css.split("}", 1)[0]


def test_twins_plive_paint_poly_on_pack_red():
    data = _eval_paint()
    if data is None:
        pytest.skip("node not available")
    tw = data["twins"]
    assert tw["PLive"] == {"skip": False, "take": True, "better": False}
    assert tw["Polymarket"] == {"skip": False, "take": False, "better": True}
    assert tw["DraftKings"] == {"skip": False, "take": False, "better": False}
    assert tw["FanDuel"] == {"skip": False, "take": False, "better": False}
    assert tw["NoVig"] == {"skip": True, "take": False, "better": False}
    assert data["twinsOrder"][0] == "PLive"
    assert data["nvWorseJunkSkip"] == {"skip": True, "take": False, "better": False}
    assert data["junk10cSkipsWorse"] is True
    assert data["onPackPolyPaints"] == {"skip": False, "take": False, "better": False}
    assert data["junkPoly455Skip"] is True
    assert data["junkPolyUnicodeSkip"] is True
    assert data["junkPoly178Skip"] is True
    assert data["pickemNotJunk"] is True
    assert data["polyOnPackRed"] == {"skip": False, "take": False, "better": True}
    assert data["polyJunkSkip"]["skip"] is True
    assert data["bfJunkSkip"]["skip"] is True
    assert data["liqEmpty"] == "-"


def test_royals_poly_minus455_skip_paint_not_gray():
    """Royals ML +163: Poly −455 is junk (44c + sign-flip). Omit the tile. Keep rec-pack POWER."""
    data = _eval_paint()
    if data is None:
        pytest.skip("node not available")
    r = data["royals"]
    assert data["royalsTake"] == 163
    assert r["Kalshi"] == {"skip": False, "take": True, "better": False}
    assert r["Polymarket"] == {"skip": True, "take": False, "better": False}
    assert r["PLive"] == {"skip": False, "take": False, "better": False}
    cell = JS[JS.index("booksTableHtml += ") : JS.index("booksTableHtml += '</div></div>'")]
    assert "grayed-out" not in cell
    assert "junk-tile" not in cell
    assert r["Betfair Exchange"] == {"skip": False, "take": False, "better": False}
    assert r["FanDuel"] == {"skip": False, "take": False, "better": False}
    assert r["Caesars"] == {"skip": False, "take": False, "better": False}
    # |implied(+105)−implied(+163)| ≈ 10.8c and +100 ≈ 12c → junk, omit (not gray).
    assert r["DraftKings"]["skip"] is True
    assert r["Bet365"]["skip"] is True


def test_worse_css_full_opacity():
    cell = CSS[CSS.index(".book-cell {") : CSS.index(".book-cell:hover")]
    assert "opacity: 1" in cell


def test_unique_logos():
    assert "'Bet365': '/logos/Bet365.png'" in JS
    assert "'Betfair Exchange': '/logos/Betfair.png'" in JS
    assert "substring(0, 2)" not in JS
    assert "'NoVig': '/logos/NV.png'" in JS
    assert "'BetMGM': '/logos/BetMGM.png'" in JS
