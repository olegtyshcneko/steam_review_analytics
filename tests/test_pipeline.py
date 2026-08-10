from __future__ import annotations

import pytest

from steam_market.domain import ReviewPage, ReviewSummary
from steam_market.pipeline import Pipeline


def review(rec_id: str):
    return {"recommendationid": rec_id, "language": "english", "review": "This is a sufficiently detailed fixture review.",
            "author": {"steamid": rec_id}, "timestamp_created": 1_700_000_000,
            "timestamp_updated": 1_700_000_000, "voted_up": True}


class FakeReviews:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.cursors = []

    async def get_page(self, appid, cursor, page_size):
        self.cursors.append(cursor)
        return next(self.pages)


@pytest.mark.asyncio
async def test_review_pagination_and_empty_final_page(settings, db):
    pipeline = Pipeline(settings, db)
    await pipeline.reviews.close()
    fake = FakeReviews([
        ReviewPage(summary=ReviewSummary(total_reviews=2), reviews=[review("1")], cursor="a+b="),
        ReviewPage(summary=ReviewSummary(total_reviews=2), reviews=[review("2")], cursor="last"),
        ReviewPage(summary=ReviewSummary(total_reviews=2), reviews=[], cursor="last"),
    ])
    pipeline.reviews = fake
    try:
        db.upsert_catalog_game(10, "Game")
        assert await pipeline.ingest_reviews(10) == 2
        assert fake.cursors == ["*", "a+b=", "last"]
        assert db.get_checkpoint("reviews:10") is None
        assert db.con.execute("SELECT count(*) FROM reviews").fetchone()[0] == 2
    finally:
        await pipeline.store.close(); await pipeline.spy.close(); await pipeline.llm.close()


@pytest.mark.asyncio
async def test_cursor_repeat_is_rejected(settings, db):
    pipeline = Pipeline(settings, db)
    await pipeline.reviews.close()
    pipeline.reviews = FakeReviews([ReviewPage(summary=ReviewSummary(total_reviews=1), reviews=[review("1")], cursor="*")])
    try:
        db.upsert_catalog_game(10, "Game")
        with pytest.raises(RuntimeError, match="non-advancing"):
            await pipeline.ingest_reviews(10)
    finally:
        await pipeline.store.close(); await pipeline.spy.close(); await pipeline.llm.close()


@pytest.mark.asyncio
async def test_resume_starts_at_saved_cursor(settings, db):
    db.upsert_catalog_game(10, "Game")
    db.checkpoint("reviews:10", "reviews", 10, {"cursor": "resume", "seen_cursors": ["*"]})
    pipeline = Pipeline(settings, db)
    await pipeline.reviews.close()
    fake = FakeReviews([ReviewPage(summary=ReviewSummary(total_reviews=0), reviews=[], cursor="done")])
    pipeline.reviews = fake
    try:
        assert await pipeline.ingest_reviews(10) == 0
        assert fake.cursors == ["resume"]
    finally:
        await pipeline.store.close(); await pipeline.spy.close(); await pipeline.llm.close()
