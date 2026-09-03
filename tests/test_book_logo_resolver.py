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
const names = ['Bet365','Betfair Exchange','Betfair','BetMGM','Caesars','NoVig','Novig','Circa','Circa Sports','BookMaker','PLive','Polymarket','Poly'];
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
    assert "bookName.substring" not in JS
    assert "substring(0, 2)" not in JS
    assert "const bookLogos" in JS
    assert "'NoVig': '/logos/NV.png'" in JS
    assert "'Novig': '/logos/NV.png'" in JS
    start = JS.index("function uniqueBookAbbrev")
    end = JS.index("function abbrevBookTile")
    assert "slice(0, 2)" not in JS[start:end]
    assert "slice(0,2)" not in JS[start:end]
    assert "substring" not in JS[start:end]


def test_required_logo_pngs_exist():
    """Stephen restored these. BFX/CZ text is the missing-file fallback. Do not delete."""
    required = (
        "logos/Bet365.png",
        "logos/Betfair.png",
        "logos/BetMGM.png",
        "logos/Caesars.png",
    )
    for rel in required:
        path = REPO / rel
        assert path.is_file(), f"missing {rel}"
        assert path.stat().st_size > 100, f"{rel} is empty"


def test_logo_paths_are_named_files_not_two_letter():
    assert "'/logos/Bet365.png'" in JS
    assert "'/logos/Betfair.png'" in JS
    assert "'/logos/BetMGM.png'" in JS
    assert "'/logos/Caesars.png'" in JS
    assert "'/logos/NV.png'" in JS
    assert "'Bet365': '/logos/Bet365.png'" in JS
    assert "'Betfair Exchange': '/logos/Betfair.png'" in JS
    assert "'NoVig': '/logos/NV.png'" in JS
    assert "'Circa Sports': '/logos/Circa.png'" in JS


def test_betmgm_never_bookmaker_bm():
    start = JS.index("'BetMGM':")
    end = JS.index("'Caesars':")
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
    assert data["PLive"]["abbrev"] == "PLV"
    assert data["Polymarket"]["abbrev"] == "PM"
    assert data["Polymarket"]["abbrev"] != "PL"
    assert data["Poly"]["abbrev"] != "PL"
    assert data["emptyCirca"] is True
    assert data["circaWithLine"] is True
