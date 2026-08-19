from __future__ import annotations

from datetime import datetime
from typing import Any

from ..domain import StoreProduct, StoreReview, StoreReviewPage
from .base import HttpSource, SourceError


class AppStoreSource(HttpSource):
    """Best-effort collector for public Apple App Store storefront data."""

    LOOKUP_URL = "https://itunes.apple.com/lookup"
    REVIEWS_URL = "https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={app_id}/sortby=mostrecent/json"

    async def get_product(self, app_id: str, country: str = "us") -> StoreProduct:
        payload = await self.get_json(self.LOOKUP_URL, params={"id": app_id, "country": country})
        results = payload.get("results") or []
        if not results or not isinstance(results[0], dict):
            raise SourceError(f"Apple App Store app not found: {app_id}")
        item = results[0]
        return StoreProduct(
            platform="app_store",
            product_id=str(app_id),
            name=str(item.get("trackName") or f"App {app_id}"),
            developer=item.get("sellerName") or item.get("artistName"),
            description=item.get("description"),
            url=item.get("trackViewUrl"),
            metadata={key: value for key, value in item.items() if key not in {"description"}},
        )

    async def get_reviews(self, app_id: str, *, country: str = "us", page: int = 1) -> StoreReviewPage:
        if not 1 <= page <= 10:
            raise ValueError("Apple public review feed supports pages 1 through 10 per storefront")
        url = self.REVIEWS_URL.format(country=country.lower(), page=page, app_id=app_id)
        payload = await self.get_json(url)
        entries = (payload.get("feed") or {}).get("entry") or []
        if not isinstance(entries, list):
            raise SourceError(f"Malformed Apple App Store review response for {app_id}")
        reviews = [_parse_review(entry, country) for entry in entries if isinstance(entry, dict)]
        next_cursor = str(page + 1) if len(reviews) >= 50 and page < 10 else None
        return StoreReviewPage(reviews=reviews, next_cursor=next_cursor)


def _label(entry: dict[str, Any], key: str) -> Any:
    value = entry.get(key)
    return value.get("label") if isinstance(value, dict) else None


def _parse_review(entry: dict[str, Any], country: str) -> StoreReview:
    updated = _datetime(_label(entry, "updated"))
    raw = {
        "review_id": _label(entry, "id"),
        "rating": _label(entry, "im:rating"),
        "title": _label(entry, "title"),
        "content": _label(entry, "content"),
        "version": _label(entry, "im:version"),
        "updated": _label(entry, "updated"),
        "vote_count": _label(entry, "im:voteCount"),
        "vote_sum": _label(entry, "im:voteSum"),
        "storefront": country.lower(),
    }
    return StoreReview(
        review_id=str(raw["review_id"]),
        text=str(raw["content"] or ""),
        rating=int(raw["rating"]),
        title=raw["title"],
        language=None,
        created_at=updated,
        updated_at=updated,
        app_version=raw["version"],
        votes_up=int(raw["vote_count"] or 0),
        raw_payload=raw,
    )


def _datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None
