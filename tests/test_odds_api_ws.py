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


def test_store_replaces_markets_never_merges():
    store = OddsWsStore()
    store.apply_message(
        {
            "type": "created",
            "seq": 10,
            "id": "100",
            "bookie": "FanDuel",
            "markets": [{"name": "ML", "odds": [{"home": "1.9", "away": "2.0"}]}, {"name": "Totals"}],
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
    assert names == ["ML"]  # Totals gone — replace, not merge
    assert fd[0]["odds"][0]["home"] == "1.8"
    assert store.last_seq == 11
    assert "FanDuel" in doc["book_updated_at"]
    assert store.book_updated_at[(100, "FanDuel")] == doc["book_updated_at"]["FanDuel"]


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


def test_rest_docs_replace_per_book():
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
    assert [m["name"] for m in store.merged_doc(9)["bookmakers"]["Kalshi"]] == ["Spread"]
    assert store.merged_doc(9)["home"] == "Yankees"
