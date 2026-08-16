from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Iterable

import duckdb

from .domain import GameClassification, ReviewEnrichment, ReviewSummary, utcnow


SCHEMA_VERSION = "2"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_metadata(version VARCHAR PRIMARY KEY, applied_at TIMESTAMP NOT NULL);
CREATE TABLE IF NOT EXISTS games(
 appid BIGINT PRIMARY KEY, name VARCHAR NOT NULL, app_type VARCHAR, short_description VARCHAR,
 release_date DATE, release_date_raw VARCHAR, is_free BOOLEAN, current_price_cents BIGINT,
 currency VARCHAR, developers JSON, publishers JSON, steam_genres JSON, steam_categories JSON,
 source_metadata JSON, first_seen_at TIMESTAMP NOT NULL, last_refreshed_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS game_review_summary(
 appid BIGINT PRIMARY KEY, total_reviews BIGINT, total_positive BIGINT, total_negative BIGINT,
 review_score INTEGER, review_score_desc VARCHAR, fetched_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS game_tags(
 appid BIGINT NOT NULL, tag VARCHAR NOT NULL, weight BIGINT, source VARCHAR NOT NULL,
 fetched_at TIMESTAMP NOT NULL, PRIMARY KEY(appid, tag, source)
);
CREATE TABLE IF NOT EXISTS game_genre_classification(
 appid BIGINT NOT NULL, taxonomy_version VARCHAR NOT NULL, primary_genre VARCHAR NOT NULL,
 secondary_genres JSON, mechanics JSON, themes JSON, modes JSON, confidence DOUBLE,
 reasoning_summary VARCHAR, proposed_labels JSON, model_id VARCHAR NOT NULL,
 prompt_version VARCHAR NOT NULL, classified_at TIMESTAMP NOT NULL,
 PRIMARY KEY(appid, taxonomy_version)
);
CREATE TABLE IF NOT EXISTS reviews(
 recommendation_id VARCHAR PRIMARY KEY, appid BIGINT NOT NULL, author_steamid_hash VARCHAR,
 language VARCHAR, review_text VARCHAR NOT NULL, timestamp_created TIMESTAMP,
 timestamp_updated TIMESTAMP, voted_up BOOLEAN, votes_up BIGINT, votes_funny BIGINT,
 weighted_vote_score DOUBLE, comment_count BIGINT, steam_purchase BOOLEAN,
 received_for_free BOOLEAN, written_during_early_access BOOLEAN, primarily_steam_deck BOOLEAN,
 playtime_forever_minutes BIGINT, playtime_last_two_weeks_minutes BIGINT,
 playtime_at_review_minutes BIGINT, last_played TIMESTAMP, raw_payload JSON,
 ingested_at TIMESTAMP NOT NULL, source_updated_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS review_enrichment(
 recommendation_id VARCHAR NOT NULL, enrichment_version VARCHAR NOT NULL, sentiment VARCHAR,
 review_intent VARCHAR, player_context JSON, complaints JSON, praises JSON,
 feature_requests JSON, technical_issues JSON, monetization_comments JSON,
 accessibility_comments JSON, multiplayer_comments JSON, confidence DOUBLE,
 model_id VARCHAR NOT NULL, prompt_version VARCHAR NOT NULL, enrichment_status VARCHAR NOT NULL,
 error_message VARCHAR, enriched_at TIMESTAMP NOT NULL,
 PRIMARY KEY(recommendation_id, enrichment_version)
);
CREATE TABLE IF NOT EXISTS review_aspects(
 recommendation_id VARCHAR NOT NULL, enrichment_version VARCHAR NOT NULL, appid BIGINT NOT NULL,
 category VARCHAR NOT NULL, subcategory VARCHAR NOT NULL, sentiment VARCHAR NOT NULL,
 confidence DOUBLE, PRIMARY KEY(recommendation_id, enrichment_version, category, subcategory, sentiment)
);
CREATE TABLE IF NOT EXISTS review_discovered_topics(
 recommendation_id VARCHAR NOT NULL, enrichment_version VARCHAR NOT NULL, appid BIGINT NOT NULL,
 category VARCHAR NOT NULL, novel_topic VARCHAR NOT NULL, sentiment VARCHAR NOT NULL,
 confidence DOUBLE,
 PRIMARY KEY(recommendation_id, enrichment_version, category, novel_topic, sentiment)
);
CREATE TABLE IF NOT EXISTS ingestion_runs(
 run_id UUID PRIMARY KEY, run_type VARCHAR NOT NULL, started_at TIMESTAMP NOT NULL,
 completed_at TIMESTAMP, status VARCHAR NOT NULL, config_snapshot JSON,
 games_considered BIGINT DEFAULT 0, games_qualified BIGINT DEFAULT 0,
 reviews_ingested BIGINT DEFAULT 0, reviews_enriched BIGINT DEFAULT 0,
 error_count BIGINT DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ingestion_checkpoints(
 job_key VARCHAR PRIMARY KEY, job_type VARCHAR NOT NULL, appid BIGINT,
 checkpoint JSON, updated_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS source_errors(
 error_id UUID PRIMARY KEY, run_id UUID, appid BIGINT, recommendation_id VARCHAR,
 source VARCHAR, operation VARCHAR, status_code INTEGER, error_type VARCHAR,
 message VARCHAR, retry_count INTEGER, payload JSON, occurred_at TIMESTAMP NOT NULL
);
CREATE OR REPLACE VIEW latest_game_classification AS
 SELECT * EXCLUDE(rn) FROM (
  SELECT *, row_number() OVER(PARTITION BY appid ORDER BY classified_at DESC) rn
  FROM game_genre_classification) WHERE rn=1;
CREATE OR REPLACE VIEW latest_review_enrichment AS
 SELECT * EXCLUDE(rn) FROM (
  SELECT *, row_number() OVER(PARTITION BY recommendation_id ORDER BY enriched_at DESC) rn
  FROM review_enrichment) WHERE rn=1;
CREATE OR REPLACE VIEW game_market_data AS
 SELECT g.*, s.total_reviews, s.total_positive, s.total_negative,
        c.primary_genre, c.secondary_genres
 FROM games g LEFT JOIN game_review_summary s USING(appid)
 LEFT JOIN latest_game_classification c USING(appid);
CREATE OR REPLACE VIEW qualified_games AS
 SELECT * FROM game_market_data WHERE total_reviews >= 50;
CREATE OR REPLACE VIEW review_analysis AS
 SELECT r.*, e.sentiment llm_sentiment, e.complaints, e.praises, e.feature_requests,
        g.name game_name, c.primary_genre
 FROM reviews r JOIN games g USING(appid)
 LEFT JOIN latest_review_enrichment e USING(recommendation_id)
 LEFT JOIN latest_game_classification c USING(appid);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(self.path))

    def close(self) -> None:
        self.con.close()

    def initialize(self) -> None:
        self.con.execute(SCHEMA_SQL)
        self.con.execute(
            "INSERT INTO schema_metadata VALUES (?, ?) ON CONFLICT(version) DO NOTHING",
            [SCHEMA_VERSION, utcnow()],
        )

    def upsert_catalog_game(self, appid: int, name: str, raw: dict | None = None) -> None:
        now = utcnow()
        self.con.execute("""
          INSERT INTO games(appid,name,source_metadata,first_seen_at,last_refreshed_at)
          VALUES(?,?,?,?,?) ON CONFLICT(appid) DO UPDATE SET
          name=excluded.name, source_metadata=excluded.source_metadata,
          last_refreshed_at=excluded.last_refreshed_at
        """, [appid, name or f"App {appid}", _json(raw or {}), now, now])

    def upsert_game_metadata(self, appid: int, data: dict) -> None:
        now = utcnow()
        price = data.get("price_overview") or {}
        release = data.get("release_date") or {}
        genres = [x.get("description") for x in data.get("genres", []) if x.get("description")]
        categories = [x.get("description") for x in data.get("categories", []) if x.get("description")]
        self.con.execute("""
          INSERT INTO games VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(appid) DO UPDATE SET name=excluded.name, app_type=excluded.app_type,
          short_description=excluded.short_description, release_date_raw=excluded.release_date_raw,
          is_free=excluded.is_free, current_price_cents=excluded.current_price_cents,
          currency=excluded.currency, developers=excluded.developers, publishers=excluded.publishers,
          steam_genres=excluded.steam_genres, steam_categories=excluded.steam_categories,
          source_metadata=excluded.source_metadata,last_refreshed_at=excluded.last_refreshed_at
        """, [appid, data.get("name") or f"App {appid}", data.get("type"),
          data.get("short_description"), None, release.get("date"), data.get("is_free"),
          price.get("final"), price.get("currency"), _json(data.get("developers", [])),
          _json(data.get("publishers", [])), _json(genres), _json(categories), _json(data), now, now])

    def upsert_summary(self, appid: int, summary: ReviewSummary) -> None:
        self.con.execute("""
          INSERT INTO game_review_summary VALUES(?,?,?,?,?,?,?) ON CONFLICT(appid) DO UPDATE SET
          total_reviews=excluded.total_reviews,total_positive=excluded.total_positive,
          total_negative=excluded.total_negative,review_score=excluded.review_score,
          review_score_desc=excluded.review_score_desc,fetched_at=excluded.fetched_at
        """, [appid, summary.total_reviews, summary.total_positive, summary.total_negative,
               summary.review_score, summary.review_score_desc, utcnow()])

    def upsert_tags(self, appid: int, tags: dict[str, int], source: str = "steamspy") -> None:
        now = utcnow()
        self.con.executemany("""
          INSERT INTO game_tags VALUES(?,?,?,?,?) ON CONFLICT(appid,tag,source) DO UPDATE SET
          weight=excluded.weight,fetched_at=excluded.fetched_at
        """, [[appid, tag, weight, source, now] for tag, weight in tags.items()])

    def upsert_reviews(self, appid: int, reviews: Iterable[dict], hash_author: Any) -> int:
        rows = []
        for review in reviews:
            author = review.get("author") or {}
            rows.append([
                str(review["recommendationid"]), appid, hash_author(author.get("steamid")),
                review.get("language"), review.get("review", ""), _timestamp(review.get("timestamp_created")),
                _timestamp(review.get("timestamp_updated")), review.get("voted_up"), review.get("votes_up"),
                review.get("votes_funny"), _float(review.get("weighted_vote_score")), review.get("comment_count"),
                review.get("steam_purchase"), review.get("received_for_free"),
                review.get("written_during_early_access"), review.get("primarily_steam_deck"),
                author.get("playtime_forever"), author.get("playtime_last_two_weeks"),
                author.get("playtime_at_review"), _timestamp(author.get("last_played")), _json(review),
                utcnow(), _timestamp(review.get("timestamp_updated")),
            ])
        if not rows:
            return 0
        self.con.executemany("""
          INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(recommendation_id) DO UPDATE SET
          review_text=excluded.review_text,timestamp_updated=excluded.timestamp_updated,
          voted_up=excluded.voted_up,votes_up=excluded.votes_up,votes_funny=excluded.votes_funny,
          weighted_vote_score=excluded.weighted_vote_score,comment_count=excluded.comment_count,
          playtime_forever_minutes=excluded.playtime_forever_minutes,
          playtime_last_two_weeks_minutes=excluded.playtime_last_two_weeks_minutes,
          playtime_at_review_minutes=excluded.playtime_at_review_minutes,last_played=excluded.last_played,
          raw_payload=excluded.raw_payload,ingested_at=excluded.ingested_at,
          source_updated_at=excluded.source_updated_at
        """, rows)
        return len(rows)

    def checkpoint(self, job_key: str, job_type: str, appid: int | None, value: dict) -> None:
        self.con.execute("""INSERT INTO ingestion_checkpoints VALUES(?,?,?,?,?)
          ON CONFLICT(job_key) DO UPDATE SET checkpoint=excluded.checkpoint,updated_at=excluded.updated_at
        """, [job_key, job_type, appid, _json(value), utcnow()])

    def get_checkpoint(self, job_key: str) -> dict | None:
        row = self.con.execute("SELECT checkpoint FROM ingestion_checkpoints WHERE job_key=?", [job_key]).fetchone()
        return json.loads(row[0]) if row else None

    def clear_checkpoint(self, job_key: str) -> None:
        self.con.execute("DELETE FROM ingestion_checkpoints WHERE job_key=?", [job_key])

    def start_run(self, run_type: str, config: dict) -> str:
        run_id = str(uuid.uuid4())
        self.con.execute("INSERT INTO ingestion_runs VALUES(?,?,?,NULL,'running',?,0,0,0,0,0)",
                         [run_id, run_type, utcnow(), _json(config)])
        return run_id

    def update_run_config(self, run_id: str, config: dict) -> None:
        self.con.execute("UPDATE ingestion_runs SET config_snapshot=? WHERE run_id=?", [_json(config), run_id])

    def record_error(self, run_id: str | None, source: str, operation: str, message: str,
                     appid: int | None = None, recommendation_id: str | None = None,
                     status_code: int | None = None, retry_count: int = 0,
                     payload: dict | None = None) -> None:
        self.con.execute("INSERT INTO source_errors VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", [
            str(uuid.uuid4()), run_id, appid, recommendation_id, source, operation, status_code,
            type(message).__name__ if not isinstance(message, str) else "error", str(message)[:4000],
            retry_count, _json(payload or {}), utcnow(),
        ])

    def finish_run(self, run_id: str, status: str, stats: dict[str, int]) -> None:
        self.con.execute("""UPDATE ingestion_runs SET completed_at=?,status=?,games_considered=?,
          games_qualified=?,reviews_ingested=?,reviews_enriched=?,error_count=? WHERE run_id=?""",
          [utcnow(), status, stats.get("games_considered", 0), stats.get("games_qualified", 0),
           stats.get("reviews_ingested", 0), stats.get("reviews_enriched", 0),
           stats.get("error_count", 0), run_id])

    def save_classification(self, appid: int, result: GameClassification, taxonomy_version: str,
                            model_id: str, prompt_version: str = "v1") -> None:
        self.con.execute("""INSERT INTO game_genre_classification VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(appid,taxonomy_version) DO UPDATE SET primary_genre=excluded.primary_genre,
          secondary_genres=excluded.secondary_genres,mechanics=excluded.mechanics,themes=excluded.themes,
          modes=excluded.modes,confidence=excluded.confidence,reasoning_summary=excluded.reasoning_summary,
          proposed_labels=excluded.proposed_labels,model_id=excluded.model_id,prompt_version=excluded.prompt_version,
          classified_at=excluded.classified_at""", [appid, taxonomy_version, result.primary_genre,
          _json(result.secondary_genres), _json(result.mechanics), _json(result.themes), _json(result.modes),
          result.confidence, result.reasoning_summary, _json(result.proposed_labels), model_id, prompt_version, utcnow()])

    def save_enrichment(self, recommendation_id: str, appid: int, result: ReviewEnrichment | None,
                        version: str, model_id: str, status: str, error: str | None = None,
                        prompt_version: str = "v2") -> None:
        empty: Any = []
        values = result or empty
        get = lambda name, default=None: getattr(values, name, default)
        self.con.execute("""INSERT INTO review_enrichment VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(recommendation_id,enrichment_version) DO UPDATE SET
          sentiment=excluded.sentiment,review_intent=excluded.review_intent,player_context=excluded.player_context,
          complaints=excluded.complaints,praises=excluded.praises,feature_requests=excluded.feature_requests,
          technical_issues=excluded.technical_issues,monetization_comments=excluded.monetization_comments,
          accessibility_comments=excluded.accessibility_comments,multiplayer_comments=excluded.multiplayer_comments,
          confidence=excluded.confidence,enrichment_status=excluded.enrichment_status,
          error_message=excluded.error_message,enriched_at=excluded.enriched_at""",
          [recommendation_id, version, get("sentiment"), get("review_intent"), _json(get("player_context", [])),
           _json([x.model_dump() for x in get("complaints", [])]), _json([x.model_dump() for x in get("praises", [])]),
           _json([x.model_dump() for x in get("feature_requests", [])]), _json([x.model_dump() for x in get("technical_issues", [])]),
           _json([x.model_dump() for x in get("monetization_comments", [])]),
           _json([x.model_dump() for x in get("accessibility_comments", [])]),
           _json([x.model_dump() for x in get("multiplayer_comments", [])]), get("confidence"), model_id, prompt_version, status, error, utcnow()])
        if result:
            self.con.execute("DELETE FROM review_aspects WHERE recommendation_id=? AND enrichment_version=?",
                             [recommendation_id, version])
            self.con.execute("DELETE FROM review_discovered_topics WHERE recommendation_id=? AND enrichment_version=?",
                             [recommendation_id, version])
            rows = [[recommendation_id, version, appid, x.category, x.subcategory, x.sentiment, x.confidence]
                    for x in result.aspects]
            if rows:
                self.con.executemany("INSERT INTO review_aspects VALUES(?,?,?,?,?,?,?)", rows)
            discoveries = [
                [recommendation_id, version, appid, x.category, x.novel_topic, x.sentiment, x.confidence]
                for x in result.aspects if x.novel_topic is not None
            ]
            if discoveries:
                self.con.executemany("INSERT INTO review_discovered_topics VALUES(?,?,?,?,?,?,?)", discoveries)

    def table_counts(self) -> dict[str, int]:
        tables = ["games", "game_review_summary", "game_tags", "game_genre_classification", "reviews",
                  "review_enrichment", "review_aspects", "review_discovered_topics", "ingestion_runs",
                  "ingestion_checkpoints", "source_errors"]
        return {table: self.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}


def _timestamp(value: Any) -> Any:
    from datetime import datetime, timezone
    if value in (None, "", 0):
        return None
    return datetime.fromtimestamp(int(value), timezone.utc).replace(tzinfo=None)


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
