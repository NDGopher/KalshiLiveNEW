"""Odds-API.io WebSocket unit tests — no live ODDS_API_KEY required."""
from __future__ import annotations

import os

import pytest

from odds_api_client import (
    DEFAULT_ODDS_API_BOOKMAKERS,
    _bookmaker_for_odds_request,
    api_wire_bookmakers,
    parse_odds_api_bookmakers,
    parse_odds_api_seq_header,
)
from odds_api_ws import (
    OddsWsStore,
    WsFilterError,
    bookmaker_list_mismatch,
    build_ws_url,
    mlb_ws_slice_active,
    odds_api_ws_wanted,
    redact_ws_url,
    ws_filters_from_env,
)


def test_default_ten_books_excludes_bookmaker_eu(monkeypatch):
    monkeypatch.delenv("ODDS_API_BOOKMAKERS", raising=False)
    books = parse_odds_api_bookmakers()
    assert books == [x.strip() for x in DEFAULT_ODDS_API_BOOKMAKERS.split(",")]
    assert len(books) == 10
    assert "BookMaker.eu" not in books
    assert "Betfair Exchange" in books
    assert "Kalshi" in books
    assert "PLive" not in books


def test_betfair_exchange_on_the_wire(monkeypatch):
    monkeypatch.delenv("ODDS_API_BETFAIR_REQUEST_NAME", raising=False)
    assert _bookmaker_for_odds_request("Betfair Exchange") == "Betfair Exchange"
    assert _bookmaker_for_odds_request("betfair") == "Betfair Exchange"


def test_betfair_request_name_is_override_only(monkeypatch):
    monkeypatch.setenv("ODDS_API_BETFAIR_REQUEST_NAME", "Betfair Sportsbook")
    assert _bookmaker_for_odds_request("Betfair Exchange") == "Betfair Sportsbook"


def test_plive_never_on_api_wire():
    assert api_wire_bookmakers(["Kalshi", "PLive", "FanDuel"]) == ["Kalshi", "FanDuel"]


def test_seq_header_parser():
    assert parse_odds_api_seq_header({"X-OddsAPI-Seq": "482900"}) == 482900
    assert parse_odds_api_seq_header({"x-oddsapi-seq": "7"}) == 7
    assert parse_odds_api_seq_header({}) is None


def test_ws_wanted_defaults_true_when_key(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-not-a-real-key")
    monkeypatch.delenv("ODDS_API_WS", raising=False)
    assert odds_api_ws_wanted() is True
    monkeypatch.setenv("ODDS_API_WS", "false")
    assert odds_api_ws_wanted() is False
    monkeypatch.delenv("ODDS_API_KEY")
    monkeypatch.setenv("ODDS_API_WS", "true")
    assert odds_api_ws_wanted() is False


def test_mlb_ws_filters_prematch_and_live(monkeypatch):
    monkeypatch.setenv("ODDS_API_SPORTS", "baseball")
    monkeypatch.setenv("ODDS_API_LEAGUE_MLB", "usa-mlb")
    monkeypatch.delenv("ODDS_API_WS_STATUS", raising=False)
    monkeypatch.delenv("ODDS_API_WS_LEAGUES", raising=False)
    monkeypatch.delenv("ODDS_API_WS_SPORT", raising=False)
    monkeypatch.delenv("ODDS_API_WS_EVENT_IDS", raising=False)
    assert mlb_ws_slice_active() is True
    f = ws_filters_from_env()
    assert f["sport"] == ["baseball"]
    assert f["leagues"] == ["usa-mlb"]
    assert f["eventIds"] == []
    assert f["status"] is None  # omit = prematch + live
    assert f["markets"] == ["ML", "Spread", "Totals"]
    assert "odds" in f["channels"]


def test_build_url_leagues_xor_event_ids():
    url = build_ws_url(
        "k",
        markets=["ML", "Spread", "Totals"],
        sport=["baseball"],
        leagues=["usa-mlb"],
        last_seq=99,
    )
    assert "apiKey=k" in url
    assert "sport=baseball" in url
    assert "leagues=usa-mlb" in url
    assert "markets=ML%2CSpread%2CTotals" in url or "markets=ML,Spread,Totals" in url
    assert "lastSeq=99" in url
    assert "status=" not in url.split("?")[1] or "status" not in url
    with pytest.raises(WsFilterError):
        build_ws_url("k", leagues=["usa-mlb"], event_ids=[1, 2])


def test_redact_api_key_in_url():
    url = build_ws_url("super-secret-key", markets=["ML"], sport=["baseball"])
    red = redact_ws_url(url)
    assert "super-secret-key" not in red
    assert "apiKey=%2A%2A%2A" in red or "apiKey=***" in red


def test_store_ml_only_update_keeps_totals():
    """Merge-by-market-name: created/updated must not replace the whole list.

    Subscribe ML,Spread,Totals is not this test. An ML-only updated payload
    must leave the stored Totals/Spread blocks (same over/under) in the store.
    """
    store = OddsWsStore()
    totals_odds = [{"max": 11.5, "hdp": 11.5, "over": 1.892857, "under": 1.847458}]
    store.apply_message(
        {
            "type": "created",
            "seq": 10,
            "id": "100",
            "bookie": "FanDuel",
            "markets": [
                {"name": "ML", "odds": [{"home": "1.9", "away": "2.0"}]},
                {
                    "name": "Spread",
                    "odds": [{"hdp": -1.5, "home": 1.91, "away": 1.91}],
                },
                {"name": "Totals", "odds": list(totals_odds)},
            ],
        }
    )
    store.apply_message(
        {
            "type": "updated",
            "seq": 11,
            "id": "100",
            "bookie": "FanDuel",
            "markets": [{"name": "ML", "odds": [{"home": "1.8", "away": "2.1"}]}],
        }
    )
    doc = store.merged_doc(100)
    fd = doc["bookmakers"]["FanDuel"]
    names = [m["name"] for m in fd]
    assert names.count("Totals") == 1
    assert "ML" in names
    assert "Totals" in names
    assert "Spread" in names
    ml = next(m for m in fd if m["name"] == "ML")
    tot = next(m for m in fd if m["name"] == "Totals")
    assert ml["odds"][0]["home"] == "1.8"
    assert tot["odds"][0]["over"] == 1.892857
    assert tot["odds"][0]["under"] == 1.847458
    counts = store.market_family_counts()
    assert counts["ml"] == 1
    assert counts["totals"] == 1
    assert counts["spread"] == 1
    assert store.last_seq == 11
    assert "FanDuel" in doc["book_updated_at"]
    assert store.book_updated_at[(100, "FanDuel")] == doc["book_updated_at"]["FanDuel"]


def test_same_family_alias_does_not_wipe_totals_name():
    """Family merge would drop Totals when a later payload says Total Runs."""
    store = OddsWsStore()
    store.apply_message(
        {
            "type": "created",
            "seq": 1,
            "id": 44,
            "bookie": "DraftKings",
            "markets": [
                {"name": "ML", "odds": [{"home": 1.9, "away": 2.0}]},
                {"name": "Totals", "odds": [{"max": 8.5, "over": 1.91, "under": 1.91}]},
            ],
        }
    )
    store.apply_message(
        {
            "type": "updated",
            "seq": 2,
            "id": 44,
            "bookie": "DraftKings",
            "markets": [
                {"name": "Total Runs", "odds": [{"max": 9.5, "over": 1.80, "under": 2.00}]},
            ],
        }
    )
    names = [m["name"] for m in store.merged_doc(44)["bookmakers"]["DraftKings"]]
    assert "Totals" in names
    assert "Total Runs" in names
    tot = next(m for m in store.merged_doc(44)["bookmakers"]["DraftKings"] if m["name"] == "Totals")
    assert tot["odds"][0]["max"] == 8.5


def test_rest_ml_only_snapshot_keeps_totals():
    """REST /odds ML-only row must not wipe Spread+Totals (same merge as WS)."""
    store = OddsWsStore()
    store.apply_rest_docs(
        [
            {
                "id": 200,
                "home": "Minnesota Twins",
                "away": "Detroit Tigers",
                "bookmakers": {
                    "FanDuel": [
                        {"name": "ML", "odds": [{"home": 1.9, "away": 2.0}]},
                        {"name": "Spread", "odds": [{"hdp": -1.5, "home": 1.91, "away": 1.91}]},
                        {"name": "Totals", "odds": [{"max": 11.5, "over": 1.91, "under": 1.91}]},
                    ]
                },
            }
        ]
    )
    store.apply_rest_docs(
        [
            {
                "id": 200,
                "bookmakers": {
                    "FanDuel": [{"name": "ML", "odds": [{"home": 1.85, "away": 2.05}]}],
                },
            }
        ]
    )
    names = [m["name"] for m in store.merged_doc(200)["bookmakers"]["FanDuel"]]
    assert "ML" in names
    assert "Totals" in names
    assert "Spread" in names


def test_totals_name_in_payload_replaces_totals_only():
    """Payload that includes Totals replaces Totals; ML/Spread stay."""
    store = OddsWsStore()
    store.apply_message(
        {
            "type": "created",
            "seq": 1,
            "id": 7,
            "bookie": "Caesars",
            "markets": [
                {"name": "ML", "odds": [{"home": 1.9, "away": 2.0}]},
                {"name": "Spread", "odds": [{"hdp": -1.5, "home": 1.91, "away": 1.91}]},
                {"name": "Totals", "odds": [{"max": 8.5, "over": 1.91, "under": 1.91}]},
            ],
        }
    )
    store.apply_message(
        {
            "type": "updated",
            "seq": 2,
            "id": 7,
            "bookie": "Caesars",
            "markets": [
                {"name": "Totals", "odds": [{"max": 9.5, "over": 1.80, "under": 2.05}]},
            ],
        }
    )
    names = [m["name"] for m in store.merged_doc(7)["bookmakers"]["Caesars"]]
    assert names.count("Totals") == 1
    assert "ML" in names and "Spread" in names
    tot = next(m for m in store.merged_doc(7)["bookmakers"]["Caesars"] if m["name"] == "Totals")
    assert tot["odds"][0]["max"] == 9.5


def test_store_deleted_and_no_markets():
    store = OddsWsStore()
    store.apply_message(
        {"type": "created", "seq": 1, "id": 5, "bookie": "Kalshi", "markets": [{"name": "ML"}]}
    )
    store.apply_message({"type": "no_markets", "seq": 2, "id": 5, "bookie": "Kalshi", "markets": []})
    assert store.merged_doc(5)["bookmakers"]["Kalshi"] == []
    store.apply_message({"type": "deleted", "seq": 3, "id": 5, "bookie": "Kalshi"})
    assert "Kalshi" not in store.merged_doc(5)["bookmakers"]


def test_resync_required_and_welcome_mismatch():
    store = OddsWsStore()
    applied = store.apply_message({"type": "resync_required", "reason": "replay_limit_exceeded"})
    assert applied.type == "resync_required"
    missing, extra = bookmaker_list_mismatch(["FanDuel"], ["FanDuel", "Kalshi"])
    assert missing == ["Kalshi"]
    assert extra == []


def test_rest_docs_merge_by_name_per_book():
    """REST snapshot upserts by market name. A Spread-only row must not wipe ML."""
    store = OddsWsStore()
    store.apply_rest_docs(
        [
            {
                "id": 9,
                "home": "Yankees",
                "away": "Red Sox",
                "bookmakers": {"Kalshi": [{"name": "ML"}]},
            }
        ]
    )
    store.apply_rest_docs(
        [
            {
                "id": 9,
                "bookmakers": {"Kalshi": [{"name": "Spread"}]},
            }
        ]
    )
    names = [m["name"] for m in store.merged_doc(9)["bookmakers"]["Kalshi"]]
    assert "Spread" in names
    assert "ML" in names
    assert store.merged_doc(9)["home"] == "Yankees"
