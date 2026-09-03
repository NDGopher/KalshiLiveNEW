"""PLive is a take venue, never a sharp / minSharp / devig book."""
from __future__ import annotations

from pathlib import Path

from ev_calculator import american_to_decimal
from odds_ev_monitor import (
    OddsEVMonitor,
    _build_display_books_payload,
    fair_sharp_names,
    is_betting_take_book,
)


def test_fair_pack_excludes_plive_and_take_venue():
    pack = [
        "DraftKings",
        "FanDuel",
        "NoVig",
        "Bet365",
        "Betfair Exchange",
        "BetMGM",
        "Caesars",
        "Circa",
        "Polymarket",
        "Kalshi",
        "PLive",
    ]
    kalshi_fair = fair_sharp_names(pack, "Kalshi")
    names = {n.lower() for n in kalshi_fair}
    assert "plive" not in names
    assert "kalshi" not in names
    assert "draftkings" in names
    assert "novig" in names
    plive_fair = fair_sharp_names(pack, "PLive")
    plive_names = {n.lower() for n in plive_fair}
    assert "plive" not in plive_names
    # PLive cards may use Kalshi in fair/devig. Kalshi cards never use PLive.
    assert "kalshi" in plive_names
    assert is_betting_take_book("PLive")
    assert is_betting_take_book("Kalshi")
    assert not is_betting_take_book("FanDuel")


def test_display_payload_puts_take_book_first():
    bks = {
        "FanDuel": [{"name": "ML", "odds": [{"home": 1.8, "away": 2.1}]}],
        "PLive": [{"name": "ML", "odds": [{"home": 1.91, "away": 1.95}]}],
        "Kalshi": [{"name": "ML", "odds": [{"home": 1.85, "away": 2.05}]}],
    }
    kalshi = _build_display_books_payload(
        "Yankees", bks, "ML", "home", ["Kalshi", "FanDuel", "PLive"], -118, {}, take_book="Kalshi"
    )
    books = [r["book"] for r in kalshi["Yankees"]]
    assert books[0] == "Kalshi"
    plive = _build_display_books_payload(
        "Yankees", bks, "ML", "home", ["Kalshi", "FanDuel", "PLive"], -110, {}, take_book="PLive"
    )
    pbooks = [r["book"] for r in plive["Yankees"]]
    assert pbooks[0] == "PLive"
    assert "PLive" not in pbooks[1:]


def test_plive_take_emits_when_plus_vs_pack():
    mon = OddsEVMonitor(auth_token=None)
    mon.set_filter(
        {
            "betTypes": ["GAMELINES"],
            "minRoi": 0,
            "devigFilter": {
                "sharps": ["FanDuel", "DraftKings", "NoVig"],
                "method": "POWER",
                "type": "AVERAGE",
                "minEv": 0,
                "minSharpBooks": 3,
                "hold": [{"book": "Any", "max": 20}],
            },
            "oddsRanges": [{"book": "Any", "min": -500, "max": 500}],
            "minLimits": [{"book": "Kalshi", "min": 0}],
            "minSharpLimits": [],
            "displayBooks": ["Kalshi", "FanDuel", "DraftKings", "NoVig", "PLive"],
        }
    )
    # Pack around -139/-141/-142. PLive -120 is +EV vs that favorite pack.
    k_dec = american_to_decimal(-133)
    pl_dec = american_to_decimal(-120)
    fd = american_to_decimal(-142)
    dk = american_to_decimal(-141)
    nv = american_to_decimal(-139)
    opp = american_to_decimal(125)
    vb = {
        "event": {"home": "Houston Astros", "away": "Chicago White Sox", "league": "MLB"},
        "market": {"name": "ML", "home": k_dec, "away": opp},
        "betSide": "home",
        "bookmakerOdds": {"home": k_dec, "away": opp, "href": "https://kalshi.com/markets/KXTEST"},
        "expectedValue": 0.0,
    }
    odds_doc = {
        "id": 1,
        "home": "Houston Astros",
        "away": "Chicago White Sox",
        "bookmakers": {
            "Kalshi": [{"name": "ML", "odds": [{"home": k_dec, "away": opp}]}],
            "PLive": [{"name": "ML", "odds": [{"home": pl_dec, "away": opp}]}],
            "FanDuel": [{"name": "ML", "odds": [{"home": fd, "away": opp}]}],
            "DraftKings": [{"name": "ML", "odds": [{"home": dk, "away": opp}]}],
            "NoVig": [{"name": "ML", "odds": [{"home": nv, "away": opp}]}],
        },
    }
    kalshi = mon._value_bet_to_normalized_bet(vb, odds_doc, take_book="Kalshi")
    plive = mon._value_bet_to_normalized_bet(vb, odds_doc, take_book="PLive")
    assert plive is not None
    assert plive["take_book"] == "PLive"
    assert plive["ev"] > 0
    assert "PLive" not in (plive.get("devigBooks") or [])
    assert all(str(n).lower() != "plive" for n in (plive.get("devigBooks") or []))
    left = (plive["displayBooks"][plive["selection"]] or [])[0]
    assert left["book"] == "PLive"
    if kalshi is not None:
        assert "PLive" not in (kalshi.get("devigBooks") or [])
        assert all(str(n).lower() != "kalshi" for n in (kalshi.get("devigBooks") or []))
    assert any(str(n).lower() == "kalshi" for n in (plive.get("devigBooks") or []))


def test_plive_missing_price_does_not_emit():
    mon = OddsEVMonitor(auth_token=None)
    mon.set_filter(
        {
            "betTypes": ["GAMELINES"],
            "minRoi": 0,
            "devigFilter": {
                "sharps": ["FanDuel", "DraftKings", "NoVig"],
                "method": "POWER",
                "type": "AVERAGE",
                "minEv": 0,
                "minSharpBooks": 3,
            },
            "oddsRanges": [{"book": "Any", "min": -500, "max": 500}],
            "minLimits": [{"book": "Kalshi", "min": 0}],
            "displayBooks": ["Kalshi", "FanDuel"],
        }
    )
    k_dec = american_to_decimal(-110)
    opp = american_to_decimal(-110)
    vb = {
        "event": {"home": "A", "away": "B", "league": "MLB"},
        "market": {"name": "ML", "home": k_dec, "away": opp},
        "betSide": "home",
        "bookmakerOdds": {"home": k_dec, "away": opp},
    }
    odds_doc = {
        "bookmakers": {
            "Kalshi": [{"name": "ML", "odds": [{"home": k_dec, "away": opp}]}],
            "FanDuel": [{"name": "ML", "odds": [{"home": k_dec, "away": opp}]}],
        }
    }
    assert mon._value_bet_to_normalized_bet(vb, odds_doc, take_book="PLive") is None


def test_dashboard_plive_take_is_display_only():
    src = (Path(__file__).resolve().parents[1] / "dashboard.py").read_text(encoding="utf-8")
    assert "handle_plive_take_display_alert" in src
    assert "PLive take cards are display-only" in src
    assert 'take_part = f"|{take_book}"' in src
    assert "auto_bet_enabled = False" in src
    assert "sharps_list.append(_extra_bk)" not in src
