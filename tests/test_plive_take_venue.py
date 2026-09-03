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
    assert "polymarket" in names
    plive_fair = fair_sharp_names(pack, "PLive")
    plive_names = {n.lower() for n in plive_fair}
    assert "plive" not in plive_names
    # PLive cards may use Kalshi in fair/devig. Kalshi cards never use PLive.
    assert "kalshi" in plive_names
    assert "polymarket" in plive_names
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


def _gameline_monitor():
    mon = OddsEVMonitor(auth_token=None)
    mon.set_filter(
        {
            "betTypes": ["GAMELINES"],
            "minRoi": 0,
            "devigFilter": {
                "sharps": [
                    "FanDuel",
                    "DraftKings",
                    "NoVig",
                    "Caesars",
                    "Betfair Exchange",
                    "Polymarket",
                ],
                "method": "POWER",
                "type": "AVERAGE",
                "minEv": 0,
                "minSharpBooks": 3,
                "hold": [{"book": "Any", "max": 20}],
            },
            "oddsRanges": [{"book": "Any", "min": -500, "max": 500}],
            "minLimits": [{"book": "Kalshi", "min": 0}],
            "minSharpLimits": [],
            "displayBooks": [
                "Kalshi",
                "FanDuel",
                "DraftKings",
                "NoVig",
                "Caesars",
                "Betfair Exchange",
                "Polymarket",
                "PLive",
            ],
        }
    )
    return mon


def _ml_vb(home_dec: float, away_dec: float):
    return {
        "event": {"home": "Houston Astros", "away": "Chicago White Sox", "league": "MLB"},
        "market": {"name": "ML", "home": home_dec, "away": away_dec},
        "betSide": "home",
        "bookmakerOdds": {
            "home": home_dec,
            "away": away_dec,
            "href": "https://kalshi.com/markets/KXTEST",
        },
        "expectedValue": 0.0,
    }


def test_display_payload_on_pack_poly_paints_junk_poly_skipped():
    take_am = 163
    opp = american_to_decimal(-180)
    on_pack = _build_display_books_payload(
        "Royals",
        {
            "FanDuel": [{"name": "ML", "odds": [{"home": american_to_decimal(156), "away": opp}]}],
            "Polymarket": [{"name": "ML", "odds": [{"home": american_to_decimal(158), "away": opp}]}],
        },
        "ML",
        "home",
        ["Kalshi", "FanDuel", "Polymarket"],
        take_am,
        {},
        take_book="Kalshi",
    )
    painted = [r["book"] for r in on_pack["Royals"]]
    assert any("poly" in n.lower() for n in painted)
    junk = _build_display_books_payload(
        "Royals",
        {
            "FanDuel": [{"name": "ML", "odds": [{"home": american_to_decimal(156), "away": opp}]}],
            "Polymarket": [{"name": "ML", "odds": [{"home": american_to_decimal(-455), "away": opp}]}],
        },
        "ML",
        "home",
        ["Kalshi", "FanDuel", "Polymarket"],
        take_am,
        {},
        take_book="Kalshi",
    )
    skipped = [r["book"] for r in junk["Royals"]]
    assert all("poly" not in n.lower() for n in skipped)


def test_astros_ml_two_exchange_card_suppressed():
    """Astros ML −204 vs BF −163 and NV −130 → card does not emit."""
    mon = _gameline_monitor()
    take = american_to_decimal(-204)
    opp = american_to_decimal(170)
    vb = _ml_vb(take, opp)
    odds_doc = {
        "id": 1,
        "home": "Houston Astros",
        "away": "Chicago White Sox",
        "bookmakers": {
            "Kalshi": [{"name": "ML", "odds": [{"home": take, "away": opp}]}],
            "FanDuel": [{"name": "ML", "odds": [{"home": american_to_decimal(-200), "away": opp}]}],
            "DraftKings": [{"name": "ML", "odds": [{"home": american_to_decimal(-198), "away": opp}]}],
            "Caesars": [{"name": "ML", "odds": [{"home": american_to_decimal(-195), "away": opp}]}],
            "Betfair Exchange": [{"name": "ML", "odds": [{"home": american_to_decimal(-163), "away": opp}]}],
            "NoVig": [{"name": "ML", "odds": [{"home": american_to_decimal(-130), "away": opp}]}],
        },
    }
    assert mon._value_bet_to_normalized_bet(vb, odds_doc, take_book="Kalshi") is None


def test_junk_nv_bf_and_soft_better_still_emit():
    """Junk NV +317 / BF +567 do not hide. DK/FD/CZ on a KEEP board still emit."""
    mon = _gameline_monitor()
    take = american_to_decimal(144)
    opp = american_to_decimal(-164)
    vb = _ml_vb(take, opp)
    odds_doc = {
        "id": 2,
        "home": "Minnesota Twins",
        "away": "Milwaukee Brewers",
        "bookmakers": {
            "Kalshi": [{"name": "ML", "odds": [{"home": take, "away": opp}]}],
            "FanDuel": [{"name": "ML", "odds": [{"home": american_to_decimal(110), "away": opp}]}],
            "DraftKings": [{"name": "ML", "odds": [{"home": american_to_decimal(115), "away": opp}]}],
            "Caesars": [{"name": "ML", "odds": [{"home": american_to_decimal(100), "away": opp}]}],
            "NoVig": [{"name": "ML", "odds": [{"home": american_to_decimal(317), "away": opp}]}],
            "Betfair Exchange": [{"name": "ML", "odds": [{"home": american_to_decimal(567), "away": opp}]}],
        },
    }
    bet = mon._value_bet_to_normalized_bet(vb, odds_doc, take_book="Kalshi")
    assert bet is not None
    assert bet["ev"] > 0
    tiles = [r["book"] for r in (bet.get("displayBooks") or {}).get(bet["selection"], [])]
    assert all("poly" not in n.lower() for n in tiles)


def test_twins_plus144_keep_emits():
    mon = _gameline_monitor()
    take = american_to_decimal(144)
    opp = american_to_decimal(-164)
    vb = _ml_vb(take, opp)
    odds_doc = {
        "id": 3,
        "home": "Minnesota Twins",
        "away": "Milwaukee Brewers",
        "bookmakers": {
            "Kalshi": [{"name": "ML", "odds": [{"home": take, "away": opp}]}],
            "FanDuel": [{"name": "ML", "odds": [{"home": american_to_decimal(110), "away": opp}]}],
            "DraftKings": [{"name": "ML", "odds": [{"home": american_to_decimal(115), "away": opp}]}],
            "Caesars": [{"name": "ML", "odds": [{"home": american_to_decimal(100), "away": opp}]}],
            "NoVig": [{"name": "ML", "odds": [{"home": american_to_decimal(90), "away": opp}]}],
            "Betfair Exchange": [{"name": "ML", "odds": [{"home": american_to_decimal(80), "away": opp}]}],
        },
    }
    bet = mon._value_bet_to_normalized_bet(vb, odds_doc, take_book="Kalshi")
    assert bet is not None
    assert bet["ev"] > 0


def test_on_pack_poly_in_devig_and_display():
    mon = _gameline_monitor()
    take = american_to_decimal(317)
    opp = american_to_decimal(-413)
    vb = _ml_vb(take, opp)
    odds_doc = {
        "id": 4,
        "home": "Minnesota Twins",
        "away": "Milwaukee Brewers",
        "bookmakers": {
            "Kalshi": [{"name": "ML", "odds": [{"home": take, "away": opp}]}],
            "FanDuel": [{"name": "ML", "odds": [{"home": american_to_decimal(270), "away": american_to_decimal(-344)}]}],
            "DraftKings": [{"name": "ML", "odds": [{"home": american_to_decimal(252), "away": american_to_decimal(-318)}]}],
            "NoVig": [{"name": "ML", "odds": [{"home": american_to_decimal(260), "away": american_to_decimal(-330)}]}],
            "Polymarket": [{"name": "ML", "odds": [{"home": american_to_decimal(265), "away": american_to_decimal(-337)}]}],
        },
    }
    bet = mon._value_bet_to_normalized_bet(vb, odds_doc, take_book="Kalshi")
    assert bet is not None
    assert any("poly" in str(n).lower() for n in (bet.get("devigBooks") or []))
    tiles = [r["book"] for r in (bet.get("displayBooks") or {}).get(bet["selection"], [])]
    assert any("poly" in n.lower() for n in tiles)


def test_royals_ml_plus163_omits_junk_poly_keeps_plive_tile():
    """Screenshot board: Poly −455 omitted. PLive +118 on-pack paints. Rec pack stays."""
    take_am = 163
    opp = american_to_decimal(-180)
    payload = _build_display_books_payload(
        "Royals",
        {
            "Betfair Exchange": [{"name": "ML", "odds": [{"home": american_to_decimal(134), "away": opp}]}],
            "DraftKings": [{"name": "ML", "odds": [{"home": american_to_decimal(105), "away": opp}]}],
            "FanDuel": [{"name": "ML", "odds": [{"home": american_to_decimal(116), "away": opp}]}],
            "Bet365": [{"name": "ML", "odds": [{"home": american_to_decimal(100), "away": opp}]}],
            "Caesars": [{"name": "ML", "odds": [{"home": american_to_decimal(110), "away": opp}]}],
            "PLive": [{"name": "ML", "odds": [{"home": american_to_decimal(118), "away": opp}]}],
            "Polymarket": [{"name": "ML", "odds": [{"home": american_to_decimal(-455), "away": opp}]}],
            "Pinnacle": [{"name": "ML", "odds": [{"home": american_to_decimal(-455), "away": opp}]}],
        },
        "ML",
        "home",
        ["Kalshi", "Betfair Exchange", "DraftKings", "FanDuel", "Bet365", "Caesars", "PLive", "Polymarket", "Pinnacle"],
        take_am,
        {},
        take_book="Kalshi",
    )
    names = [r["book"] for r in payload["Royals"]]
    assert all("poly" not in n.lower() for n in names)
    assert all("pinnacle" not in n.lower() for n in names)
    assert any(n == "PLive" for n in names)
    assert "Betfair Exchange" in names


def test_junk_poly_minus455_vs_plus163_no_tile_no_power():
    mon = _gameline_monitor()
    take = american_to_decimal(317)
    opp = american_to_decimal(-413)
    vb = _ml_vb(take, opp)
    odds_doc = {
        "id": 5,
        "home": "Kansas City Royals",
        "away": "Miami Marlins",
        "bookmakers": {
            "Kalshi": [{"name": "ML", "odds": [{"home": take, "away": opp}]}],
            "FanDuel": [{"name": "ML", "odds": [{"home": american_to_decimal(270), "away": american_to_decimal(-344)}]}],
            "DraftKings": [{"name": "ML", "odds": [{"home": american_to_decimal(252), "away": american_to_decimal(-318)}]}],
            "NoVig": [{"name": "ML", "odds": [{"home": american_to_decimal(260), "away": american_to_decimal(-330)}]}],
            "Polymarket": [{"name": "ML", "odds": [{"home": american_to_decimal(-455), "away": american_to_decimal(350)}]}],
        },
    }
    bet = mon._value_bet_to_normalized_bet(vb, odds_doc, take_book="Kalshi")
    assert bet is not None
    assert all("poly" not in str(n).lower() for n in (bet.get("devigBooks") or []))
    tiles = [r["book"] for r in (bet.get("displayBooks") or {}).get(bet["selection"], [])]
    assert all("poly" not in n.lower() for n in tiles)


def test_dashboard_plive_take_is_not_display_only():
    src = (Path(__file__).resolve().parents[1] / "dashboard.py").read_text(encoding="utf-8")
    assert "handle_plive_take_display_alert" in src
    assert "PLive take cards are display-only" not in src
    assert "same keep/kill as Kalshi" in src
    assert "PLive take does not auto-bet on Kalshi" in src
    assert 'take_part = f"|{take_book}"' in src
    assert "auto_bet_enabled = False" in src
    assert "sharps_list.append(_extra_bk)" not in src
