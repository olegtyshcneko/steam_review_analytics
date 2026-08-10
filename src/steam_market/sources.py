from __future__ import annotations

import asyncio
import random
import time
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .domain import CatalogGame, ReviewPage, ReviewSummary


USER_AGENT = "steam-review-analytics/0.1 (local research; contact via GitHub)"


class SourceError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class RateLimiter:
    def __init__(self, requests_per_second: float):
        self.interval = 1.0 / requests_per_second
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self.interval - (time.monotonic() - self._last)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


class HttpSource:
    def __init__(self, requests_per_second: float, max_retries: int, timeout: float = 30):
        self.limiter = RateLimiter(requests_per_second)
        self.max_retries = max_retries
        self.client = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True)

    async def close(self) -> None:
        await self.client.aclose()

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict:
        for attempt in range(self.max_retries + 1):
            await self.limiter.wait()
            try:
                response = await self.client.get(url, params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == self.max_retries:
                        raise SourceError(f"HTTP {response.status_code} from {url}", response.status_code)
                    retry_after = _retry_after(response.headers.get("Retry-After"))
                    await asyncio.sleep(retry_after if retry_after is not None else min(30, 2**attempt + random.random()))
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise SourceError(f"Expected JSON object from {url}")
                return payload
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == self.max_retries:
                    raise SourceError(f"Request failed for {url}: {exc}") from exc
                await asyncio.sleep(min(30, 2**attempt + random.random()))
            except (httpx.HTTPStatusError, ValueError) as exc:
                raise SourceError(f"Invalid response from {url}: {exc}") from exc
        raise AssertionError("unreachable")


class SteamReviewSource(HttpSource):
    URL = "https://store.steampowered.com/appreviews/{appid}"

    async def get_page(self, appid: int, cursor: str = "*", page_size: int = 100) -> ReviewPage:
        payload = await self.get_json(self.URL.format(appid=appid), params={
            "json": 1, "language": "all", "review_type": "all", "purchase_type": "all",
            "filter": "updated", "num_per_page": min(page_size, 100),
            "filter_offtopic_activity": 0, "cursor": cursor,
        })
        if payload.get("success") != 1:
            raise SourceError(f"Steam reviews returned success={payload.get('success')} for appid {appid}")
        summary = ReviewSummary.model_validate(payload.get("query_summary") or {})
        reviews = payload.get("reviews") or []
        if not isinstance(reviews, list):
            raise SourceError(f"Malformed reviews payload for appid {appid}")
        return ReviewPage(summary=summary, reviews=reviews, cursor=payload.get("cursor"))

    async def get_summary(self, appid: int) -> ReviewSummary:
        return (await self.get_page(appid, page_size=1)).summary


class SteamStoreSource(HttpSource):
    URL = "https://store.steampowered.com/api/appdetails"

    async def get_game(self, appid: int) -> dict | None:
        payload = await self.get_json(self.URL, params={"appids": appid, "l": "english"})
        entry = payload.get(str(appid)) or {}
        return entry.get("data") if entry.get("success") else None


class SteamSpySource(HttpSource):
    URL = "https://steamspy.com/api.php"

    async def catalog(self, pages: int = 1) -> list[CatalogGame]:
        games: list[CatalogGame] = []
        for page in range(pages):
            payload = await self.get_json(self.URL, params={"request": "all", "page": page})
            for key, raw in payload.items():
                if not isinstance(raw, dict):
                    continue
                appid = int(raw.get("appid") or key)
                games.append(CatalogGame(appid=appid, name=str(raw.get("name") or f"App {appid}"), source_metadata=raw))
        return games

    async def get_tags(self, appid: int) -> dict[str, int]:
        payload = await self.get_json(self.URL, params={"request": "appdetails", "appid": appid})
        tags = payload.get("tags") or {}
        return {str(k): int(v) for k, v in tags.items()} if isinstance(tags, dict) else {}


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            from datetime import datetime, timezone
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None
