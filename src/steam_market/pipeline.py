from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from rich.console import Console

from .config import Settings
from .database import Database
from .domain import enrichment_eligibility
from .llm import LLMClient
from .sources import SteamReviewSource, SteamSpySource, SteamStoreSource
from .taxonomy import AspectTaxonomy, Taxonomy, deterministic_candidates


console = Console()


@dataclass
class RunStats:
    games_considered: int = 0
    games_qualified: int = 0
    reviews_ingested: int = 0
    reviews_enriched: int = 0
    skipped_reviews: int = 0
    error_count: int = 0
    expected_reviews: int = 0
    selected: list[tuple[int, str]] = field(default_factory=list)

    def db_fields(self) -> dict[str, int]:
        return {key: getattr(self, key) for key in (
            "games_considered", "games_qualified", "reviews_ingested", "reviews_enriched", "error_count")}


class Pipeline:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.reviews = SteamReviewSource(settings.steam_requests_per_second, settings.http_max_retries)
        self.store = SteamStoreSource(settings.steam_requests_per_second, settings.http_max_retries)
        self.spy = SteamSpySource(settings.steamspy_requests_per_second, settings.http_max_retries)
        self.llm = LLMClient(settings)
        self.taxonomy = Taxonomy()
        self.aspects = AspectTaxonomy()

    async def close(self) -> None:
        await self.reviews.close()
        await self.store.close()
        await self.spy.close()
        await self.llm.close()

    def author_hash(self, steamid: str | None) -> str | None:
        if not steamid:
            return None
        return hashlib.sha256(f"{self.settings.steamid_hash_salt}:{steamid}".encode()).hexdigest()

    async def discover(self) -> int:
        games = await self.spy.catalog(self.settings.steamspy_catalog_pages)
        for game in games:
            self.db.upsert_catalog_game(game.appid, game.name, game.source_metadata)
        return len(games)

    async def select_random_games(self, count: int, minimum: int, seed: int) -> tuple[list[tuple[int, str, dict, object]], int]:
        candidates = await self.spy.catalog(self.settings.steamspy_catalog_pages)
        random.Random(seed).shuffle(candidates)
        selected: list[tuple[int, str, dict, object]] = []
        considered = 0
        for candidate in candidates:
            considered += 1
            summary = await self.reviews.get_summary(candidate.appid)
            self.db.upsert_catalog_game(candidate.appid, candidate.name, candidate.source_metadata)
            self.db.upsert_summary(candidate.appid, summary)
            if summary.total_reviews < minimum:
                continue
            metadata = await self.store.get_game(candidate.appid)
            if not metadata or metadata.get("type") != "game":
                continue
            self.db.upsert_game_metadata(candidate.appid, metadata)
            selected.append((candidate.appid, metadata.get("name", candidate.name), metadata, summary))
            console.print(f"Qualified [{len(selected)}/{count}] {candidate.appid} {metadata.get('name')} ({summary.total_reviews:,} reviews)")
            if len(selected) == count:
                break
        if len(selected) != count:
            raise RuntimeError(f"Only found {len(selected)} qualifying games among {considered} candidates")
        return selected, considered

    async def classify(self, appid: int, metadata: dict) -> str:
        tags = await self.spy.get_tags(appid) if self.settings.steamspy_enabled else {}
        self.db.upsert_tags(appid, tags)
        genres = [item.get("description", "") for item in metadata.get("genres", [])]
        candidates = deterministic_candidates(list(tags), genres, metadata.get("short_description", ""))
        result = await self.llm.classify_game(metadata, tags, candidates, self.taxonomy)
        self.db.save_classification(appid, result, self.settings.genre_taxonomy_version, self.settings.llm_model)
        return result.primary_genre

    async def ingest_reviews(self, appid: int, on_page: Callable[[int], None] | None = None) -> int:
        key = f"reviews:{appid}"
        checkpoint = self.db.get_checkpoint(key) or {}
        cursor = checkpoint.get("cursor", "*")
        seen = set(checkpoint.get("seen_cursors", []))
        stored = 0
        while True:
            if cursor in seen:
                raise RuntimeError(f"Pagination cursor repeated for appid {appid}: {cursor!r}")
            seen.add(cursor)
            page = await self.reviews.get_page(appid, cursor, self.settings.steam_reviews_per_page)
            self.db.upsert_summary(appid, page.summary)
            if not page.reviews:
                self.db.clear_checkpoint(key)
                break
            stored += self.db.upsert_reviews(appid, page.reviews, self.author_hash)
            next_cursor = page.cursor
            if not next_cursor or next_cursor == cursor:
                raise RuntimeError(f"Steam returned a non-advancing cursor for appid {appid}")
            self.db.checkpoint(key, "reviews", appid, {"cursor": next_cursor, "seen_cursors": list(seen)[-50:]})
            cursor = next_cursor
            if on_page:
                on_page(stored)
        return stored

    async def enrich_pending(self, appid: int, run_id: str | None = None) -> tuple[int, int]:
        rows = self.db.con.execute("""
          SELECT r.recommendation_id,r.review_text,r.language,r.voted_up FROM reviews r
          LEFT JOIN review_enrichment e ON r.recommendation_id=e.recommendation_id AND e.enrichment_version=?
          WHERE r.appid=? AND e.recommendation_id IS NULL ORDER BY r.recommendation_id
        """, [self.settings.enrichment_version, appid]).fetchall()
        enriched = skipped = 0
        for recommendation_id, text, language, voted_up in rows:
            eligible, status = enrichment_eligibility(text, language or "", self.settings)
            if not eligible:
                self.db.save_enrichment(recommendation_id, appid, None, self.settings.enrichment_version,
                                        self.settings.llm_model, status)
                skipped += 1
                continue
            try:
                result = await self.llm.enrich_review(text, voted_up, self.aspects)
                self.db.save_enrichment(recommendation_id, appid, result, self.settings.enrichment_version,
                                        self.settings.llm_model, "completed")
                enriched += 1
            except Exception as exc:
                self.db.save_enrichment(recommendation_id, appid, None, self.settings.enrichment_version,
                                        self.settings.llm_model, "error", str(exc)[:2000])
                self.db.record_error(run_id, "llm", "review_enrichment", str(exc), appid, recommendation_id,
                                     retry_count=self.settings.llm_max_retries)
        return enriched, skipped

    async def dry_run(self, games: int, minimum: int, seed: int) -> tuple[str, RunStats, dict]:
        await self.llm.health_check()
        config = {"games": games, "min_reviews": minimum, "seed": seed,
                  "model": self.settings.llm_model, "catalog_pages": self.settings.steamspy_catalog_pages}
        run_id = self.db.start_run("dry-run", config)
        stats = RunStats()
        started = datetime.now()
        try:
            selected, stats.games_considered = await self.select_random_games(games, minimum, seed)
            stats.games_qualified = len(selected)
            stats.selected = [(appid, name) for appid, name, _, _ in selected]
            stats.expected_reviews = sum(summary.total_reviews for _, _, _, summary in selected)
            config["selected_appids"] = [appid for appid, _, _, _ in selected]
            self.db.update_run_config(run_id, config)
            classifications: dict[int, str] = {}
            for appid, name, metadata, _ in selected:
                console.rule(f"{name} ({appid})")
                classifications[appid] = await self.classify(appid, metadata)
                count = await self.ingest_reviews(appid, lambda n: console.print(f"  reviews processed: {n:,}", end="\r"))
                stats.reviews_ingested += count
                enriched, skipped = await self.enrich_pending(appid, run_id)
                stats.reviews_enriched += enriched
                stats.skipped_reviews += skipped
            validation = validate_database(self.db, self.taxonomy, self.aspects)
            if not validation["passed"]:
                raise RuntimeError("Validation failed: " + "; ".join(validation["errors"]))
            self.db.finish_run(run_id, "completed", stats.db_fields())
            result = {"classifications": classifications, "validation": validation,
                      "elapsed_seconds": (datetime.now() - started).total_seconds()}
            return run_id, stats, result
        except BaseException as exc:
            self.db.record_error(run_id, "pipeline", "dry_run", str(exc))
            stats.error_count += 1
            self.db.finish_run(run_id, "failed", stats.db_fields())
            raise

    async def production_run(self, minimum: int, limit_games: int | None = None,
                             appid: int | None = None, skip_llm: bool = False,
                             skip_enrichment: bool = False, metadata_only: bool = False,
                             reviews_only: bool = False) -> tuple[str, RunStats, dict]:
        """Run a resumable production batch; omitted limit processes the configured catalog snapshot."""
        if metadata_only and reviews_only:
            raise ValueError("--metadata-only and --reviews-only are mutually exclusive")
        if not skip_llm and not reviews_only:
            await self.llm.health_check()
        config = {"min_reviews": minimum, "limit_games": limit_games, "appid": appid,
                  "skip_llm": skip_llm, "skip_enrichment": skip_enrichment,
                  "metadata_only": metadata_only, "reviews_only": reviews_only,
                  "model": self.settings.llm_model}
        run_id = self.db.start_run("production", config)
        stats = RunStats()
        selected: list[tuple[int, str, dict | None, object]] = []
        try:
            if reviews_only:
                where = "WHERE s.total_reviews>=?"
                params: list[object] = [minimum]
                if appid is not None:
                    where += " AND g.appid=?"
                    params.append(appid)
                sql = f"""SELECT g.appid,g.name,g.source_metadata,s.total_reviews,s.total_positive,
                  s.total_negative,s.review_score,s.review_score_desc FROM games g
                  JOIN game_review_summary s USING(appid) {where} ORDER BY g.appid"""
                if limit_games:
                    sql += f" LIMIT {int(limit_games)}"
                for row in self.db.con.execute(sql, params).fetchall():
                    summary = type("Summary", (), {"total_reviews": row[3]})()
                    selected.append((row[0], row[1], None, summary))
                stats.games_considered = len(selected)
            else:
                if appid is not None:
                    metadata = await self.store.get_game(appid)
                    candidates = [(appid, (metadata or {}).get("name", f"App {appid}"), metadata)]
                else:
                    catalog = await self.spy.catalog(self.settings.steamspy_catalog_pages)
                    candidates = [(item.appid, item.name, None) for item in catalog]
                for candidate_id, candidate_name, known_metadata in candidates:
                    stats.games_considered += 1
                    try:
                        summary = await self.reviews.get_summary(candidate_id)
                        self.db.upsert_catalog_game(candidate_id, candidate_name)
                        self.db.upsert_summary(candidate_id, summary)
                        if summary.total_reviews < minimum:
                            continue
                        metadata = known_metadata or await self.store.get_game(candidate_id)
                        if not metadata or metadata.get("type") != "game":
                            continue
                        self.db.upsert_game_metadata(candidate_id, metadata)
                        selected.append((candidate_id, metadata.get("name", candidate_name), metadata, summary))
                        if limit_games and len(selected) >= limit_games:
                            break
                    except Exception as exc:
                        stats.error_count += 1
                        self.db.record_error(run_id, "upstream", "qualification", str(exc), candidate_id)
            stats.games_qualified = len(selected)
            stats.selected = [(game_id, name) for game_id, name, _, _ in selected]
            stats.expected_reviews = sum(summary.total_reviews for _, _, _, summary in selected)
            config["selected_appids"] = [game_id for game_id, _, _, _ in selected]
            self.db.update_run_config(run_id, config)
            classifications: dict[int, str] = {}
            for game_id, name, metadata, _ in selected:
                console.rule(f"{name} ({game_id})")
                try:
                    if not reviews_only:
                        if not skip_llm:
                            classifications[game_id] = await self.classify(game_id, metadata or {})
                        elif self.settings.steamspy_enabled:
                            self.db.upsert_tags(game_id, await self.spy.get_tags(game_id))
                    if not metadata_only:
                        stats.reviews_ingested += await self.ingest_reviews(game_id)
                        if not skip_llm and not skip_enrichment:
                            enriched, skipped = await self.enrich_pending(game_id, run_id)
                            stats.reviews_enriched += enriched
                            stats.skipped_reviews += skipped
                except Exception as exc:
                    stats.error_count += 1
                    self.db.record_error(run_id, "pipeline", "game_ingestion", str(exc), game_id)
                    console.print(f"[red]{game_id} failed: {exc}[/red]")
            validation = validate_database(self.db, self.taxonomy, self.aspects)
            status = "completed" if validation["passed"] else "failed"
            self.db.finish_run(run_id, status, stats.db_fields())
            return run_id, stats, {"classifications": classifications, "validation": validation}
        except BaseException as exc:
            stats.error_count += 1
            self.db.record_error(run_id, "pipeline", "production_run", str(exc))
            self.db.finish_run(run_id, "failed", stats.db_fields())
            raise


def validate_database(db: Database, taxonomy: Taxonomy | None = None, aspects: AspectTaxonomy | None = None) -> dict:
    taxonomy = taxonomy or Taxonomy()
    aspects = aspects or AspectTaxonomy()
    errors: list[str] = []
    warnings: list[str] = []
    checks = {
        "duplicate_games": "SELECT count(*)-count(DISTINCT appid) FROM games",
        "duplicate_reviews": "SELECT count(*)-count(DISTINCT recommendation_id) FROM reviews",
        "orphan_enrichment": "SELECT count(*) FROM review_enrichment e ANTI JOIN reviews r USING(recommendation_id)",
        "orphan_aspects": "SELECT count(*) FROM review_aspects a ANTI JOIN reviews r USING(recommendation_id)",
        "empty_game_names": "SELECT count(*) FROM games WHERE trim(name)=''",
        "null_review_text": "SELECT count(*) FROM reviews WHERE review_text IS NULL",
        "invalid_confidence": "SELECT count(*) FROM review_enrichment WHERE confidence NOT BETWEEN 0 AND 1",
    }
    values = {}
    for name, sql in checks.items():
        values[name] = db.con.execute(sql).fetchone()[0]
        if values[name]:
            errors.append(f"{name}: {values[name]}")
    for appid, label in db.con.execute("SELECT appid,primary_genre FROM game_genre_classification").fetchall():
        if label not in taxonomy.labels:
            errors.append(f"appid {appid}: invalid genre {label}")
    for rec, category, subcategory in db.con.execute("SELECT recommendation_id,category,subcategory FROM review_aspects").fetchall():
        if not aspects.validate(category, subcategory):
            errors.append(f"review {rec}: invalid aspect {category}/{subcategory}")
    completeness = []
    for appid, expected, actual in db.con.execute("""
      SELECT s.appid,s.total_reviews,count(r.recommendation_id) FROM game_review_summary s
      LEFT JOIN reviews r USING(appid) GROUP BY 1,2 HAVING count(r.recommendation_id)>0
    """).fetchall():
        difference = actual - expected
        percent = (abs(difference) / expected * 100) if expected else 0
        completeness.append({"appid": appid, "expected": expected, "actual": actual,
                             "difference": difference, "difference_percentage": round(percent, 2)})
        if percent > 10:
            warnings.append(f"appid {appid}: review count differs by {percent:.1f}%")
    return {"passed": not errors, "errors": errors, "warnings": warnings,
            "checks": values, "review_completeness": completeness}
