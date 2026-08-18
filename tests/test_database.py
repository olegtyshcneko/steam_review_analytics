from __future__ import annotations

import json

from steam_market.domain import Aspect, GameClassification, ReviewEnrichment, ReviewSummary, Statement
from steam_market.pipeline import validate_database


REVIEW = {
    "recommendationid": "123", "language": "english", "review": "Combat is great but performance is bad.",
    "timestamp_created": 1_700_000_000, "timestamp_updated": 1_700_000_001,
    "voted_up": False, "votes_up": 2, "votes_funny": 0, "weighted_vote_score": "0.8",
    "comment_count": 0, "steam_purchase": True, "received_for_free": False,
    "written_during_early_access": False, "primarily_steam_deck": False,
    "author": {"steamid": "7656119", "playtime_forever": 500, "playtime_at_review": 400},
}


def test_schema_and_idempotent_upserts(db):
    db.upsert_catalog_game(10, "First")
    db.upsert_catalog_game(10, "Updated")
    db.upsert_summary(10, ReviewSummary(total_reviews=50, total_positive=40, total_negative=10))
    db.upsert_summary(10, ReviewSummary(total_reviews=51, total_positive=41, total_negative=10))
    db.upsert_reviews(10, [REVIEW], lambda _: "hash")
    db.upsert_reviews(10, [{**REVIEW, "review": "updated"}], lambda _: "hash")
    assert db.con.execute("SELECT name FROM games").fetchone()[0] == "Updated"
    assert db.con.execute("SELECT total_reviews FROM game_review_summary").fetchone()[0] == 51
    assert db.con.execute("SELECT count(*),max(review_text) FROM reviews").fetchone() == (1, "updated")


def test_raw_payload_does_not_retain_author_steamid(db):
    db.upsert_catalog_game(10, "Game")
    db.upsert_reviews(10, [REVIEW], lambda _: "hashed-author")
    author_hash, raw_payload = db.con.execute(
        "SELECT author_steamid_hash,raw_payload FROM reviews"
    ).fetchone()
    assert author_hash == "hashed-author"
    assert "steamid" not in json.loads(raw_payload)["author"]


def test_initialize_scrubs_legacy_raw_author_steamid(db):
    db.upsert_catalog_game(10, "Game")
    db.upsert_reviews(10, [REVIEW], lambda _: "hashed-author")
    db.con.execute(
        "UPDATE reviews SET raw_payload=?",
        [json.dumps({"author": {"steamid": "legacy-secret", "playtime_forever": 10}})],
    )
    db.initialize()
    raw_payload = db.con.execute("SELECT raw_payload FROM reviews").fetchone()[0]
    assert "steamid" not in json.loads(raw_payload)["author"]


def test_checkpoint_resume(db):
    db.checkpoint("reviews:10", "reviews", 10, {"cursor": "a+b="})
    assert db.get_checkpoint("reviews:10") == {"cursor": "a+b="}
    db.checkpoint("reviews:10", "reviews", 10, {"cursor": "next"})
    assert db.get_checkpoint("reviews:10")["cursor"] == "next"
    db.clear_checkpoint("reviews:10")
    assert db.get_checkpoint("reviews:10") is None


def test_empty_tag_upsert_is_a_noop(db):
    db.upsert_tags(10, {})
    assert db.con.execute("SELECT count(*) FROM game_tags").fetchone()[0] == 0


def test_enrichment_versioning_and_aspect_explosion(db):
    db.upsert_catalog_game(10, "Game")
    db.upsert_reviews(10, [REVIEW], lambda _: "hash")
    result = ReviewEnrichment(
        sentiment="mixed", review_intent="mixed", confidence=.9,
        complaints=[Statement(label="technical.performance", statement="Performance is bad")],
        aspects=[Aspect(category="gameplay", subcategory="combat", sentiment="positive", confidence=.9),
                 Aspect(category="technical", subcategory="performance", sentiment="negative", confidence=.8),
                 Aspect(category="gameplay", subcategory="other", novel_topic="finisher_pacing",
                        sentiment="negative", confidence=.7)],
    )
    db.save_enrichment("123", 10, result, "v1", "fixture", "completed")
    db.save_enrichment("123", 10, result, "v2", "fixture", "completed")
    assert db.con.execute("SELECT count(*) FROM review_enrichment").fetchone()[0] == 2
    assert db.con.execute("SELECT count(*) FROM review_aspects").fetchone()[0] == 6
    assert db.con.execute("SELECT category,novel_topic FROM review_discovered_topics").fetchall() == [
        ("gameplay", "finisher_pacing"), ("gameplay", "finisher_pacing")
    ]
    assert validate_database(db)["passed"]


def test_classification_and_analytical_views(db):
    db.upsert_catalog_game(10, "Game")
    db.upsert_summary(10, ReviewSummary(total_reviews=100, total_positive=80, total_negative=20))
    db.save_classification(10, GameClassification(primary_genre="FPS", confidence=.9,
                           reasoning_summary="Shooter"), "v1", "fixture")
    assert db.con.execute("SELECT name,total_reviews,primary_genre FROM game_market_data").fetchone() == ("Game", 100, "FPS")
    assert db.con.execute("SELECT count(*) FROM qualified_games").fetchone()[0] == 1


def test_run_tracking(db):
    run_id = db.start_run("test", {"seed": 42})
    db.finish_run(run_id, "completed", {"games_considered": 2, "games_qualified": 1, "reviews_ingested": 3})
    assert db.con.execute("SELECT status,games_considered,reviews_ingested FROM ingestion_runs").fetchone() == ("completed", 2, 3)
