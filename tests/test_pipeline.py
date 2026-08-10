from __future__ import annotations

import pytest

from steam_market.domain import Aspect, ReviewEnrichment, ReviewPage, ReviewSummary
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


class FakeLLM:
    def __init__(self):
        self.batch_sizes = []

    async def enrich_reviews(self, rows, aspects):
        self.batch_sizes.append(len(rows))
        return {rec_id: ReviewEnrichment(sentiment="positive", review_intent="praise", confidence=.9,
                aspects=[Aspect(category="gameplay", subcategory="core_loop", sentiment="positive", confidence=.9)])
                for rec_id, _, _ in rows}

    async def enrich_review(self, text, voted_up, aspects):
        return ReviewEnrichment(sentiment="positive", review_intent="praise", confidence=.9)


@pytest.mark.asyncio
async def test_enrichment_is_bounded_and_batched(settings, db):
    settings.llm_batch_size = 3
    settings.llm_batch_max_characters = 10_000
    settings.llm_concurrency = 2
    db.upsert_catalog_game(10, "Game")
    db.upsert_reviews(10, [review(str(number)) for number in range(10)], lambda value: value)
    pipeline = Pipeline(settings, db)
    await pipeline.llm.close()
    fake = FakeLLM()
    pipeline.llm = fake
    try:
        enriched, skipped = await pipeline.enrich_pending(10)
        assert (enriched, skipped) == (10, 0)
        assert fake.batch_sizes == [3, 3, 3, 1]
        assert db.con.execute("SELECT count(*) FROM review_enrichment").fetchone()[0] == 10
    finally:
        await pipeline.reviews.close(); await pipeline.store.close(); await pipeline.spy.close()


@pytest.mark.asyncio
async def test_invalid_batch_falls_back_per_review(settings, db):
    settings.llm_batch_size = 3
    db.upsert_catalog_game(10, "Game")
    db.upsert_reviews(10, [review(str(number)) for number in range(3)], lambda value: value)
    pipeline = Pipeline(settings, db)
    await pipeline.llm.close()

    class BrokenBatch(FakeLLM):
        async def enrich_reviews(self, rows, aspects):
            raise ValueError("bad batch")

    pipeline.llm = BrokenBatch()
    try:
        assert await pipeline.enrich_pending(10) == (3, 0)
        assert db.con.execute("SELECT count(*) FROM review_enrichment WHERE enrichment_status='completed'").fetchone()[0] == 3
    finally:
        await pipeline.reviews.close(); await pipeline.store.close(); await pipeline.spy.close()
