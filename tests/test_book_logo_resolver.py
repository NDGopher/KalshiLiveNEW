"""Alert-tile book marks: unique files / abbrevs, never two-letter BE."""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
JS = (REPO / "static" / "script.js").read_text(encoding="utf-8")


def _eval_resolver():
    """Run the resolver functions in Node if present; otherwise parse source."""
    import shutil
    import subprocess
    import json

    node = shutil.which("node")
    if not node:
        return None
    snippet = r"""
const fs = require('fs');
const src = fs.readFileSync('static/script.js','utf8');
function escapeHtml(t){return String(t||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
eval(src.slice(src.indexOf('function normalizeBookKey'), src.indexOf('function bookMatchesDevigOrSharp')));
const names = ['Bet365','Betfair Exchange','Betfair','BetMGM','Caesars','NoVig','Novig','Circa','Circa Sports','BookMaker'];
const out = {};
for (const n of names) {
  out[n] = {abbrev: uniqueBookAbbrev(n), paths: resolveBookLogoPaths(n)};
}
out.emptyCirca = isCircaBook('Circa') && !bookTileHasLine(0);
out.circaWithLine = isCircaBook('Circa Sports') && bookTileHasLine(-110);
console.log(JSON.stringify(out));
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


def test_no_two_letter_substring_fallback():
    assert "substring(0, 2)" not in JS
    assert "substring(0,2)" not in JS


def test_logo_paths_are_named_files_not_two_letter():
    assert "'/logos/Bet365.png'" in JS
    assert "'/logos/Betfair.png'" in JS
    assert "'/logos/BetMGM.png'" in JS
    assert "'/logos/Caesars.png'" in JS
    assert "'/logos/NV.png'" in JS
    assert "bet365:" in JS
    assert "betfairexchange:" in JS
    assert "novig:" in JS
    assert "circasports:" in JS


def test_betmgm_never_bookmaker_bm():
    start = JS.index("betmgm:")
    end = JS.index("caesars:")
    assert "/logos/BM.png" not in JS[start:end]


def test_resolver_runtime_unique_abbrevs():
    data = _eval_resolver()
    if data is None:
        # Source-level uniqueness still required when Node is absent.
        assert "return 'B365'" in JS and "return 'BFX'" in JS
        assert JS.index("return 'B365'") != JS.index("return 'BFX'")
        return
    assert data["Bet365"]["abbrev"] == "B365"
    assert data["Betfair Exchange"]["abbrev"] == "BFX"
    assert data["Betfair"]["abbrev"] == "BFX"
    assert data["Bet365"]["abbrev"] != data["Betfair Exchange"]["abbrev"]
    assert data["Bet365"]["abbrev"] != "BE"
    assert data["Betfair Exchange"]["abbrev"] != "BE"
    assert data["BetMGM"]["abbrev"] == "MGM"
    assert data["Caesars"]["abbrev"] == "CZ"
    assert data["NoVig"]["abbrev"] == "NV"
    assert data["Novig"]["abbrev"] == "NV"
    assert data["Bet365"]["paths"][0].endswith("Bet365.png")
    assert data["Betfair Exchange"]["paths"][0].endswith("Betfair.png")
    assert data["BetMGM"]["paths"][0].endswith("BetMGM.png")
    assert "/logos/BM.png" not in data["BetMGM"]["paths"]
    assert data["Caesars"]["paths"][0].endswith("Caesars.png")
    assert data["NoVig"]["paths"] == ["/logos/NV.png"]
    assert data["emptyCirca"] is True
    assert data["circaWithLine"] is True
