from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Annotated

import duckdb
import httpx
import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import Settings
from .database import Database
from .domain import GameClassification
from .llm import LLMClient, LLMUnavailable
from .platforms.app_store import AppStoreSource
from .platforms.google_play import GooglePlaySource
from .pipeline import Pipeline, validate_database
from .platforms.steam import SteamReviewSource
from .taxonomy import AspectTaxonomy, Taxonomy, deterministic_candidates


app = typer.Typer(no_args_is_help=True, help="Build and analyze cross-platform game review datasets.")
console = Console()


def _open() -> tuple[Settings, Database]:
    settings = Settings()
    settings.ensure_directories()
    db = Database(settings.duckdb_path)
    db.initialize()
    return settings, db


def _run(coro):
    return asyncio.run(coro)


@app.command()
def init() -> None:
    """Initialize directories and the DuckDB schema."""
    settings, db = _open()
    db.close()
    console.print(f"Initialized DuckDB schema at {settings.duckdb_path}")
    if settings.steamid_hash_salt in {"change-me", "change-me-to-a-long-random-local-secret"}:
        console.print("[yellow]Set STEAMID_HASH_SALT in .env before ingesting reviews.[/yellow]")


@app.command()
def doctor() -> None:
    """Check environment, storage, Steam connectivity, and the local LLM."""
    settings, db = _open()

    async def checks() -> list[tuple[str, str]]:
        results = [("Python", sys.version.split()[0]), ("DuckDB", duckdb.__version__),
                   ("Database write", f"OK: {settings.duckdb_path}"),
                   ("Free disk", f"{shutil.disk_usage(settings.duckdb_path.parent).free / 2**30:.1f} GiB")]
        source = SteamReviewSource(settings.steam_requests_per_second, 1)
        llm = LLMClient(settings)
        try:
            summary = await source.get_summary(10)
            results.append(("Steam reviews", f"OK ({summary.total_reviews:,} reviews for app 10)"))
        except Exception as exc:
            results.append(("Steam reviews", f"FAIL: {exc}"))
        try:
            models = await llm.health_check()
            results.append(("Local LLM", f"OK: {', '.join(models)}"))
        except Exception as exc:
            results.append(("Local LLM", f"FAIL: {exc}"))
        await source.close()
        await llm.close()
        return results

    table = Table("Check", "Result")
    for key, value in _run(checks()):
        table.add_row(key, value)
    console.print(table)
    db.close()


@app.command()
def discover() -> None:
    """Discover/update a broad game candidate catalog."""
    settings, db = _open()

    async def work() -> int:
        pipeline = Pipeline(settings, db)
        try:
            return await pipeline.discover()
        finally:
            await pipeline.close()

    count = _run(work())
    db.close()
    console.print(f"Discovered {count:,} catalog entries")


@app.command()
def qualify(
    min_reviews: Annotated[int, typer.Option("--min-reviews", min=0)] = 50,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
) -> None:
    """Fetch review summaries for discovered games and report qualification."""
    settings, db = _open()
    rows = db.con.execute("SELECT appid,name FROM games ORDER BY appid" + (f" LIMIT {int(limit)}" if limit else "")).fetchall()

    async def work() -> int:
        source = SteamReviewSource(settings.steam_requests_per_second, settings.http_max_retries)
        qualified = 0
        try:
            for idx, (appid, name) in enumerate(rows, 1):
                summary = await source.get_summary(appid)
                db.upsert_summary(appid, summary)
                qualified += summary.total_reviews >= min_reviews
                console.print(f"[{idx}/{len(rows)}] {name}: {summary.total_reviews:,}")
            return qualified
        finally:
            await source.close()

    qualified = _run(work())
    db.close()
    console.print(f"Qualified: {qualified:,} / {len(rows):,}")


@app.command()
def ingest(appid: Annotated[int, typer.Option("--appid")]) -> None:
    """Ingest metadata, tags, and all reviews for one game."""
    settings, db = _open()

    async def work() -> tuple[str, int]:
        pipeline = Pipeline(settings, db)
        try:
            metadata = await pipeline.store.get_game(appid)
            if not metadata or metadata.get("type") != "game":
                raise typer.BadParameter(f"appid {appid} is unavailable or not a game")
            db.upsert_game_metadata(appid, metadata)
            tags = await pipeline.spy.get_tags(appid) if settings.steamspy_enabled else {}
            db.upsert_tags(appid, tags)
            count = await pipeline.ingest_reviews(appid, lambda n: console.print(f"Processed {n:,} reviews", end="\r"))
            return metadata["name"], count
        finally:
            await pipeline.close()

    name, count = _run(work())
    db.close()
    console.print(f"\nIngested {count:,} review rows for {name}")


@app.command("mine-store")
def mine_store(
    platform: Annotated[str, typer.Option("--platform", help="google-play or app-store")],
    product_id: Annotated[str, typer.Option("--product-id", help="Android package name or Apple numeric app ID")],
    country: Annotated[str, typer.Option("--country", help="Two-letter storefront code")] = "us",
    language: Annotated[str, typer.Option("--language", help="Google Play locale language")] = "en",
    max_reviews: Annotated[int, typer.Option("--max-reviews", min=1, max=100_000)] = 500,
) -> None:
    """Mine public Google Play or Apple App Store reviews without API credentials."""
    settings, db = _open()
    canonical = platform.strip().lower().replace("_", "-")
    if canonical not in {"google-play", "app-store"}:
        db.close()
        raise typer.BadParameter("--platform must be google-play or app-store")

    async def work() -> tuple[str, str, int]:
        source = (
            GooglePlaySource(settings.store_requests_per_second, settings.http_max_retries)
            if canonical == "google-play"
            else AppStoreSource(settings.store_requests_per_second, settings.http_max_retries)
        )
        try:
            if canonical == "google-play":
                product = await source.get_product(product_id, language, country)
            else:
                product = await source.get_product(product_id, country)
            product_key = db.upsert_store_product(product)
            stored = 0
            cursor: str | None = None
            while stored < max_reviews:
                remaining = max_reviews - stored
                if canonical == "google-play":
                    page = await source.get_reviews(
                        product_id,
                        language=language,
                        country=country,
                        count=min(500, remaining),
                        cursor=cursor,
                    )
                else:
                    page_number = int(cursor or "1")
                    page = await source.get_reviews(product_id, country=country, page=page_number)
                if not page.reviews:
                    break
                selected = page.reviews[:remaining]
                stored += db.upsert_store_reviews(product_key, selected)
                console.print(f"Processed {stored:,} {canonical} reviews", end="\r")
                if not page.next_cursor or page.next_cursor == cursor:
                    break
                cursor = page.next_cursor
            return product.name, product_key, stored
        finally:
            await source.close()

    try:
        name, product_key, count = _run(work())
    finally:
        db.close()
    console.print(f"\nMined {count:,} reviews for {name} ({product_key})")


@app.command("classify-games")
def classify_games(min_reviews: int = 50, limit: int | None = None) -> None:
    """Classify pending qualifying games with the configured local model."""
    settings, db = _open()
    sql = """SELECT g.appid,g.source_metadata FROM games g JOIN game_review_summary s USING(appid)
      LEFT JOIN game_genre_classification c ON g.appid=c.appid AND c.taxonomy_version=?
      WHERE s.total_reviews>=? AND c.appid IS NULL ORDER BY g.appid"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = db.con.execute(sql, [settings.genre_taxonomy_version, min_reviews]).fetchall()

    async def work() -> None:
        pipeline = Pipeline(settings, db)
        try:
            await pipeline.llm.health_check()
            for appid, raw in rows:
                metadata = json.loads(raw) if isinstance(raw, str) else raw
                if not metadata or not metadata.get("short_description"):
                    metadata = await pipeline.store.get_game(appid) or metadata
                label = await pipeline.classify(appid, metadata)
                console.print(f"{appid}: {label}")
        finally:
            await pipeline.close()

    _run(work())
    db.close()


@app.command("enrich-reviews")
def enrich_reviews(appid: int | None = None) -> None:
    """Enrich pending eligible reviews with the configured local model."""
    settings, db = _open()
    appids = [appid] if appid else [row[0] for row in db.con.execute("SELECT DISTINCT appid FROM reviews ORDER BY appid").fetchall()]

    async def work() -> None:
        pipeline = Pipeline(settings, db)
        try:
            await pipeline.llm.health_check()
            for game_id in appids:
                enriched, skipped = await pipeline.enrich_pending(game_id)
                console.print(f"{game_id}: enriched={enriched:,}, skipped={skipped:,}")
        finally:
            await pipeline.close()

    _run(work())
    db.close()


@app.command()
def dry_run(
    games: Annotated[int, typer.Option("--games", min=1)] = 5,
    min_reviews: Annotated[int, typer.Option("--min-reviews", min=0)] = 50,
    seed: Annotated[int, typer.Option("--seed")] = 42,
) -> None:
    """Run the complete pipeline on random qualifying games."""
    settings, db = _open()

    async def work():
        pipeline = Pipeline(settings, db)
        try:
            return await pipeline.dry_run(games, min_reviews, seed)
        finally:
            await pipeline.close()

    try:
        run_id, stats, result = _run(work())
    finally:
        db.close()
    console.rule("Dry run completed")
    console.print(f"Run ID: {run_id}")
    console.print("Selected games: " + ", ".join(f"{name} ({appid})" for appid, name in stats.selected))
    console.print(f"Reviews expected: {stats.expected_reviews:,}")
    console.print(f"Reviews stored: {stats.reviews_ingested:,}")
    console.print(f"Reviews enriched: {stats.reviews_enriched:,}")
    console.print(f"Skipped reviews: {stats.skipped_reviews:,}")
    console.print(f"Classification: {result['classifications']}")
    console.print(f"Database: {settings.duckdb_path}")
    console.print(f"Validation: {'PASS' if result['validation']['passed'] else 'FAIL'}")
    console.print(f"Elapsed: {result['elapsed_seconds']:.1f}s; errors: {stats.error_count}")


@app.command()
def run(
    min_reviews: Annotated[int, typer.Option("--min-reviews", min=0)] = 50,
    limit_games: Annotated[int | None, typer.Option("--limit-games", min=1)] = None,
    appid: Annotated[int | None, typer.Option("--appid")] = None,
    skip_llm: Annotated[bool, typer.Option("--skip-llm")] = False,
    skip_enrichment: Annotated[bool, typer.Option("--skip-enrichment")] = False,
    metadata_only: Annotated[bool, typer.Option("--metadata-only")] = False,
    reviews_only: Annotated[bool, typer.Option("--reviews-only")] = False,
    resume: Annotated[bool, typer.Option("--resume/--no-resume")] = True,
) -> None:
    """Run a full resumable production batch over the configured catalog snapshot."""
    del resume  # checkpoints are always honored; flag documents explicit operator intent
    settings, db = _open()

    async def work():
        pipeline = Pipeline(settings, db)
        try:
            return await pipeline.production_run(min_reviews, limit_games, appid, skip_llm,
                                                 skip_enrichment, metadata_only, reviews_only)
        finally:
            await pipeline.close()

    try:
        run_id, stats, result = _run(work())
    finally:
        db.close()
    console.print(f"Run {run_id}: games={stats.games_qualified}, reviews={stats.reviews_ingested:,}, "
                  f"enriched={stats.reviews_enriched:,}, errors={stats.error_count}, "
                  f"validation={'PASS' if result['validation']['passed'] else 'FAIL'}")


@app.command()
def status() -> None:
    """Show row counts, checkpoints, latest run, and errors."""
    settings, db = _open()
    table = Table("Table", "Rows", title=str(settings.duckdb_path))
    for name, count in db.table_counts().items():
        table.add_row(name, f"{count:,}")
    console.print(table)
    latest = db.con.execute("SELECT run_id,run_type,status,started_at,completed_at FROM ingestion_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    console.print(f"Latest run: {latest or 'none'}")
    db.close()


@app.command()
def validate() -> None:
    """Run database integrity, taxonomy, enrichment, and completeness checks."""
    _, db = _open()
    result = validate_database(db)
    console.print_json(data=result)
    db.close()
    if not result["passed"]:
        raise typer.Exit(1)


@app.command()
def sql(query: Annotated[str, typer.Argument(help="Read-only SQL query")]) -> None:
    """Execute one read-only SQL query and print the result."""
    settings = Settings()
    statement = query.strip().lower()
    if not (statement.startswith("select") or statement.startswith("with") or statement.startswith("describe") or statement.startswith("show")):
        raise typer.BadParameter("Only SELECT, WITH, DESCRIBE, and SHOW queries are accepted")
    con = duckdb.connect(str(settings.duckdb_path), read_only=True)
    result = con.execute(query)
    table = Table(*[item[0] for item in result.description])
    for row in result.fetchall():
        table.add_row(*[str(value) for value in row])
    console.print(table)
    con.close()


@app.command("export-parquet")
def export_parquet(directory: Path = Path("data/export")) -> None:
    """Export major analytical tables as Parquet."""
    _, db = _open()
    directory.mkdir(parents=True, exist_ok=True)
    for table in ("games", "reviews", "store_products", "store_reviews", "review_aspects"):
        target = (directory / f"{table}.parquet").resolve()
        db.con.execute(f"COPY {table} TO ? (FORMAT PARQUET)", [str(target)])
        console.print(target)
    db.close()


@app.command("db-info")
def db_info() -> None:
    """Show database path, size, and major table counts."""
    settings, db = _open()
    size = settings.duckdb_path.stat().st_size if settings.duckdb_path.exists() else 0
    console.print(f"Path: {settings.duckdb_path.resolve()}\nSize: {size / 2**20:.2f} MiB")
    for name, count in db.table_counts().items():
        console.print(f"{name}: {count:,}")
    db.close()


@app.command("test-llm")
def test_llm() -> None:
    """Test both structured-output tasks against the local Qwen endpoint."""
    settings = Settings()

    async def work() -> tuple[GameClassification, object]:
        llm = LLMClient(settings)
        taxonomy = Taxonomy()
        try:
            await llm.health_check()
            classification = await llm.classify_game({"name": "Fixture Factory", "short_description": "Build conveyor belts and automate production.",
                "genres": [{"description": "Simulation"}], "categories": [{"description": "Single-player"}]},
                {"Automation": 100, "Base Building": 80}, ["Factory / Automation"], taxonomy)
            enrichment = (await llm.enrich_reviews([(
                "fixture-review",
                "The automation is excellent, but late-game stuttering needs fixing.",
                False,
            )], AspectTaxonomy()))["fixture-review"]
            return classification, enrichment
        finally:
            await llm.close()

    classification, enrichment = _run(work())
    console.print_json(data={"classification": classification.model_dump(), "enrichment": enrichment.model_dump()})


if __name__ == "__main__":
    app()
