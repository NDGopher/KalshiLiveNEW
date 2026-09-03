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
const tigersTake = takeAmericanFromAlert(tigersAlert, tigersBooks);
const tigers = {};
for (const b of tigersBooks) tigers[b.book] = tilePaintState(b.book, b.odds, tigersAlert, tigersTake);
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
console.log(JSON.stringify({
  tigersTake, tigers, tigersOrder: orderBooksTakeFirst(tigersBooks, tigersAlert).map(b=>b.book),
  twinsTake, twins, twinsOrder: orderBooksTakeFirst(twinsBooks, twinsAlert).map(b=>b.book),
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


def test_twins_plive_paint_poly_on_pack_red():
    data = _eval_paint()
    if data is None:
        pytest.skip("node not available")
    tw = data["twins"]
    assert tw["PLive"] == {"skip": False, "take": True, "better": False}
    assert tw["Polymarket"] == {"skip": False, "take": False, "better": True}
    assert tw["DraftKings"] == {"skip": False, "take": False, "better": False}
    assert tw["FanDuel"] == {"skip": False, "take": False, "better": False}
    assert tw["NoVig"] == {"skip": False, "take": False, "better": False}
    assert data["twinsOrder"][0] == "PLive"
    assert data["nvWorseNotSkipped"] == {"skip": False, "take": False, "better": False}
    assert data["junk10cDoesNotSkipWorse"] is True
    assert data["onPackPolyPaints"] == {"skip": False, "take": False, "better": False}
    assert data["junkPoly455Skip"] is True
    assert data["junkPoly178Skip"] is True
    assert data["polyOnPackRed"] == {"skip": False, "take": False, "better": True}
    assert data["polyJunkSkip"]["skip"] is True
    assert data["bfJunkSkip"]["skip"] is True
    assert data["liqEmpty"] == "-"


def test_worse_css_full_opacity():
    cell = CSS[CSS.index(".book-cell {") : CSS.index(".book-cell:hover")]
    assert "opacity: 1" in cell


def test_unique_logos():
    assert "'Bet365': '/logos/Bet365.png'" in JS
    assert "'Betfair Exchange': '/logos/Betfair.png'" in JS
    assert "substring(0, 2)" not in JS
    assert "'NoVig': '/logos/NV.png'" in JS
    assert "'BetMGM': '/logos/BetMGM.png'" in JS
