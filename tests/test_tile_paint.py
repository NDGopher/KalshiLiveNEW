"""Tile paint lock: take=green left, better=red, worse=unshaded visible.

Skip-paint is junk only (empty / sign-flip). Worse recs stay painted.
Anonymous boards. No ticker pins. EV math is not under test here.
"""
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
const spreadAlert = { take_book: 'PLive', book_price: -338 };
const spreadBooks = [
  {book:'Caesars', odds:-455},
  {book:'PLive', odds:-338},
  {book:'Polymarket', odds:-285},
  {book:'FanDuel', odds:-245},
  {book:'DraftKings', odds:-476},
  {book:'Kalshi', odds:-556},
  {book:'Circa', odds:0},
];
const spreadTake = takeAmericanFromAlert(spreadAlert, spreadBooks);
const spread = {};
for (const b of spreadBooks) {
  spread[b.book] = tilePaintState(b.book, b.odds, spreadAlert, spreadTake);
}
const twinsAlert = { take_book: 'PLive', book_price: 306 };
const twinsBooks = [
  {book:'DraftKings', odds:250},
  {book:'FanDuel', odds:280},
  {book:'NoVig', odds:178},
  {book:'Polymarket', odds:335},
  {book:'Bet365', odds:260},
  {book:'Caesars', odds:255},
  {book:'PLive', odds:306},
];
const twinsTake = takeAmericanFromAlert(twinsAlert, twinsBooks);
const twins = {};
for (const b of twinsBooks) {
  twins[b.book] = tilePaintState(b.book, b.odds, twinsAlert, twinsTake);
}
const twinsOrder = orderBooksTakeFirst(twinsBooks, twinsAlert).map((b) => b.book);
console.log(JSON.stringify({
  spreadTake,
  spread,
  twinsTake,
  twins,
  twinsOrder,
  liqEmpty: formatLiquidityUsd(0),
  liqMissing: formatLiquidityUsd(null),
  noFallbackKalshi: isCardTakeBook('Kalshi', {take_book:'PLive'}),
  missingTakeDoesNotGreenKalshi: isCardTakeBook('Kalshi', {}),
  kalshiTakeGreensKalshi: isCardTakeBook('Kalshi', {take_book:'Kalshi'}),
  oppSkip: tilePaintState('Polymarket', 170, {take_book:'PLive'}, -285).skip,
  junkSignFlipSkip: tilePaintState('Pinnacle', -455, {take_book:'Kalshi'}, 163).skip,
  nvWorseNotSkipped: tilePaintState('NoVig', 178, {take_book:'PLive'}, 306),
  junk10cDoesNotSkipWorse: isJunkVsKalshi(178, 306) === true
    && tilePaintState('NoVig', 178, {take_book:'PLive'}, 306).skip === false,
  onPackPolyPaints: tilePaintState('Polymarket', 158, {take_book:'Kalshi'}, 163),
  junkPoly455Skip: tilePaintState('Polymarket', -455, {take_book:'Kalshi'}, 163).skip,
  junkPoly178Skip: tilePaintState('Polymarket', -178, {take_book:'Kalshi'}, -104).skip,
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


def test_liq_badge_is_ascii_dash_not_emdash():
    assert "return '-'" in JS
    assert "Liq ${escapeHtml(liqBadge)}" in JS
    start = JS.index("function formatLiquidityUsd")
    end = JS.index("function toTitleCaseWords")
    chunk = JS[start:end]
    assert "\u2014" not in chunk
    assert "â€" not in chunk


def test_script_has_no_liq_emdash_literal():
    assert "Liq —" not in JS
    assert "Liq â€" not in JS
    assert "if (!x || x <= 0 || Number.isNaN(x)) return '-'" in JS
    card = JS[JS.index("function createAlertCard") : JS.index("function removeAlert")]
    assert "grayed-out" not in card
    assert "junk-tile" not in card
    assert "worse-than-kalshi" not in card


def test_paint_lock_plive_take_spread_example():
    data = _eval_paint()
    if data is None:
        pytest.skip("node not available to eval tilePaintState")
    assert data["spreadTake"] == -338
    s = data["spread"]
    assert s["PLive"] == {"skip": False, "take": True, "better": False}
    assert s["Polymarket"] == {"skip": False, "take": False, "better": True}
    assert s["FanDuel"] == {"skip": False, "take": False, "better": True}
    assert s["DraftKings"] == {"skip": False, "take": False, "better": False}
    assert s["Kalshi"] == {"skip": False, "take": False, "better": False}
    assert s["Caesars"] == {"skip": False, "take": False, "better": False}
    assert s["Circa"]["skip"] is True
    assert data["noFallbackKalshi"] is False
    assert data["missingTakeDoesNotGreenKalshi"] is False
    assert data["kalshiTakeGreensKalshi"] is True
    assert data["oppSkip"] is True
    assert data["junkSignFlipSkip"] is True
    assert data["liqEmpty"] == "-"
    assert data["liqMissing"] == "-"


def test_paint_lock_worse_recs_stay_visible_unshaded():
    """Twins -1.5: PLive +306 take green left; Poly +335 red; DK/FD/NV unshaded."""
    data = _eval_paint()
    if data is None:
        pytest.skip("node not available to eval tilePaintState")
    assert data["twinsTake"] == 306
    t = data["twins"]
    assert t["PLive"] == {"skip": False, "take": True, "better": False}
    assert t["Polymarket"] == {"skip": False, "take": False, "better": True}
    assert t["DraftKings"] == {"skip": False, "take": False, "better": False}
    assert t["FanDuel"] == {"skip": False, "take": False, "better": False}
    assert t["NoVig"] == {"skip": False, "take": False, "better": False}
    assert t["Bet365"] == {"skip": False, "take": False, "better": False}
    assert t["Caesars"] == {"skip": False, "take": False, "better": False}
    assert data["twinsOrder"][0] == "PLive"
    assert data["nvWorseNotSkipped"] == {"skip": False, "take": False, "better": False}
    assert data["junk10cDoesNotSkipWorse"] is True
    assert data["onPackPolyPaints"] == {"skip": False, "take": False, "better": False}
    assert data["junkPoly455Skip"] is True
    assert data["junkPoly178Skip"] is True


def test_worse_rec_css_is_full_opacity_not_red():
    cell = CSS[CSS.index(".book-cell {") : CSS.index(".book-cell:hover")]
    assert "opacity: 1" in cell
    worse = CSS[CSS.index(".book-cell.worse-than-kalshi") : CSS.index(".book-logo {")]
    assert "opacity: 1" in worse
    assert "#ff4444" not in worse


def test_logos_still_unique_not_be():
    assert "'Bet365': '/logos/Bet365.png'" in JS
    assert "'Betfair Exchange': '/logos/Betfair.png'" in JS
    assert "'BetMGM': '/logos/BetMGM.png'" in JS
    assert "'NoVig': '/logos/NV.png'" in JS
    assert "substring(0, 2)" not in JS
    assert "return 'B365'" in JS
    assert "return 'BFX'" in JS
    assert "return 'MGM'" in JS
    assert "return 'CZ'" in JS
    assert "return 'NV'" in JS
