from __future__ import annotations

import httpx
import pytest
import respx

from steam_market.sources import SourceError, SteamReviewSource, SteamSpySource, SteamStoreSource


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
