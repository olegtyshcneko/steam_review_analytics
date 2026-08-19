from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from typing import Any

from ..domain import StoreProduct, StoreReview, StoreReviewPage
from .base import HttpSource, SourceError


class GooglePlaySource(HttpSource):
    """Best-effort collector for public Google Play storefront data."""

    DETAILS_URL = "https://play.google.com/store/apps/details"
    REVIEWS_URL = "https://play.google.com/_/PlayStoreUi/data/batchexecute"

    async def get_product(self, package_name: str, language: str = "en", country: str = "us") -> StoreProduct:
        text = await self.request_text(
            "GET", self.DETAILS_URL, params={"id": package_name, "hl": language, "gl": country}
        )
        title = _meta_content(text, "og:title")
        if not title:
            raise SourceError(f"Google Play app not found or missing title: {package_name}")
        description = _meta_content(text, "og:description")
        return StoreProduct(
            platform="google_play",
            product_id=package_name,
            name=re.sub(r"\s*-\s*Apps on Google Play\s*$", "", title),
            description=description,
            url=f"{self.DETAILS_URL}?id={package_name}",
            metadata={"language": language, "country": country},
        )

    async def get_reviews(
        self,
        package_name: str,
        *,
        language: str = "en",
        country: str = "us",
        count: int = 100,
        cursor: str | None = None,
        sort: int = 2,
    ) -> StoreReviewPage:
        count = min(max(count, 1), 500)
        page_spec: list[Any] = [count]
        if cursor:
            page_spec.extend([None, cursor])
        inner = [None, [2, sort, page_spec, None, [None, None, None, None, None, None, None, None, None]], [package_name, 7]]
        request = [[["oCPfdb", json.dumps(inner, separators=(",", ":")), None, "generic"]]]
        text = await self.request_text(
            "POST",
            self.REVIEWS_URL,
            params={"hl": language, "gl": country},
            data={"f.req": json.dumps(request, separators=(",", ":"))},
        )
        try:
            body = text.split("\n\n", 1)[1]
            envelope = json.loads(body)
            payload = json.loads(envelope[0][2])
            items = payload[0] or []
            next_cursor = payload[-2][-1]
            if not isinstance(next_cursor, str):
                next_cursor = None
        except (IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SourceError(f"Malformed Google Play review response for {package_name}") from exc

        reviews = [_parse_review(item, language) for item in items if isinstance(item, list)]
        return StoreReviewPage(reviews=reviews, next_cursor=next_cursor)


def _value(container: Any, *path: int) -> Any:
    try:
        for index in path:
            container = container[index]
        return container
    except (IndexError, TypeError):
        return None


def _parse_review(item: list[Any], language: str) -> StoreReview:
    created = _timestamp(_value(item, 5, 0))
    replied = _timestamp(_value(item, 7, 2, 0))
    raw = {
        "review_id": _value(item, 0),
        "score": _value(item, 2),
        "content": _value(item, 4),
        "created_at": _value(item, 5, 0),
        "thumbs_up": _value(item, 6),
        "developer_response": _value(item, 7, 1),
        "developer_response_at": _value(item, 7, 2, 0),
        "app_version": _value(item, 10),
        "language": language,
    }
    return StoreReview(
        review_id=str(raw["review_id"]),
        text=str(raw["content"] or ""),
        rating=int(raw["score"]),
        language=language,
        created_at=created,
        updated_at=replied or created,
        app_version=raw["app_version"],
        votes_up=int(raw["thumbs_up"] or 0),
        developer_response=raw["developer_response"],
        raw_payload=raw,
    )


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value, timezone.utc).replace(tzinfo=None)


def _meta_content(document: str, property_name: str) -> str | None:
    patterns = (
        rf'<meta[^>]+property=["\']{re.escape(property_name)}["\'][^>]+content=["\']([^"\']+)',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(property_name)}["\']',
    )
    for pattern in patterns:
        if match := re.search(pattern, document, flags=re.IGNORECASE):
            return html.unescape(match.group(1)).strip()
    return None
