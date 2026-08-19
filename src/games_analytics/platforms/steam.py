from __future__ import annotations

from ..domain import CatalogGame, ReviewPage, ReviewSummary
from .base import HttpSource, SourceError


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
