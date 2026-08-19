from __future__ import annotations

import json

import httpx
import pytest
import respx

from games_analytics.platforms.base import SourceError
from games_analytics.platforms.app_store import AppStoreSource
from games_analytics.platforms.google_play import GooglePlaySource
from games_analytics.platforms.steam import SteamReviewSource, SteamSpySource, SteamStoreSource


@pytest.mark.asyncio
@respx.mock
async def test_review_summary_and_cursor_query():
    route = respx.get("https://store.steampowered.com/appreviews/10").mock(return_value=httpx.Response(200, json={
        "success": 1, "query_summary": {"total_reviews": 55, "total_positive": 50, "total_negative": 5},
        "reviews": [{"recommendationid": "1", "review": "text"}], "cursor": "a+b/c="}))
    source = SteamReviewSource(10_000, 0)
    try:
        page = await source.get_page(10, cursor="*", page_size=100)
    finally:
        await source.close()
    assert page.summary.total_reviews == 55
    assert page.cursor == "a+b/c="
    request = route.calls[0].request
    assert request.url.params["filter"] == "updated"
    assert request.url.params["num_per_page"] == "100"
    assert request.url.params["filter_offtopic_activity"] == "0"
    assert request.url.params["cursor"] == "*"


@pytest.mark.asyncio
@respx.mock
async def test_429_is_retried(monkeypatch):
    route = respx.get("https://store.steampowered.com/appreviews/10").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"success": 1, "query_summary": {}, "reviews": [], "cursor": "x"}),
    ])
    source = SteamReviewSource(10_000, 1)
    try:
        await source.get_page(10)
    finally:
        await source.close()
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_permanent_4xx_not_retried():
    route = respx.get("https://store.steampowered.com/appreviews/999").mock(return_value=httpx.Response(404))
    source = SteamReviewSource(10_000, 3)
    try:
        with pytest.raises(SourceError):
            await source.get_page(999)
    finally:
        await source.close()
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_malformed_review_payload():
    respx.get("https://store.steampowered.com/appreviews/10").mock(return_value=httpx.Response(200, json={
        "success": 1, "query_summary": {}, "reviews": {"bad": "shape"}}))
    source = SteamReviewSource(10_000, 0)
    try:
        with pytest.raises(SourceError, match="Malformed"):
            await source.get_page(10)
    finally:
        await source.close()


@pytest.mark.asyncio
@respx.mock
async def test_catalog_and_store_parsing():
    respx.get("https://steamspy.com/api.php").mock(return_value=httpx.Response(200, json={
        "10": {"appid": 10, "name": "Game", "positive": 12}}))
    respx.get("https://store.steampowered.com/api/appdetails").mock(return_value=httpx.Response(200, json={
        "10": {"success": True, "data": {"type": "game", "name": "Game"}}}))
    spy = SteamSpySource(10_000, 0)
    store = SteamStoreSource(10_000, 0)
    try:
        games = await spy.catalog()
        metadata = await store.get_game(10)
    finally:
        await spy.close(); await store.close()
    assert games[0].appid == 10
    assert metadata == {"type": "game", "name": "Game"}


@pytest.mark.asyncio
@respx.mock
async def test_google_play_product_and_review_parsing_omits_author_identity():
    respx.get("https://play.google.com/store/apps/details").mock(return_value=httpx.Response(
        200,
        text='<meta property="og:title" content="Fixture Game - Apps on Google Play">'
             '<meta property="og:description" content="A fixture game">',
    ))
    item = ["review-1", ["Private Name", "private-image"], 2, None,
            "The game crashes every time I open the inventory screen.", [1_700_000_000], 7,
            [None, "We are investigating.", [1_700_000_100]], None, None, "1.2.3"]
    payload = [[item], ["next-token"], []]
    envelope = [[None, None, json.dumps(payload)]]
    respx.post("https://play.google.com/_/PlayStoreUi/data/batchexecute").mock(
        return_value=httpx.Response(200, text=")]}'\n\n" + json.dumps(envelope)))
    source = GooglePlaySource(10_000, 0)
    try:
        product = await source.get_product("com.example.game")
        page = await source.get_reviews("com.example.game", count=1)
    finally:
        await source.close()
    assert product.name == "Fixture Game"
    assert page.next_cursor == "next-token"
    assert page.reviews[0].rating == 2
    assert "Private Name" not in json.dumps(page.reviews[0].raw_payload)


@pytest.mark.asyncio
@respx.mock
async def test_app_store_product_and_review_parsing_omits_author_identity():
    respx.get("https://itunes.apple.com/lookup").mock(return_value=httpx.Response(200, json={
        "resultCount": 1,
        "results": [{"trackName": "Fixture Game", "sellerName": "Studio", "description": "Desc"}],
    }))
    respx.get("https://itunes.apple.com/us/rss/customerreviews/page=1/id=123/sortby=mostrecent/json").mock(
        return_value=httpx.Response(200, json={"feed": {"entry": [{
            "id": {"label": "review-1"}, "author": {"name": {"label": "Private Name"}},
            "im:rating": {"label": "1"}, "title": {"label": "Broken"},
            "content": {"label": "The game crashes every time I open the inventory screen."},
            "im:version": {"label": "1.2.3"}, "updated": {"label": "2026-08-19T10:00:00Z"},
            "im:voteCount": {"label": "2"},
        }]}}))
    source = AppStoreSource(10_000, 0)
    try:
        product = await source.get_product("123")
        page = await source.get_reviews("123")
    finally:
        await source.close()
    assert product.name == "Fixture Game"
    assert page.reviews[0].rating == 1
    assert "Private Name" not in json.dumps(page.reviews[0].raw_payload)
